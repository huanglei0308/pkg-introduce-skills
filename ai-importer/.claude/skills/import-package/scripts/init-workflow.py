#!/usr/bin/env python3
"""初始化或恢复 workflow_<pkgname>.json。

用法：
  python3 init-workflow.py --session-dir <session_dir> --pkgname <pkgname>
"""
import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkgname", required=True)
    args = parser.parse_args()

    p = Path(args.session_dir) / f"workflow_{args.pkgname}.json"
    if not p.exists():
        p.write_text(json.dumps({
            "pkgname": args.pkgname,
            "goal": "build_success",
            "loop_count": 0,
            "max_loops": 20,
            "built_pkgs": [],
            "reused_pkgs": [],
            "error": None,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[workflow] initialized")
    else:
        wf = json.loads(p.read_text(encoding="utf-8"))
        print(f"[workflow] resumed, loop_count: {wf['loop_count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
