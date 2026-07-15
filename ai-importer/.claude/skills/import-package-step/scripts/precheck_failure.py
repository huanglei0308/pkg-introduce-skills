#!/usr/bin/env python3
"""Build failure pre-check: scan build log for high-confidence fixable patterns.

If a pattern matches, directly write failure_analysis_*.json with verdict=rebuild
and apply the spec fix. This bypasses the AI agent for known-deterministic cases.

Usage:
  python3 precheck_failure.py --session-dir <dir> --pkgname <pkg>
  # stdout: auto_fixed | needs_ai
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path


# ── Pattern definitions ─────────────────────────────────────────────────────

def _fix_cmake_build(spec_lines: list[str], macro: str) -> list[str]:
    """Replace %cmake_build with cmake --build . -j$(nproc)."""
    new_lines = []
    for line in spec_lines:
        if line.strip() == f"%{macro}":
            new_lines.append("cmake --build . -j$(nproc)\n")
        else:
            new_lines.append(line)
    return new_lines


def _fix_make_build(spec_lines: list[str], macro: str) -> list[str]:
    """Replace %make_build with make -j$(nproc)."""
    new_lines = []
    for line in spec_lines:
        if line.strip() == f"%{macro}":
            new_lines.append("make -j$(nproc)\n")
        else:
            new_lines.append(line)
    return new_lines


# Each pattern is a dict:
#   regex: compiled regex to match against build log
#   verdict: always "rebuild"
#   reason_template: format string for reason field
#   fix_instructions: human-readable fix description
#   spec_fixer: function(spec_lines, captured_groups) -> new_spec_lines or None
PATTERNS = [
    {
        "name": "fg_no_job_control_cmake",
        "regex": re.compile(r"fg: no job control", re.MULTILINE),
        "verdict": "rebuild",
        "reason_template": "%%cmake_build 宏在非交互 shell 中调用 fg 失败，configure 已完成",
        "fix_instructions": (
            "将 %%cmake_build 替换为 cmake --build . -j$(nproc)。"
            "cmake configure 阶段（%%cmake ...）保持不变，只替换 build 步骤。"
        ),
        "spec_fixer": lambda lines: _fix_cmake_build(lines, "cmake_build"),
    },
    {
        "name": "fg_no_job_control_make",
        "regex": re.compile(r"fg: no job control", re.MULTILINE),
        "verdict": "rebuild",
        "reason_template": "%%make_build 宏在非交互 shell 中调用 fg 失败，configure 已完成",
        "fix_instructions": (
            "将 %%make_build 替换为 make -j$(nproc)。"
        ),
        "spec_fixer": lambda lines: _fix_make_build(lines, "make_build"),
    },
    {
        "name": "bg_no_job_control",
        "regex": re.compile(r"bg: no job control", re.MULTILINE),
        "verdict": "rebuild",
        "reason_template": "shell job control 错误（bg），%%build 宏在非交互 shell 中不兼容",
        "fix_instructions": (
            "将构建宏替换为显式命令：%%cmake_build → cmake --build . -j$(nproc)，"
            "%%make_build → make -j$(nproc)。"
        ),
        "spec_fixer": lambda lines: (_fix_cmake_build(lines, "cmake_build")
                                      if any(l.strip() == "%cmake_build" for l in lines)
                                      else _fix_make_build(lines, "make_build")),
    },
    {
        "name": "cd_no_such_file_prep",
        "regex": re.compile(r"cd: (.+?): No such file or directory", re.MULTILINE),
        "verdict": "rebuild",
        "reason_template": "%%prep 阶段 cd 失败：目录 %s 不存在",
        "fix_instructions": (
            "将 %%autosetup -n 参数改为 %%{name}-%%{version}（build-rpm 的 --transform 已统一目录名）。"
        ),
        "spec_fixer": None,  # pkg-builder rebuild 模式会处理
    },
]


# ── Main logic ───────────────────────────────────────────────────────────────

def find_pattern(log_text: str) -> dict | None:
    """Return the first matching pattern dict, or None."""
    for pat in PATTERNS:
        m = pat["regex"].search(log_text)
        if m:
            pat["_match"] = m
            return pat
    return None


def write_analysis(session_dir: Path, pkgname: str, copr_build_id: str,
                   pattern: dict, log_text: str) -> None:
    """Write failure_analysis_*.json with the matched verdict."""
    pkg_dir = session_dir / "pkgs" / pkgname
    pkg_dir.mkdir(parents=True, exist_ok=True)

    reason = pattern["reason_template"]
    m = pattern.get("_match")
    if m and len(m.groups()) > 0:
        reason = reason % m.groups()

    if copr_build_id:
        analysis_path = pkg_dir / f"failure_analysis_{pkgname}_{copr_build_id}.json"
    else:
        analysis_path = pkg_dir / f"failure_analysis_{pkgname}.json"

    analysis = {
        "verdict": pattern["verdict"],
        "reason": reason,
        "fix_instructions": pattern["fix_instructions"],
        "missing_deps": [],
        "spec_patch": [],
    }
    analysis_path.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Also append to fix_instructions history
    fix_path = pkg_dir / "fix_instructions.md"
    today = date.today().isoformat()
    fix_entry = (
        f"## build_id={copr_build_id} {today}\n"
        f"verdict: {pattern['verdict']}\n"
        f"reason: {reason}\n"
        f"fix: {pattern['fix_instructions']}\n"
    )
    with open(fix_path, "a", encoding="utf-8") as f:
        f.write(fix_entry)

    # Apply spec fix if provided
    fixer = pattern.get("spec_fixer")
    if fixer:
        spec_path = pkg_dir / f"{pkgname}.spec"
        if spec_path.exists():
            original = spec_path.read_text(encoding="utf-8").splitlines(keepends=True)
            fixed = fixer(original)
            spec_path.write_text("".join(fixed), encoding="utf-8")
            print(f"[precheck] applied spec fix for pattern: {pattern['name']}", file=sys.stderr)


def get_build_log(session_dir: Path, pkgname: str) -> tuple[str, str]:
    """Read build log from build_rpm_result.json. Returns (log_text, copr_build_id)."""
    result_path = session_dir / "pkgs" / pkgname / "build_rpm_result.json"
    if not result_path.exists():
        return "", ""

    data = json.loads(result_path.read_text(encoding="utf-8"))
    log_text = data.get("build_log_tail", "") or data.get("build_log", "")
    copr_build_id = str(data.get("copr_build_id", "") or "")
    return log_text, copr_build_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-check build failure for known fixable patterns"
    )
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkgname", required=True)
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    pkgname = args.pkgname

    log_text, copr_build_id = get_build_log(session_dir, pkgname)
    if not log_text:
        print("[precheck] no build log found, falling back to AI", file=sys.stderr)
        print("needs_ai")
        return 0

    pattern = find_pattern(log_text)
    if not pattern:
        print("[precheck] no known pattern matched, falling back to AI", file=sys.stderr)
        print("needs_ai")
        return 0

    print(f"[precheck] matched pattern: {pattern['name']}", file=sys.stderr)
    write_analysis(session_dir, pkgname, copr_build_id, pattern, log_text)
    print("auto_fixed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
