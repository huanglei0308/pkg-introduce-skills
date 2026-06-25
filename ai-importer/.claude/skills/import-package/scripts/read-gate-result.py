#!/usr/bin/env python3
"""读取 gate_result_<pkgname>.json，输出 shell eval 格式。

用法：
  eval "$(python3 read-gate-result.py --session-dir <dir> --pkgname <pkg>)"
  # → GATE_STATUS=done
  #   GATE_DECISION=introduce_new
  #   GATE_LANG=python
  #   GATE_VERSION=0.6.0

exit codes:
  0  gate_result 存在且 overall_status=done
  1  文件不存在或 overall_status!=done
"""
import argparse
import json
import shlex
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkgname", required=True)
    args = parser.parse_args()

    path = Path(args.session_dir) / "pkgs" / args.pkgname / f"gate_result_{args.pkgname}.json"

    if not path.exists():
        print(f"[ERROR] {path} not found", file=sys.stderr)
        return 1

    gate = json.loads(path.read_text(encoding="utf-8"))

    if gate.get("overall_status") != "done":
        print(f"[ERROR] overall_status={gate.get('overall_status')!r}", file=sys.stderr)
        return 1

    result = gate.get("result", {})
    fields = {
        "GATE_STATUS":   gate.get("overall_status", ""),
        "GATE_DECISION": result.get("decision", ""),
        "GATE_LANG":     result.get("lang", ""),
        "GATE_VERSION":  result.get("version", ""),
    }
    for key, val in fields.items():
        print(f"{key}={shlex.quote(str(val))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
