#!/usr/bin/env python3
"""从 dep_registry.json 读取指定包的字段。

用法：
  python3 read-dep-registry.py --session-dir . --pkg setuptools --field url
  python3 read-dep-registry.py --session-dir . --pkg setuptools --field constraint
  python3 read-dep-registry.py --session-dir . --pkg setuptools  # 输出所有字段的 export
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

    reg_file = Path(args.session_dir) / "dep_registry.json"
    if not reg_file.exists():
        if args.field:
            print("")
        sys.exit(0)

    reg = json.loads(reg_file.read_text(encoding="utf-8"))
    entry = reg.get(args.pkg, {})
    if isinstance(entry, str):
        entry = {"url": entry}

    if args.field:
        print(entry.get(args.field, ""))
        return

    for k, v in entry.items():
        print(f"export DEP_{k.upper()}={shlex.quote(str(v))}")


if __name__ == "__main__":
    main()
