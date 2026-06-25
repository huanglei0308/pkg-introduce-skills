#!/usr/bin/env python3
"""
通知 job_runner job 完成，写 redis job hash 和 done log。

用法：
  python3 notify_job.py --session-dir <dir> --status success|failed
"""
import argparse
import json
import os
import pathlib
import sys

import redis


def notify(session_dir: str, status: str) -> None:
    sd = pathlib.Path(session_dir)
    job_id = sd.name  # session 目录名 = job_id

    wf_files = list(sd.glob("workflow_*.json"))
    wf = json.loads(wf_files[0].read_text()) if wf_files else {}

    fields = {
        "status":      status,
        "built_pkgs":  " ".join(wf.get("built_pkgs", [])),
        "reused_pkgs": " ".join(wf.get("reused_pkgs", [])),
        "loop_count":  str(wf.get("loop_count", "")),
        "error":       (wf.get("error") or wf.get("failure_reason") or "")
                       if status == "failed" else "",
    }

    host = os.environ.get("REDIS_HOST", "redis")
    r = redis.Redis(host=host, port=6379, decode_responses=True)
    r.hset(f"job:ai:{job_id}", mapping=fields)
    r.rpush(f"logs:ai:{job_id}", json.dumps({"done": True, "status": status}))

    built  = fields["built_pkgs"]
    err    = fields["error"]
    suffix = f"  built={built}" if built else ""
    suffix += f"  reason={err}" if err else ""
    print(f"[引包] 完成  status={status}{suffix}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--session-dir", required=True)
    p.add_argument("--status", required=True, choices=["success", "failed"])
    args = p.parse_args()
    try:
        notify(args.session_dir, args.status)
    except Exception as e:
        print(f"[notify_job] warning: {e}", file=sys.stderr)
        sys.exit(1)
