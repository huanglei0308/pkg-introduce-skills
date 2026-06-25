#!/usr/bin/env python3
"""Analysis-to-planning bridge: aggregate same-layer pending items into DependencyRequest records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
except Exception:  # pragma: no cover - optional runtime dependency
    Requirement = None
    SpecifierSet = None


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_requested_by(value: Any, default_pkg: str) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return [default_pkg] if default_pkg else []


def append_unique(items: list[Any], value: Any) -> None:
    if value not in items:
        items.append(value)


def classify_constraint_conflict(constraints: list[str], constraint_type: str) -> tuple[bool, str]:
    normalized = [item.strip() for item in constraints if item and item.strip()]
    if len(normalized) <= 1:
        return False, ""

    if constraint_type == "exact":
        unique = set(normalized)
        if len(unique) > 1:
            return True, f"multiple exact constraints are incompatible: {', '.join(sorted(unique))}"
        return False, ""

    if Requirement is None:
        return False, ""

    try:
        combined = Requirement(f"placeholder{','.join(normalized)}")
        specifiers = list(combined.specifier)
        exact_values = {str(spec)[2:] for spec in specifiers if str(spec).startswith('==')}
        if len(exact_values) > 1:
            return True, f"multiple exact range members are incompatible: {', '.join(sorted(exact_values))}"
    except Exception:
        return False, ""

    return False, ""


def merge_constraints(constraints: list[str], constraint_type: str) -> tuple[str, str]:
    normalized = [item.strip() for item in constraints if item and item.strip()]
    if not normalized:
        return "", constraint_type or "unknown"
    if len(normalized) == 1:
        return normalized[0], constraint_type or "unknown"

    if constraint_type == "exact":
        return normalized[0], "exact"

    if Requirement is None or SpecifierSet is None:
        return normalized[0], "range" if len(normalized) > 1 else (constraint_type or "unknown")

    collected: list[str] = []
    for item in normalized:
        try:
            requirement = Requirement(f"placeholder{item}")
            for specifier in requirement.specifier:
                spec_text = str(specifier)
                if spec_text not in collected:
                    collected.append(spec_text)
        except Exception:
            continue

    if not collected:
        return normalized[0], "range"

    merged = ",".join(collected)
    return merged, "range"


def choose_constraint_type(items: list[dict[str, Any]], fallback: str) -> str:
    precedence = {"exact": 4, "range": 3, "unbounded": 2, "unknown": 1}
    best = fallback or "unknown"
    best_score = precedence.get(best, 0)
    for item in items:
        candidate = str(item.get("constraint_type") or "").strip() or "unknown"
        score = precedence.get(candidate, 0)
        if score > best_score:
            best = candidate
            best_score = score
    return best


def aggregate_pending_requests(summary: dict[str, Any], requested_by: str) -> list[dict[str, Any]]:
    """Aggregate same-layer pending items into normalized DependencyRequest records."""
    pending = list(summary.get("pending") or [])
    grouped: dict[str, dict[str, Any]] = {}

    for item in pending:
        name = str(item.get("name") or item.get("dep") or "").strip()
        if not name:
            continue
        entry = grouped.get(name)
        if entry is None:
            dep_type = item.get("type") or summary.get("lang") or ""
            entry = {
                "name": name,
                "dep": item.get("dep") or name,
                "type": dep_type,
                "identity": f"{dep_type}:{name}" if dep_type else name,
                "upstream_url": item.get("upstream_url") or "",
                "upstream_resolution": item.get("upstream_resolution") or "",
                "constraint_type": item.get("constraint_type") or "unknown",
                "version_source": item.get("version_source") or "unknown",
                "constraints": [],
                "requirement_info_candidates": [],
                "categories": [],
                "requested_by": [],
                "members": [],
            }
            grouped[name] = entry

        constraint = str(item.get("constraint") or item.get("requirement") or "").strip()
        if constraint:
            append_unique(entry["constraints"], constraint)

        requirement_info = item.get("requirement_info")
        if isinstance(requirement_info, dict) and requirement_info not in entry["requirement_info_candidates"]:
            entry["requirement_info_candidates"].append(requirement_info)

        category = str(item.get("category") or "").strip()
        if category:
            append_unique(entry["categories"], category)

        for owner in normalize_requested_by(item.get("requested_by"), requested_by):
            append_unique(entry["requested_by"], owner)

        entry["members"].append(item)

    requests: list[dict[str, Any]] = []
    for name, entry in grouped.items():
        constraint_values = entry.pop("constraints")
        requirement_infos = entry.pop("requirement_info_candidates")
        members = entry.pop("members")
        effective_constraint_type = choose_constraint_type(members, entry.get("constraint_type") or "unknown")

        primary_constraint, merged_constraint_type = merge_constraints(
            constraint_values,
            effective_constraint_type,
        )

        request = {
            **entry,
            "constraint": primary_constraint,
            "requirement": primary_constraint,
            "constraint_type": merged_constraint_type,
            "all_constraints": constraint_values,
            "requirement_info": requirement_infos[0] if requirement_infos else {},
            "requested_by": entry.get("requested_by") or ([requested_by] if requested_by else []),
            "member_count": len(members),
            "member_preview": [
                {
                    "name": item.get("name") or item.get("dep") or "",
                    "constraint": item.get("constraint") or item.get("requirement") or "",
                    "requested_by": item.get("requested_by") or requested_by,
                    "decision": item.get("decision") or "",
                    "found_version": item.get("found_version") or item.get("existing_check", {}) and
                        ((item.get("existing_check") or {}).get("official") or {}).get("highest", {}) and
                        (((item.get("existing_check") or {}).get("official") or {}).get("highest") or {}).get("version") or "",
                }
                for item in members[:5]
            ],
            "node_state": "discovered",
            "decision_trace": [
                {
                    "name": item.get("name") or item.get("dep") or "",
                    "decision": item.get("decision") or "",
                    "action": item.get("action") or "",
                    "reason": item.get("reason") or "",
                    "found_version": item.get("found_version") or "",
                }
                for item in members
            ],
        }
        conflict, conflict_reason = classify_constraint_conflict(constraint_values, merged_constraint_type)
        request["conflict"] = conflict
        request["conflict_reason"] = conflict_reason
        if conflict:
            request["node_state"] = "blocked"
        requests.append(request)

    requests.sort(key=lambda item: item["name"])
    return requests


def main() -> int:
    parser = argparse.ArgumentParser(description="将同层 pending 依赖聚合为标准化 DependencyRequest 列表")
    parser.add_argument("--summary-json", required=True, help="pre_check_<pkg>.json 路径")
    parser.add_argument("--requested-by", required=True, help="当前请求这些依赖的包名")
    parser.add_argument("-o", "--output", default="", help="输出 JSON 文件路径")
    args = parser.parse_args()

    try:
        summary = read_json(Path(args.summary_json))
        requests = aggregate_pending_requests(summary, args.requested_by)
        payload = {
            "pkgname": summary.get("pkgname", ""),
            "lang": summary.get("lang", ""),
            "requested_by": args.requested_by,
            "requests": requests,
        }
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
