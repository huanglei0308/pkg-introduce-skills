# python-qpid-proton 打包经验（build 128 成功，2026-06-22）

## 关键修法

1. `%global debug_package %{nil}` — 禁用 debuginfo/debugsource 包
2. `cp VERSION.txt python/VERSION.txt` — 修复版本号（避免 0.0.0）
3. `pushd python / popd` — Python binding 在 python/ 子目录
4. `SETUPTOOLS_SCM_PRETEND_VERSION=%{version}` — 强制正确版本
5. `qpid-proton-c-cpp-devel`（不是 qpid-proton-c-devel）
6. `%files` 需包含 `__pycache__/cproton*.pyc`

## 成功 spec

```spec
%global debug_package %{nil}

Name:           python-qpid-proton
Version:        0.40.0
Release:        1%{?dist}
Summary:        Python bindings for Qpid Proton AMQP messaging library
License:        Apache-2.0
URL:            https://qpid.apache.org/proton
Source0:        https://github.com/apache/qpid-proton/archive/refs/tags/%{version}/%{name}-%{version}.tar.gz

BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  python3-pip
BuildRequires:  python3-cffi
BuildRequires:  gcc
BuildRequires:  cmake
BuildRequires:  qpid-proton-c-cpp-devel

%description
Python bindings for Qpid Proton AMQP messaging library.

%package -n python3-qpid-proton
Summary:        Python bindings for Qpid Proton AMQP messaging library
Provides:       python-qpid-proton
Provides:       python3dist(python-qpid-proton) = %{version}
Requires:       python3-cffi

%description -n python3-qpid-proton
Python bindings for Qpid Proton AMQP messaging library.

%package help
Summary:        Development documents and examples for python-qpid-proton
Provides:       python3-qpid-proton-doc
Requires:       python3-qpid-proton
%description help
Development documents and examples for the python-qpid-proton package.

%prep
%autosetup -n %{name}-%{version} -p1
cp VERSION.txt python/VERSION.txt

%build
%define _lto_cflags %{nil}
%undefine _hardened_build
pushd python
QPID_PYTHON_UNBUNDLING=unbundled SETUPTOOLS_SCM_PRETEND_VERSION=%{version} \
    python3 -m pip wheel --no-build-isolation --no-deps --wheel-dir %{_builddir}/wheels .
popd

%install
python3 -m pip install --no-build-isolation --no-index \
    %{_builddir}/wheels/python_qpid_proton-*.whl \
    --root %{buildroot} --prefix /usr

%files -n python3-qpid-proton
%license python/LICENSE.txt
%{python3_sitearch}/proton/
%{python3_sitearch}/cproton*.so
%{python3_sitearch}/cproton.py
%{python3_sitearch}/__pycache__/cproton*.pyc
%{python3_sitearch}/python_qpid_proton-*.dist-info/

%files help
%license python/LICENSE.txt
%doc python/README.rst

%changelog
* Sun Jun 21 2026 Python_Bot <Python_Bot@openeuler.org> - 0.40.0-1
- Initial package
```
