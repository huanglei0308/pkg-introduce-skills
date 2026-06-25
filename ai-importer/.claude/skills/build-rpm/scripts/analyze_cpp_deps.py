#!/usr/bin/env python3
"""
C/C++ 包 RPM 依赖分析脚本

依赖来源：
  1. CMakeLists.txt — find_package() / pkg_check_modules() / target_link_libraries()
  2. configure.ac   — AC_CHECK_LIB / PKG_CHECK_MODULES
  3. Makefile       — -l<lib> 链接标志（兜底）

RPM 三级查询策略（共享一次性批量查询，无需映射表）：
  Level 1: `cmake(Foo)`
  Level 2: `pkgconfig(foo)`
  Level 3: `libfoo.so*` / `*-devel` 回退

用法：
  python3 analyze_cpp_deps.py <source_dir>
  python3 analyze_cpp_deps.py <source_dir> --check-rpm --container oe-build-env
  python3 analyze_cpp_deps.py <source_dir> --check-rpm --container oe-build-env -o result.json
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

from rpm_batch_lookup import (
    BatchLookupError,
    fallback_results,
    file_glob_query,
    name_query,
    provides_query,
    run_batch_lookup,
)

GLIBC_BUILTINS = {"pthread", "m", "dl", "c", "rt", "gcc_s", "stdc++", "resolv", "util"}

# 纯构建辅助包，不需要查 RPM
CMAKE_SKIP = {
    "CMakePackageConfigHelpers", "GNUInstallDirs", "PackageHandleStandardArgs",
    "CheckCXXCompilerFlag", "CheckCCompilerFlag", "CheckIncludeFile",
    "CheckFunctionExists", "CheckLibraryExists", "FeatureSummary",
    "CTest", "CPack", "ExternalProject", "FetchContent",
}

IGNORED_PKG_MODULE_TOKENS = {"REQUIRED", "QUIET", "IMPORTED_TARGET", "STATIC"}
OPERATORS = {">=", "<=", "==", "=", ">", "<"}


# ── 1. 源码解析 ───────────────────────────────────────────────────────────────

def normalize_requirement(raw: str, default_operator: str = ">=") -> str:
    raw = raw.strip().strip('"\'')
    if not raw:
        return ""
    match = re.match(r"^(>=|<=|==|=|>|<)\s*(.+)$", raw)
    if match:
        operator = "==" if match.group(1) == "=" else match.group(1)
        return f"{operator} {match.group(2).strip()}"
    if re.match(r"^[0-9]", raw):
        return f"{default_operator} {raw}"
    return ""


def build_dependency_item(dep_type: str, name: str, requirement: str = "") -> Dict[str, str]:
    rpm_requirement = name
    if dep_type == "cmake":
        rpm_requirement = f"cmake({name})"
    elif dep_type == "pkgconfig":
        rpm_requirement = f"pkgconfig({name.lower()})"
    elif dep_type == "link":
        rpm_requirement = f"lib{name.lower()}.so"
    if requirement and dep_type != "link":
        rpm_requirement = f"{rpm_requirement} {requirement}"
    return {
        "dep": name,
        "name": name,
        "type": dep_type,
        "requirement": requirement,
        "rpm_requirement": rpm_requirement,
        "upstream_url": "",
    }


def merge_dependency_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    merged: Dict[tuple[str, str], Dict[str, str]] = {}
    for item in items:
        key = (item["type"], item["dep"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            continue
        if not existing.get("requirement") and item.get("requirement"):
            merged[key] = item
        elif existing.get("requirement", "").startswith(">=") and item.get("requirement", "").startswith("=="):
            merged[key] = item
    return list(merged.values())


def parse_pkg_module_clause(raw_clause: str) -> List[Dict[str, str]]:
    tokens = [token.strip('"\'') for token in re.split(r"\s+", raw_clause.replace("\n", " ")) if token.strip()]
    items: List[Dict[str, str]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        upper = token.upper()
        if upper in IGNORED_PKG_MODULE_TOKENS or token.startswith("["):
            i += 1
            continue

        combined = re.match(r"^([A-Za-z0-9_.+-]+)(>=|<=|==|=|>|<)(.+)$", token)
        if combined:
            items.append(build_dependency_item("pkgconfig", combined.group(1), normalize_requirement(f"{combined.group(2)} {combined.group(3)}")))
            i += 1
            continue

        if i + 2 < len(tokens) and tokens[i + 1] in OPERATORS:
            items.append(build_dependency_item("pkgconfig", token, normalize_requirement(f"{tokens[i + 1]} {tokens[i + 2]}")))
            i += 3
            continue

        items.append(build_dependency_item("pkgconfig", token))
        i += 1
    return items


def parse_cmake(source_dir: str) -> Dict:
    """扫描 CMakeLists.txt，提取 find_package / pkg_check_modules / -l 链接库。"""
    src = Path(source_dir)
    find_items: List[Dict[str, str]] = []
    pkg_items: List[Dict[str, str]] = []
    link_items: List[Dict[str, str]] = []
    cmake_ver = ""

    for cmake_file in list(src.rglob("CMakeLists.txt")) + list(src.rglob("*.cmake")):
        try:
            content = cmake_file.read_text(errors="ignore")
        except OSError:
            continue

        if not cmake_ver:
            m = re.search(r"cmake_minimum_required\s*\(\s*VERSION\s+([\d.]+)", content, re.IGNORECASE)
            if m:
                cmake_ver = m.group(1)

        for m in re.finditer(r"find_package\s*\(\s*(\w+)([^)]*)\)", content, re.IGNORECASE | re.DOTALL):
            pkg = m.group(1)
            if pkg in CMAKE_SKIP:
                continue
            tail = m.group(2).strip()
            tokens = [token.strip('"\'') for token in re.split(r"\s+", tail) if token.strip()]
            requirement = ""
            if tokens and re.match(r"^[0-9]", tokens[0]):
                default_operator = "==" if any(t.upper() == "EXACT" for t in tokens[1:]) else ">="
                requirement = normalize_requirement(tokens[0], default_operator=default_operator)
            find_items.append(build_dependency_item("cmake", pkg, requirement))

        for m in re.finditer(r"pkg_(?:check|search)_modules?\s*\(\s*\w+\s+([^)]+)\)", content, re.IGNORECASE | re.DOTALL):
            pkg_items.extend(parse_pkg_module_clause(m.group(1)))

        for m in re.finditer(r"(?:target_link_libraries|link_libraries)\s*\([^)]+\)", content, re.IGNORECASE | re.DOTALL):
            for lib in re.findall(r"-l(\w+)", m.group(0)):
                if lib not in GLIBC_BUILTINS:
                    link_items.append(build_dependency_item("link", lib))

    find_items = merge_dependency_items(find_items)
    pkg_items = merge_dependency_items(pkg_items)
    link_items = merge_dependency_items(link_items)
    dependency_items = merge_dependency_items(find_items + pkg_items + link_items)

    return {
        "build_system": "cmake",
        "cmake_min_version": cmake_ver,
        "find_packages": sorted(item["dep"] for item in find_items),
        "pkg_modules": sorted(item["dep"] for item in pkg_items),
        "link_libs": sorted(item["dep"] for item in link_items),
        "find_package_items": find_items,
        "pkg_module_items": pkg_items,
        "link_lib_items": link_items,
        "dependency_items": dependency_items,
    }


def parse_autoconf(source_dir: str) -> Dict:
    """解析 configure.ac，提取 AC_CHECK_LIB / PKG_CHECK_MODULES。"""
    src = Path(source_dir)
    libs: List[Dict[str, str]] = []
    pkg_items: List[Dict[str, str]] = []

    for ac_file in [src / "configure.ac", src / "configure.in"]:
        if not ac_file.exists():
            continue
        content = ac_file.read_text(errors="ignore")

        for m in re.finditer(r"AC_CHECK_LIB\s*\(\s*\[?(\w+)\]?", content):
            lib = m.group(1)
            if lib not in GLIBC_BUILTINS:
                libs.append(build_dependency_item("link", lib))

        for m in re.finditer(r"PKG_CHECK_MODULES\s*\(\s*\w+\s*,\s*([^)]+)\)", content, re.IGNORECASE | re.DOTALL):
            pkg_items.extend(parse_pkg_module_clause(m.group(1)))

    pkg_items = merge_dependency_items(pkg_items)
    libs = merge_dependency_items(libs)
    dependency_items = merge_dependency_items(pkg_items + libs)

    return {
        "build_system": "autoconf",
        "link_libs": sorted(item["dep"] for item in libs),
        "pkg_modules": sorted(item["dep"] for item in pkg_items),
        "find_packages": [],
        "find_package_items": [],
        "pkg_module_items": pkg_items,
        "link_lib_items": libs,
        "dependency_items": dependency_items,
    }


def parse_makefile(source_dir: str) -> Dict:
    """从 Makefile 提取 -l<lib> 作为兜底。"""
    src = Path(source_dir)
    libs: List[Dict[str, str]] = []

    for mk in [src / "Makefile", src / "GNUmakefile"]:
        if not mk.exists():
            continue
        content = mk.read_text(errors="ignore")
        for lib in re.findall(r"-l(\w+)", content):
            if lib not in GLIBC_BUILTINS:
                libs.append(build_dependency_item("link", lib))

    libs = merge_dependency_items(libs)
    return {
        "build_system": "make",
        "link_libs": sorted(item["dep"] for item in libs),
        "find_packages": [],
        "pkg_modules": [],
        "find_package_items": [],
        "pkg_module_items": [],
        "link_lib_items": libs,
        "dependency_items": libs,
    }


def detect_build_system(source_dir: str) -> str:
    src = Path(source_dir)
    if (src / "CMakeLists.txt").exists():
        return "cmake"
    if (src / "configure.ac").exists() or (src / "configure.in").exists():
        return "autoconf"
    if (src / "meson.build").exists():
        return "meson"
    if (src / "Makefile").exists():
        return "make"
    return "unknown"


# ── 2. 三级 RPM 查询 ──────────────────────────────────────────────────────────

def build_lookup_tasks(parsed: Dict) -> List[Dict]:
    tasks: List[Dict] = []
    for item in parsed.get("find_package_items", []):
        pkg = item["dep"]
        pkg_lower = pkg.lower()
        tasks.append({
            **item,
            "prefer_devel": True,
            "queries": [
                provides_query(f"cmake({pkg})", "cmake()"),
                provides_query(f"pkgconfig({pkg_lower})", "pkgconfig()"),
                file_glob_query(f"*/lib{pkg_lower}.so*", "libso", prefer_devel=True),
                name_query(f"{pkg_lower}-devel", "name", prefer_devel=True),
            ],
        })
    for item in parsed.get("pkg_module_items", []):
        pc = item["dep"]
        pc_lower = pc.lower()
        tasks.append({
            **item,
            "prefer_devel": True,
            "queries": [
                provides_query(f"pkgconfig({pc_lower})", "pkgconfig()"),
                provides_query(f"cmake({pc})", "cmake()"),
                file_glob_query(f"*/lib{pc_lower}.so*", "libso", prefer_devel=True),
                name_query(f"{pc_lower}-devel", "name", prefer_devel=True),
            ],
        })
    for item in parsed.get("link_lib_items", []):
        lib = item["dep"]
        lib_lower = lib.lower()
        tasks.append({
            **item,
            "prefer_devel": True,
            "queries": [
                file_glob_query(f"*/lib{lib_lower}.so*", "libso", prefer_devel=True),
                name_query(f"{lib_lower}-devel", "name", prefer_devel=True),
                name_query(f"lib{lib_lower}-devel", "name", prefer_devel=True),
                provides_query(f"pkgconfig({lib_lower})", "pkgconfig()"),
            ],
        })
    return tasks


def check_rpm_availability(parsed: Dict = None) -> Dict:
    if parsed is None:
        parsed = {}
    tasks = build_lookup_tasks(parsed)
    print(f"\n[INFO] 本地查询 RPM 可用性（单次批量查询）...")

    try:
        results = run_batch_lookup(tasks, timeout=120)
    except (BatchLookupError, OSError, json.JSONDecodeError) as e:
        print(f"[WARN] 批量 RPM 查询失败（{e}），跳过依赖检查")
        results = fallback_results(tasks)

    available, missing = [], []
    for item in results:
        dep_type, dep, rpm, level = item["type"], item["dep"], item.get("rpm"), item.get("level", "")
        requirement = item.get("requirement", "")
        label = f"[{dep_type}] {dep}"
        if requirement:
            label = f"{label} {requirement}"
        if rpm:
            rpm_version = item.get("version") or ""
            version_label = f" {rpm_version}" if rpm_version else ""
            # 版本约束验证
            version_ok = True
            if rpm_version and requirement:
                try:
                    import sys as _sys
                    from pathlib import Path as _Path
                    _sd = str(_Path(__file__).resolve().parent)
                    if _sd not in _sys.path:
                        _sys.path.insert(0, _sd)
                    import importlib as _il
                    _cep = _il.import_module("check_existing_package")
                    req_info = _cep.parse_requirement(requirement)
                    result = _cep.evaluate_requirement(rpm_version, req_info)
                    if result is False:
                        version_ok = False
                except Exception:
                    pass
            if version_ok:
                print(f"  ✓ {label:<40} → {rpm}{version_label}  ({level})")
                available.append({
                    "dep": dep,
                    "name": item.get("name", dep),
                    "type": dep_type,
                    "requirement": requirement,
                    "rpm_requirement": item.get("rpm_requirement", dep),
                    "rpm": rpm,
                    "version": item.get("version"),
                    "release": item.get("release"),
                    "level": level,
                    "upstream_url": item.get("upstream_url", ""),
                })
            else:
                print(f"  ~ {label:<40} → {rpm}{version_label} 不满足约束 {requirement}，需递归引入")
                missing.append({
                    "dep": dep,
                    "name": item.get("name", dep),
                    "type": dep_type,
                    "requirement": requirement,
                    "rpm_requirement": item.get("rpm_requirement", dep),
                    "upstream_url": item.get("upstream_url", ""),
                })
        else:
            print(f"  ✗ {label:<40} → 未找到")
            missing.append({
                "dep": dep,
                "name": item.get("name", dep),
                "type": dep_type,
                "requirement": requirement,
                "rpm_requirement": item.get("rpm_requirement", dep),
                "upstream_url": item.get("upstream_url", ""),
            })

    return {"available": available, "missing": missing}


# ── 3. BuildRequires 生成 & 报告 ──────────────────────────────────────────────

def build_rpm_requires(build_system: str, rpm_check: Optional[Dict]) -> List[str]:
    result = ["gcc", "gcc-c++"]
    if build_system == "cmake":
        result.append("cmake")
    elif build_system == "autoconf":
        result += ["autoconf", "automake", "libtool", "make"]
    elif build_system == "meson":
        result += ["meson", "ninja-build"]
    else:
        result.append("make")

    if rpm_check:
        seen = set(result)
        for item in rpm_check.get("available", []):
            rpm = item["rpm"]
            if rpm not in seen:
                result.append(rpm)
                seen.add(rpm)
    return result


def print_items(title: str, items: List[Dict[str, str]]) -> None:
    if not items:
        return
    print(f"\n[{title}]  {len(items)} 个")
    for item in items:
        suffix = f" {item['requirement']}" if item.get("requirement") else ""
        print(f"  - {item['dep']}{suffix}")


def print_report(parsed: Dict, rpm_check: Optional[Dict]):
    sep = "=" * 60
    print(f"\n{sep}")
    print("C/C++ 包 RPM 依赖分析报告")
    print(sep)
    bs = parsed.get("build_system", "unknown")
    print(f"  构建系统 : {bs}")
    if parsed.get("cmake_min_version"):
        print(f"  CMake 版本: >= {parsed['cmake_min_version']}")

    print_items("find_package 依赖", parsed.get("find_package_items", []))
    print_items("pkg_check_modules 依赖", parsed.get("pkg_module_items", []))
    print_items("-l 链接库", parsed.get("link_lib_items", []))

    if rpm_check:
        avail = rpm_check["available"]
        miss = rpm_check["missing"]
        print(f"\n[RPM 可用性]  已有 {len(avail)} / 缺失 {len(miss)}")
        for item in avail:
            suffix = f" {item['requirement']}" if item.get("requirement") else ""
            version = f" {item['version']}" if item.get("version") else ""
            print(f"  ✓ {item['dep']}{suffix:<30} → {item['rpm']}{version}  [{item['level']}]")
        for item in miss:
            suffix = f" {item['requirement']}" if item.get("requirement") else ""
            print(f"  ✗ {item['dep']}{suffix}")

    br = build_rpm_requires(bs, rpm_check)
    print(f"\n[BuildRequires 建议]")
    for r in br:
        print(f"  BuildRequires: {r}")
    print(sep)


# ── 4. 主入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="C/C++ 包 RPM 依赖分析")
    parser.add_argument("source_dir", help="C/C++ 项目源码目录")
    parser.add_argument("--check-rpm", action="store_true", help="在容器内查询 RPM 可用性")
    parser.add_argument("-o", "--output", default="", help="结果输出到 JSON 文件")
    args = parser.parse_args()

    source_dir = os.path.abspath(args.source_dir)
    if not os.path.isdir(source_dir):
        print(f"[ERROR] 目录不存在: {source_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 分析目录: {source_dir}")
    bs = detect_build_system(source_dir)
    print(f"[INFO] 构建系统: {bs}")

    if bs == "cmake":
        parsed = parse_cmake(source_dir)
    elif bs == "autoconf":
        parsed = parse_autoconf(source_dir)
    else:
        parsed = parse_makefile(source_dir)

    rpm_check = None
    if args.check_rpm:
        total = len(parsed.get("dependency_items", []))
        if total == 0:
            print("[INFO] 未检测到外部依赖，跳过 RPM 查询")
        else:
            rpm_check = check_rpm_availability(parsed=parsed)

    print_report(parsed, rpm_check)

    if args.output:
        result = {
            "build_system": parsed.get("build_system"),
            "cmake_min_version": parsed.get("cmake_min_version", ""),
            "find_packages": parsed.get("find_packages", []),
            "pkg_modules": parsed.get("pkg_modules", []),
            "link_libs": parsed.get("link_libs", []),
            "find_package_items": parsed.get("find_package_items", []),
            "pkg_module_items": parsed.get("pkg_module_items", []),
            "link_lib_items": parsed.get("link_lib_items", []),
            "dependency_items": parsed.get("dependency_items", []),
            "rpm_check": rpm_check,
            "build_requires": build_rpm_requires(parsed.get("build_system", ""), rpm_check),
        }
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[INFO] 结果已保存: {args.output}")

    if rpm_check and rpm_check["missing"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
