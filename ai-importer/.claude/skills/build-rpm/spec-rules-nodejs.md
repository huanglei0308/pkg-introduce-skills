# Node.js spec 规范

当 `<lang>=nodejs` 时，spec 初稿应优先采用 openEuler Node.js 包风格模板；可参考 `nodejsporter` 的模板结构，但**不直接复用其依赖映射结论**。

## 0. 源码 tarball 前置检查（生成 spec 之前必须执行）

在生成 spec 之前，**必须**对源码目录做以下检查，任一失败则立即阻断，不进入 spec 生成阶段。

### 0.1 dist/ 目录存在性检查

```bash
# 检查是否存在编译产物目录
if [ ! -d "./sources/<pkgname>/dist" ] && \
   [ ! -d "./sources/<pkgname>/lib" ] && \
   [ ! -d "./sources/<pkgname>/build" ]; then
  echo "[BLOCK] 源码目录中不存在 dist/、lib/ 或 build/ 编译产物目录"
  echo "        原因：openEuler 社区源没有 TypeScript / esbuild 等构建工具链，"
  echo "              无法在 %build 阶段完成编译。"
  echo "        解决：改用 npm registry tarball（已包含预编译产物）作为 Source0"
  exit 1
fi
```

**判断规则：**

| 情况 | 处理 |
|------|------|
| 存在 `dist/`、`lib/` 或 `build/` 且含 `.js` 文件 | 通过，继续生成 spec |
| 目录均不存在，且 `.gitignore` 含 `dist` | **阻断**：TypeScript 包未预编译，需换用 npm tarball |
| 目录均不存在，且无 `.gitignore` | **阻断**：无编译产物，需换用 npm tarball |
| 存在 `tsconfig.json` 但无 `dist/` | **阻断**：TypeScript 包未预编译，需换用 npm tarball |

**阻断时给出的提示必须包含：**
1. 阻断原因
2. npm registry tarball URL（格式：`https://registry.npmjs.org/<pkgname>/-/<pkgname>-<version>.tgz`）
3. 说明 npm tarball 通常已包含预编译的 `dist/`，下载后验证一下

### 0.2 monorepo/workspace 检查

若 `package.json` 含 `"workspaces"` 字段或 `"private": true`：

- **阻断**：monorepo 根包不能直接打 RPM
- 提示：应改为对具体可分发子包（`packages/cli`、`packages/core` 等）分别引入，或使用 npm tarball

### 0.3 native addon 检查

若存在 `binding.gyp` 或 `dependencies` 含 `node-gyp`、`nan`、`node-addon-api`：

- **阻断**：native addon 需要专门的编译分支，不在当前标准模板范围内

---

## 1. 适用范围
该模板优先适用于：
- 存在 `package.json`
- 单包仓库
- 纯 JavaScript 包
- 不包含 `binding.gyp`、`node-gyp`、`nan`、`node-addon-api`
- 不使用 `workspaces` / monorepo

若检测到 native addon 或 workspace/monorepo，不能直接使用该模板，需进入特殊分支处理。

## 2. spec 头部与命名规则
Node.js 模板应优先采用以下结构：

```spec
%global __nodejs_requires %{nil}
%global packagename <upstream_name>

Name:           nodejs-<normalized_name>
Version:        <version>
Release:        1%{?dist}
Summary:        <summary>
License:        <license>
URL:            <url>
Source0:        <source0>

ExclusiveArch:  %{nodejs_arches} noarch
BuildArch:      noarch

BuildRequires:  nodejs-packaging
Requires:       nodejs-<dep1>
Requires:       nodejs-<dep2>
```

规则：
- 包名统一使用 `nodejs-<normalized_name>`
- `packagename` 保留上游 npm 包名
- `Source0` 优先与当前流程实际下载/打包的源码 tarball 保持一致
- 若使用 npm tarball 结构，`Source0` 可参考 npm registry tarball URL 规则
- 对 scoped package（如 `@scope/pkg`）必须先做 RPM 名称规范化，不能直接原样写入 `Name`
- **不使用 `%{?nodejs_find_provides_and_requires}` 宏**：该宏在 openEuler 24.03 上会生成 RPM Rich Dependency（`with` 语法，如 `(npm(foo) >= X with npm(foo) < Y)`），导致 `repoclosure` CI 门禁误报失败。根因：openEuler `rpm --whatprovides` 无法解析 `npm()` 虚拟 provides 与 `with` 语法的组合，即使包实际能正常安装也会报 unresolved deps。
- 用 `%global __nodejs_requires %{nil}` 替代，彻底禁用自动 requires 生成；每个运行时依赖手工写一行 `Requires: nodejs-<pkgname>`
- `%__nodejs_provides` 无需禁用，`npm(pkgname) = version` 格式不产生 rich dep，不影响 repoclosure

