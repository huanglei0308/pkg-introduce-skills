#!/usr/bin/env python3
"""Runtime-side finalize application for dependency resolution execution."""

from __future__ import annotations

from typing import Any

from dependency_resolution_state import (
    record_attempt,
    record_dependency_outcome,
    record_resolution,
    resolution_payload_from_finalize,
)

SUCCESS_ACTIONS = {"built_new", "upgraded_user_repo", "reused_official", "reused_user_repo"}


def build_constraint_record(dep_item: dict[str, Any], requested_by: str) -> dict[str, Any]:
    return {
        "from": requested_by,
        "lang": dep_item.get("type") or "",
        "constraint": dep_item.get("constraint") or dep_item.get("requirement") or "",
    }


def apply_finalize_runtime(
    dep_item: dict[str, Any],
    resolution_result: dict[str, Any],
    finalize_result: dict[str, Any],
    build_state_dir: str,
) -> dict[str, Any]:
    dep_name = resolution_result.get("name") or dep_item.get("name") or dep_item.get("dep") or ""
    constraint_record = resolution_result.get("constraint_record") or build_constraint_record(
        dep_item,
        str(resolution_result.get("requested_by") or ""),
    )
    resolution_type = str(resolution_result.get("constraint_type") or dep_item.get("constraint_type") or "unknown")
    strategy = str(resolution_result.get("selected_strategy") or "manual")
    action = str(finalize_result.get("action") or "")
    version = str(finalize_result.get("requested_version") or finalize_result.get("version") or "")
    failure_type = str(finalize_result.get("failure_type") or "")
    failure_reason = str(finalize_result.get("failure_reason") or finalize_result.get("reason") or "")

    if action in SUCCESS_ACTIONS:
        payload = resolution_payload_from_finalize(
            finalize_result,
            strategy,
            resolution_type,
            [str(resolution_result.get("requested_by") or "")],
            [constraint_record],
        )
        record_resolution(
            build_state_dir,
            dep_name,
            payload["version"],
            payload["requested_version"],
            payload["source"],
            payload["resolution_type"],
            payload["status"],
            payload["requested_by"],
            payload["constraints"],
        )
        return {
            "status": "accepted",
            "dep_name": dep_name,
            "version": payload["version"],
            "requested_version": payload["requested_version"],
            "action": action,
        }

    if version:
        record_attempt(
            build_state_dir,
            dep_name,
            version,
            failure_type or action or "failed",
            failure_reason,
        )

    return {
        "status": "retry" if finalize_result.get("retryable") else "blocked",
        "dep_name": dep_name,
        "version": version,
        "action": action,
        "failure_type": failure_type,
        "failure_reason": failure_reason,
    }


def record_execution_outcome(build_state_dir: str, dep_name: str, outcome: dict[str, Any]) -> None:
    record_dependency_outcome(build_state_dir, dep_name, outcome)
