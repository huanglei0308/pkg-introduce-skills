#!/usr/bin/env python3
"""统一依赖查询入口。

所有"查社区源/用户源某包是否满足约束"的操作统一走 query_repo_for_dep()，
返回三态结果：ok / too_low / not_exist。

内部复用 rpm_batch_lookup，按语言选择最合适的查询策略：
- Python:  provides_query("python3dist(x)")
- Node.js: provides_query("npm(x)") + name_query 回退
- Java:    provides_query("mvn(group:artifact)")
- Go/Rust/C/C++: file_glob + name_query（系统库）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from rpm_batch_lookup import (
    BatchLookupError,
    fallback_results,
    file_glob_query,
    name_query,
    provides_query,
    run_batch_lookup,
)
from constraint_parser import parse_constraint

# 从 check_existing_package 复用版本比较和约束评估
import importlib.util as _ilu

def _load_cep():
    spec = _ilu.spec_from_file_location("check_existing_package", SCRIPT_DIR / "check_existing_package.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_CEP = _load_cep()


RepoStatus = Literal["ok", "too_low", "not_exist"]


class RepoQueryResult:
    """单次仓库查询结果。"""

    def __init__(
        self,
        status: RepoStatus,
        rpm_name: Optional[str] = None,
        found_version: Optional[str] = None,
        required: str = "",
    ) -> None:
        self.status = status
        self.rpm_name = rpm_name
        self.found_version = found_version
        self.required = required

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rpm_name": self.rpm_name,
            "found_version": self.found_version,
            "required": self.required,
        }

    def __repr__(self) -> str:
        return f"RepoQueryResult(status={self.status!r}, rpm={self.rpm_name!r}, version={self.found_version!r})"


def _build_tasks_for_lang(dep_name: str, lang: str) -> list[dict[str, Any]]:
    """按语言构建 DNF 查询任务列表（优先级从高到低）。"""
    lang = (lang or "").lower()

    if lang == "python":
        return [{
            "dep": dep_name,
            "queries": [
                provides_query(f"python3dist({dep_name})", "python3dist()"),
                # 回退：包名候选
                name_query(f"python3-{dep_name.replace('_', '-')}", "name"),
                name_query(f"python3-{dep_name.replace('-', '_')}", "name"),
            ],
        }]

    if lang == "nodejs":
        normalized = dep_name.lower().replace("_", "-")
        return [{
            "dep": dep_name,
            "queries": [
                provides_query(f"npm({dep_name})", "npm()"),
                name_query(f"nodejs-{normalized}", "name"),
                name_query(f"nodejs_{normalized.replace('-', '_')}", "name"),
            ],
        }]

    if lang == "java":
        # dep_name 格式为 "group:artifact"
        return [{
            "dep": dep_name,
            "queries": [
                provides_query(f"mvn({dep_name})", "mvn()"),
            ],
        }]

    # Go / Rust / C / C++ / 系统库
    lib_lower = dep_name.lower()
    return [{
        "dep": dep_name,
        "prefer_devel": True,
        "queries": [
            provides_query(f"pkgconfig({lib_lower})", "pkgconfig()"),
            file_glob_query(f"*/lib{lib_lower}.so*", "libso", prefer_devel=True),
            name_query(f"{lib_lower}-devel", "name", prefer_devel=True),
            name_query(f"lib{lib_lower}-devel", "name", prefer_devel=True),
            name_query(lib_lower, "name"),
        ],
    }]


def _evaluate_version(found_version: str, requirement: str) -> bool:
    """检查 found_version 是否满足 requirement 约束。"""
    if not requirement:
        return True
    req_info = _CEP.parse_requirement(requirement)
    result = _CEP.evaluate_requirement(found_version, req_info)
    # None 表示无法解析约束，保守认为满足（不阻断）
    return result is not False


def query_repo_for_dep(
    dep_name: str,
    lang: str,
    requirement: str,
    enabled_repos: Optional[list[str]] = None,
) -> RepoQueryResult:
    """查询本地 dnf 中某依赖包的版本状态。

    Args:
        dep_name:      包名（Python: pypi name；Java: group:artifact；其他: 库名）
        lang:          语言类型（python/nodejs/java/go/rust/c/cpp）
        requirement:   版本约束字符串，如 ">= 2.6.3"，空串表示无约束
        container:     已废弃，保留参数兼容旧调用，不再使用
        enabled_repos: 限定查询的 repo ID 列表，None 表示使用所有启用的 repo
    """
    tasks = _build_tasks_for_lang(dep_name, lang)
    if enabled_repos is not None:
        for t in tasks:
            t["enabled_repos"] = enabled_repos

    try:
        results = run_batch_lookup(tasks, timeout=600, enabled_repos=enabled_repos)
    except (BatchLookupError, OSError):
        return RepoQueryResult("not_exist", required=requirement)

    for item in results:
        rpm = item.get("rpm")
        if not rpm:
            continue
        found_version = item.get("version") or ""
        if not requirement or not found_version:
            return RepoQueryResult("ok", rpm_name=rpm, found_version=found_version, required=requirement)
        if _evaluate_version(found_version, requirement):
            return RepoQueryResult("ok", rpm_name=rpm, found_version=found_version, required=requirement)
        else:
            return RepoQueryResult("too_low", rpm_name=rpm, found_version=found_version, required=requirement)

    return RepoQueryResult("not_exist", required=requirement)


def query_both_repos(
    dep_name: str,
    lang: str,
    requirement: str,
    official_repo_ids: list[str] = None,
    user_repo_id: str = "",
) -> tuple[RepoQueryResult, RepoQueryResult]:
    """查询社区源（通过 query_repo_for_dep 本地执行）。user_repo 查询已废弃。"""
    official = query_repo_for_dep(dep_name, lang, requirement, enabled_repos=official_repo_ids)
    # user_repo 在 COPR 模式下由 check_existing_package.py 的 COPR API 查询替代
    not_exist = RepoQueryResult("not_exist", required=requirement)
    return official, not_exist
