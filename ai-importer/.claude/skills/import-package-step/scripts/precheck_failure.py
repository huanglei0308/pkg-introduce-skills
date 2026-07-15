#!/usr/bin/env python3
"""Build failure pre-check: scan build log for high-confidence fixable patterns.

If a pattern matches, writes failure_analysis_*.json with verdict=rebuild and
populated spec_patch for AI to apply in rebuild mode. Does NOT modify the spec
itself — the AI reads the analysis and learns to apply the fix, so it can handle
similar issues in the future without new precheck rules.

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


# ── Macro → explicit-command mapping ────────────────────────────────────────

_MACRO_REPLACEMENTS = {
    "%cmake_build": "cmake --build . -j$(nproc)",
    "%make_build": "make -j$(nproc)",
}


def _detect_broken_macro(spec_lines: list[str]) -> str | None:
    """Return the first macro in *spec_lines* that needs replacing, or None."""
    for line in spec_lines:
        stripped = line.strip()
        if stripped in _MACRO_REPLACEMENTS:
            return stripped
    return None


def _resolve_macro_fix(spec_lines: list[str]) -> tuple[list[str], list[dict]] | None:
    """Check which macro needs replacement.

    Returns (fixed_lines, spec_patch) if a macro was found and can be fixed,
    or None if no known macro is present in the spec.
    """
    macro = _detect_broken_macro(spec_lines)
    if not macro:
        return None
    replacement = _MACRO_REPLACEMENTS[macro]
    fixed = [replacement + "\n" if line.strip() == macro else line
             for line in spec_lines]
    spec_patch = [{
        "description": f"将 {macro} 替换为 {replacement}，避免非交互 shell 中的 job control 错误（fg/bg）",
        "before": macro,
        "after": replacement,
    }]
    return fixed, spec_patch


# ── Pattern definitions ─────────────────────────────────────────────────────

# Each pattern dict:
#   name:      unique identifier (for logging)
#   regex:     compiled regex to match against build log
#   verdict:   always "rebuild"
#   reason:    human-readable reason (static)
#   fix_instructions: human-readable fix description
#   resolve:   function(spec_lines) -> (fixed_lines, spec_patch) or None
#              None means "AI must handle this in rebuild mode"

PATTERNS = [
    {
        "name": "fg_no_job_control",
        "regex": re.compile(r"fg: no job control", re.MULTILINE),
        "verdict": "rebuild",
        "reason": "%build 宏在非交互 shell 中调用 fg 失败",
        "fix_instructions": (
            "将 %cmake_build 替换为 cmake --build . -j$(nproc)，"
            "或将 %make_build 替换为 make -j$(nproc)。"
            "cmake configure 阶段（%cmake 或 %configure）保持不变，只替换 build 步骤。"
        ),
        "resolve": _resolve_macro_fix,
    },
    {
        "name": "bg_no_job_control",
        "regex": re.compile(r"bg: no job control", re.MULTILINE),
        "verdict": "rebuild",
        "reason": "shell job control 错误（bg），%build 宏在非交互 shell 中不兼容",
        "fix_instructions": (
            "将构建宏替换为显式命令。"
            "若使用 %cmake_build → cmake --build . -j$(nproc)，"
            "若使用 %make_build → make -j$(nproc)。"
        ),
        "resolve": _resolve_macro_fix,
    },
    {
        "name": "cd_no_such_file_prep",
        "regex": re.compile(r"cd: (.+?): No such file or directory", re.MULTILINE),
        "verdict": "rebuild",
        "reason": "",  # filled dynamically from match group
        "fix_instructions": (
            "将 %autosetup -n 参数改为 %{name}-%{version}"
            "（build-rpm 的 --transform 已统一目录名）。"
        ),
        "resolve": None,  # pkg-builder rebuild 模式会根据 fix_instructions 处理
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
                   pattern: dict) -> None:
    """Write failure_analysis_*.json and fix_instructions.md.

    Does NOT modify the spec — only diagnoses and generates structured fix
    instructions (spec_patch). The AI in rebuild mode reads the analysis and
    applies the fix, so it learns the pattern for future similar issues.
    """
    pkg_dir = session_dir / "pkgs" / pkgname
    pkg_dir.mkdir(parents=True, exist_ok=True)

    # Build reason (may use match groups for patterns like cd_no_such_file)
    reason = pattern["reason"]
    m = pattern.get("_match")
    if m and len(m.groups()) > 0:
        if "%s" in reason:
            reason = reason % m.groups()
        else:
            reason = f"{reason}：{m.group(1)}"

    # Generate spec_patch: try resolve() to detect the exact fix needed
    spec_patch: list[dict] = []
    spec_path = pkg_dir / f"{pkgname}.spec"

    resolver = pattern.get("resolve")
    if resolver and spec_path.exists():
        original = spec_path.read_text(encoding="utf-8").splitlines(keepends=True)
        resolved = resolver(original)
        if resolved is not None:
            _fixed_lines, spec_patch = resolved
            print(f"[precheck] diagnosed fix for pattern: {pattern['name']}, "
                  f"spec_patch={len(spec_patch)} entries", file=sys.stderr)
        else:
            print(f"[precheck] pattern {pattern['name']} matched but macro not found in spec, "
                  f"AI will diagnose from fix_instructions", file=sys.stderr)
    else:
        # No resolver — AI will figure out the fix from fix_instructions
        print(f"[precheck] pattern {pattern['name']} requires AI-driven spec fix", file=sys.stderr)

    # Write failure_analysis JSON — AI reads this in rebuild mode
    if copr_build_id:
        analysis_path = pkg_dir / f"failure_analysis_{pkgname}_{copr_build_id}.json"
    else:
        analysis_path = pkg_dir / f"failure_analysis_{pkgname}.json"

    analysis = {
        "verdict": pattern["verdict"],
        "reason": reason,
        "fix_instructions": pattern["fix_instructions"],
        "missing_deps": [],
        "spec_patch": spec_patch,
    }
    analysis_path.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Append to fix_instructions history
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
    write_analysis(session_dir, pkgname, copr_build_id, pattern)
    print("auto_fixed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
