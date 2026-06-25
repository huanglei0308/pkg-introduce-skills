#!/usr/bin/env python3
"""
RPM 编译前依赖预检脚本

在 rpmbuild 循环前调用，分析运行时依赖在 openEuler 源、官方归档仓库、用户 RPM 仓库中的可用性，
输出需要递归引入的包列表（格式：<pkgname> <upstream_url>），供调用方继续处理。

用法：
  python3 pre_check_deps.py <pkgname> <lang> <source_dir> [--container oe-build-env]

退出码：
  0 — 所有依赖均已满足或可复用
  2 — 存在需要递归引入/升级的依赖（stdout 输出 <name> <url> 列表）
  1 — 存在阻断项或脚本执行出错
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from rpm_naming import get_rpm_pkg_name, get_compat_srpm_name, get_compat_rpm_pkg_name, extract_compat_major_version  # noqa: E402
from constraint_parser import parse_constraint as _parse_constraint  # noqa: E402

CHECK_EXISTING_SCRIPT = SCRIPT_DIR / "check_existing_package.py"
ANALYZE_PYTHON_SCRIPT = SCRIPT_DIR / "analyze_python_deps.py"


def _load_pkg_introduce_config() -> dict:
    config_path = SCRIPT_DIR.parent.parent / "pkg-introduce" / "config.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _dep_conflict_mode() -> str:
    """返回依赖冲突处理模式: 'block'（默认阻断）、'compat'（兼容包名引入）或 'force_compat'（所有语言强制 compat）。"""
    cfg = _load_pkg_introduce_config()
    return cfg.get("dep_conflict", {}).get("mode", "block")

# ── 语言 → 分析脚本映射 ───────────────────────────────────────────────────────

ANALYZERS = {
    "python": {"script": "analyze_python_deps.py", "extra_args": []},
    "go":     {"script": "analyze_go_deps.py",     "extra_args": []},
    "rust":   {"script": "analyze_rust_deps.py",   "extra_args": []},
    "c":      {"script": "analyze_c_deps.py",      "extra_args": []},
    "cpp":    {"script": "analyze_cpp_deps.py",    "extra_args": []},
    "nodejs": {"script": "analyze_nodejs_deps.py", "extra_args": []},
    "java":   {"script": "analyze_java_deps.py",   "extra_args": []},
}


def load_existing_checker() -> Any:
    script_dir = str(SCRIPT_DIR)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("check_existing_package", CHECK_EXISTING_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {CHECK_EXISTING_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXISTING_CHECKER = load_existing_checker()


def load_python_upstream_helpers() -> dict[str, Any]:
    script_dir = str(SCRIPT_DIR)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("analyze_python_deps", ANALYZE_PYTHON_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {ANALYZE_PYTHON_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        "fetch_pypi_info": module.fetch_pypi_info,
        "canonical_upstream_url": module.canonical_upstream_url,
        "classify_upstream_url": module.classify_upstream_url,
        "normalize_candidate_upstream": module.normalize_candidate_upstream,
        "candidate_urls_from_pypi_info": module.candidate_urls_from_pypi_info,
    }


PYTHON_UPSTREAM_HELPERS = load_python_upstream_helpers()


# ── PyPI 上游地址查询 ──────────────────────────────────────────────────────────

def _github_search_repo(pkg_name: str) -> str:
    """通过 GitHub Search API 查找包名对应的仓库，返回 html_url 或空串。"""
    # 尝试常见 org 直接命中（避免 Search API 速率限制）
    normalized = pkg_name.replace("-", "_")
    for candidate in [pkg_name, normalized]:
        for org in ["", "BeanieODM", "roman-right"]:
            path = f"{org}/{candidate}" if org else candidate
            try:
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{path}",
                    headers={"User-Agent": "pre_check_deps/1.0", "Accept": "application/vnd.github+json"},
                )
                data = json.loads(urllib.request.urlopen(req, timeout=8).read())
                if data.get("html_url"):
                    normalized_url = normalize_upstream_candidate(data["html_url"])
                    if normalized_url:
                        return normalized_url
            except Exception:
                pass
    # fallback: GitHub Search API
    try:
        query = urllib.parse.quote(f"{pkg_name} language:python")
        req = urllib.request.Request(
            f"https://api.github.com/search/repositories?q={query}&per_page=3",
            headers={"User-Agent": "pre_check_deps/1.0", "Accept": "application/vnd.github+json"},
        )
        results = json.loads(urllib.request.urlopen(req, timeout=10).read())
        for item in results.get("items", []):
            name_lower = item.get("name", "").lower().replace("-", "_")
            pkg_lower = pkg_name.lower().replace("-", "_")
            if name_lower == pkg_lower and item.get("html_url"):
                normalized_url = normalize_upstream_candidate(item["html_url"])
                if normalized_url:
                    return normalized_url
    except Exception:
        pass
    return ""


def classify_upstream_candidate(url: str) -> str:
    return PYTHON_UPSTREAM_HELPERS["classify_upstream_url"](url)


def normalize_upstream_candidate(url: str) -> str:
    return PYTHON_UPSTREAM_HELPERS["normalize_candidate_upstream"](url)


def is_trusted_upstream_url(url: str) -> bool:
    return classify_upstream_candidate(url) == "trusted"


def is_suspicious_upstream_url(url: str) -> bool:
    return classify_upstream_candidate(url) == "suspicious"


def get_pypi_upstream(pypi_name: str) -> str:
    """从 PyPI JSON API 提取可信源码仓地址，必要时回退到 GitHub 搜索。"""
    try:
        pypi_json = PYTHON_UPSTREAM_HELPERS["fetch_pypi_info"](pypi_name)
        if pypi_json:
            canonical = PYTHON_UPSTREAM_HELPERS["canonical_upstream_url"](pypi_json, pypi_name)
            if canonical and is_trusted_upstream_url(canonical):
                return canonical
            info = pypi_json.get("info", {})
            for url in PYTHON_UPSTREAM_HELPERS["candidate_urls_from_pypi_info"](info):
                normalized = normalize_upstream_candidate(url)
                if normalized and is_trusted_upstream_url(normalized):
                    return normalized
    except Exception:
        pass
    github_url = _github_search_repo(pypi_name)
    if github_url and is_trusted_upstream_url(github_url):
        return github_url
    return ""


# ── 通用辅助 ──────────────────────────────────────────────────────────────────

def resolve_python_executable() -> str:
    """优先使用 python3.11，不存在时回退到当前 python3。"""
    candidates = [
        "/usr/bin/python3.11",
        "/usr/local/bin/python3.11",
        shutil.which("python3.11"),
        sys.executable,
        shutil.which("python3"),
    ]
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if candidate.startswith("/") and not Path(candidate).exists():
            continue
        return candidate
    return "python3"


def make_output_path(pkgname: str, requested: str) -> str:
    return requested or f"/tmp/dep_check_{pkgname}.json"


def make_analysis_path(output_path: str, pkgname: str) -> Path:
    out_path = Path(output_path)
    suffix = out_path.suffix or ".json"
    stem = out_path.name[:-len(suffix)] if out_path.name.endswith(suffix) else out_path.name
    return out_path.with_name(f"{stem}_analysis{suffix}") if stem else Path(f"/tmp/dep_check_{pkgname}_analysis.json")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_source_match(dep: dict[str, Any], source_item: dict[str, Any] | None) -> dict[str, Any]:
    requirement = dep.get("requirement", "")
    if not source_item:
        return {
            "status": "missing",
            "rpm": None,
            "version": None,
            "release": None,
            "satisfies_requirement": False,
            "reason": "openEuler 源中未找到可用包",
        }

    requirement_info = EXISTING_CHECKER.parse_requirement(requirement)
    version = source_item.get("version")
    if requirement_info["status"] == "parsed":
        satisfies = EXISTING_CHECKER.evaluate_requirement(version, requirement_info)
        if satisfies:
            reason = f"openEuler 源中已有满足约束 {requirement} 的包"
            status = "satisfied"
        else:
            reason = f"openEuler 源中已有包，但版本 {version or '未知'} 不满足约束 {requirement}"
            status = "older"
    elif requirement_info["status"] == "unknown":
        satisfies = False
        reason = f"openEuler 源中已有包，但版本约束 {requirement} 无法可靠解析，保守继续"
        status = "unknown_requirement"
    else:
        satisfies = True
        reason = "openEuler 源中已有可用包"
        status = "satisfied"

    return {
        "status": status,
        "rpm": source_item.get("rpm"),
        "version": version,
        "release": source_item.get("release"),
        "satisfies_requirement": bool(satisfies),
        "reason": reason,
    }


def build_source_index(items: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        keys = {
            (item.get("dep", ""), item.get("requirement", "")),
            (item.get("dep", ""), ""),
            (item.get("name", ""), item.get("requirement", "")),
            (item.get("name", ""), ""),
        }
        for key in keys:
            if key[0]:
                index.setdefault(key, item)
    return index


def lookup_source_item(dep: dict[str, Any], source_index: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any] | None:
    keys = [
        (dep.get("dep", ""), dep.get("requirement", "")),
        (dep.get("dep", ""), ""),
        (dep.get("name", ""), dep.get("requirement", "")),
        (dep.get("name", ""), ""),
    ]
    for key in keys:
        if key[0] and key in source_index:
            return source_index[key]
    return None


def merge_official_source_older_result(
    dep: dict[str, Any],
    source_check: dict[str, Any],
    existing_check: dict[str, Any],
) -> dict[str, Any]:
    requested = dict(existing_check.get("requested") or {})
    requested_version = (requested.get("version") or "").strip()
    requirement = (requested.get("requirement") or dep.get("requirement") or "").strip()

    official = dict(existing_check.get("official") or {})
    user_repo = dict(existing_check.get("user_repo") or {})

    highest = {
        "path": "<openeuler-source>",
        "match_type": "source_repo",
        "name": source_check.get("rpm") or dep.get("name") or dep.get("dep") or "",
        "version": source_check.get("version"),
        "release": source_check.get("release"),
        "arch": None,
    }

    matched_paths = list(official.get("matched_paths") or [])
    if "<openeuler-source>" not in matched_paths:
        matched_paths.append("<openeuler-source>")

    candidates = list(official.get("candidates") or [])
    candidates.append(highest)

    official.update(
        {
            "exists": True,
            "matched_paths": matched_paths,
            "candidates": candidates,
            "highest": highest,
            "satisfies_requested_version": False if requested_version else None,
            "satisfies_requirement": False,
            "meets_need": False,
            "comparison_unknown": False,
        }
    )

    decision = EXISTING_CHECKER.choose_decision(official, user_repo, requested_version, requirement)
    patched = dict(existing_check)
    patched["official"] = official
    patched["exists_in_official"] = True
    patched["decision"] = decision
    patched["reason"] = EXISTING_CHECKER.build_reason(
        decision,
        official,
        user_repo,
        requested_version,
        requirement,
    )
    return patched


def resolve_upstream_url(name: str, lang: str) -> str:
    """尝试为任意语言的依赖包解析可信上游仓库根 URL。"""
    if not name:
        return ""
    if lang == "go":
        if name.startswith("github.com/") or name.startswith("gitlab.com/") or name.startswith("golang.org/"):
            candidate = normalize_upstream_candidate("https://" + name)
            return candidate if is_trusted_upstream_url(candidate) else ""
        return _github_search_repo(name.split("/")[-1])
    if lang == "python":
        return get_pypi_upstream(name)
    if lang == "rust":
        try:
            req = urllib.request.Request(
                f"https://crates.io/api/v1/crates/{name}",
                headers={"User-Agent": "pre_check_deps/1.0"},
            )
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            repo = data.get("crate", {}).get("repository") or data.get("crate", {}).get("homepage")
            normalized = normalize_upstream_candidate(repo) if repo else ""
            if normalized and is_trusted_upstream_url(normalized):
                return normalized
        except Exception:
            pass
    if lang == "nodejs":
        try:
            req = urllib.request.Request(
                f"https://registry.npmjs.org/{name}/latest",
                headers={"User-Agent": "pre_check_deps/1.0"},
            )
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            repo = data.get("repository", {})
            if isinstance(repo, dict):
                url = repo.get("url", "")
            else:
                url = str(repo)
            url = url.replace("git+", "").replace("git://", "https://")
            if url.startswith("github:"):
                url = "https://github.com/" + url[7:]
            normalized = normalize_upstream_candidate(url)
            if normalized and is_trusted_upstream_url(normalized):
                return normalized
        except Exception:
            pass
    return _github_search_repo(name)


def ensure_dependency_upstream(item: dict[str, Any], lang: str) -> tuple[str, str]:
    name = item.get("name") or item.get("dep") or ""
    existing_url = item.get("upstream_url", "") or ""
    suspicious_urls: list[str] = []

    normalized_existing = normalize_upstream_candidate(existing_url)
    if normalized_existing and is_trusted_upstream_url(normalized_existing):
        return normalized_existing, "provided"
    if existing_url:
        suspicious_urls.append(existing_url)
        if normalized_existing and not is_trusted_upstream_url(normalized_existing):
            suspicious_urls.append(normalized_existing)

    resolved = resolve_upstream_url(name, lang)
    if resolved and is_trusted_upstream_url(resolved):
        return resolved, "registry"
    if resolved:
        suspicious_urls.append(resolved)

    if lang == "python" and name:
        metadata_url = f"https://pypi.org/project/{name}"  # noqa: F841 — kept for future use

    return "", "unresolved"


def normalize_dependency_item(item: dict[str, Any], lang: str, category: str) -> dict[str, Any]:
    name = item.get("name") or item.get("dep") or ""
    upstream_url, upstream_resolution = ensure_dependency_upstream(item, lang)
    requirement = item.get("requirement", "") or item.get("constraint", "")
    raw_requirement_info = item.get("requirement_info")
    if not isinstance(raw_requirement_info, dict):
        raw_requirement_info = None
    constraint_type, requirement_info = classify_requirement_constraint(requirement, raw_requirement_info)
    version_source = infer_version_source({**item, "requirement_info": requirement_info})
    return {
        "name": name,
        "dep": item.get("dep") or name,
        "spec": item.get("spec") or item.get("dep") or name,
        "type": item.get("type") or lang,
        "category": category,
        "requirement": requirement,
        "constraint": requirement,
        "constraint_type": constraint_type,
        "version_source": version_source,
        "requirement_info": requirement_info,
        "rpm_requirement": item.get("rpm_requirement") or item.get("rpm_name") or item.get("dep") or name,
        "rpm_pkg_name": item.get("rpm_pkg_name") or get_rpm_pkg_name(lang, name),
        "upstream_url": upstream_url,
        "upstream_resolution": upstream_resolution,
    }


def classify_requirement_constraint(requirement: str, requirement_info: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """委托给 constraint_parser.parse_constraint，保留函数签名向后兼容。"""
    return _parse_constraint(requirement, requirement_info)


def infer_version_source(item: dict[str, Any], existing_check: dict[str, Any] | None = None) -> str:
    explicit_source = (item.get("version_source") or "").strip()
    if explicit_source:
        return explicit_source

    requirement_info = (item.get("requirement_info") or {}) if isinstance(item.get("requirement_info"), dict) else {}
    if requirement_info.get("source"):
        return str(requirement_info["source"]).strip() or "unknown"

    requested = dict((existing_check or {}).get("requested") or {})
    requested_requirement_info = requested.get("requirement_info")
    if isinstance(requested_requirement_info, dict) and requested_requirement_info.get("source"):
        return str(requested_requirement_info["source"]).strip() or "unknown"

    return "manifest" if (item.get("requirement") or "").strip() else "unknown"


def dependency_items_from_result(lang: str, result: dict[str, Any], pkgname: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (pending_items, preblocked_items)。

    preblocked_items: analyze 阶段已确认版本冲突（社区源有但版本低）的依赖，
                      携带 found_version，供 classify_dependency 直接决策，
                      无需再调用 check_existing_package。
    """
    pending: list[dict[str, Any]] = []
    preblocked: list[dict[str, Any]] = []

    if lang == "python":
        rpm_check = result.get("rpm_check") or {}
        # version_conflict 的包名集合，从 dependency_items 中排除，避免重复处理
        conflict_names = {item.get("name", "") for item in rpm_check.get("version_conflict", [])}
        for item in result.get("dependency_items", []):
            if item.get("name", "") not in conflict_names:
                pending.append(normalize_dependency_item(item, lang, "runtime"))
        for item in result.get("build_sys_dependency_items", []):
            # 跳过自引用：bootstrap 包（如 flit-core）build-backend 指向自身
            if pkgname and item.get("name", "") == pkgname:
                continue
            pending.append(normalize_dependency_item(item, lang, "build_system"))
        for item in rpm_check.get("version_conflict", []):
            norm = normalize_dependency_item(item, lang, "runtime")
            norm["found_version"] = item.get("found_version", "")
            norm["preblocked"] = True
            preblocked.append(norm)
        return pending, preblocked

    if lang == "cpp":
        for item in result.get("dependency_items", []):
            pending.append(normalize_dependency_item(item, lang, "runtime"))
        return pending, preblocked

    rpm_check = result.get("rpm_check") or {}
    for item in rpm_check.get("missing", []):
        pending.append(normalize_dependency_item(item, lang, "runtime"))
    for item in rpm_check.get("version_conflict", []):
        norm = normalize_dependency_item(item, lang, "runtime")
        norm["found_version"] = item.get("found_version", "")
        norm["preblocked"] = True
        preblocked.append(norm)

    # nodejs: 同时处理运行时 npm 依赖中未在社区源找到的包
    if lang == "nodejs":
        runtime_deps = result.get("runtime_deps") or {}
        for item in runtime_deps.get("missing", []):
            pending.append(normalize_dependency_item(item, lang, "runtime"))
        for item in runtime_deps.get("version_conflict", []):
            norm = normalize_dependency_item(item, lang, "runtime")
            norm["found_version"] = item.get("found_version", "")
            norm["preblocked"] = True
            preblocked.append(norm)

    return pending, preblocked


