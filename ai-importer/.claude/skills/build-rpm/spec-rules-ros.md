# ROS spec 规范

当 `<lang>=ros`（伪 gate_result 由 `ros_prep.py` 产出，`ros_pkg_manifest.json` 提供包元数据）时，spec 初稿应遵循以下规范。ROS 包构建进 `/opt/ros/%{ros_distro}` 前缀，与普通包（`/usr`）的布局、依赖模型、二进制扫描策略完全不同——**严禁按普通包经验写 ROS spec**。

## 1. 适用范围

适用于：
- `package.xml` 存在、由 ament/colcon 构建的 ROS 2 包（ament_cmake / ament_python）
- 纯 CMake 的 ROS 风格包（3rdparty、vendor 包）
- ROS 依赖元包（`ros_workspace` 等基座包）

**包名纪律（最高优先级）**：
- `Name: ros-%{ros_distro}-%{RosPkgName}`，其中 `%define ros_distro humble`（取自 `session.json` 的 `ros_distro` 字段，**禁止硬编码**），`%define RosPkgName <包名>`（package.xml 的 `<name>`）
- 例：`Name: ros-humble-rclcpp`、`Name: ros-humble-ament-cmake`
- **严禁**在 spec 中硬编码 `/opt/ros/humble`、`humble`、具体版本号——一律走 `%{ros_distro}` / `%{RosPkgName}` 宏。这是 ROS spec 最容易出错的地方（rpmbuild 宏展开在 `%prep` 前，`%{ros_distro}` 必须用 `%define` 而非 `%global` 时机错误的写法——两者在本文件中统一用 `%define`，放在 `Name:` 之前）

## 2. 命名与基础结构

```spec
%define ros_distro humble
%define RosPkgName rclcpp

Name:       ros-%{ros_distro}-%{RosPkgName}
Version:    <清单版本（去发布号）>
Release:    1%{?dist}
Summary:    <package.xml 的 <description> 或摘要>
License:    <package.xml 的 <license>，多许可证空格分隔>
Source0:    <上游仓库 URL>/archive/<ref>.tar.gz

# ROS 包不做自动依赖扫描（.so 全在 /opt/ros 下，由 ament 间接依赖模型替代）
%global __provides_exclude_from ^/opt/ros/%{ros_distro}/.*$
%global __requires_exclude_from ^/opt/ros/%{ros_distro}/.*$
```

**Version 来源**：`ros_pkg_manifest.json` 的 `listed_version`（格式 `2.0.2-3`，发布号 `-3` 剥离）。**不得**用 package.xml 的 `<version>`（那是上游开发版本，可能落后于清单发布版本）。

## 3. 安装前缀（所有形态强制）

- CMake 形态：`-DCMAKE_INSTALL_PREFIX="/opt/ros/%{ros_distro}"`
- 同时注入 ament 前缀（让 colcon/ament_cmake 找到已装包）：
  `-DAMENT_PREFIX_PATH="/opt/ros/%{ros_distro}"`、`-DCMAKE_PREFIX_PATH="/opt/ros/%{ros_distro}"`
- ament 布局强制（禁用 ament 的默认前缀拆分，全部装进前缀内）：
  `-UINCLUDE_INSTALL_DIR -ULIB_INSTALL_DIR -USYSCONF_INSTALL_DIR -USHARE_INSTALL_PREFIX -ULIB_SUFFIX`
- Python 形态：`export PYTHONPATH=/opt/ros/%{ros_distro}/lib/python%{python3_version}/site-packages`，`%{python3_sitearch}` 替换为 `/opt/ros/%{ros_distro}/lib/python%{python3_version}/site-packages`

## 4. 构建 bootstrap（`%build` / `%install` 开头）

```bash
# ROS 基座环境（chroot 已配置 ROS SIG repo，ros-humble-ros-workspace 等已安装）
source /opt/ros/%{ros_distro}/setup.sh 2>/dev/null || true
export PYTHONPATH=/opt/ros/%{ros_distro}/lib/python%{python3_version}/site-packages${PYTHONPATH:+:$PYTHONPATH}
export PKG_CONFIG_PATH=/opt/ros/%{ros_distro}/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}
```

## 5. 构建系统形态判定（读 package.xml 的 `<build_type>`，`ros_pkg_manifest.json` 可辅助）

