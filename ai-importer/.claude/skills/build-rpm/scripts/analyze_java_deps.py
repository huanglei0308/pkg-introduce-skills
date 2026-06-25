#!/usr/bin/env python3
"""
Java 包 RPM 依赖分析脚本

依赖来源：
  1. pom.xml       — Maven <dependency> groupId:artifactId:version
  2. build.gradle  — Gradle implementation/compile/api 声明

RPM 查询策略：
  通过共享的一次性批量查询检查 `mvn(groupId:artifactId)` Provides。
  openEuler 的 Java RPM 包遵循 mvn() Provides 规范。
  大多数 Maven 依赖在 openEuler 上缺失，需要 bundle 或 generate_spec。

用法：
  python3 analyze_java_deps.py <source_dir>
  python3 analyze_java_deps.py <source_dir> --check-rpm --container oe-build-env
  python3 analyze_java_deps.py <source_dir> --check-rpm --container oe-build-env -o result.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

from rpm_batch_lookup import BatchLookupError, fallback_results, provides_query, run_batch_lookup

try:
    import xml.etree.ElementTree as ET
except ImportError:
    ET = None  # type: ignore


# ── 1. 源码解析 ───────────────────────────────────────────────────────────────

def _strip_ns(tag: str) -> str:
    """去掉 XML 命名空间前缀，如 {http://...}dependency → dependency"""
    return re.sub(r"\{[^}]+\}", "", tag)


def collect_shaded_deps(source_dir: Path) -> Set[str]:
    """递归扫描所有子模块 pom，收集被 maven-shade-plugin shade 进 jar 的依赖坐标。

    shade plugin 的 <artifactSet><includes><include> 格式为 "groupId:artifactId"
    或 "groupId:*"（通配符）。返回精确坐标集合，通配符条目保留 "groupId:" 前缀形式
    供调用方做前缀匹配。
    """
    shaded: Set[str] = set()

    def _scan_pom(pom_path: Path) -> None:
        try:
            root = ET.parse(str(pom_path)).getroot()
        except Exception:
            return

        # 收集当前 pom 中 shade plugin 的 <include> 条目
        for plugin in root.iter():
            if _strip_ns(plugin.tag) != "plugin":
                continue
            children = {_strip_ns(c.tag): c for c in plugin}
            aid_node = children.get("artifactId")
            if aid_node is None or (aid_node.text or "").strip() != "maven-shade-plugin":
                continue
            for include in plugin.iter():
                if _strip_ns(include.tag) == "include" and include.text:
                    shaded.add(include.text.strip())

        # 递归子模块
        for mod in root.iter():
            if _strip_ns(mod.tag) != "module" or not mod.text:
                continue
            sub_pom = pom_path.parent / mod.text.strip() / "pom.xml"
            if sub_pom.exists():
                _scan_pom(sub_pom)

    root_pom = source_dir / "pom.xml"
    if root_pom.exists():
        _scan_pom(root_pom)
    return shaded


def _is_shaded(group: str, artifact: str, shaded: Set[str]) -> bool:
    """判断 group:artifact 是否被 shade，支持通配符 group:*"""
    return (f"{group}:{artifact}" in shaded) or (f"{group}:*" in shaded)


def parse_pom(source_dir: str) -> Dict:
    """解析 pom.xml，提取 groupId:artifactId:version 依赖列表"""
    src = Path(source_dir)
    pom = src / "pom.xml"
    if not pom.exists():
        return {"build_system": "maven", "deps": [], "java_version": "", "found": False}

    try:
        tree = ET.parse(str(pom))
        root = tree.getroot()
    except Exception as e:
        print(f"[WARN] 解析 pom.xml 失败: {e}", file=sys.stderr)
        return {"build_system": "maven", "deps": [], "java_version": "", "found": False}

    shaded = collect_shaded_deps(src)
    if shaded:
        print(f"[INFO] 检测到 shade 依赖，将从分析结果中排除: {sorted(shaded)}", file=sys.stderr)

    deps: List[Dict] = []
    seen: Set[str] = set()
    java_version = ""

    # 提取 Java 版本（properties 中的 maven.compiler.source）
    for props in root.iter():
        if _strip_ns(props.tag) == "properties":
            for child in props:
                tag = _strip_ns(child.tag)
                if tag in ("maven.compiler.source", "java.version") and child.text:
                    java_version = child.text.strip()
                    break

    # 提取 <dependencies> 下的 <dependency>
    for dep in root.iter():
        if _strip_ns(dep.tag) != "dependency":
            continue
        children = {_strip_ns(c.tag): (c.text or "").strip() for c in dep}
        group = children.get("groupId", "")
        artifact = children.get("artifactId", "")
        version = children.get("version", "")
        scope = children.get("scope", "compile")

        # 跳过 test/provided scope
        if scope in ("test", "provided", "system"):
            continue
        if not group or not artifact:
            continue
        # 跳过已被 shade 进 jar 的依赖——运行时不需要单独的 RPM
        if _is_shaded(group, artifact, shaded):
            continue

        key = f"{group}:{artifact}"
        if key not in seen:
            seen.add(key)
            deps.append({"group": group, "artifact": artifact,
                         "version": version, "scope": scope})

    return {"build_system": "maven", "deps": deps,
            "java_version": java_version, "found": True}


def parse_gradle(source_dir: str) -> Dict:
    """解析 build.gradle，提取 group:artifact:version 依赖"""
    src = Path(source_dir)
    deps: List[Dict] = []
    seen: Set[str] = set()
    java_version = ""

    for gradle_file in [src / "build.gradle", src / "build.gradle.kts"]:
        if not gradle_file.exists():
            continue
        content = gradle_file.read_text(errors="ignore")

        # sourceCompatibility = '11' 或 JavaVersion.VERSION_11
        m = re.search(r"sourceCompatibility\s*[=:]\s*['\"]?([\d.]+)", content)
        if m and not java_version:
            java_version = m.group(1)

        # implementation 'group:artifact:version' 或 "group:artifact:version"
        for m in re.finditer(
            r"""(?:implementation|compile|api|runtimeOnly)\s*['"]([\w.\-]+):([\w.\-]+):([^'"]+)['"]""",
            content
        ):
            group, artifact, version = m.group(1), m.group(2), m.group(3).strip()
            key = f"{group}:{artifact}"
            if key not in seen:
                seen.add(key)
                deps.append({"group": group, "artifact": artifact,
                             "version": version, "scope": "compile"})

    return {"build_system": "gradle", "deps": deps,
            "java_version": java_version, "found": len(deps) > 0}


def detect_build_system(source_dir: str) -> str:
    src = Path(source_dir)
    if (src / "pom.xml").exists():
        return "maven"
    if (src / "build.gradle").exists() or (src / "build.gradle.kts").exists():
        return "gradle"
    return "unknown"


# ── 2. RPM 查询（mvn() Provides）────────────────────────────────────────────

def build_lookup_tasks(deps: List[Dict]) -> List[Dict]:
    tasks: List[Dict] = []
    for dep in deps:
        coord = f"{dep['group']}:{dep['artifact']}"
        tasks.append({
            "dep": coord,
            **dep,
            "queries": [provides_query(f"mvn({coord})", "mvn()")],
        })
    return tasks


def check_rpm_availability(deps: List[Dict] = None) -> Dict:
    available, missing = [], []
    if deps is None:
        deps = []
    print(f"\n[INFO] 本地查询 mvn() RPM 可用性（单次批量查询）...")
    tasks = build_lookup_tasks(deps)

    try:
        results = run_batch_lookup(tasks, timeout=120)
    except (BatchLookupError, OSError, json.JSONDecodeError) as e:
        print(f"[WARN] 批量 mvn() 查询失败（{e}），跳过依赖检查")
        results = fallback_results(tasks)

    for item in results:
        coord = f"{item['group']}:{item['artifact']}"
        rpm = item.get("rpm")
        if rpm:
            print(f"  mvn({coord}) ... ✓ {rpm}")
            available.append({**{k: v for k, v in item.items() if k not in {'queries', 'level'}}, "rpm": rpm})
        else:
            print("  mvn({}) ... ✗ 未找到（需 bundle 或 generate_spec）".format(coord))
            missing.append({k: v for k, v in item.items() if k not in {"queries", "rpm", "level"}})
    return {"available": available, "missing": missing}


# ── 3. BuildRequires 生成 & 报告 ──────────────────────────────────────────────

# Map pom compiler source version → openEuler JDK package name.
# openEuler 25.09 does not ship java-1.7-openjdk-devel; 1.7 source compiles fine with 1.8.
_JDK_PKG_MAP: Dict[str, str] = {
    "1.7": "java-1.8.0-openjdk-devel",
    "7":   "java-1.8.0-openjdk-devel",
    "1.8": "java-1.8.0-openjdk-devel",
    "8":   "java-1.8.0-openjdk-devel",
    "11":  "java-11-openjdk-devel",
    "17":  "java-17-openjdk-devel",
    "21":  "java-21-openjdk-devel",
}
_DEFAULT_JDK_PKG = "java-1.8.0-openjdk-devel"


def resolve_jdk_pkg(java_version: str) -> str:
    """Map pom java_version to a JDK package name."""
    return _JDK_PKG_MAP.get(java_version, _DEFAULT_JDK_PKG)


def build_rpm_requires(build_system: str, java_version: str,
                       rpm_check: Optional[Dict]) -> List[str]:
    jdk_pkg = resolve_jdk_pkg(java_version) if java_version else _DEFAULT_JDK_PKG
    result = [jdk_pkg]
    if build_system == "maven":
        result.append("maven-local")
    elif build_system == "gradle":
        result.append("gradle-local")

    if rpm_check:
        seen = set(result)
        for item in rpm_check.get("available", []):
            rpm = item["rpm"]
            if rpm not in seen:
                result.append(rpm)
                seen.add(rpm)
    return result


def print_report(parsed: Dict, rpm_check: Optional[Dict]):
    sep = "=" * 60
    print(f"\n{sep}")
    print("Java 包 RPM 依赖分析报告")
    print(sep)
    print(f"  构建系统 : {parsed['build_system']}")
    if parsed.get("java_version"):
        print(f"  Java 版本 : {parsed['java_version']}")
    print(f"  依赖总数 : {len(parsed['deps'])} 个")

    if parsed["deps"]:
        print(f"\n[依赖列表]")
        for d in parsed["deps"]:
            ver = f"  ({d['version']})" if d["version"] else ""
            print(f"  {d['group']}:{d['artifact']}{ver}")

    if rpm_check:
        avail = rpm_check["available"]
        miss  = rpm_check["missing"]
        print(f"\n[RPM 可用性]  已有 {len(avail)} / 缺失 {len(miss)}")
        for item in avail:
            print(f"  ✓ {item['group']}:{item['artifact']:<30} → {item['rpm']}")
        if miss:
            print(f"\n  ✗ 缺失（建议 bundle 打包或通过 generate_spec 引入）:")
            for item in miss:
                print(f"    {item['group']}:{item['artifact']}")

    br = build_rpm_requires(parsed["build_system"], parsed.get("java_version", ""), rpm_check)
    print(f"\n[BuildRequires 建议]")
    for r in br:
        print(f"  BuildRequires: {r}")
    print(sep)


# ── 4. 主入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Java 包 RPM 依赖分析")
    parser.add_argument("source_dir", help="Java 项目源码目录")
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

    if bs == "maven":
        parsed = parse_pom(source_dir)
    elif bs == "gradle":
        parsed = parse_gradle(source_dir)
    else:
        print("[ERROR] 未找到 pom.xml 或 build.gradle", file=sys.stderr)
        sys.exit(1)

    rpm_check = None
    if args.check_rpm:
        if not parsed["deps"]:
            print("[INFO] 无依赖，跳过 RPM 查询")
        else:
            rpm_check = check_rpm_availability(deps=parsed["deps"])

    print_report(parsed, rpm_check)

    if args.output:
        result = {**parsed, "rpm_check": rpm_check,
                  "build_requires": build_rpm_requires(
                      parsed["build_system"], parsed.get("java_version", ""), rpm_check)}
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n[INFO] 结果已保存: {args.output}")

    if rpm_check and rpm_check["missing"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
