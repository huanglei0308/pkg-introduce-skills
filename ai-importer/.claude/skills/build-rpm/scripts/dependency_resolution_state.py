#!/usr/bin/env python3
"""管理依赖版本解析会话状态。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

STATE_FILE_NAMES = {
    "resolved_versions": "resolved_versions.json",
    "dependency_attempts": "dependency_attempts.json",
    "dependency_outcomes": "dependency_outcomes.json",
    "session_snapshot": "session_snapshot.json",
}


def state_file_path(build_state_dir: str, state_name: str) -> Path:
    if state_name not in STATE_FILE_NAMES:
        raise ValueError(f"未知状态文件类型: {state_name}")
    return Path(build_state_dir) / STATE_FILE_NAMES[state_name]


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ensure_state_files(build_state_dir: str) -> dict[str, str]:
    created: dict[str, str] = {}
    for state_name in STATE_FILE_NAMES:
        path = state_file_path(build_state_dir, state_name)
        if not path.exists():
            write_json(path, {})
        created[state_name] = str(path)
    return created


def append_unique_list_item(items: list[Any], value: Any) -> list[Any]:
    if value not in items:
        items.append(value)
    return items


def text_state_path(build_state_dir: str, filename: str) -> Path:
    return Path(build_state_dir) / filename


def load_text_state(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_text_state(path: Path, values: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    unique_values: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in unique_values:
            unique_values.append(normalized)
    path.write_text(("\n".join(unique_values) + "\n") if unique_values else "", encoding="utf-8")


def resolution_payload_from_finalize(finalize_data: dict[str, Any], source: str, resolution_type: str, requested_by: list[str], constraints: list[dict[str, Any]]) -> dict[str, Any]:
    requested_version = str(finalize_data.get("requested_version") or "")
    version = str(finalize_data.get("version") or requested_version)
    return {
        "version": version,
        "requested_version": requested_version or version,
        "source": source,
        "resolution_type": resolution_type,
        "status": "resolved",
        "requested_by": requested_by,
        "constraints": constraints,
    }


def record_resolution(
    build_state_dir: str,
    dep_name: str,
    version: str,
    requested_version: str,
    source: str,
    resolution_type: str,
    status: str,
    requested_by: list[str],
    constraints: list[dict[str, Any]],
) -> Path:
    path = state_file_path(build_state_dir, "resolved_versions")
    data = read_json(path)
    existing = dict(data.get(dep_name) or {})

    merged_requested_by = list(existing.get("requested_by") or [])
    for item in requested_by:
        if item:
            append_unique_list_item(merged_requested_by, item)

    merged_constraints = list(existing.get("constraints") or [])
    for item in constraints:
        if item and item not in merged_constraints:
            merged_constraints.append(item)

    data[dep_name] = {
        "version": version,
        "requested_version": requested_version or version,
        "source": source,
        "resolution_type": resolution_type,
        "status": status,
        "requested_by": merged_requested_by,
        "constraints": merged_constraints,
    }
    write_json(path, data)
    return path


def record_attempt(
    build_state_dir: str,
    dep_name: str,
    version: str,
    result: str,
    reason: str,
) -> Path:
    path = state_file_path(build_state_dir, "dependency_attempts")
    data = read_json(path)
    dep_entry = dict(data.get(dep_name) or {})
    attempts = list(dep_entry.get("attempted_versions") or [])
    if not any((item.get("version") == version) for item in attempts if isinstance(item, dict)):
        attempts.append({
            "version": version,
            "result": result,
            "reason": reason,
        })
    dep_entry["attempted_versions"] = attempts
    data[dep_name] = dep_entry
    write_json(path, data)
    return path


def record_dependency_outcome(build_state_dir: str, dep_name: str, outcome: dict[str, Any]) -> Path:
    path = state_file_path(build_state_dir, "dependency_outcomes")
    data = read_json(path)
    data[dep_name] = outcome
    write_json(path, data)
    return path


def append_constraint(build_state_dir: str, dep_name: str, constraint: dict[str, Any]) -> Path:
    path = state_file_path(build_state_dir, "resolved_versions")
    data = read_json(path)
    entry = dict(data.get(dep_name) or {})
    constraints = list(entry.get("constraints") or [])
    if constraint and constraint not in constraints:
        constraints.append(constraint)
    entry["constraints"] = constraints
    data[dep_name] = entry
    write_json(path, data)
    return path


def mark_building(build_state_dir: str, dep_name: str) -> Path:
    path = text_state_path(build_state_dir, "building.txt")
    values = load_text_state(path)
    if dep_name and dep_name not in values:
        values.append(dep_name)
    write_text_state(path, values)
    return path


def clear_building(build_state_dir: str, dep_name: str) -> Path:
    path = text_state_path(build_state_dir, "building.txt")
    values = [value for value in load_text_state(path) if value != dep_name]
    write_text_state(path, values)
    return path


def mark_introduced(build_state_dir: str, dep_name: str) -> Path:
    path = text_state_path(build_state_dir, "introduced.txt")
    values = load_text_state(path)
    if dep_name and dep_name not in values:
        values.append(dep_name)
    write_text_state(path, values)
    return path


def build_session_snapshot(build_state_dir: str) -> dict[str, Any]:
    snapshot = {
        "resolved_versions": load_state(build_state_dir, "resolved_versions"),
        "dependency_attempts": load_state(build_state_dir, "dependency_attempts"),
        "dependency_outcomes": load_state(build_state_dir, "dependency_outcomes"),
        "building": load_text_state(text_state_path(build_state_dir, "building.txt")),
        "introduced": load_text_state(text_state_path(build_state_dir, "introduced.txt")),
        "_meta": {
            "source": "live-state-files",
        },
    }
    return snapshot


def load_session_state(build_state_dir: str) -> dict[str, Any]:
    return build_session_snapshot(build_state_dir)


def write_session_snapshot(build_state_dir: str) -> Path:
    path = state_file_path(build_state_dir, "session_snapshot")
    snapshot = load_session_state(build_state_dir)
    snapshot["_meta"]["written_to"] = str(path)
    write_json(path, snapshot)
    return path


def dump_session_snapshot(build_state_dir: str) -> Path:
    return write_session_snapshot(build_state_dir)


def load_state(build_state_dir: str, state_name: str) -> dict[str, Any]:
    return read_json(state_file_path(build_state_dir, state_name))


def main() -> int:
    parser = argparse.ArgumentParser(description="管理依赖版本解析会话状态")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化状态文件")
    init_parser.add_argument("--build-state-dir", default="./build_state")

    show_parser = subparsers.add_parser("show", help="显示状态文件内容")
    show_parser.add_argument("state_name", choices=sorted(STATE_FILE_NAMES))
    show_parser.add_argument("--build-state-dir", default="./build_state")

    resolve_parser = subparsers.add_parser("record-resolution", help="记录最终接受的依赖版本")
    resolve_parser.add_argument("dep_name")
    resolve_parser.add_argument("--build-state-dir", default="./build_state")
    resolve_parser.add_argument("--version", required=True)
    resolve_parser.add_argument("--requested-version", default="")
    resolve_parser.add_argument("--source", default="manual")
    resolve_parser.add_argument("--resolution-type", default="unknown")
    resolve_parser.add_argument("--status", default="resolved")
    resolve_parser.add_argument("--requested-by", action="append", default=[])
    resolve_parser.add_argument("--constraints-json", default="[]")

    attempt_parser = subparsers.add_parser("record-attempt", help="记录依赖候选尝试结果")
    attempt_parser.add_argument("dep_name")
    attempt_parser.add_argument("--build-state-dir", default="./build_state")
    attempt_parser.add_argument("--version", required=True)
    attempt_parser.add_argument("--result", required=True)
    attempt_parser.add_argument("--reason", default="")

    finalize_parser = subparsers.add_parser("resolution-from-finalize", help="根据 finalize JSON 记录最终版本")
    finalize_parser.add_argument("dep_name")
    finalize_parser.add_argument("--build-state-dir", default="./build_state")
    finalize_parser.add_argument("--finalize-json", required=True)
    finalize_parser.add_argument("--source", default="manual")
    finalize_parser.add_argument("--resolution-type", default="unknown")
    finalize_parser.add_argument("--requested-by", action="append", default=[])
    finalize_parser.add_argument("--constraints-json", default="[]")

    outcome_parser = subparsers.add_parser("record-outcome", help="记录依赖最终执行结果")
    outcome_parser.add_argument("dep_name")
    outcome_parser.add_argument("--build-state-dir", default="./build_state")
    outcome_parser.add_argument("--outcome-json", required=True)

    constraint_parser = subparsers.add_parser("append-constraint", help="向依赖记录追加约束")
    constraint_parser.add_argument("dep_name")
    constraint_parser.add_argument("--build-state-dir", default="./build_state")
    constraint_parser.add_argument("--constraint-json", required=True)

    building_parser = subparsers.add_parser("mark-building", help="标记依赖正在构建")
    building_parser.add_argument("dep_name")
    building_parser.add_argument("--build-state-dir", default="./build_state")

    clear_building_parser = subparsers.add_parser("clear-building", help="清理构建中依赖标记")
    clear_building_parser.add_argument("dep_name")
    clear_building_parser.add_argument("--build-state-dir", default="./build_state")

    introduced_parser = subparsers.add_parser("mark-introduced", help="标记依赖已实际引入")
    introduced_parser.add_argument("dep_name")
    introduced_parser.add_argument("--build-state-dir", default="./build_state")

    dump_parser = subparsers.add_parser("dump-session", help="生成统一 session snapshot")
    dump_parser.add_argument("--build-state-dir", default="./build_state")

    session_parser = subparsers.add_parser("show-session", help="读取实时 session state 视图")
    session_parser.add_argument("--build-state-dir", default="./build_state")

    args = parser.parse_args()

    try:
        if args.command == "init":
            print(json.dumps(ensure_state_files(args.build_state_dir), ensure_ascii=False, indent=2))
            return 0

        if args.command == "show":
            print(json.dumps(load_state(args.build_state_dir, args.state_name), ensure_ascii=False, indent=2))
            return 0

        if args.command == "record-resolution":
            constraints = json.loads(args.constraints_json)
            path = record_resolution(
                args.build_state_dir,
                args.dep_name,
                args.version,
                args.requested_version,
                args.source,
                args.resolution_type,
                args.status,
                args.requested_by,
                constraints,
            )
            print(path)
            return 0

        if args.command == "record-attempt":
            path = record_attempt(
                args.build_state_dir,
                args.dep_name,
                args.version,
                args.result,
                args.reason,
            )
            print(path)
            return 0

        if args.command == "resolution-from-finalize":
            finalize_data = json.loads(args.finalize_json)
            constraints = json.loads(args.constraints_json)
            payload = resolution_payload_from_finalize(
                finalize_data,
                args.source,
                args.resolution_type,
                args.requested_by,
                constraints,
            )
            path = record_resolution(
                args.build_state_dir,
                args.dep_name,
                payload["version"],
                payload["requested_version"],
                payload["source"],
                payload["resolution_type"],
                payload["status"],
                payload["requested_by"],
                payload["constraints"],
            )
            print(path)
            return 0

        if args.command == "record-outcome":
            outcome = json.loads(args.outcome_json)
            path = record_dependency_outcome(args.build_state_dir, args.dep_name, outcome)
            print(path)
            return 0

        if args.command == "append-constraint":
            constraint = json.loads(args.constraint_json)
            path = append_constraint(args.build_state_dir, args.dep_name, constraint)
            print(path)
            return 0

        if args.command == "mark-building":
            path = mark_building(args.build_state_dir, args.dep_name)
            print(path)
            return 0

        if args.command == "clear-building":
            path = clear_building(args.build_state_dir, args.dep_name)
            print(path)
            return 0

        if args.command == "mark-introduced":
            path = mark_introduced(args.build_state_dir, args.dep_name)
            print(path)
            return 0

        if args.command == "dump-session":
            path = dump_session_snapshot(args.build_state_dir)
            print(path)
            return 0

        if args.command == "show-session":
            print(json.dumps(load_session_state(args.build_state_dir), ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
