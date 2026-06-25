#!/usr/bin/env python3
"""
review_spec.py — RPM spec 静态审查脚本（Critic agent 的执行层）

用法：
  python3 review_spec.py <pkgname> <stage> --spec <spec_path>
                         [--rpmlint <rpmlint_output_file>]
                         [--dist-dir <dist_dir>]
                         [--round <N>]
                         [--prev-report <prev_report_path>]
                         -o <output_json>

stage: spec | lint | final
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path


# ── 规则检查函数 ──────────────────────────────────────────────────────────────

def check_spec(spec_text: str, pkgname: str) -> list[dict]:
    issues = []

    def add(rule_id, severity, location, message, suggestion=""):
        issues.append({
            "rule_id": rule_id,
            "severity": severity,
            "location": location,
            "message": message,
            "suggestion": suggestion,
        })

    lines = spec_text.splitlines()

    # § 1 基础字段
    name_match = re.search(r"^Name:\s*(\S+)", spec_text, re.MULTILINE)
    if name_match:
        if name_match.group(1) != pkgname:
            add("F-01", "E", "Name", f"Name 字段 '{name_match.group(1)}' 与包名 '{pkgname}' 不一致", "修改 Name 字段与包目录名一致")
    else:
        add("F-01", "E", "Name", "缺少 Name 字段", "添加 Name 字段")

    if not re.search(r"^Version:\s*\S+", spec_text, re.MULTILINE):
        add("F-02", "E", "Version", "缺少 Version 字段", "添加 Version 字段")

    release_match = re.search(r"^Release:\s*(.+)", spec_text, re.MULTILINE)
    if release_match:
        if "1%{?dist}" not in release_match.group(1):
            add("F-03", "E", "Release", f"Release 字段应为 '1%{{?dist}}'，当前为 '{release_match.group(1).strip()}'", "改为 1%{?dist}")
    else:
        add("F-03", "E", "Release", "缺少 Release 字段", "添加 Release: 1%{?dist}")

    if not re.search(r"^License:\s*\S+", spec_text, re.MULTILINE):
        add("F-04", "E", "License", "缺少 License 字段", "添加 SPDX 标识符")

    source0_match = re.search(r"^Source0:\s*(.+)", spec_text, re.MULTILINE)
    if source0_match:
        s0 = source0_match.group(1).strip()
        if not re.search(r"%\{name\}-%\{version\}\.tar\.gz", s0):
            add("F-06", "E", "Source0", f"Source0 应为 '%{{name}}-%{{version}}.tar.gz'，当前为 '{s0}'", "改为 %{name}-%{version}.tar.gz")

    # § 2 BuildRequires
    is_cmake = "%cmake" in spec_text
    is_meson = "%meson" in spec_text
    is_autotools = "%configure" in spec_text or "%make_build" in spec_text

    if is_cmake:
        if "BuildRequires:  cmake" not in spec_text and "BuildRequires: cmake" not in spec_text:
            add("B-01", "E", "BuildRequires", "CMake 项目缺少 BuildRequires: cmake", "添加 BuildRequires: cmake")
        if "BuildRequires:  gcc-c++" not in spec_text and "BuildRequires: gcc-c++" not in spec_text:
            add("B-01", "E", "BuildRequires", "CMake 项目缺少 BuildRequires: gcc-c++", "添加 BuildRequires: gcc-c++")

    if is_meson:
        for req in ["meson", "gcc-c++", "ninja-build"]:
            if f"BuildRequires: {req}" not in spec_text and f"BuildRequires:  {req}" not in spec_text:
                add("B-02", "E", "BuildRequires", f"Meson 项目缺少 BuildRequires: {req}", f"添加 BuildRequires: {req}")

    # § 3 分包规则
    has_main_files = bool(re.search(r"^%files\s*$", spec_text, re.MULTILINE))
    has_so_versioned = bool(re.search(r"\.so\.\d", spec_text))
    has_shared_lib = has_so_versioned or (
        re.search(r"^%files\s*$", spec_text, re.MULTILINE) and
        re.search(r"_libdir.*\.so", spec_text)
    )
    # header-only: debug_package nil 且没有带版本号的 .so（有 .so.* 说明是有共享库的普通库）
    is_header_only = "%global debug_package %{nil}" in spec_text and not has_so_versioned

    if is_header_only:
        # § 3.2 Header-only 规则
        if has_main_files:
            # 检查主包 %files 是否为空（只有 %files 行，下一行就是 %files devel 或 %changelog）
            main_files_match = re.search(r"^%files\s*$(.*?)(?=^%files\s|\Z)", spec_text, re.MULTILINE | re.DOTALL)
            if main_files_match:
                main_content = main_files_match.group(1).strip()
                if not main_content or all(l.startswith('#') or not l.strip() for l in main_content.splitlines()):
                    add("H-02", "E", "%files", "header-only 库存在空主包，会触发 no-binary E 错误", "删除空的 %files 主包段落")

        # 检查 -devel 是否错误声明了 Requires: %{name}
        devel_section = re.search(r"%package devel.*?(?=%package|\Z)", spec_text, re.DOTALL)
        if devel_section:
            devel_text = devel_section.group(0)
            if re.search(r"Requires:\s*%\{name\}", devel_text):
                add("H-03", "E", "%package devel", "header-only 库的 -devel 包不应声明 Requires: %{name}（没有主包）", "删除该 Requires 行")
    else:
        # § 3.1 有共享库的规则
        if has_so_versioned:
            if not re.search(r"%post\s+-p\s+/sbin/ldconfig", spec_text):
                add("P-06", "E", "%post", "共享库缺少 %post -p /sbin/ldconfig", "添加 %post -p /sbin/ldconfig")
            if not re.search(r"%postun\s+-p\s+/sbin/ldconfig", spec_text):
                add("P-06", "E", "%postun", "共享库缺少 %postun -p /sbin/ldconfig", "添加 %postun -p /sbin/ldconfig")

        # 检查 -devel 是否有 Requires: %{name}
        devel_section = re.search(r"%package devel.*?(?=%package|\Z)", spec_text, re.DOTALL)
        if devel_section and has_so_versioned:
            devel_text = devel_section.group(0)
            if not re.search(r"Requires:\s*%\{name\}", devel_text):
                add("P-05", "E", "%package devel", "-devel 包缺少 Requires: %{name}（或 %{name}%{?_isa}）= %{version}-%{release}", "添加 Requires: %{name}%{?_isa} = %{version}-%{release}")

    # § 3.3 noarch vs arch
    is_noarch = "BuildArch:      noarch" in spec_text or "BuildArch: noarch" in spec_text
    has_libdir_cmake = bool(re.search(r"%\{_libdir\}/cmake", spec_text))
    has_libdir_pkgconfig = bool(re.search(r"%\{_libdir\}/pkgconfig", spec_text))
    has_datadir_cmake = bool(re.search(r"%\{_datadir\}/cmake", spec_text))

    if is_noarch and has_libdir_cmake:
        add("A-01", "E", "BuildArch / %files", "cmake 文件在 %{_libdir}/cmake/ 但声明了 BuildArch: noarch，会触发 noarch-with-lib64", "去掉 BuildArch: noarch")
    if is_noarch and has_libdir_pkgconfig:
        add("A-03", "E", "BuildArch / %files", "pkgconfig 文件在 %{_libdir}/pkgconfig/ 但声明了 BuildArch: noarch，会触发 noarch-with-lib64", "去掉 BuildArch: noarch")
    if not is_noarch and has_datadir_cmake and not has_libdir_cmake and not has_so_versioned and is_header_only:
        add("A-02", "W", "BuildArch", "cmake 文件在 %{_datadir}/cmake/，header-only 库建议声明 BuildArch: noarch", "添加 BuildArch: noarch")

    # § 4 %build 规则
    if is_cmake:
        if "cmake .." in spec_text or re.search(r"cmake\s+\.\.", spec_text):
            add("BD-01", "E", "%build", "不得手写 cmake ..，应使用 %cmake 宏", "改用 %cmake 宏")
    if is_meson:
        if "meson setup" in spec_text or re.search(r"meson\s+\.\.", spec_text):
            add("BD-02", "E", "%build", "不得手写 meson setup，应使用 %meson 宏", "改用 %meson 宏")

    # § 5 %install 规则
    if is_cmake and "make install" in spec_text and "%cmake_install" not in spec_text:
        add("I-01", "E", "%install", "CMake 项目不得手写 make install，应使用 %cmake_install", "改用 %cmake_install")

    # § 7 %changelog 规则
    if "%changelog" not in spec_text:
        add("C-01", "E", "%changelog", "缺少 %changelog 段落", "添加 %changelog 段落")
    else:
        changelog_match = re.search(r"%changelog\s*\n(\*.*)", spec_text)
        if not changelog_match:
            add("C-01", "E", "%changelog", "%changelog 段落为空，至少需要一条条目", "添加初始 changelog 条目")
        else:
            entry = changelog_match.group(1)
            if not re.search(r"\* \w{3} \w{3} \d{2} \d{4}", entry):
                add("C-02", "E", "%changelog", f"changelog 日期格式不正确：'{entry[:50]}'", "格式应为 '* Www Mon DD YYYY'")

    return issues


def check_rpmlint(rpmlint_output: str) -> list[dict]:
    """解析 rpmlint 输出，过滤已知误报，返回需要处理的问题。"""
    issues = []

    # 已知环境误报，降级为 I
    known_false_positives = {
        "no-signature": "I",
        "invalid-license": "I",
        "missing-hash-section": "I",
        "no-library-dependency-for": "I",
    }

    for line in rpmlint_output.splitlines():
        line = line.strip()
        if not line:
            continue

        # 匹配 rpmlint 输出格式：pkgname: E: error-name ...
        m = re.match(r"^(.+?):\s+([EW]):\s+(\S+)(.*)", line)
        if not m:
            continue

        pkg_loc, orig_level, error_name, detail = m.groups()
        detail = detail.strip()

        # 检查是否为已知误报
        for fp_key, fp_level in known_false_positives.items():
            if fp_key in error_name:
                orig_level = fp_level
                break

        # 查找对应规则
        rule_map = {
            "noarch-with-lib64": ("A-01", "去掉 BuildArch: noarch"),
            "no-binary": ("H-02", "加 BuildArch: noarch 或删除空主包"),
            "devel-file-in-non-devel-package": ("P-03", "将头文件/pkgconfig 移到 -devel 包"),
            "non-versioned-file-in-library-package": ("P-07", "将 doc/license 移出主包"),
            "library-without-ldconfig": ("P-06", "添加 %post/%postun -p /sbin/ldconfig"),
            "static-library-without-debuginfo": ("BD-04", "禁用静态库或加 %global debug_package %{nil}"),
            "spelling-error": ("F-08", "修改描述中的拼写"),
            "non-standard-dir-in-usr": ("AR-05", "上游安装行为，记录但不阻断"),
        }

        rule_id = "LINT"
        suggestion = ""
        for key, (rid, sug) in rule_map.items():
            if key in error_name:
                rule_id = rid
                suggestion = sug
                break

        issues.append({
            "rule_id": rule_id,
            "severity": orig_level,
            "location": pkg_loc,
            "message": f"{error_name} {detail}".strip(),
            "suggestion": suggestion,
        })

    return issues


def check_final(pkgname: str, dist_dir: str, spec_path: str) -> list[dict]:
    """归档完整性检查。"""
    issues = []

    def add(rule_id, severity, location, message, suggestion=""):
        issues.append({
            "rule_id": rule_id,
            "severity": severity,
            "location": location,
            "message": message,
            "suggestion": suggestion,
        })

    dist = Path(dist_dir)

    # AR-01: binary RPM
    rpms = list(dist.glob(f"{pkgname}*.rpm"))
    rpms = [r for r in rpms if ".src.rpm" not in r.name]
    if not rpms:
        add("AR-01", "E", f"dist/{pkgname}*.rpm", f"dist/ 目录中找不到 {pkgname} 的 binary RPM", "确认 rpmbuild 成功并已复制 RPM 到 dist/")

    # AR-02: source RPM
    srpms = list(dist.glob(f"{pkgname}*.src.rpm"))
    if not srpms:
        add("AR-02", "E", f"dist/{pkgname}*.src.rpm", f"dist/ 目录中找不到 {pkgname} 的 source RPM", "复制 SRPM 到 dist/")

    # AR-03: spec 文件
    if spec_path and not Path(spec_path).exists():
        add("AR-03", "E", spec_path, f"spec 文件不存在：{spec_path}", "确认 spec 已写入包目录")

    # AR-05: repodata
    repodata = dist / "repodata"
    if not repodata.exists():
        add("AR-05", "W", "dist/repodata/", "repodata/ 目录不存在，可能未运行 createrepo", "运行 createrepo dist/")

    # AR-04: 版本一致性
    if rpms and spec_path and Path(spec_path).exists():
        spec_text = Path(spec_path).read_text()
        version_match = re.search(r"^Version:\s*(\S+)", spec_text, re.MULTILINE)
        if version_match:
            spec_version = version_match.group(1)
            for rpm in rpms:
                if spec_version not in rpm.name:
                    add("AR-04", "W", rpm.name, f"RPM 文件名中的版本与 spec Version ({spec_version}) 不一致", "检查版本号是否正确")

    return issues


# ── 报告生成 ──────────────────────────────────────────────────────────────────

def determine_verdict(issues: list[dict]) -> str:
    severities = {i["severity"] for i in issues}
    if "E" in severities:
        return "BLOCK"
    if "W" in severities:
        return "WARN"
    return "PASS"


def generate_report(pkgname: str, version: str, stage: str, round_n: int,
                    input_file: str, issues: list[dict],
                    prev_issues: list[dict] | None = None) -> str:
    verdict = determine_verdict(issues)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    e_issues = [i for i in issues if i["severity"] == "E"]
    w_issues = [i for i in issues if i["severity"] == "W"]
    i_issues = [i for i in issues if i["severity"] == "I"]

    def fmt_table(rows, has_suggestion=True):
        if not rows:
            return "_无_\n"
        if has_suggestion:
            lines = ["| # | 位置 | 问题描述 | 修复建议 |",
                     "|---|------|----------|----------|"]
            for idx, r in enumerate(rows, 1):
                lines.append(f"| {idx} | `{r['location']}` | {r['message']} | {r['suggestion']} |")
        else:
            lines = ["| # | 位置 | 说明 |",
                     "|---|------|------|"]
            for idx, r in enumerate(rows, 1):
                lines.append(f"| {idx} | `{r['location']}` | {r['message']} |")
        return "\n".join(lines) + "\n"

    # 与上轮对比
    comparison = ""
    if prev_issues and round_n > 1:
        prev_msgs = {i["message"] for i in prev_issues}
        curr_msgs = {i["message"] for i in issues}
        fixed = prev_msgs - curr_msgs
        new_issues = curr_msgs - prev_msgs
        still_present = prev_msgs & curr_msgs

        rows = []
        for msg in fixed:
            rows.append(f"| {msg[:60]} | 已修复 | ✓ 已修复 |")
        for msg in still_present:
            sev = next((i["severity"] for i in issues if i["message"] == msg), "?")
            rows.append(f"| {msg[:60]} | 存在 | {sev}（仍存在） |")
        for msg in new_issues:
            sev = next((i["severity"] for i in issues if i["message"] == msg), "?")
            rows.append(f"| {msg[:60]} | 不存在 | {sev}（新增） |")

        if rows:
            comparison = "\n## 与上轮对比\n\n| 问题 | 上轮状态 | 本轮状态 |\n|------|----------|----------|\n"
            comparison += "\n".join(rows) + "\n"

    # 裁决依据
    rule_ids = sorted({i["rule_id"] for i in issues if i["severity"] in ("E", "W") and i["rule_id"] != "LINT"})
    if rule_ids:
        basis_lines = [f"- 依据 `spec-review-rules.md § {rid}`" for rid in rule_ids]
        basis = "\n".join(basis_lines)
    else:
        basis = "- 无规则违反"

    verdict_emoji = {"PASS": "✅", "WARN": "⚠️", "BLOCK": "🚫"}[verdict]

    report = f"""# RPM 审查报告 — {pkgname} @ {stage}

