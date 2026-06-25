#!/usr/bin/env python3
"""打印当前 session 的引包进度摘要。

用法：
  python3 print-summary.py --session-dir /tmp/ai-sessions/abc123
"""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    args = parser.parse_args()

    sd = Path(args.session_dir)
    wf_files = list(sd.glob("workflow_*.json"))
    if not wf_files:
        print("[summary] no workflow file found")
        return

    wf = json.loads(wf_files[0].read_text(encoding="utf-8"))
    status_str = "SUCCESS" if wf.get("goal_achieved") else "IN_PROGRESS"
    built  = " ".join(wf.get("built_pkgs", []))
    reused = " ".join(wf.get("reused_pkgs", []))
    print(f"[{wf['pkgname']}] {status_str} | built: {built or '-'} | reused: {reused or '-'} | loops: {wf.get('loop_count', 0)}")


if __name__ == "__main__":
    main()
