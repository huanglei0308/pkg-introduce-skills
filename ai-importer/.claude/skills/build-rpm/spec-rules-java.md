# Java spec 规范

当 `<lang>=java` 时，spec 初稿应根据上游构建系统选择对应路径。当前环境仅支持 **Maven** 构建路径；**Gradle** 构建路径暂不支持，遇到时应立即阻断并给出说明。

## 1. 构建系统判断（首要步骤）

拿到源码后，第一步判断构建系统：

| 判断依据 | 构建系统 | 是否支持 |
|---|---|---|
| 根目录有 `pom.xml` | Maven | ✅ 支持 |
| 根目录有 `build.gradle` 或 `build.gradle.kts` | Gradle | ❌ 暂不支持 |
| 根目录有 `build.xml` | Ant | 视情况，参见第 5 节 |

**若为 Gradle 项目，立即阻断，不要继续生成 spec。** 原因和处理方式见第 2 节。

---

## 2. Gradle 项目：暂不支持

### 2.1 阻断原因

openEuler 的 Java 离线构建体系基于 `maven-local` + `javapackages-tools`，所有依赖以 `mvn(groupId:artifactId)` 格式声明，对应系统中已打包的 RPM。

Gradle 项目在构建时需要：
1. **Gradle 插件**（如 `shadow`、`github-release`、`git-versioner`、`spotless` 等）——这些插件在 openEuler 仓库中**没有对应 RPM**
2. **Gradle 本身的插件解析机制**——即使安装了 `gradle-local`，也无法在离线环境中解析未打包的插件

目前 src-openeuler 中使用 `gradle-local` 构建的包极少（仅 `gradle` 本身和 `groovy`），且这两个包的 Gradle 构建经过特殊改造，所有依赖均已替换为系统 RPM，不依赖任何外部 Gradle 插件。普通 Gradle 项目无法套用此路径。

### 2.2 阻断输出格式

```
❌ 引入失败：<pkg> 使用 Gradle 构建，当前环境暂不支持

原因：
- 构建系统：Gradle（build.gradle 存在）
- 依赖的 Gradle 插件（如 <plugin1>、<plugin2>）在 openEuler 仓库中无对应 RPM
- gradle-local 离线构建方案不可行，前置插件依赖链缺失
- 不允许使用预构建 jar 包

建议：
1. 等待 openEuler 社区仓库收录（如有 MIT/Apache 许可证，可提交收录申请）
2. 先将所有 Gradle 插件依赖逐一打包引入，再重新发起引入
3. 检查上游是否提供 Maven 构建路径（部分项目同时维护 pom.xml）
```

### 2.3 不要尝试的变通方案

- ❌ 使用预构建 jar/tar 包打 RPM（不符合引入规范）
- ❌ 配置 Gradle 仓库代理（下载的是二进制 jar，不是源码，不合规）
- ❌ 用 `%build` 留空跳过编译（等同于预构建包方案）
- ❌ 降级到旧版本寻找 Maven 构建路径（需用户决策，不能自行切换版本）

---

## 3. Maven 项目：标准路径

### 3.1 适用范围

- 根目录有 `pom.xml`
- 单模块或多模块 Maven 项目
- 不含复杂 monorepo / workspace 布局

### 3.2 总原则

- 使用 `maven-local` + `javapackages-tools` 体系
- `%build` 用 `%mvn_build`，`%install` 用 `%mvn_install`
- 所有 Java 依赖以 `mvn(groupId:artifactId)` 格式声明 `BuildRequires`
- 用 `%pom_*` 宏在 `%prep` 阶段修改 pom.xml，移除不兼容插件和不可用依赖
- 测试默认跳过（`%mvn_build -f`），不要在 spec 中启用网络测试

### 3.3 命名规则

| 字段 | 规则 | 示例 |
|---|---|---|
| `Name:` | 直接用上游项目名（小写，连字符分隔） | `log4j`、`commons-lang3` |
| `BuildArch:` | 纯 Java 包设 `noarch` | `BuildArch: noarch` |
| 子包 | 按功能模块拆分，用 `%mvn_package` 映射 | `log4j-slf4j`、`log4j-web` |

### 3.3b Source0 URL 规范

Source0 **必须**填写完整的上游下载 URL，不得只写文件名。

```spec
# GitHub releases
Source0: %{url}/releases/download/v%{version}/%{name}-%{version}.tar.gz

# GitHub tag archive（推荐用 %{url} 宏拼接，与 URL 字段联动）
Source0: %{url}/archive/%{name}-%{version}.tar.gz
Source0: %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

# Apache 归档
Source0: http://archive.apache.org/dist/logging/%{name}/%{version}/apache-%{name}-%{version}-src.tar.gz

# 其他 HTTPS 直链
Source0: https://github.com/<owner>/<repo>/archive/v_%{version}.tar.gz
```