| 形态 | 判定 | 构建要点 |
|------|------|---------|
| ament_cmake | `<build_type>ament_cmake</build_type>`（默认） | `%cmake` + `%cmake_build` + `%cmake_install`，见 §3 前缀参数；子目录为 `share/ament_index`、`share/<pkg>`、`lib` |
| ament_python | `<build_type>ament_python</build_type>` | `%{__python3} setup.py install` 或 `%py3_install`，前缀同上；`--install-layout` 由 `PYTHONPATH` 兜底 |
| 纯 cmake（3rdparty） | 无 ament 标记，上游纯 CMake | 标准 `%cmake` 流程 + §3 前缀 |
| vendor 包 | `_vendor` 后缀 / manifest 标 3rdparty | 见 §7 参考资产 |

**禁用测试优先转构建参数，而非 patch**（report730 §4.2 认知）：
- 优先 `-DBUILD_TESTING=OFF -DTESTING=OFF -DCMAKE_SUPPRESS_REGENERATION=ON` 等参数关闭
- 参数无法关闭时，其次考虑 `%cmake_build` 后删除测试目标；**最后**才用 patch
- 测试依赖（package.xml `<test_depend>`）**不写 BuildRequires**

## 6. 依赖填写（BuildRequires 纪律）

- 读 `ros_pkg_manifest.json`：
  - `official_deps_rpm[]` → 直接写 BuildRequires（官方 ROS SIG repo 已有，如 `ros-humble-ament-cmake-core`）
  - `official_deps[]`（原始名）→ 写作 `ros-%{ros_distro}-<dep>`
  - `registered_deps[]` → 已注册进 dep_registry 的依赖，**写 BuildRequires**（同一 COPR project 构建，安装时同源解析）
  - `missing_deps[]` → 缺口包，**不写 BuildRequires**（显式模式任务已终止，此文件不应出现在 spec 阶段）
- 系统依赖（`analyze_ros_deps.py` 的 `build_requires[]` / `unresolved[]` 经 `--check-rpm` 实证）→ 普通 BuildRequires（`-devel` 命名）
- **反幻觉铁律**：禁止凭 Fedora/Ubuntu ROS 经验猜依赖名。每个 `ros-humble-*` BuildRequires 必须能在 `ros-projects.list` 或 manifest 的依赖清单里找到依据；查不到就留空让构建失败诊断循环兜底，不得编造
- `pkg.remap` 命中（deb→rpm 映射，`data/ros/global_config/pkg.remap`）→ 按 rpm 名写

## 7. 参考资产（必须查）

- **spec 基线**：`./pkgs/<pkgname>/reference/<pkgname>.spec`（ros_fetch 从 cache 拷入）存在时，**以其为起点做 diff 审查**，而不是从零写（对应 ros-porting-tools 的 pkg-update 思路）：对照 §2-§6 纪律核对后适配版本/路径，保留其架构修正
- **76 包修正资产**：`/app/.claude/skills/build-rpm/scripts/data/ros/humble/package_fix/<pkg>/`，本包名存在时**必须读**：
  - `source.fix`（上游源码修正说明）、`prep.fix`（%prep 阶段修正，如 vendor 源码替换、patch 应用清单）
  - `custom.spec`（该包的全旁路 spec 参考）
  - `BuildRequires` / `Requires` / `Provides`（修正后的依赖清单，`-` 前缀=删除官方默认，`+` 前缀=添加）
  - `*.patch`（历史构建修正补丁，按需应用，不盲目）
  - `README.md`（该包构建要点说明）
- 修正资产的 `-` 前缀条目必须落实（官方默认依赖在 openEuler 不成立）

## 8. 常见失败模式（构建失败诊断参考）

| 症状 | 根因 | 处理 |
|------|------|------|
| `ament_cmake` not found | BuildRequires 缺 `ros-humble-ament-cmake` | 补基座依赖 |
| `package 'rclcpp' not found`（cmake） | 缺依赖包或前缀参数漏 `-DCMAKE_PREFIX_PATH` | 补依赖 / 查 §3 参数 |
| `setup.sh: No such file or directory` | chroot 未装 `ros-humble-ros-workspace` / 未配 ROS SIG repo | 部署期 chroot repo 前置（非 spec 问题，报告运维） |
| python 模块装进 `/usr/lib` | 漏 `PYTHONPATH` 前缀注入 | §4 bootstrap |
| `.so` 被自动打包 | 漏 `%global __provides_exclude_from` | §2 豁免 |
| vendor 包下载网络失败 | 上游 CMake FetchContent | 查 `prep.fix` / `custom.spec` 是否已有本地化方案 |