## 基本信息

| 字段 | 值 |
|------|----|
| 包名 | {pkgname} |
| 版本 | {version} |
| 审查阶段 | {stage} |
| 审查时间 | {now} |
| 审查轮次 | {round_n} |
| 输入文件 | `{input_file}` |

---

## 审查结论

**裁决：{verdict_emoji} `{verdict}`**

> {"无 E 级问题，可继续。" if verdict == "PASS" else f"发现 {len(e_issues)} 个 E 级问题，必须修复后重新审查。" if verdict == "BLOCK" else f"发现 {len(w_issues)} 个 W 级问题，建议修复，不阻断流程。"}

---

## 问题清单

### E（必须修复，否则阻断）

{fmt_table(e_issues)}
### W（建议修复，不阻断）

{fmt_table(w_issues)}
### I（信息，无需处理）

{fmt_table(i_issues, has_suggestion=False)}
---

## 裁决依据

{basis}
{comparison}
---

_由 review-rpm skill 自动生成_
"""
    return report


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RPM spec 静态审查")
    parser.add_argument("pkgname", help="包名")
    parser.add_argument("stage", choices=["spec", "lint", "final"], help="审查阶段")
    parser.add_argument("--spec", help="spec 文件路径")
    parser.add_argument("--rpmlint", help="rpmlint 输出文件路径")
    parser.add_argument("--dist-dir", default="./dist", help="dist 目录路径")
    parser.add_argument("--round", type=int, default=1, help="审查轮次")
    parser.add_argument("--prev-report", help="上一轮审查 JSON 路径（用于对比）")
    parser.add_argument("-o", "--output", required=True, help="输出 JSON 路径")
    parser.add_argument("--report-dir", default="./reports", help="报告 Markdown 输出目录")
    args = parser.parse_args()

    issues = []
    version = "unknown"
    input_file = args.spec or args.rpmlint or args.dist_dir

    # 读取 spec
    spec_text = ""
    if args.spec and Path(args.spec).exists():
        spec_text = Path(args.spec).read_text()
        version_match = re.search(r"^Version:\s*(\S+)", spec_text, re.MULTILINE)
        if version_match:
            version = version_match.group(1)

    # 读取上一轮报告
    prev_issues = None
    if args.prev_report and Path(args.prev_report).exists():
        try:
            prev_data = json.loads(Path(args.prev_report).read_text())
            prev_issues = prev_data.get("issues", [])
        except Exception:
            pass

    # 执行对应阶段的检查
    if args.stage == "spec":
        if not spec_text:
            print(f"[ERROR] --spec 文件不存在或为空：{args.spec}", file=sys.stderr)
            sys.exit(1)
        issues = check_spec(spec_text, args.pkgname)

    elif args.stage == "lint":
        if spec_text:
            issues.extend(check_spec(spec_text, args.pkgname))
        if args.rpmlint and Path(args.rpmlint).exists():
            rpmlint_text = Path(args.rpmlint).read_text()
            issues.extend(check_rpmlint(rpmlint_text))
        elif args.rpmlint:
            print(f"[WARN] rpmlint 输出文件不存在：{args.rpmlint}", file=sys.stderr)

    elif args.stage == "final":
        if spec_text:
            issues.extend(check_spec(spec_text, args.pkgname))
        spec_pkg_path = f"./{args.pkgname}/{args.pkgname}.spec"
        issues.extend(check_final(args.pkgname, args.dist_dir, spec_pkg_path))

    verdict = determine_verdict(issues)

    # 生成 Markdown 报告
    os.makedirs(args.report_dir, exist_ok=True)
    report_md = generate_report(
        pkgname=args.pkgname,
        version=version,
        stage=args.stage,
        round_n=args.round,
        input_file=str(input_file),
        issues=issues,
        prev_issues=prev_issues,
    )
    report_path = Path(args.report_dir) / f"review_{args.pkgname}_{args.stage}.md"
    report_path.write_text(report_md)

    # 输出 JSON
    result = {
        "pkgname": args.pkgname,
        "version": version,
        "stage": args.stage,
        "round": args.round,
        "verdict": verdict,
        "issues": issues,
        "report_path": str(report_path),
        "e_count": sum(1 for i in issues if i["severity"] == "E"),
        "w_count": sum(1 for i in issues if i["severity"] == "W"),
        "i_count": sum(1 for i in issues if i["severity"] == "I"),
    }

    os.makedirs(Path(args.output).parent, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))

    print(f"[review-rpm] {args.pkgname} @ {args.stage} 轮次{args.round}: {verdict} "
          f"(E={result['e_count']}, W={result['w_count']}, I={result['i_count']})")
    print(f"[review-rpm] 报告: {report_path}")

    # 返回码：0=PASS/WARN，1=BLOCK
    sys.exit(0 if verdict in ("PASS", "WARN") else 1)


if __name__ == "__main__":
    main()
