#!/usr/bin/env python3
"""
归档前检查每个包的 review_summary 步骤状态。

用法：
  # 检查并输出需要补齐的包列表
  python3 ensure_review_summary.py \
    --pkgs <pkg1> [pkg2...] \
    --reports-dir ./reports

  # 标记某个包的 review_summary 为 done
  python3 ensure_review_summary.py \
    --pkgs <pkgname> \
    --reports-dir ./reports \
    --mark-done

输出（JSON 到 stdout）：
  {"status": "ok", "needs_summary": []}
  {"status": "needs_summary", "needs_summary": ["pkg1", "pkg2"]}

退出码：
  0  所有包 review_summary 均已完成
  2  存在需要补齐的包（由 skill 负责调用 /review-rpm summary）
  1  脚本执行出错
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PKG_INTRODUCE_FLOW = (
    Path(__file__).resolve().parents[2]
    / "pkg-introduce" / "scripts" / "run_pkg_introduce_flow.py"
)


def load_steps(reports_dir: Path, pkgname: str) -> dict:
    p = reports_dir / f"steps_{pkgname}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def mark_done(pkgname: str, reports_dir: Path) -> None:
    subprocess.run(
        [sys.executable, str(PKG_INTRODUCE_FLOW),
         "mark-step",
         "--pkg", pkgname,
         "--step", "review_summary",
         "--status", "done",
         "--reports-dir", str(reports_dir)],
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pkgs", nargs="+", required=True)
    parser.add_argument("--reports-dir", default="./reports")
    parser.add_argument("--mark-done", action="store_true",
                        help="将 --pkgs 中所有包的 review_summary 标记为 done")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)

    if args.mark_done:
        for pkg in args.pkgs:
            mark_done(pkg, reports_dir)
            print(f"[INFO] {pkg}: review_summary 已标记为 done", file=sys.stderr)
        print(json.dumps({"status": "ok", "marked": args.pkgs}))
        return 0

    needs_summary: list[str] = []
    for pkg in args.pkgs:
        steps = load_steps(reports_dir, pkg)
        status = steps.get("review_summary", "pending")
        if status not in ("done", "skipped"):
            needs_summary.append(pkg)
            print(f"[INFO] {pkg}: review_summary={status}，需要补齐", file=sys.stderr)
        else:
            print(f"[INFO] {pkg}: review_summary={status}，已完成", file=sys.stderr)

    if needs_summary:
        print(json.dumps({"status": "needs_summary", "needs_summary": needs_summary}))
        return 2

    print(json.dumps({"status": "ok", "needs_summary": []}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
