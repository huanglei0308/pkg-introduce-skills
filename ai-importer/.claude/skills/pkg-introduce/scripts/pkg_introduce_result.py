#!/usr/bin/env python3
"""管理 pkg-introduce 结果文件。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_DECISIONS = {
    "reuse_official",
    "reuse_user_repo",
    "upgrade_user_repo",
    "block_official_older",
    "introduce_new",
}

VALID_ACTIONS = {
    "reused_official",
    "reused_user_repo",
    "upgraded_user_repo",
    "built_new",
    "blocked",
}

VALID_FAILURE_TYPES = {
    "retryable_version_conflict",
    "retryable_dependency_resolution_failure",
    "non_retryable_repo_blocked",
    "non_retryable_license_blocked",
    "non_retryable_source_missing",
    "non_retryable_build_failure",
    "non_retryable_toolchain_failure",
    "non_retryable_official_conflict",
}

# building: 正在处理（替代 building.txt 的循环依赖检测）
# done:     已完成（成功或复用）
# failed:   已失败
VALID_STATUSES = {"building", "done", "failed"}

VALID_MODES = {"top-level", "dependency"}
BOOLEAN_CHOICES = {"true": True, "false": False}


def parse_bool(value: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized not in BOOLEAN_CHOICES:
        raise argparse.ArgumentTypeError("布尔值只能为 true 或 false")
    return BOOLEAN_CHOICES[normalized]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def result_path(pkgname: str, reports_dir: str) -> Path:
    return Path(reports_dir) / f"pkg_introduce_result_{pkgname}.json"


def load_result(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_result(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def validate_choice(value: str, allowed: set[str], label: str) -> str:
    if not value:
        return value
    if value not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{label} 非法: {value}（允许值: {allowed_text}）")
    return value


def build_updates(args: argparse.Namespace) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    field_map = {
        "upstream_url": args.upstream_url,
        "lang": args.lang,
        "requested_version": args.requested_version,
        "version": args.version,
        "decision": args.decision,
        "action": args.action,
        "reason": args.reason,
        "failure_type": getattr(args, "failure_type", None),
        "failure_reason": getattr(args, "failure_reason", None),
        "existing_check": args.existing_check,
        "repo_check": args.repo_check,
        "license_check": args.license_check,
        "analysis_file": args.analysis_file,
        "status": getattr(args, "status", None),
    }
    for key, value in field_map.items():
        if value is not None:
            updates[key] = value

    if getattr(args, "depth", None) is not None:
        updates["depth"] = args.depth
    if getattr(args, "mode", None) is not None:
        updates["mode"] = args.mode
    if getattr(args, "archived", None) is not None:
        updates["archived"] = args.archived
    return updates


def merge_result(existing: dict[str, Any], pkgname: str, updates: dict[str, Any]) -> dict[str, Any]:
    created_at = existing.get("created_at") or now_iso()
    merged = {
        "pkgname": pkgname,
        "upstream_url": existing.get("upstream_url", ""),
        "lang": existing.get("lang", ""),
        "requested_version": existing.get("requested_version", ""),
        "version": existing.get("version", ""),
        "decision": existing.get("decision", ""),
        "action": existing.get("action", ""),
        "reason": existing.get("reason", ""),
        "failure_type": existing.get("failure_type", ""),
        "failure_reason": existing.get("failure_reason", ""),
        "status": existing.get("status", ""),
        "mode": existing.get("mode", "top-level"),
        "depth": existing.get("depth", 0),
        "existing_check": existing.get("existing_check", ""),
        "repo_check": existing.get("repo_check", ""),
        "license_check": existing.get("license_check", ""),
        "analysis_file": existing.get("analysis_file", ""),
        "archived": existing.get("archived", False),
        "created_at": created_at,
    }
    merged.update(updates)
    merged["updated_at"] = now_iso()
    return merged


def add_common_arguments(parser: argparse.ArgumentParser, *, require_action_reason: bool) -> None:
    parser.add_argument("pkgname", help="包名")
    parser.add_argument("--reports-dir", default="./reports", help="结果文件目录，默认 ./reports")
    parser.add_argument("--upstream-url", dest="upstream_url", default=None, help="上游地址")
    parser.add_argument("--lang", default=None, help="语言类型")
    parser.add_argument("--requested-version", default=None, help="用户请求版本")
    parser.add_argument("--version", default=None, help="真实版本号")
    parser.add_argument(
        "--decision",
        required=False,
        default=None,
        help="决策：reuse_official/reuse_user_repo/upgrade_user_repo/block_official_older/introduce_new",
    )
    parser.add_argument(
        "--action",
        required=require_action_reason,
        default=None,
        help="动作：reused_official/reused_user_repo/upgraded_user_repo/built_new/blocked",
    )
    parser.add_argument("--reason", required=require_action_reason, default=None, help="决策原因")
    parser.add_argument("--mode", default=None, help="调用模式：top-level/dependency")
    parser.add_argument("--depth", type=int, default=None, help="当前递归深度")
    parser.add_argument("--existing-check", default=None, help="existing check 结果文件路径")
    parser.add_argument("--repo-check", default=None, help="repo check 结果文件路径")
    parser.add_argument("--license-check", default=None, help="license check 结果文件路径")
    parser.add_argument("--analysis-file", default=None, help="分析结果文件路径")
    parser.add_argument("--failure-type", default=None, help="失败分类")
    parser.add_argument("--failure-reason", default=None, help="失败分类原因")
    parser.add_argument("--archived", type=parse_bool, default=None, help="是否已归档：true/false")
    parser.add_argument(
        "--status",
        default=None,
        help="处理状态：building（进行中）/ done（已完成）/ failed（已失败）",
    )


def command_write(args: argparse.Namespace) -> int:
    validate_choice(args.decision, VALID_DECISIONS, "decision")
    validate_choice(args.action, VALID_ACTIONS, "action")
    if getattr(args, "status", None):
        validate_choice(args.status, VALID_STATUSES, "status")
    if getattr(args, "mode", None):
        validate_choice(args.mode, VALID_MODES, "mode")
    path = result_path(args.pkgname, args.reports_dir)
    updates = build_updates(args)
    data = merge_result({}, args.pkgname, updates)
    write_result(path, data)
    print(path)
    return 0


def command_update(args: argparse.Namespace) -> int:
    if args.decision is not None:
        validate_choice(args.decision, VALID_DECISIONS, "decision")
    if args.action is not None:
        validate_choice(args.action, VALID_ACTIONS, "action")
    if getattr(args, "status", None) is not None:
        validate_choice(args.status, VALID_STATUSES, "status")
    if getattr(args, "mode", None) is not None:
        validate_choice(args.mode, VALID_MODES, "mode")
    path = result_path(args.pkgname, args.reports_dir)
    existing = load_result(path)
    updates = build_updates(args)
    data = merge_result(existing, args.pkgname, updates)
    write_result(path, data)
    print(path)
    return 0


def command_show(args: argparse.Namespace) -> int:
    path = result_path(args.pkgname, args.reports_dir)
    if not path.exists():
        print(f"结果文件不存在: {path}", file=sys.stderr)
        return 1
    data = load_result(path)
    if args.field:
        value = data
        for part in args.field.split("."):
            if not isinstance(value, dict) or part not in value:
                print(f"字段不存在: {args.field}", file=sys.stderr)
                return 1
            value = value[part]
        if isinstance(value, (dict, list)):
            print(json.dumps(value, ensure_ascii=False, indent=2))
        elif value is None:
            print("null")
        elif isinstance(value, bool):
            print("true" if value else "false")
        else:
            print(value)
        return 0
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def command_path(args: argparse.Namespace) -> int:
    print(result_path(args.pkgname, args.reports_dir))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="管理 pkg-introduce 结果文件")
    subparsers = parser.add_subparsers(dest="command", required=True)

    write_parser = subparsers.add_parser("write", help="写入新的结果文件")
    add_common_arguments(write_parser, require_action_reason=True)
    write_parser.set_defaults(func=command_write)

    update_parser = subparsers.add_parser("update", help="更新已有结果文件")
    add_common_arguments(update_parser, require_action_reason=False)
    update_parser.set_defaults(func=command_update)

    show_parser = subparsers.add_parser("show", help="显示结果文件或字段")
    show_parser.add_argument("pkgname", help="包名")
    show_parser.add_argument("--reports-dir", default="./reports", help="结果文件目录，默认 ./reports")
    show_parser.add_argument("--field", default="", help="读取单个字段，如 action 或 reason")
    show_parser.set_defaults(func=command_show)

    path_parser = subparsers.add_parser("path", help="输出结果文件路径")
    path_parser.add_argument("pkgname", help="包名")
    path_parser.add_argument("--reports-dir", default="./reports", help="结果文件目录，默认 ./reports")
    path_parser.set_defaults(func=command_path)

    args = parser.parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