def build_available_index_for_result(lang: str, result: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    available_items: list[dict[str, Any]] = []
    rpm_check = result.get("rpm_check") or {}
    available_items.extend(rpm_check.get("available", []))
    if lang == "python":
        build_sys_check = result.get("build_sys_rpm_check") or {}
        available_items.extend(build_sys_check.get("available", []))
    if lang == "nodejs":
        runtime_deps = result.get("runtime_deps") or {}
        available_items.extend(runtime_deps.get("available", []))
    return build_source_index(available_items)


def classify_preblocked_dependency(dep: dict[str, Any], lang: str) -> dict[str, Any]:
    """处理 analyze 阶段已确认版本冲突的依赖（社区源有但版本低）。

    COPR 场景下官方源版本不满足要求时，直接引入更高版本到 AiRepo，
    不需要 compat 包机制（COPR 仓库与官方源叠加，不存在覆盖冲突）。
    """
    found_version = dep.get("found_version", "")
    requirement = dep.get("requirement", "")

    # 社区版本比要求版本更新且同主版本时，直接 reuse（requirements.txt == 精确锁版的误判修正）
    import re as _re
    req_ver_m = _re.search(r"[\d][0-9A-Za-z.+_~\-]*", requirement or "")
    req_ver_only = req_ver_m.group(0) if req_ver_m else ""
    if found_version and req_ver_only:
        off_major = found_version.split(".")[0]
        req_major = req_ver_only.split(".")[0]
        try:
            _cmp = (list(map(int, found_version.split("."))) > list(map(int, req_ver_only.split("."))))
        except ValueError:
            _cmp = False
        if _cmp and off_major == req_major:
            official_info = {
                "exists": True,
                "highest": {"version": found_version},
                "satisfies_requirement": True,
                "meets_need": True,
                "comparison_unknown": False,
            }
            return {
                **dep,
                "source_check": {"status": "ok", "satisfies_requirement": True},
                "existing_check": {
                    "official": official_info,
                    "decision": "reuse_official",
                    "reason": f"社区源版本 {found_version} 与要求版本 {req_ver_only} 同主版本且更新，直接复用",
                },
                "decision": "reuse_official",
                "action": "resolved",
                "reason": f"社区源版本 {found_version} 与要求版本 {req_ver_only} 同主版本且更新，直接复用",
            }

    reason_base = (
        f"社区仓库已存在同名包，但最高版本 {found_version or '未知版本'} "
        f"不满足要求（{requirement or '无版本约束'}），引入更高版本到 COPR"
    )

    # COPR 场景：不需要 compat 包，直接引入更高版本
    if dep.get("upstream_url"):
        action = "recurse"
        reason = reason_base + "（官方源版本不满足要求，将引入更高版本到 COPR）"
    else:
        action = "needs_ai"
        reason = reason_base + "（需 AI web search 补全 upstream URL 后引入）"

    official_info = {
        "exists": True,
        "highest": {"version": found_version} if found_version else None,
        "satisfies_requirement": False,
        "meets_need": False,
        "comparison_unknown": False,
    }
    existing_check = {
        "official": official_info,
        "decision": "block_official_older",
        "reason": reason_base,
    }

    return {
        **dep,
        "source_check": {"status": "older", "satisfies_requirement": False},
        "existing_check": existing_check,
        "decision": "block_official_older",
        "action": action,
        "reason": reason,
    }


def classify_dependency(dep: dict[str, Any], lang: str, source_index: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    source_item = lookup_source_item(dep, source_index)
    source_check = summarize_source_match(dep, source_item)

    original_requirement_info = dep.get("requirement_info") if isinstance(dep.get("requirement_info"), dict) else None
    dep["constraint_type"], dep["requirement_info"] = classify_requirement_constraint(
        dep.get("constraint") or dep.get("requirement", ""),
        original_requirement_info,
    )

    debug_flow = {
        "name": dep.get("name") or dep.get("dep") or "",
        "before": {
            "constraint": dep.get("constraint") or dep.get("requirement", ""),
            "constraint_type": dep.get("constraint_type", "unknown"),
            "requirement_info": dep.get("requirement_info", {}),
        },
    }

    if source_check["satisfies_requirement"]:
        debug_flow["after"] = {
            "constraint_type": dep.get("constraint_type", "unknown"),
            "requirement_info": dep.get("requirement_info", {}),
            "decision": "reuse_source",
        }
        return {
            **dep,
            "source_check": source_check,
            "existing_check": None,
            "decision": "reuse_source",
            "action": "resolved",
            "reason": source_check["reason"],
            "debug_constraint_flow": debug_flow,
        }

    existing_check = EXISTING_CHECKER.check_existing_package(
        dep["name"],
        requirement=dep.get("requirement", ""),
        lang=lang,
    )
    requested = dict(existing_check.get("requested") or {})
    requested_requirement_info = requested.get("requirement_info")
    if isinstance(requested_requirement_info, dict):
        dep["requirement_info"] = requested_requirement_info
        dep["constraint_type"], dep["requirement_info"] = classify_requirement_constraint(
            dep.get("constraint") or dep.get("requirement", ""),
            requested_requirement_info,
        )
        dep["version_source"] = infer_version_source(dep, existing_check)
    elif dep.get("requirement"):
        dep["constraint_type"], dep["requirement_info"] = classify_requirement_constraint(
            dep.get("constraint") or dep.get("requirement", ""),
            dep.get("requirement_info") if isinstance(dep.get("requirement_info"), dict) else None,
        )
    if source_check["status"] == "older" and not existing_check.get("official", {}).get("exists"):
        existing_check = merge_official_source_older_result(dep, source_check, existing_check)
    decision = existing_check["decision"]
    if decision in {"reuse_official", "reuse_user_repo"}:
        action = "resolved"
        reason = existing_check["reason"]
    elif decision == "block_official_older":
        conflict_mode = _dep_conflict_mode()
        _COMPAT_SUPPORTED_LANGS = {"c", "cpp", "java"}
        can_compat = (
            (conflict_mode == "compat" and lang in _COMPAT_SUPPORTED_LANGS)
            or conflict_mode == "force_compat"
        )
        if can_compat:
            found_ver = existing_check.get("official", {}).get("highest", {}).get("version", "") or ""
            major = extract_compat_major_version(found_ver)
            compat_srpm = get_compat_srpm_name(lang, dep.get("name", ""), major)
            compat_rpm = get_compat_rpm_pkg_name(lang, dep.get("name", ""), major)
            if dep.get("upstream_url"):
                action = "recurse"
                reason = existing_check["reason"] + f"（compat 模式：将以 compat 包名 {compat_rpm} 引入新版本）"
                dep = {**dep, "compat_introduce": True, "compat_srpm_name": compat_srpm, "compat_rpm_name": compat_rpm}
            else:
                action = "needs_ai"
                reason = existing_check["reason"] + f"（compat 模式：需 AI web search 补全 upstream URL 后以 {compat_rpm} 引入）"
                dep = {**dep, "compat_introduce": True, "compat_srpm_name": compat_srpm, "compat_rpm_name": compat_rpm}
        else:
            action = "blocked"
            if conflict_mode in ("compat", "force_compat") and lang not in _COMPAT_SUPPORTED_LANGS and conflict_mode != "force_compat":
                reason = existing_check["reason"] + f"（{lang} 包文件路径不含版本号，不支持 compat 共存）"
            else:
                reason = existing_check["reason"]
    else:
        if not dep.get("upstream_url"):
            action = "needs_ai"
            reason = "无法确定依赖上游源码仓库地址，需 AI web search 补全"
        else:
            action = "recurse"
            reason = existing_check["reason"]

    debug_flow["after"] = {
        "constraint_type": dep.get("constraint_type", "unknown"),
        "requirement_info": dep.get("requirement_info", {}),
        "decision": decision,
        "action": action,
    }

    return {
        **dep,
        "source_check": source_check,
        "existing_check": existing_check,
        "decision": decision,
        "action": action,
        "reason": reason,
        "debug_constraint_flow": debug_flow,
    }


def build_summary(pkgname: str, lang: str, source_dir: str, analysis_file: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "pkgname": pkgname,
        "lang": lang,
        "source_dir": source_dir,
        "analysis_file": analysis_file,
        "dependency_decisions": decisions,
        "resolved": [item for item in decisions if item["action"] == "resolved"],
        "pending": [item for item in decisions if item["action"] == "recurse"],
        "needs_ai": [item for item in decisions if item["action"] == "needs_ai"],
        "blocked": [item for item in decisions if item["action"] == "blocked"],
    }


def print_pending_to_stdout(pending: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str]] = set()
    for item in pending:
        key = (item["name"], item.get("upstream_url", ""))
        if key in seen:
            continue
        seen.add(key)
        print(f"{item['name']} {item.get('upstream_url', '')}".rstrip())


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RPM 编译前依赖预检")
    parser.add_argument("pkgname", help="包名")
    parser.add_argument("lang", help="语言：python/go/rust/c/cpp/nodejs/java/ruby")
    parser.add_argument("source_dir", help="源码目录（绝对路径）")
    parser.add_argument("-o", "--output", default="", help="JSON 结果输出路径")
    args = parser.parse_args()

    lang = args.lang.lower()
    if lang not in ANALYZERS:
        print(f"[WARN] 不支持的语言 {lang}，跳过预检", file=sys.stderr)
        sys.exit(0)

    cfg = ANALYZERS[lang]
    script = SCRIPT_DIR / cfg["script"]
    if not script.exists():
        print(f"[WARN] 分析脚本不存在: {script}，跳过预检", file=sys.stderr)
        sys.exit(0)

    out_file = make_output_path(args.pkgname, args.output)
    analysis_path = make_analysis_path(out_file, args.pkgname)

    # ── vendor 语言早退：Go/Rust 永远 vendor，跳过语言级依赖存在性检查 ──────────
    # 构建环境离线，这两种语言没有"不 vendor"的场景。
    # 系统库依赖（CGO、-sys crate）由 rpmbuild 循环兜底（报 missing header → 补 BuildRequires）。
    VENDOR_LANGS = {"go", "rust"}
    if lang in VENDOR_LANGS:
        output_path = Path(out_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        summary = build_summary(args.pkgname, lang, args.source_dir, "", [])
        summary["vendor_mode"] = True
        output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[pre_check] {lang} vendor 模式，跳过语言级依赖检查", file=sys.stderr)
        sys.exit(0)

    # ── Node.js vendor 阈值判断：依赖多时自动切换 vendor 模式 ────────────────────
    # 先做纯静态分析（不查 RPM 源），用 package.json dependencies 总数作为上界。
    # 大部分 npm 包不在 openEuler 社区源，依赖多意味着 missing 也多，vendor 更经济。
    NODEJS_VENDOR_THRESHOLD = 10
    if lang == "nodejs":
        static_cmd = [resolve_python_executable(), str(script), args.source_dir, "-o", str(analysis_path)]
        subprocess.run(static_cmd, capture_output=False)
        try:
            static_result = load_json(analysis_path)
        except Exception:
            static_result = {}

        # dependencies 总数是 missing 的保守上界（不需要查 RPM 源）
        deps_count = len(static_result.get("dependencies", {}))
        source_path = Path(args.source_dir)
        has_lockfile = (source_path / "package-lock.json").exists() or (source_path / "yarn.lock").exists()

        if deps_count > NODEJS_VENDOR_THRESHOLD:
            output_path = Path(out_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if not has_lockfile:
                print(f"[pre_check] nodejs: {deps_count} 个依赖 > 阈值 {NODEJS_VENDOR_THRESHOLD} 但无 lockfile，无法确定性 vendor", file=sys.stderr)
                summary = build_summary(args.pkgname, lang, args.source_dir, str(analysis_path), [])
                summary["blocked"] = [{"name": args.pkgname, "reason": f"{deps_count} npm deps declared but no lockfile (package-lock.json/yarn.lock), cannot vendor deterministically"}]
                output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                sys.exit(1)
            print(f"[pre_check] nodejs: {deps_count} 个依赖 > 阈值 {NODEJS_VENDOR_THRESHOLD}，切换 vendor 模式", file=sys.stderr)
            summary = build_summary(args.pkgname, lang, args.source_dir, str(analysis_path), [])
            summary["vendor_mode"] = True
            output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            sys.exit(0)
        # deps <= THRESHOLD → 走原有 --check-rpm 路径（查实际 missing 数量）
        print(f"[pre_check] nodejs: {deps_count} 个依赖 <= 阈值 {NODEJS_VENDOR_THRESHOLD}，走 RPM-native 路径", file=sys.stderr)

    cmd = [resolve_python_executable(), str(script), args.source_dir, "--check-rpm", "-o", str(analysis_path)]
    if lang == "python":
        cmd += ["--pkg", args.pkgname]
        copr_chroot = os.environ.get("COPR_CHROOT", "")
        if copr_chroot:
            cmd += ["--chroot", copr_chroot]

    print(f"[pre_check] 运行: {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=False)
    if proc.returncode not in (0, 2):
        print(f"[ERROR] 依赖分析脚本执行失败，退出码: {proc.returncode}", file=sys.stderr)
        sys.exit(1)

    try:
        result = load_json(analysis_path)
    except Exception as e:
        print(f"[ERROR] 无法读取分析结果: {e}", file=sys.stderr)
        sys.exit(1)

    dependency_items, preblocked_items = dependency_items_from_result(lang, result, args.pkgname)
    source_index = build_available_index_for_result(lang, result)

    # preblocked_items: analyze 阶段已确认版本冲突，直接决策，不再调用 check_existing_package
    preblocked_decisions = [classify_preblocked_dependency(dep, lang) for dep in preblocked_items]
    # 其余依赖走完整的 classify_dependency 流程
    decisions = preblocked_decisions + [classify_dependency(dep, lang, source_index) for dep in dependency_items]
    summary = build_summary(args.pkgname, lang, args.source_dir, str(analysis_path), decisions)

    output_path = Path(out_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    blocked = summary["blocked"]
    pending = summary["pending"]
    resolved = summary["resolved"]

    print(f"[pre_check] 已解决 {len(resolved)} 个依赖，待递归 {len(pending)} 个，阻断 {len(blocked)} 个", file=sys.stderr)

    if blocked:
        for item in blocked:
            print(f"[BLOCK] {item['name']}: {item['reason']}", file=sys.stderr)
        sys.exit(1)

    if not pending:
        print("[pre_check] 所有依赖均已满足或可复用", file=sys.stderr)
        sys.exit(0)

    print(f"[pre_check] 发现 {len(pending)} 个需递归处理的依赖：", file=sys.stderr)
    for item in pending:
        print(f"  - {item['name']}  {item.get('upstream_url', '')}  [{item['decision']}]", file=sys.stderr)
    print_pending_to_stdout(pending)
    sys.exit(2)


if __name__ == "__main__":
    main()
