#!/usr/bin/env python3
"""从 COPR 构建日志中提取缺失 RPM 包，注册到 dep_registry.json。

用法：
  python3 register-missing-deps.py --session-dir . --pkg setuptools
"""
import argparse
import json
import re
from pathlib import Path


def _extract_constraint(log_text: str, rpm_pkg: str) -> str:
    """从 log 里提取包名对应的版本约束，如 '>= 1.4.0'。"""
    # 匹配 "No matching package to install: 'xxx >= y.z'"
    m = re.search(
        r"No matching package to install: ['\"]" + re.escape(rpm_pkg) + r"\s*([><=!][^'\"]+)['\"]",
        log_text,
    )
    if m:
        return m.group(1).strip()
    # 匹配 "nothing provides xxx >= y.z needed by"
    m = re.search(
        r"nothing provides " + re.escape(rpm_pkg) + r"\s*([><=!][^\s]+(?:\s*[0-9][^\s]*)?)",
        log_text,
    )
    if m:
        return m.group(1).strip()
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True)
    args = parser.parse_args()

    sd = Path(args.session_dir)
    build_result_path = sd / "pkgs" / args.pkg / "build_rpm_result.json"
    build_log_path    = sd / "pkgs" / args.pkg / "build.log"

    log_text = ""
    if build_result_path.exists():
        d = json.loads(build_result_path.read_text(encoding="utf-8"))
        log_text = d.get("build_log", "") or d.get("build_log_tail", "")
    if not log_text and build_log_path.exists():
        log_text = build_log_path.read_text(encoding="utf-8", errors="replace")

    missing = re.findall(r"No matching package to install: '([^']+)'", log_text)
    missing += re.findall(r"nothing provides ([^\s]+) needed by", log_text)

    if not missing:
        print("[register-missing-deps] no missing packages found")
        return

    reg_path = sd / "dep_registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {}

    added = []
    for rpm_pkg in missing:
        # 去掉 python3- 前缀还原 pypi/pkg 名
        pkg_name = rpm_pkg.removeprefix("python3-")
        constraint = _extract_constraint(log_text, rpm_pkg)
        if pkg_name not in reg:
            reg[pkg_name] = {
                "url": "",
                "constraint": constraint,
                "status": "pending_evaluate",
                "required_by": args.pkg,
            }
            added.append(pkg_name)
        elif constraint and not reg[pkg_name].get("constraint"):
            # 补充已有条目缺失的 constraint
            reg[pkg_name]["constraint"] = constraint

    reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[register-missing-deps] added: {added}")


if __name__ == "__main__":
    main()
