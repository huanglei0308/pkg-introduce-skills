#!/usr/bin/env python3
"""从 gate_result_<pkgname>.json 读取 lang/version/decision。

用法：
  eval "$(python3 read-gate-fields.py --session-dir . --pkg setuptools)"
  # 产出：LANG=python  VERSION=82.0.1  GATE_DECISION=introduce_new
"""
import argparse
import json
import shlex
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--field", default="", help="只输出指定字段的值")
    args = parser.parse_args()

    gate_file = Path(args.session_dir) / "pkgs" / args.pkg / f"gate_result_{args.pkg}.json"
    if not gate_file.exists():
        print(f"ERROR: gate_result not found: {gate_file}", file=sys.stderr)
        sys.exit(1)

    d = json.loads(gate_file.read_text(encoding="utf-8"))
    result = d.get("result", {})

    if args.field:
        print(result.get(args.field, ""))
        return

    mapping = [
        ("LANG",          result.get("lang", "")),
        ("VERSION",       result.get("version", "")),
        ("GATE_DECISION", result.get("decision", "")),
    ]
    for k, v in mapping:
        print(f"{k}={shlex.quote(str(v))}")


if __name__ == "__main__":
    main()