## 3. `%prep` 规则
Node.js 模板默认使用：

```spec
%prep
%autosetup -n package
```

说明：
- `-n package` 适用于**直接从 npm registry 下载的 tarball**，其解包目录固定为 `package/`
- 若 source tarball 由 `prepare_build_inputs.py` 从 git 源码打包，解包目录名为 `<pkg>-<version>/`（如 `is-buffer-1.1.6/`），**必须改为 `%autosetup -n <pkg>-<version>`**，不能沿用 `-n package`
- 判断方式：`tar tzf <tarball> | head -1` 确认实际目录名后再写 `-n` 参数

## 4. `%build` 规则
纯 JavaScript 包默认：

```spec
%build
# nothing to do!
```

只有在源码中能明确识别出稳定、可控的构建步骤时，才允许扩展 `%build`，例如：
- `package.json` 中存在明确的 `build` / `compile` 脚本
- 且该脚本不依赖额外未纳入控制的前端工具链

默认不要自动生成 `npm test`、`npm run build` 等命令。

## 5. `%install` 规则
Node.js 模板的 `%install` 应至少覆盖以下动作：
- 统一规范化 `LICENSE`
- 若存在 `bin/`，安装到 `%{_bindir}`
- 安装源码内容到 `%{nodejs_sitelib}/%{packagename}`
- 调用 `%nodejs_symlink_deps`

推荐结构：

```spec
%install
if [ -f license ]; then
    mv license LICENSE
fi
if [ -f License ]; then
    mv License LICENSE
fi

if [ -d bin ]; then
    mkdir -p %{buildroot}%{_bindir}
    cp -ar bin/* %{buildroot}%{_bindir}
fi

mkdir -p %{buildroot}%{nodejs_sitelib}/%{packagename}
cp -ra * %{buildroot}%{nodejs_sitelib}/%{packagename}

%nodejs_symlink_deps
```

注意：
- `cp -ra *` 只是模板初稿策略；若包含明显不应入包的目录（如测试数据、CI 配置、缓存目录等），应在后续修正中排除
- 若项目包含 native addon、预编译产物或特殊安装布局，不能直接沿用该默认安装逻辑

## 6. `%check` 规则
默认仅执行基础依赖链接检查：

```spec
%check
%nodejs_symlink_deps --check
```

不要默认执行：
- `npm test`
- `npm run test`
- 任何依赖 devDependencies、大型测试框架、网络环境或浏览器环境的测试命令

如需启用测试，应单独判断并显式添加。

## 7. `%files` 规则
Node.js 模板初稿优先采用**显式 `%files`**，不要默认使用动态 filelist。

推荐初稿：

```spec
%files
%license LICENSE
%{nodejs_sitelib}/%{packagename}
```

若存在 `bin/` 安装内容，再补充：

```spec
%{_bindir}/*
```

说明：
- 显式 `%files` 更稳定、可读、便于后续 rpmlint / `%files` 修正
- 动态 filelist 可作为兜底方案，但不应作为默认首选

## 8. 依赖分析规则
Node.js 依赖分析可以参考 `nodejsporter` 的**元数据提取思路**，但**不能直接复用其 `Requires` / `BuildRequires` 映射逻辑**。

规则：
- 从本地 `package.json` 中提取：
  - `dependencies`
  - `devDependencies`
  - `peerDependencies`
  - `optionalDependencies`
- 这些依赖只作为**候选输入**，供后续预检与 resolver 使用
- 不要机械生成：
  - `dependencies -> BuildRequires`
  - `devDependencies -> Requires`

建议分类：
- `dependencies`：运行时依赖候选
- `devDependencies`：构建/测试依赖候选
- `peerDependencies`：人工审查候选
- `optionalDependencies`：保守处理，不默认写入 spec

最终是否写入 `BuildRequires` / `Requires`，仍以：
- 容器内 openEuler 软件源真值
- `dnf builddep`
- `rpmbuild`
- 缺包递归引入

