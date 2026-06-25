#!/usr/bin/env python3
"""将 build_rpm_result.json 的 status 标记为 interrupted（若不在合法值内）。

用法：
  python3 mark-interrupted.py --session-dir . --pkg hello-openeuler
"""
import argparse
import json
from pathlib import Path

VALID_STATUSES = {"success", "dep_needed", "failed", "copr_running", "precheck_done"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True)
    args = parser.parse_args()

    p = Path(args.session_dir) / "pkgs" / args.pkg / "build_rpm_result.json"
    r = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    if r.get("status") not in VALID_STATUSES:
        old = r.get("status")
        r["status"] = "interrupted"
        r.setdefault("failure", {})["failure_reason"] = f"agent exited with status={old!r}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[mark-interrupted] {args.pkg}: {old!r} → interrupted")
    else:
        print(f"[mark-interrupted] {args.pkg}: status={r.get('status')!r} ok, no change")


if __name__ == "__main__":
    main()
