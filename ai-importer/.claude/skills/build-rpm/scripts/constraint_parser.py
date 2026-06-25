#!/usr/bin/env python3
"""统一约束解析模块。

合并原先分散在 pre_check_deps._normalize_npm_constraint、
classify_requirement_constraint 和 resolve_dependency_versions.parse_requirement_specifier
三处的重复逻辑，提供单一入口 parse_constraint()。
"""

from __future__ import annotations

import re
from typing import Any

try:
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version
except Exception:
    Requirement = None
    SpecifierSet = None
    Version = None
    InvalidVersion = Exception


def normalize_npm_constraint(requirement: str) -> str:
    """将 npm ^ / ~ semver 约束转换为 PEP 440 风格的范围字符串。"""
    req = requirement.strip()
    m = re.match(r'^\^(\d+)\.(\d+)\.(\d+)', req)
    if m:
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f">={major}.{minor}.{patch},<{major + 1}.0.0"
    m = re.match(r'^\^(\d+)\.(\d+)', req)
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
        return f">={major}.{minor}.0,<{major + 1}.0.0"
    m = re.match(r'^\^(\d+)', req)
    if m:
        major = int(m.group(1))
        return f">={major}.0.0,<{major + 1}.0.0"
    m = re.match(r'^~(\d+)\.(\d+)\.(\d+)', req)
    if m:
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f">={major}.{minor}.{patch},<{major}.{minor + 1}.0"
    m = re.match(r'^~(\d+)\.(\d+)', req)
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
        return f">={major}.{minor}.0,<{major}.{minor + 1}.0"
    return req


