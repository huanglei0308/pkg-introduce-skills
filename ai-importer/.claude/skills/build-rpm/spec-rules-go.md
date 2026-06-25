# Go spec 规范

当 `<lang>=go` 时，spec 初稿应根据项目复杂性选择构建路径，先保证在当前容器环境中稳定通过 `rpmlint`、`dnf builddep` 与 `rpmbuild`。

## 1. 适用范围

适用于：
- 存在 `go.mod` 的 Go 项目
- 单模块或多二进制项目
- 含或不含 CGO 的项目

不适用：
- 无 `go.mod`（旧版 GOPATH 模式）：需人工判断，标记 `needs_ai`
- `replace` 指令指向本地路径的 monorepo：需人工拆分

## 2. 构建路径决策

**根据项目复杂性选择构建方式**，不得随意混用：

### 2.1 判断标准

| 条件 | 构建路径 |
|------|---------|
| 依赖少（< 20 个直接依赖）、无 CGO、无 replace 指令 | **直接构建**（`go build`） |
| 依赖多（≥ 20 个）、或有 CGO、或有 replace 指令 | **vendor 构建** |
| 上游 repo 已包含 `vendor/` 目录 | **vendor 构建**（直接复用，不重新生成） |
| 离线构建环境（无网络） | **vendor 构建**（必须预先生成 vendor） |

### 2.2 直接构建路径

```spec
%build
export GOFLAGS="-mod=mod"
export GOPATH=%{_builddir}/go
go build -v -o %{name} ./...
```

- 不需要额外 Source，`go` 工具链在线下载依赖
- 适合简单项目，但**离线容器内不可用**
- 需在 BuildRequires 中声明 `golang`

### 2.3 vendor 构建路径

vendor 有两种来源，必须选其一，**不得混用**：

#### A. 上游自带 vendor/

```spec
Source0: https://github.com/<owner>/<repo>/archive/v%{version}.tar.gz#/%{name}-%{version}.tar.gz

%prep
%autosetup -n %{name}-%{version}
# vendor/ 已包含在 tarball 中，无需生成

%build
export GOFLAGS="-mod=vendor"
go build -v -o %{name} ./...
```

验证上游 vendor 与 go.sum 一致性：
```bash
# 在 %check 中加入
go mod verify
```

#### B. 预生成 vendor（上游无 vendor/）

```bash
# 在容器内预生成（action_type=vendor_fetch，必须记录到 build_actions.json）
docker exec ${SESSION_CONTAINER} bash -c "
  cd /build/source
  go mod vendor
  # 验证版本一致性
  go mod verify
"
# 将 vendor/ 打包为额外 Source
docker exec ${SESSION_CONTAINER} bash -c "
  cd /build/source && tar czf /tmp/${pkgname}-vendor.tar.gz vendor/
"
docker cp ${SESSION_CONTAINER}:/tmp/${pkgname}-vendor.tar.gz \
  ./sources/${pkgname}/${pkgname}-vendor.tar.gz
```

spec 中声明：
```spec
Source0: https://github.com/<owner>/<repo>/archive/v%{version}.tar.gz#/%{name}-%{version}.tar.gz
Source1: %{name}-vendor.tar.gz

%prep
%autosetup -n %{name}-%{version}
tar xf %{SOURCE1}

%build
export GOFLAGS="-mod=vendor"
go build -v -o %{name} ./...
```

**版本一致性保证**：
- vendor/ 必须由 `go mod vendor` 在与 go.sum **完全相同的 go.mod** 上生成
- 不得手动修改 vendor/ 中的文件（review-rpm 会检测 `vendor_direct_edit`）
- 若需 patch 依赖，使用 `%prep` 中的 `sed`/`patch` 而不是直接编辑

## 3. CGO 处理

| 情况 | 处理 |
|------|------|
| 纯 Go（无 C 代码）| `export CGO_ENABLED=0`，不需要 `gcc` |
| 有 C 扩展（`import "C"`）| `CGO_ENABLED=1`（默认），BuildRequires 加 `gcc` |
| 有系统库依赖（如 `libssl`）| BuildRequires 加对应 `-devel` 包，Requires 加运行时库 |

检测方式：
```bash
# 检查是否有 CGO
grep -r 'import "C"' ./sources/<pkgname>/ && echo "has_cgo" || echo "no_cgo"
```

## 4. 命名规则

Go 项目不使用语言前缀，直接用项目名：

| 字段 | 规则 | 示例 |
|------|------|------|
| `Name:` | 小写，连字符分隔 | `fzf`、`golangci-lint` |
| 安装路径 | `%{_bindir}/<name>` | `/usr/bin/fzf` |
| 库包（不常见）| `golang-<module-path>` | `golang-github-spf13-cobra` |

## 5. 版本与 Source0

```spec
Version: 1.2.3   # 不含 v 前缀
Source0: https://github.com/<owner>/<repo>/archive/v%{version}.tar.gz#/%{name}-%{version}.tar.gz
```

- 必须用 tag/commit 对应的不可变 tarball，不得用 branch/HEAD
- `#/` 后的别名确保解压目录名一致

## 6. BuildRequires / Requires

```spec
BuildRequires: golang >= 1.19
# CGO 项目额外加：
BuildRequires: gcc
BuildRequires: glibc-devel

# 纯 Go 静态链接通常无运行时 Requires
# CGO 项目按实际 .so 依赖填写
```

## 7. %install 与多二进制

```spec
%install
install -Dm755 %{name} %{buildroot}%{_bindir}/%{name}

# 多二进制项目
for bin in cmd/*/; do
    install -Dm755 $(basename $bin) %{buildroot}%{_bindir}/$(basename $bin)
done
```

## 8. %check

```spec
%check
# vendor 项目验证版本一致性
go mod verify
# 运行单元测试（有网络时可用）
# go test ./...
```

## 9. 常见问题处理

| 问题 | 原因 | 处理 |
|------|------|------|
| `go: no module found` | 无 go.mod 或路径不对 | `cd` 到含 go.mod 的目录 |
| `verifying module: checksum mismatch` | vendor 与 go.sum 不一致 | 重新生成 vendor |
| `missing go.sum entry` | vendor 构建但 go.sum 不完整 | 删 vendor/，重跑 `go mod vendor` |
| `undefined: C.*` | CGO 依赖未安装 | 检查系统库 devel 包 |
| `GOFLAGS: unknown flag -mod=vendor` | golang 版本过低 | 更新 golang 或改用 `-mod=mod` |
