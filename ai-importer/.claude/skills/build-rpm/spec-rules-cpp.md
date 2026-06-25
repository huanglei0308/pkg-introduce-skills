# C/C++ spec 规范

当 `<lang>=cpp` 或 `<lang>=c` 时，spec 初稿应根据上游构建系统选择对应路径。

## 1. 构建系统判断

| 判断依据 | 构建系统 | 是否支持 |
|---|---|---|
| 根目录有 `CMakeLists.txt` | CMake | ✅ 支持 |
| 根目录有 `meson.build` | Meson | ✅ 支持 |
| 根目录有 `configure.ac` / `Makefile.am` | Autotools | ✅ 支持 |
| 根目录有 `BUILD` / `BUILD.bazel` | Bazel | ❌ 暂不支持 |

---

## 2. BuildRequires 最小集合

**CMake 项目：**
```spec
BuildRequires: cmake
BuildRequires: gcc-c++
# 如上游推荐 ninja：
BuildRequires: ninja-build
```

**Meson 项目：**
```spec
BuildRequires: meson
BuildRequires: gcc-c++
BuildRequires: ninja-build
```

**Autotools 项目：**
```spec
BuildRequires: autoconf
BuildRequires: automake
BuildRequires: libtool
BuildRequires: gcc-c++
```

`%cmake` / `%cmake_build` / `%cmake_install` 宏由 `cmake` 包提供，无需额外声明。

---

## 3. 分包规范

参考 src-openeuler 社区惯例（以 yaml-cpp 为基准）：

| 子包 | 内容 | 说明 |
|---|---|---|
| **主包** | `*.so.*`（带版本号 soname）、doc、license | 运行时，普通用户安装 |
| **`-devel`** | 头文件（`%{_includedir}/`）、`*.so`（无版本号符号链接）、`*.a`（静态库）、cmake 文件（`%{_libdir}/cmake/`）、pkgconfig（`%{_libdir}/pkgconfig/`） | 编译期，开发者安装 |

`-devel` 包必须声明：
```spec
Requires: %{name} = %{version}-%{release}
```

### 3.1 Header-only 库

无 `.so` 的纯头文件库（如 argparse、expected-lite、nlohmann-json）：

- **不生成主包**，只保留 `-devel` 包，license/doc 也放入 `-devel`
- 空主包会触发 rpmlint `no-binary` E 错误，必须避免
- 必须在 spec 顶部加：

```spec
%global debug_package %{nil}
```

原因：无二进制产物，debuginfo 包为空会导致 `rpmbuild` 报错。

`-devel` 包**不需要** `Requires: %{name} = %{version}-%{release}`，因为没有主包。

### 3.2 同时提供静态库和共享库

若上游同时构建 static 和 shared（如 yaml-cpp），需两次 cmake：

```spec
%build
%define _vpath_builddir build_static
%cmake -DBUILD_SHARED_LIBS=OFF ...
%cmake_build

%define _vpath_builddir build_shared
%cmake -DBUILD_SHARED_LIBS=ON ...
%cmake_build

%install
%define _vpath_builddir build_static
%cmake_install
# 重命名避免被 shared install 覆盖
mv %{buildroot}%{_libdir}/cmake/%{name} \
   %{buildroot}%{_libdir}/cmake/%{name}-static

%define _vpath_builddir build_shared
%cmake_install
```

---

## 4. %build 标准写法

**CMake：**
```spec
%build
%cmake \
    -DBUILD_TESTING=OFF \
    -DBUILD_EXAMPLES=OFF
%cmake_build
```

**Meson：**
```spec
%build
%meson \
    -Dtests=disabled
%meson_build
```

**Autotools：**
```spec
%build
%configure \
    --disable-static
%make_build
```

---

## 5. %install 标准写法

```spec
%install
%cmake_install      # CMake
# 或
%meson_install      # Meson
# 或
%make_install       # Autotools
```

---

## 6. rpmlint 常见 E 错误处理

| 错误 | 原因 | 修法 |
|---|---|---|
| `noarch-with-lib64` | cmake/pkgconfig 安装到 `/usr/lib64/`，不能用 `BuildArch: noarch` | 去掉 `BuildArch: noarch`，改为 arch 包 |
| `no-binary` | arch 包里没有二进制文件（cmake 文件在 `%{_datadir}/cmake/`） | 加 `BuildArch: noarch`，cmake/pkgconfig 在 `%{_datadir}/` 时应为 noarch |
| `devel-file-in-non-devel-package` | 头文件或 pkgconfig 放在主包 | 分离到 `-devel` 子包 |
| `spelling-error` | description 里有 rpmlint 不认识的词 | 改写：`subcommands`→`sub-commands`，`pkg-config`→`pkgconfig`，`config`→`integration files` |
| `Empty %files file debugfiles.list` | header-only 库无二进制 | 顶部加 `%global debug_package %{nil}` |
| `no-signature` | 构建环境未签名 | 忽略，W 级别，构建环境正常现象 |
| `invalid-license MIT` | 该版本 rpmlint 不认 MIT（环境问题） | 忽略，W 级别，不影响归档 |

---

## 7. Source0 URL 规范

