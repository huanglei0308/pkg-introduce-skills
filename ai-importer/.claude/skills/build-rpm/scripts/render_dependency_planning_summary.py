#!/usr/bin/env python3
"""Render a human-readable dependency planning summary from layer plan/outcome JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_execution(executed: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in executed:
        dep_name = str(item.get("dep_name") or item.get("name") or "").strip()
        if dep_name:
            result[dep_name] = item
    return result


def render_summary(payload: dict[str, Any]) -> str:
    pkgname = str(payload.get("pkgname") or "")
    requested_by = str(payload.get("requested_by") or "")
    planning_log = list(payload.get("planning_log") or [])
    execution_index = summarize_execution(list(payload.get("executed") or []))
    summary = payload.get("summary") or {}

    lines: list[str] = []
    lines.append(f"Dependency planning summary for {pkgname or '<unknown>'}")
    lines.append(f"Requested by: {requested_by or '<unknown>'}")
    lines.append("")
    lines.append(
        f"Requests: {summary.get('request_count', len(planning_log))} | "
        f"Planned: {summary.get('planned_count', len([i for i in planning_log if i.get('node_state') == 'planned']))} | "
        f"Blocked: {summary.get('blocked_count', len([i for i in planning_log if i.get('node_state') == 'blocked']))}"
    )
    lines.append("")

    if not planning_log:
        lines.append("- No dependency planning records.")
        return "\n".join(lines) + "\n"

    for item in planning_log:
        name = item.get("name") or "<unknown>"
        node_state = item.get("node_state") or "unknown"
        lines.append(f"- {name} [{node_state}]")
        lines.append(f"  constraint      : {item.get('input_constraint') or '<none>'}")
        lines.append(f"  constraint_type : {item.get('input_constraint_type') or 'unknown'}")
        lines.append(f"  strategy        : {item.get('selected_strategy') or '<none>'}")
        lines.append(f"  locked_version  : {item.get('locked_version') or '<none>'}")
        candidates = item.get("candidates") or []
        if candidates:
            lines.append(f"  candidates      : {', '.join(str(v) for v in candidates)}")
        else:
            lines.append("  candidates      : <none>")
        lines.append(f"  reason          : {item.get('reason') or '<none>'}")

        execution = execution_index.get(str(name), {})
        if execution:
            lines.append(f"  execution       : {execution.get('status') or '<unknown>'}")
            lines.append(f"  action          : {execution.get('action') or '<none>'}")
            lines.append(f"  selected        : {execution.get('selected_candidate') or '<none>'}")
            lines.append(f"  locked_result   : {execution.get('version') or execution.get('requested_version') or '<none>'}")
            candidate_trace = execution.get("candidate_trace") or []
            if candidate_trace:
                lines.append("  candidate_trace :")
                for trace in candidate_trace:
                    lines.append(
                        "    - "
                        f"candidate={trace.get('candidate') or '<none>'}; "
                        f"status={trace.get('status') or '<unknown>'}; "
                        f"action={trace.get('action') or '<none>'}; "
                        f"failure={trace.get('failure_reason') or '<none>'}"
                    )
            attempts = execution.get("attempts") or []
            if attempts:
                lines.append("  attempts        :")
                for attempt in attempts:
                    lines.append(
                        "    - "
                        f"candidate={attempt.get('candidate') or '<none>'}; "
                        f"status={attempt.get('status') or '<unknown>'}; "
                        f"action={attempt.get('action') or '<none>'}; "
                        f"failure={attempt.get('failure_reason') or '<none>'}"
                    )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成人类可读的依赖版本规划摘要")
    parser.add_argument("--input-json", required=True, help="dependency_layer_plan_<pkg>.json 或 dependency_layer_outcome_<pkg>.json")
    parser.add_argument("-o", "--output", default="", help="输出文本文件路径")
    args = parser.parse_args()

    try:
        payload = read_json(Path(args.input_json))
        rendered = render_summary(payload)
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
