# RPM Spec 审查规则集

本文件是 `review-rpm` skill 的唯一规则来源。审查 agent 必须基于本文件的规则做出裁决，不得依赖 Actor 的输出来判断正确性（防止偏见强化）。

---

## § 1 基础字段规则

| 规则 ID | 级别 | 规则描述 |
|---------|------|----------|
| F-01 | E | `Name` 必须与包目录名一致 |
| F-02 | E | `Version` 必须与上游 tag/release 一致，不得手工捏造 |
| F-03 | E | `Release` 必须为 `1%{?dist}` |
| F-04 | E | `License` 必须使用 SPDX 标识符（如 `MIT`、`Apache-2.0`、`BSD-3-Clause`） |
| F-05 | W | `URL` 必须指向上游源码仓根地址，不得是 releases 页、文档页或 PyPI 页 |
| F-06 | E | `Source0` 必须为完整下载 URL（如 `https://github.com/.../v%{version}/%{name}-%{version}.tar.gz`），不得只写文件名（如 `%{name}-%{version}.tar.gz`） |
| F-07 | W | `Group` 应为 `Development/Libraries` |
| F-08 | W | `Packager` 应为 `openEuler Builder <builder@openeuler.org>` |

---

## § 2 BuildRequires 规则

| 规则 ID | 级别 | 规则描述 |
|---------|------|----------|
| B-01 | E | CMake 项目必须有 `BuildRequires: cmake` 和 `BuildRequires: gcc-c++` |
| B-02 | E | Meson 项目必须有 `BuildRequires: meson`、`BuildRequires: gcc-c++`、`BuildRequires: ninja-build` |
| B-03 | E | Autotools 项目必须有 `BuildRequires: autoconf`、`automake`、`libtool`、`gcc-c++` |
| B-04 | W | 不得声明不必要的 BuildRequires（如 `cmake` 已包含 `%cmake` 宏，无需额外声明） |
| B-05 | E | 外部库依赖必须声明对应的 `-devel` 包（如 `hiredis-devel`） |

---

## § 3 分包规则

### § 3.1 有共享库的普通 C/C++ 库

| 规则 ID | 级别 | 规则描述 |
|---------|------|----------|
| P-01 | E | 主包必须包含带版本号的 `.so.*` 文件（如 `libfoo.so.1`、`libfoo.so.1.2.3`） |
| P-02 | E | `-devel` 包必须包含无版本号的 `.so` 符号链接 |
| P-03 | E | 头文件（`%{_includedir}/`）必须在 `-devel` 包，不得在主包 |
| P-04 | E | cmake 文件和 pkgconfig 文件必须在 `-devel` 包 |
| P-05 | E | `-devel` 包必须声明 `Requires: %{name}%{?_isa} = %{version}-%{release}` 或 `Requires: %{name} = %{version}-%{release}` |
| P-06 | E | 主包必须有 `%post -p /sbin/ldconfig` 和 `%postun -p /sbin/ldconfig` |
| P-07 | W | `%license` 和 `%doc` 建议放在 `-devel` 包（避免 `non-versioned-file-in-library-package` 警告） |

### § 3.2 Header-only 库

| 规则 ID | 级别 | 规则描述 |
|---------|------|----------|
| H-01 | E | 必须在 spec 顶部加 `%global debug_package %{nil}` |
| H-02 | E | 不得生成空主包（会触发 `no-binary` E 错误）；只保留 `-devel` 包 |
| H-03 | E | `-devel` 包不得声明 `Requires: %{name} = %{version}-%{release}`（没有主包） |
| H-04 | I | `%license` 和 `%doc` 放在 `-devel` 包 |

### § 3.3 noarch vs arch 判断

| 规则 ID | 级别 | 规则描述 |
|---------|------|----------|
| A-01 | E | cmake 文件安装到 `%{_libdir}/cmake/`（即 `/usr/lib64/cmake/`）时，不得声明 `BuildArch: noarch`（会触发 `noarch-with-lib64`） |
| A-02 | E | cmake 文件安装到 `%{_datadir}/cmake/`（即 `/usr/share/cmake/`）时，应声明 `BuildArch: noarch` |
| A-03 | E | pkgconfig 文件安装到 `%{_libdir}/pkgconfig/` 时，不得声明 `BuildArch: noarch` |
| A-04 | W | pkgconfig 文件安装到 `%{_datadir}/pkgconfig/` 时，建议声明 `BuildArch: noarch` |

---

## § 4 %build 规则

| 规则 ID | 级别 | 规则描述 |
|---------|------|----------|
| BD-01 | E | CMake 项目必须使用 `%cmake` / `%cmake_build` 宏，不得手写 `cmake ..` |
| BD-02 | E | Meson 项目必须使用 `%meson` / `%meson_build` 宏 |
| BD-03 | E | Autotools 项目必须使用 `%configure` / `%make_build` 宏 |
| BD-04 | W | 应关闭测试和示例构建（如 `-DBUILD_TESTING=OFF`、`-DBUILD_EXAMPLES=OFF`） |
| BD-05 | W | 不得在 `%build` 中修改源码文件 |