### 3.4 spec 模板（单模块）

```spec
Name:           <pkg>
Version:        <version>
Release:        1%{?dist}
Summary:        <one-line summary>
BuildArch:      noarch
License:        <SPDX license identifier>
URL:            <upstream homepage>
Source0:        %{url}/archive/%{name}-%{version}.tar.gz

BuildRequires:  maven-local
BuildRequires:  mvn(<groupId>:<artifactId>)
# ... 其他 mvn() 依赖

%description
<multi-line description>

%package javadoc
Summary:        Javadoc for %{name}
%description javadoc
API documentation for %{name}.

%prep
%autosetup -n <tarball-name>-%{version} -p1
# 移除不兼容插件（顺序：先 disable_module，再 remove_plugin）
%pom_remove_plugin -r :maven-enforcer-plugin
%pom_remove_plugin -r :maven-site-plugin
%pom_remove_plugin -r :maven-source-plugin
%pom_remove_plugin -r :jacoco-maven-plugin
%pom_remove_plugin -r :moditect-maven-plugin
# 发布/格式化类插件（xmvn offline 模式下会尝试联网，必须移除）
# %pom_remove_plugin -r :nexus-staging-maven-plugin
# %pom_remove_plugin -r :maven-release-plugin
# %pom_remove_plugin -r :sortpom-maven-plugin
# %pom_remove_plugin -r :maven-gpg-plugin
# %pom_remove_plugin -r :central-publishing-maven-plugin

%build
%mvn_build -f

%install
%mvn_install

%files -f .mfiles
%license LICENSE
%doc README.md

%files javadoc -f .mfiles-javadoc
%license LICENSE

%changelog
* <date> <maintainer> <<email>> - <version>-1
- Initial package
```

> **发布类插件说明**：以下插件在 xmvn offline 构建时会尝试联网或执行发布操作，导致构建失败，**凡 pom.xml 中存在即必须移除**：
> - `org.sonatype.plugins:nexus-staging-maven-plugin`
> - `org.apache.maven.plugins:maven-release-plugin`
> - `com.github.ekryd.sortpom:sortpom-maven-plugin`
> - `org.apache.maven.plugins:maven-gpg-plugin`
> - `org.apache.maven.plugins:maven-source-plugin`
> - `org.sonatype.central:central-publishing-maven-plugin`
> - `org.apache.maven.plugins:maven-site-plugin`
> - `org.apache.maven.plugins:maven-enforcer-plugin`
>
> 生成 spec 时，直接读 pom.xml（及子模块 pom）扫描上述插件，凡存在即加 `%pom_remove_plugin -r :<artifactId>` 行。

### 3.5 spec 模板（多模块，含子包）

```spec
%bcond_with bootstrap
%bcond_without jp_minimal

Name:           <pkg>
Version:        <version>
Release:        1%{?dist}
Summary:        <one-line summary>
BuildArch:      noarch
License:        <SPDX license identifier>
URL:            <upstream homepage>
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

%if %{with bootstrap}
BuildRequires:  javapackages-bootstrap
%else
BuildRequires:  maven-local
BuildRequires:  mvn(<groupId>:<artifactId>)
%endif

%description
<multi-line description>

# 子包声明
%package <subpkg>
Summary:        <subpkg description>
%description <subpkg>
<subpkg description>

%package javadoc
Summary:        Javadoc for %{name}
%description javadoc
API documentation for %{name}.

%prep
%autosetup -n %{name}-%{version} -p1
# 先禁用不需要的模块（必须在 remove_plugin 之前）
# %pom_disable_module <unavailable-module>
# 再递归移除插件
%pom_remove_plugin -r :maven-enforcer-plugin
%pom_remove_plugin -r :maven-site-plugin
%pom_remove_plugin -r :maven-source-plugin
%pom_remove_plugin -r :jacoco-maven-plugin

# 子包映射
%mvn_package ':<artifact-id>' <subpkg>
%mvn_file ':{<artifact-id>}' %{name}/@1

%build
%mvn_build -f

%install
%mvn_install

%files -f .mfiles
%license LICENSE

%files <subpkg> -f .mfiles-<subpkg>

%files javadoc -f .mfiles-javadoc
%license LICENSE

%changelog
* <date> <maintainer> <<email>> - <version>-1
- Initial package
```

### 3.6 多模块项目注意事项

**`%pom_disable_module` 必须在所有 `%pom_remove_plugin -r` 之前**。`-r` 递归扫描所有子模块，若先 remove_plugin 再 disable_module，递归会进入已禁用模块并因找不到插件而报错。

```spec
%prep
%autosetup -n %{name}-%{version}
# 先禁用不需要的模块
%pom_disable_module distribution
%pom_disable_module integrationtest
%pom_disable_module documentation
# 再递归移除插件
%pom_remove_plugin -r :maven-enforcer-plugin
%pom_remove_plugin -r :maven-site-plugin
```