def parse_constraint(requirement: str, requirement_info: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """解析版本约束字符串，返回 (constraint_type, requirement_info)。

    constraint_type 取值：exact / range / unbounded / unknown
    """
    normalized = (requirement or "").strip()
    if normalized and normalized[0] in ("^", "~"):
        normalized = normalize_npm_constraint(normalized)

    parsed_info = dict(requirement_info or {})

    if not normalized:
        return "unbounded", parsed_info

    info_status = (parsed_info.get("status") or "").strip()

    if info_status == "unknown":
        if Requirement is not None:
            try:
                parsed_req = Requirement(f"placeholder{normalized}")
            except Exception:
                try:
                    parsed_req = Requirement(normalized)
                except Exception:
                    return "unknown", parsed_info
            specs = [str(s) for s in parsed_req.specifier]
            if not specs:
                return "unbounded", parsed_info
            exact_values: list[str] = []
            specifier_records: list[dict[str, str]] = []
            for spec_text in specs:
                for op in ("===", "==", ">=", "<=", "!=", "~=", ">", "<"):
                    if spec_text.startswith(op):
                        ver = spec_text[len(op):].strip()
                        specifier_records.append({"operator": op, "version": ver})
                        if op in {"==", "==="} and ver:
                            exact_values.append(ver)
                        break
                else:
                    specifier_records.append({"operator": "", "version": spec_text})
            if specifier_records and "specifiers" not in parsed_info:
                parsed_info["specifiers"] = specifier_records
            if len(set(exact_values)) == 1:
                parsed_info.setdefault("exact_version", exact_values[0])
                return "exact", parsed_info
            return "range", parsed_info
        return "unknown", parsed_info

    exact_version = (parsed_info.get("exact_version") or "").strip()
    if exact_version:
        return "exact", parsed_info

    clauses = parsed_info.get("clauses")
    if isinstance(clauses, list) and clauses:
        exact_ops = {"==", "==="}
        range_ops = {">", ">=", "<", "<=", "~=", "!="}
        clause_ops = [
            str(item.get("operator") or "").strip()
            for item in clauses
            if isinstance(item, dict) and str(item.get("operator") or "").strip()
        ]
        if clause_ops:
            if all(op in exact_ops for op in clause_ops):
                exact_candidates = [
                    str(item.get("version") or "").strip()
                    for item in clauses
                    if isinstance(item, dict)
                ]
                exact_candidates = [v for v in exact_candidates if v]
                if len(set(exact_candidates)) == 1 and exact_candidates:
                    parsed_info.setdefault("exact_version", exact_candidates[0])
                    return "exact", parsed_info
            if any(op in range_ops for op in clause_ops):
                if "specifiers" not in parsed_info:
                    parsed_info["specifiers"] = [
                        {
                            "operator": str(item.get("operator") or "").strip(),
                            "version": str(item.get("version") or "").strip(),
                        }
                        for item in clauses
                        if isinstance(item, dict)
                    ]
                return "range", parsed_info

    specifiers = parsed_info.get("specifiers")
    if isinstance(specifiers, list) and specifiers:
        range_ops = {">", ">=", "<", "<=", "~=", "!="}
        exact_ops = {"==", "==="}
        has_range = any((item.get("operator") or "") in range_ops for item in specifiers if isinstance(item, dict))
        only_exact = all((item.get("operator") or "") in exact_ops for item in specifiers if isinstance(item, dict))
        if only_exact:
            exact_candidates = [str(item.get("version") or "").strip() for item in specifiers if isinstance(item, dict)]
            exact_candidates = [v for v in exact_candidates if v]
            if len(exact_candidates) == 1:
                parsed_info.setdefault("exact_version", exact_candidates[0])
                return "exact", parsed_info
        if has_range:
            return "range", parsed_info
        return "unknown", parsed_info

    if Requirement is not None:
        try:
            parsed_req = Requirement(normalized)
            specs = [str(s) for s in parsed_req.specifier]
            if not specs:
                return "unbounded", parsed_info
            if len(specs) == 1:
                for op in ("===", "=="):
                    if specs[0].startswith(op):
                        parsed_info.setdefault("exact_version", specs[0][len(op):].strip())
                        return "exact", parsed_info
            return "range", parsed_info
        except Exception:
            # Requirement() requires a package name prefix; try SpecifierSet for bare constraint strings
            if SpecifierSet is not None:
                try:
                    ss = SpecifierSet(normalized)
                    specs = [str(s) for s in ss]
                    if not specs:
                        return "unbounded", parsed_info
                    exact_ops = {"===", "=="}
                    range_ops = {">", ">=", "<", "<=", "~=", "!="}
                    specifier_records = []
                    exact_values = []
                    for s in specs:
                        for op in ("===", "==", ">=", "<=", "!=", "~=", ">", "<"):
                            if s.startswith(op):
                                ver = s[len(op):].strip()
                                specifier_records.append({"operator": op, "version": ver})
                                if op in exact_ops:
                                    exact_values.append(ver)
                                break
                    if specifier_records and "specifiers" not in parsed_info:
                        parsed_info["specifiers"] = specifier_records
                    if exact_values and len(set(exact_values)) == 1:
                        parsed_info.setdefault("exact_version", exact_values[0])
                        return "exact", parsed_info
                    if any(r.get("operator") in range_ops for r in specifier_records):
                        return "range", parsed_info
                except Exception:
                    pass
            return "unknown", parsed_info

    return "unknown", parsed_info


def to_specifier_set(constraint: str) -> Any:
    """将约束字符串转换为 SpecifierSet，供版本候选过滤使用。返回 None 表示无法解析。"""
    normalized = (constraint or "").strip()
    if not normalized or Requirement is None:
        return None
    if normalized[0] in ("^", "~"):
        normalized = normalize_npm_constraint(normalized)
    try:
        return Requirement(f"placeholder{normalized}").specifier
    except Exception:
        try:
            return Requirement(normalized).specifier
        except Exception:
            return None


def satisfies(version: str, constraint: str, constraint_type: str, requirement_info: dict[str, Any]) -> bool:
    """检查 version 是否满足约束。"""
    if not version:
        return False
    if constraint_type == "unbounded":
        return True
    if constraint_type == "unknown":
        return False

    if constraint_type == "exact":
        from providers.pypi import normalize_version
        nv = normalize_version(version)
        exact = normalize_version((requirement_info or {}).get("exact_version") or "")
        return bool(exact) and nv == exact

    specifier = to_specifier_set(constraint)
    if specifier is None or Version is None:
        return False
    try:
        from providers.pypi import normalize_version
        nv = normalize_version(version)
        return Version(nv) in specifier
    except (InvalidVersion, Exception):
        return False