---

## § 5 %install 规则

| 规则 ID | 级别 | 规则描述 |
|---------|------|----------|
| I-01 | E | CMake 项目必须使用 `%cmake_install`，不得手写 `make install` |
| I-02 | E | Meson 项目必须使用 `%meson_install` |
| I-03 | E | Autotools 项目必须使用 `%make_install` |

---

## § 6 rpmlint 错误处理规则

| 错误 | 级别 | 处理方式 |
|------|------|----------|
| `noarch-with-lib64` | E | 去掉 `BuildArch: noarch` |
| `no-binary` | E | 加 `BuildArch: noarch`（cmake/pkgconfig 在 `%{_datadir}/`）；或去掉空主包 |
| `devel-file-in-non-devel-package` | E | 将头文件/pkgconfig 移到 `-devel` 包 |
| `non-versioned-file-in-library-package` | E | 将 doc/license 移出主包，放入 `-devel` |
| `library-without-ldconfig` | E | 补充 `%post/%postun -p /sbin/ldconfig` |
| `static-library-without-debuginfo` | E | 禁用静态库构建（`-DBUILD_STATIC=OFF`）或加 `%global debug_package %{nil}` |
| `spelling-error` | W | 修改描述文字（`subcommands`→`sub-commands`，`pkg-config`→`pkgconfig`） |
| `no-signature` | I | 构建环境正常现象，忽略 |
| `invalid-license MIT/Apache-2.0/BSD-3-Clause` | I | 环境问题，W 级别，忽略 |
| `missing-hash-section` | I | 构建环境误报，忽略 |
| `no-library-dependency-for` | I | 构建环境误报（build-id 实际存在），忽略 |
| `non-standard-dir-in-usr` | W | 上游安装行为，记录但不阻断 |

---

## § 7 %changelog 规则

| 规则 ID | 级别 | 规则描述 |
|---------|------|----------|
| C-01 | E | 必须有至少一条 changelog 条目 |
| C-02 | E | 日期格式必须为 `* Www Mon DD YYYY`（如 `* Tue May 13 2026`） |
| C-03 | E | 版本标记必须为 `- <version>-<release>`（如 `- 1.3.2-1`） |
| C-04 | W | 作者应为 `openEuler Builder <builder@openeuler.org>` |

---

## § 8 归档完整性规则（final 阶段）

| 规则 ID | 级别 | 规则描述 |
|---------|------|----------|
| AR-01 | E | `dist/` 目录必须有对应的 `.rpm`（binary RPM） |
| AR-02 | E | `dist/` 目录必须有对应的 `.src.rpm`（source RPM） |
| AR-03 | E | `<pkgname>/` 目录必须有 `<pkgname>.spec` |
| AR-04 | W | spec 中的 `Version` 必须与 RPM 文件名中的版本一致 |
| AR-05 | W | `repodata/` 目录应已更新（`createrepo` 已运行） |

---

## § 9 Python 包专项规则

| 规则 ID | 级别 | 规则描述 |
|---------|------|----------|
| PY-01 | E | `Source0` 必须使用 `%{pypi_source}` 宏或完整 PyPI/GitHub URL，不得只写文件名 |
| PY-02 | W | `%package -n python3-<name>` 应声明 `Provides: python3dist(<pypi_name>) = %{version}`，以满足 pip/PEP 503 风格依赖 |
| PY-03 | W | 应有 `%package help` 子包存放文档（README、CHANGELOG 等），主包和二进制包只放 `%license` |
| PY-04 | E | 纯 Python 包必须有 `BuildArch: noarch`；含 `.so` 的 C 扩展包不得有 `BuildArch: noarch` |
| PY-05 | W | `%prep` 应使用 `%autosetup -p1`，不得直接用 `%setup` |

---

## § 10 Java 包专项规则

| 规则 ID | 级别 | 规则描述 |
|---------|------|----------|
| JV-01 | E | `Source0` 必须为完整 URL（GitHub archive、Apache dist 等），不得只写文件名 |
| JV-02 | E | 文档子包应命名为 `%package javadoc`，`%files` 对应 `-f .mfiles-javadoc`，不得命名为 `help` |
| JV-03 | E | `%prep` 中 `%pom_disable_module` 必须在所有 `%pom_remove_plugin -r` 之前，否则递归扫描会报错 |
| JV-04 | W | `%prep` 中应移除发布类插件：`nexus-staging-maven-plugin`、`maven-release-plugin`、`sortpom-maven-plugin`、`maven-gpg-plugin`、`jacoco-maven-plugin`、`central-publishing-maven-plugin` |
| JV-05 | W | 需要 Java 17/21 特性的包，`BuildRequires` 应用 `maven-local-openjdk17` 或 `maven-local-openjdk21` 而非仅 `maven-local` |
