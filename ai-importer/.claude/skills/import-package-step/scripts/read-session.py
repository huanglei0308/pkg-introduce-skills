#!/usr/bin/env python3
"""从 session.json 读取所有字段，输出 shell eval 可用的 export 语句。

用法：
  eval "$(python3 read-session.py --session-dir /path/to/session)"

输出示例：
  export COPR_FRONTEND_URL='http://copr-frontend:5000'
  export COPR_OWNER='openeuler-ai'
  ...
"""
import argparse
import json
import shlex
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--field", default="", help="只输出指定字段的值（不带 export）")
    args = parser.parse_args()

    sd = Path(args.session_dir)
    session_file = sd / "session.json"
    if not session_file.exists():
        print(f"ERROR: session.json not found: {session_file}", file=sys.stderr)
        sys.exit(1)

    s = json.loads(session_file.read_text(encoding="utf-8"))

    if args.field:
        print(s.get(args.field, ""))
        return

    mapping = [
        ("COPR_FRONTEND_URL", s.get("copr_url", "http://copr-frontend:5000")),
        ("COPR_OWNER",        s.get("copr_owner", "")),
        ("COPR_PROJECT",      s.get("copr_project", "")),
        ("COPR_API_LOGIN",    s.get("copr_login", "")),
        ("COPR_API_TOKEN",    s.get("copr_token", "")),
        ("COPR_CHROOT",       s.get("copr_chroot", "")),
        ("SESSION_UPSTREAM_URL", s.get("upstream_url", "")),
        ("SESSION_PKGNAME",   s.get("pkgname", "")),
        ("SESSION_VERSION",   s.get("version", "")),
    ]
    for k, v in mapping:
        print(f"export {k}={shlex.quote(str(v))}")


if __name__ == "__main__":
    main()