Source0 **必须**填写完整的上游下载 URL，不得只写文件名。这样 `spectool -g` 可直接下载源码，CI 可重现构建。

### 7.1 GitHub releases（推荐优先使用）

```spec
Source0: https://github.com/<owner>/%{name}/releases/download/v%{version}/%{name}-%{version}.tar.gz
```

### 7.2 GitHub tag archive（自动生成 tarball）

```spec
# 方式 A：解压目录名与 %{name}-%{version} 一致
Source0: https://github.com/<owner>/%{name}/archive/refs/tags/v%{version}/%{name}-%{version}.tar.gz

# 方式 B：解压目录名与期望不一致时，加 #/ 锚点给 spectool 指定保存文件名
Source0: https://github.com/<owner>/%{name}/archive/v%{version}.tar.gz#/%{name}-%{version}.tar.gz
```

注意：GitHub tag archive 解压后根目录格式为 `<repo>-<tag>`（去掉前缀 `v`），如 tag `v1.3.2` -> 根目录 `cereal-1.3.2`。

### 7.3 其他托管平台

```spec
# SourceForge
Source0: https://downloads.sourceforge.net/%{name}/%{name}-%{version}.tar.gz

# GitLab
Source0: https://gitlab.com/<group>/%{name}/-/archive/v%{version}/%{name}-%{version}.tar.gz
```

---

## 8. spec 模板

### 8.1 有共享库的普通 C++ 库

```spec
Name:           libfoo
Version:        X.Y.Z
Release:        1%{?dist}
Summary:        Short description of libfoo

License:        MIT
URL:            https://github.com/example/libfoo
Source0:        https://github.com/example/libfoo/releases/download/v%{version}/%{name}-%{version}.tar.gz

Group:          Development/Libraries
Packager:       openEuler Builder <builder@openeuler.org>

BuildRequires:  cmake
BuildRequires:  gcc-c++

%description
Long description of libfoo.

%package devel
Summary:        Development files for %{name}
Group:          Development/Libraries
Packager:       openEuler Builder <builder@openeuler.org>
Requires:       %{name} = %{version}-%{release}

%description devel
Header files and development libraries for %{name}.

%prep
%autosetup -p1

%build
%cmake \
    -DBUILD_TESTING=OFF \
    -DBUILD_EXAMPLES=OFF
%cmake_build

%install
%cmake_install

%check
%ctest

%files
%license LICENSE
%doc README.md
%{_libdir}/*.so.*

%files devel
%{_includedir}/foo/
%{_libdir}/*.so
%{_libdir}/cmake/foo/
%{_libdir}/pkgconfig/foo.pc

%changelog
* Mon May 12 2026 openEuler Builder <builder@openeuler.org> - X.Y.Z-1
- Initial package
```

### 8.2 Header-only 库（有 CMake 安装）

适用于有 CMakeLists.txt 且 cmake --install 会安装头文件和 cmake 文件的包（如 cereal、nlohmann-json）：

```spec
%global debug_package %{nil}

Name:           foo
Version:        X.Y.Z
Release:        1%{?dist}
Summary:        Short description of foo

License:        MIT
URL:            https://github.com/example/foo
Source0:        https://github.com/example/foo/archive/v%{version}.tar.gz#/%{name}-%{version}.tar.gz

Group:          Development/Libraries
Packager:       openEuler Builder <builder@openeuler.org>

BuildRequires:  cmake
BuildRequires:  gcc-c++

%description
Long description of foo.

%package devel
Summary:        Development files for %{name}
Group:          Development/Libraries
Packager:       openEuler Builder <builder@openeuler.org>
Provides:       %{name}-static = %{version}-%{release}

%description devel
Header files and CMake integration files for %{name}.

%prep
%autosetup -p1

%build
%cmake \
    -DFOO_BUILD_TESTS=OFF
%cmake_build

%install
%cmake_install

%files devel
%license LICENSE
%doc README.md
%{_includedir}/foo/
%{_libdir}/cmake/foo/

%changelog
* Mon May 12 2026 openEuler Builder <builder@openeuler.org> - X.Y.Z-1
- Initial package
```

### 8.3 Header-only 库（纯手工安装，无 CMake）

适用于单头文件或无 CMakeLists 的包（如 tl-expected、nameof）：

```spec
%global debug_package %{nil}

Name:           foo
Version:        X.Y.Z
Release:        1%{?dist}
Summary:        Short description of foo

License:        MIT
URL:            https://github.com/example/foo
Source0:        https://github.com/example/foo/releases/download/v%{version}/%{name}-%{version}.tar.gz

Group:          Development/Libraries
Packager:       openEuler Builder <builder@openeuler.org>

%description
Long description of foo.

%package devel
Summary:        Development files for %{name}
Group:          Development/Libraries
Packager:       openEuler Builder <builder@openeuler.org>

%description devel
Header files for %{name}.

%prep
%autosetup -p1

%build

%install
install -d %{buildroot}%{_includedir}/foo
install -p -m 644 include/foo/*.hpp %{buildroot}%{_includedir}/foo/

%files devel
%license LICENSE
%doc README.md
%{_includedir}/foo/

%changelog
* Mon May 12 2026 openEuler Builder <builder@openeuler.org> - X.Y.Z-1
- Initial package
```