**版本提取**：多模块项目根 pom.xml 可能没有 `<version>`，版本在 `parent/pom.xml` 或某个子模块 pom 中。生成 spec 前先检查根 pom，若无版本则读 `parent/pom.xml`。

**shaded 依赖**：若某依赖被 `maven-shade-plugin` 的 `<artifactSet><includes>` 打包进 jar，xmvn 仍会为它生成 RPM Requires。若该依赖在 RPM 仓库中不存在，repoclosure 会失败。解决方法：在 spec 中用 `%pom_xpath_inject` 给该依赖加 `<optional>true</optional>`，xmvn 会跳过 optional 依赖的 Requires 生成。**不要用 `%pom_remove_dep` 移除**——编译时仍需要它。

```spec
# gem-api 已被 shade 进 processor jar，标 optional 阻止 xmvn 生成 Requires
%pom_xpath_inject "pom:dependency[pom:artifactId='gem-api']" "<optional>true</optional>" processor/pom.xml
```

**子包分配**：用 `%mvn_package "groupId:artifactId" subpkg-name` 把某模块产物分配到子包，对应 `%files subpkg-name -f .mfiles-subpkg-name`。注意文件名是 `.mfiles-<subpkg-name>`（子包名），不是 `.mfiles-<artifactId>`。

**JDK 版本选择**：容器只有 `java-1.8.0-openjdk-devel`、`java-11-openjdk-devel`、`java-latest-openjdk-devel`。若源码用了 Java 9+ API（如 `javax.lang.model.element.ModuleElement`）但需要保持 Java 8 字节码兼容性（如 maven-shade-plugin 版本较旧），可用 JDK 11 编译但目标设为 Java 8：

```spec
BuildRequires:  java-11-openjdk-devel

%build
export JAVA_HOME=/usr/lib/jvm/java-11-openjdk
%mvn_build -f -- -Dmaven.compiler.source=1.8 -Dmaven.compiler.target=1.8
```

### 3.8 常用 `%pom_*` 宏速查

| 宏 | 用途 |
|---|---|
| `%pom_remove_plugin -r :<artifactId>` | 全局移除某插件 |
| `%pom_disable_module <module>` | 禁用某子模块 |
| `%pom_remove_dep -r <groupId>:<artifactId>` | 移除某依赖声明 |
| `%pom_change_dep -r <old> <new>` | 替换依赖坐标 |
| `%pom_add_dep <groupId>:<artifactId> <module>` | 添加依赖 |
| `%pom_remove_parent` | 移除 parent pom 引用 |
| `%pom_xpath_inject 'pom:...' '<xml>'` | 向 pom 注入 XML 片段 |

### 3.9 bootstrap 模式

当某个 Maven 包的依赖链尚未完全在 openEuler 中打包时，可用 `%bcond_with bootstrap` 提供两条路径：

- **bootstrap 模式**（`--with bootstrap`）：用 `javapackages-bootstrap` 提供最小依赖集，先打出基础包
- **正常模式**（默认）：用完整的 `mvn(...)` 依赖链构建

引入新包时，若依赖链完整，直接用正常模式；若依赖链有缺口，先引入缺失的依赖包，再回来构建目标包。**不要跳过依赖链直接用 bootstrap 模式绕过**。

---

## 4. 依赖声明规则

- `BuildRequires` 中的 `mvn(groupId:artifactId)` 对应系统中已安装的 RPM，引入前需确认该 RPM 存在
- 若某个 `mvn()` 依赖在 openEuler 仓库中不存在，需先引入该依赖包，再继续
- 不要把测试依赖（JUnit、Mockito 等）写入 `Requires`
- 运行时 `Requires` 以 `dnf builddep` + `rpmbuild` 实际失败为准，不要机械翻译上游 pom.xml

---

## 5. Ant 项目

Ant 项目视具体情况处理：

- 若 `build.xml` 中所有依赖均可从系统 jar 路径（`/usr/share/java`）解析，可直接用 `ant` 构建
- `%build` 中调用 `ant jar`，`%install` 手工安装 jar 到 `%{_javadir}`
- 若依赖无法从系统解析，同样阻断，先引入依赖

---

## 6. `%changelog` 格式

```spec
%changelog
* Sat May 09 2026 Java_Bot <Java_Bot@openeuler.org> - 1.2.3-1
- Initial package
```

日期格式：`%a %b %d %Y`（英文，与 `date "+%a %b %d %Y"` 输出一致）。

---

## 7. 不直接采用的行为

- 使用预构建 jar/tar 包打 RPM
- `%build` 留空跳过编译
- 配置 Gradle/Maven 网络代理下载二进制依赖
- 自行切换版本绕过依赖冲突（需用户决策）
- 在初稿阶段写入大量未经验证的 `BuildRequires`
