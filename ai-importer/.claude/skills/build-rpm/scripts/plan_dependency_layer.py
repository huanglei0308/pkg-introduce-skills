#!/usr/bin/env python3
"""Build a same-layer dependency execution plan before invoking per-node execution."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from resolve_dependency_versions import resolve_layer_candidates


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="为同层依赖生成统一的 layer execution plan")
    parser.add_argument("--requests-json", required=True, help="dependency_requests_<pkg>.json 路径")
    parser.add_argument("--build-state-dir", default="./build_state")
    parser.add_argument("--requested-by", required=True, help="当前请求这些依赖的包名")
    parser.add_argument("-o", "--output", default="", help="输出 JSON 文件路径")
    args = parser.parse_args()

    try:
        payload = read_json(Path(args.requests_json))
        requests = list(payload.get("requests") or [])
        plan = resolve_layer_candidates(
            requests,
            args.build_state_dir,
            args.requested_by,
            pkgname=str(payload.get("pkgname") or ""),
            lang=str(payload.get("lang") or ""),
        )
        rendered = json.dumps(plan, ensure_ascii=False, indent=2)
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 2 if plan.get("blocked") else 0
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