的结果为准。手工 `Requires` 以 `Requires: nodejs-<pkgname>` 格式逐行写入 spec，不依赖宏自动生成（原因见第 2 节）。

## 9. 不直接采用的 `nodejsporter` 行为
以下行为只可作为参考，不应直接纳入当前主流程：
- 将 `dependencies` / `devDependencies` 直接翻译为 RPM 依赖字段
- 直接使用 `--build` / `--buildinstall` 替代当前构建链路
- 默认使用动态 `filelist.lst`
- 默认假设所有 Node.js 包都为纯 JS / noarch

## 11. 依赖 vendor（Bundled deps）策略

Node.js 的依赖管理与 Go/Rust 不同，**不默认 vendor node_modules**。依赖处理分三种路径：

### 11.1 优先路径：RPM 层面声明 Requires

社区源已有对应 `nodejs-<dep>` 包时，直接写 `Requires: nodejs-<dep>`，运行时由 RPM 解析器从 `/usr/share/nodejs/<dep>` 查找。这是 openEuler Node.js 打包的**标准路径**。

### 11.2 次选路径：递归引入依赖包

社区源没有、但依赖包本身值得单独引入时，走 dep_check_needed_batch 流程，先引入依赖包，再引入主包。适用于：
- 被多个包共享的通用库
- 有独立维护价值的包

### 11.3 兜底路径：Bundled deps（vendor 进包内）

以下情况允许将依赖直接 bundle 进 RPM，不单独引入：

| 适用条件 | 说明 |
|---------|------|
| 依赖仅被该包内部使用，无其他 RPM 依赖 | 单独引入成本高于收益 |
| 依赖是私有 fork 或 patch 版本 | 无法直接复用社区源包 |
| 依赖是极小的工具函数（< 50 行） | 无必要单独打包 |
| 依赖在 npm 上已废弃/归档，但上游仍在使用 | 无法从社区源获取 |

**Bundled deps 的 spec 写法：**

```spec
# 在 %prep 中将依赖包含到安装目录
%prep
%autosetup -n package

# 声明 bundled 依赖（必须逐个声明，供安全扫描工具识别）
# Provides: bundled(npm(is-buffer)) = 1.1.6
# Provides: bundled(npm(safe-buffer)) = 5.2.1

%install
mkdir -p %{buildroot}%{nodejs_sitelib}/%{packagename}
cp -ra * %{buildroot}%{nodejs_sitelib}/%{packagename}

# 将 bundled 依赖一并安装（node_modules/ 内的依赖）
# 若 npm tarball 已包含 node_modules/，直接随源码安装
%nodejs_symlink_deps
```

**必须在 spec 头部声明每个 bundled 依赖：**

```spec
# Bundled npm dependencies
Provides: bundled(npm(is-buffer)) = 1.1.6
Provides: bundled(npm(safe-buffer)) = 5.2.1
```

**bundled deps 的版本一致性保证：**

```bash
# 验证 bundle 的依赖版本与 package.json 声明一致
python3 -c "
import json
pkg = json.load(open('package.json'))
deps = {**pkg.get('dependencies', {}), **pkg.get('devDependencies', {})}
# 对比 node_modules/ 中实际版本
"
```

### 11.4 决策流程

```
依赖包 <dep> 在社区源是否存在 nodejs-<dep>?
  ├── 是 → Requires: nodejs-<dep>（标准路径）
  └── 否 → 是否被多个包共用 或 有独立维护价值?
              ├── 是 → dep_check_needed_batch，递归引入（次选路径）
              └── 否 → 是否满足 bundled 条件?
                          ├── 是 → Provides: bundled(npm(<dep>))，bundle 进包（兜底路径）
                          └── 否 → 阻断，人工决策
```

### 11.5 何时不用 vendor node_modules

以下情况**不需要**也**不应该**把 `node_modules/` 打包进 Source tarball：
- 纯 JS 包：`%build` 为空，运行时依赖通过 `Requires:` 声明，RPM 安装时自动解析
- npm tarball：已包含预编译 `dist/`，`node_modules/` 由 RPM 依赖树负责，不在 tarball 内
- 有构建步骤：应先阻断检查，确认 dist/ 存在后再继续，不依赖在线 npm install

Node.js 模板生成的 spec 只是**初稿**，后续仍必须经过：
- `rpmlint`
- `dnf builddep`
- `rpmbuild`
- 依赖递归引入
- 失败分类修复

不能把模板生成结果视为最终可用 spec。
