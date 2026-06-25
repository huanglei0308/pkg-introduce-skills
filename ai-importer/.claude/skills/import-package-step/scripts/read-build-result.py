#!/usr/bin/env python3
"""读取 build_rpm_result.json，输出 shell eval 格式。

用法：
  eval "$(python3 read-build-result.py --session-dir <dir> --pkgname <pkg>)"
  # → BUILD_STATUS=success
  #   CI_STATUS=pass

exit codes:
  0  文件存在
  1  文件不存在
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

    path = Path(args.session_dir) / "pkgs" / args.pkgname / "build_rpm_result.json"
    if not path.exists():
        print(f"BUILD_STATUS=''")
        return 1

    r = json.loads(path.read_text(encoding="utf-8"))
    print(f"BUILD_STATUS={shlex.quote(r.get('status', ''))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
