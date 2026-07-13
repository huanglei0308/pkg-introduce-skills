#!/usr/bin/env python3
"""4 级级联包存在性检查。

在 evaluate 阶段调用，统一替换独立的 check_existing_package.py（Level 2）
和 fetch_reference_spec.py（Level 3）查询。

4 级查找：
  Level 1 — EUR Repo (https://eur.openeuler.openatom.cn)
     fulltext search → 扫描 results 目录 → 下载 SRPM 重建
  Level 2 — openEuler 目标版本 (dnf repoquery)
     目标版本有匹配包 → 直接复用
  Level 3 — src-openeuler 源仓库 (gitcode.com)
     git ls-remote → clone 提取 spec/yaml/patches → 作为参考源
  Level 4 — 全新包
     所有来源都没有 → 从头构建

输出 check_result.json：
  {
    "pkgname": "snappy",
    "level": 2,
    "decision": "reuse_official",
    "match": { ... },
    "reference": null
  }

用法：
  python3 cascade_package_check.py <pkgname> --lang <lang> --target <version>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import shutil
from pathlib import Path
from typing import Optional

# ── 常量 ────────────────────────────────────────────────────────────────────────
EUR_BASE = "https://eur.openeuler.openatom.cn"
EUR_RESULTS = f"{EUR_BASE}/results"
EUR_FULLTEXT = f"{EUR_BASE}/coprs/fulltext/"
GITCODE_HOST = "gitcode.com"
PKG_NAMESPACE = "src-openeuler"
LS_REMOTE_TIMEOUT = 10
RESULTS_SCAN_TIMEOUT = 15

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from rpm_naming import get_rpm_pkg_name  # noqa: E402
import check_existing_package as _checker  # noqa: E402


# ── Level 1: EUR fulltext search ────────────────────────────────────────────────

def _eur_fulltext_search(pkgname: str) -> list[dict[str, str]]:
    """用 EUR fulltext search 查找包含目标包的 project 列表。

    返回列表格式：[{"owner": "lynlon", "project": "nginx"}, ...]
    """
    params = urllib.parse.urlencode({"fulltext": pkgname, "packagename": pkgname})
    url = f"{EUR_FULLTEXT}?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "check_package_existence/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return []

    # 解析 HTML 中的 project 链接
    projects = []
    seen = set()
    for m in re.finditer(r'href="/coprs/([^/"]+)/([^/"]+)/"', html):
        owner = m.group(1)
        project_name = m.group(2)
        key = (owner, project_name)
        if key not in seen:
            seen.add(key)
            projects.append({"owner": owner, "project": project_name})

    return projects


def _eur_pkgname_matches(build_dir_name: str, pkgname: str) -> bool:
    """检查 build 目录中的包名是否与目标包匹配。

    上游包名可能与 RPM 包名不同（如 requests vs python-requests），
    使用包含匹配（考虑到连字符分隔的包名）。
    """
    bd = build_dir_name.lower().replace("_", "-")
    pn = pkgname.lower().replace("_", "-")
    if bd == pn:
        return True
    # 模糊匹配：python-requests 匹配 requests, nodejs-lodash 匹配 lodash
    parts = bd.split("-")
    return pn in parts or any(p.startswith(pn) or pn.startswith(p) for p in parts if len(p) >= 3)


def _scan_eur_results(projects: list[dict[str, str]], pkgname: str,
                      target_chroot: str = "", target_version: str = "") -> Optional[dict]:
    """扫描 EUR project 的 results 目录，匹配包名。

    返回命中的第一个匹配结果，含 srpm_url / binary_rpm_url / version / chroot。
    若 target_version 指定，EUR 版本必须 >= 目标版本才返回命中。
    """
    for proj in projects:
        owner = proj["owner"]
        project_name = proj["project"]
        results_url = f"{EUR_RESULTS}/{owner}/{project_name}/"

        # 遍历 results 下的 chroot 目录
        try:
            req = urllib.request.Request(results_url, headers={"User-Agent": "check_package_existence/1.0"})
            with urllib.request.urlopen(req, timeout=RESULTS_SCAN_TIMEOUT) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception:
            continue

        chroot_dirs = re.findall(r'href="([^"]+/)"', html)
        for chroot_dir in chroot_dirs:
            chroot = chroot_dir.rstrip("/")
            if not chroot or chroot.startswith(".."):
                continue

            chroot_url = f"{results_url}{chroot}/"
            try:
                req = urllib.request.Request(chroot_url, headers={"User-Agent": "check_package_existence/1.0"})
                with urllib.request.urlopen(req, timeout=RESULTS_SCAN_TIMEOUT) as resp:
                    chroot_html = resp.read().decode("utf-8", errors="ignore")
            except Exception:
                continue

            # 解析构建目录：<build_id>-<pkgname>/
            build_dirs = re.findall(r'href="(\d+-[^/"]+/)"', chroot_html)
            for build_dir in build_dirs:
                build_dir_clean = build_dir.rstrip("/")
                # 提取 build 目录中的包名（去掉 build_id 前缀）
                parts = build_dir_clean.split("-", 1)
                if len(parts) < 2:
                    continue
                build_pkgname = parts[1]

                if not _eur_pkgname_matches(build_pkgname, pkgname):
                    continue

                # 进入 build 目录，列出文件
                build_url = f"{chroot_url}{build_dir}"
                try:
                    req = urllib.request.Request(build_url, headers={"User-Agent": "check_package_existence/1.0"})
                    with urllib.request.urlopen(req, timeout=RESULTS_SCAN_TIMEOUT) as resp:
                        build_html = resp.read().decode("utf-8", errors="ignore")
                except Exception:
                    continue

                # 找 SRPM 和二进制 RPM
                rpm_files = re.findall(r'href="([^"]+\.rpm)"', build_html)
                srpm_files = [f for f in rpm_files if f.endswith(".src.rpm")]
                binary_files = [f for f in rpm_files if not f.endswith(".src.rpm")]

                # 从 SRPM 文件名提取版本（<name>-<version>-<release>.src.rpm）
                version = None
                srpm_url = None
                if srpm_files:
                    srpm = srpm_files[0]
                    srpm_url = f"{build_url}{srpm}"
                    # 正则从末尾匹配：<version>-<release>.src.rpm
                    ver_match = re.match(
                        r'.+-(\d[\d\w.]*)-(\d[\d\w.]*)\.src\.rpm$', srpm
                    )
                    if ver_match:
                        version = ver_match.group(1)

                binary_urls = [f"{build_url}{f}" for f in binary_files] if binary_files else []

                match_info = {
                    "level": 1,
                    "eur_owner": owner,
                    "eur_project": project_name,
                    "srpm_url": srpm_url,
                    "srpm_file": srpm_files[0] if srpm_files else None,
                    "binary_rpm_urls": binary_urls,
                    "binary_rpm_files": binary_files,
                    "version": version,
                    "chroot": chroot,
                }

                # 版本匹配检查：EUR 版本必须 >= 目标版本
                if target_version and version:
                    try:
                        if _checker.compare_versions(version, target_version) < 0:
                            continue  # EUR 版本太低，继续搜下一个
                    except Exception:
                        pass  # 版本比较失败不阻塞

                if binary_files or srpm_files:
                    match_info["decision"] = "reuse_eur_srpm"
                else:
                    continue

                return match_info

    return None


# ── Level 2: openEuler 目标版本 ─────────────────────────────────────────────────

def _check_target_version(pkgname: str, lang: str, target: str, version: str,
                          requirement: str) -> Optional[dict]:
    """用 dnf repoquery 查目标版本是否有包。复用 check_existing_package.py 逻辑。"""
    result = _checker.check_existing_package(
        pkgname,
        version=version,
        requirement=requirement,
        lang=lang,
        chroot=target,
    )
    official = result.get("official", {})
    if official.get("exists"):
        highest = official.get("highest", {})
        return {
            "level": 2,
            "decision": "reuse_official" if official.get("meets_need") else "evaluate",
            "rpm_name": highest.get("name", ""),
            "version": highest.get("version", ""),
            "source": f"openEuler {target}",
            "reason": result.get("reason", ""),
        }
    return None


# ── Level 3: gitcode src-openeuler ──────────────────────────────────────────────

def _build_gitcode_candidates(pkgname: str, lang: str) -> list[str]:
    """生成 gitcode src-openeuler 仓库名候选列表。

    命名规则与 RPM 包名一致，复用 rpm_naming.py 映射。
    """
    candidates = [pkgname]

    lang_lower = (lang or "").lower()
    if lang_lower == "python":
        candidates.extend([f"python-{pkgname}", f"python3-{pkgname}"])
    elif lang_lower == "nodejs":
        candidates.append(f"nodejs-{pkgname}")
    elif lang_lower in ("c", "cpp"):
        # C/C++ 通常用上游名
        pass
    elif lang_lower == "go":
        # Go 通常用 golang-<full-module-path>，但 pkgname 可能已经是完整路径
        if not pkgname.startswith("golang-"):
            candidates.append(f"golang-{pkgname}")
    elif lang_lower == "rust":
        if not pkgname.startswith("rust-"):
            candidates.append(f"rust-{pkgname}")

    # 去重保持顺序
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _git_ls_remote(pkgname: str) -> Optional[bool]:
    """检查 gitcode repo 是否存在。返回 True/False/None(network_error)。"""
    url = f"https://{GITCODE_HOST}/{PKG_NAMESPACE}/{pkgname}.git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", url],
            capture_output=True, text=True, timeout=LS_REMOTE_TIMEOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode == 0:
            return True
        stderr = result.stderr.lower()
        if any(kw in stderr for kw in ("not found", "could not read",
                                        "repository not found", "403", "404")):
            return False
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def _clone_and_extract(pkgname: str, output_dir: Path) -> bool:
    """Shallow clone src-openeuler 仓库并提取 spec/yaml/patches。"""
    # 复用 fetch_reference_spec.py 的提取逻辑
    from fetch_reference_spec import _clone_and_extract as _clone_extract
    # fetch_reference_spec 里的 _clone_and_extract 接受的是 gitcode 上的 repo 名
    return _clone_extract(pkgname, output_dir)


def _check_src_openeuler(pkgname: str, lang: str) -> Optional[dict]:
    """查 gitcode.com/src-openeuler 仓库是否存在。"""
    candidates = _build_gitcode_candidates(pkgname, lang)
    for candidate in candidates:
        exists = _git_ls_remote(candidate)
        if exists is True:
            return {
                "level": 3,
                "decision": "introduce_new_with_ref",
                "gitcode_repo": f"https://{GITCODE_HOST}/{PKG_NAMESPACE}/{candidate}.git",
                "repo_name": candidate,
            }
        elif exists is None:
            # 网络错误，继续尝试下一个候选
            continue
        # exists is False → 尝试下一个候选
    return None


# ── Level 0: 用户 COPR project ─────────────────────────────────────────────────

def _check_user_copr_project(pkgname: str, copr_url: str, owner: str,
                              project: str, login: str, token: str) -> Optional[dict]:
    """检查用户自己的 COPR project 是否已有此包（避免重复构建）。"""
    if not (copr_url and owner and project and login and token):
        return None

    import base64
    creds = base64.b64encode(f"{login}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}"}

    params = urllib.parse.urlencode({
        "ownername": owner,
        "projectname": project,
        "packagename": pkgname,
        "limit": "10",
    })
    url = f"{copr_url.rstrip('/')}/api_3/build/list?{params}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("items", [])
        best = None
        for build in items:
            if build.get("state") != "succeeded":
                continue
            ver = build.get("source_package", {}).get("version", "")
            if ver and (best is None or _checker.compare_versions(ver, best["version"]) > 0):
                best = {"name": pkgname, "version": ver}
        if best:
            return {
                "level": 0,
                "decision": "reuse_copr_project",
                "rpm_name": pkgname,
                "version": best["version"],
                "source": f"{owner}/{project}",
            }
    except Exception:
        pass
    return None


# ── 主入口 ──────────────────────────────────────────────────────────────────────

def check_package_existence(
    pkgname: str,
    lang: str = "",
    version: str = "",
    requirement: str = "",
    target: str = "",
    copr_url: str = "",
    copr_owner: str = "",
    copr_project: str = "",
    copr_login: str = "",
    copr_token: str = "",
) -> dict:
    """4 级级联查找包的处置策略。

    Args:
        pkgname: 上游包名
        lang: 语言（python/go/rust/c/cpp/nodejs/java）
        version: 目标版本号
        requirement: 版本约束（如 >= 1.0）
        target: 目标 openEuler 版本（如 openEuler-24.03-LTS-SP3）
        copr_url / copr_owner / copr_project / copr_login / copr_token:
            用户 COPR 凭据，用于 L0 检查自己的 project 是否已有此包。

    Returns:
        {
            "pkgname": str,
            "level": int (0-4),
            "decision": str,
            "match": { ... } | None,
            "reference": { ... } | None,
        }
    """
    result: dict = {
        "pkgname": pkgname,
        "level": 4,
        "decision": "introduce_new",
        "match": None,
        "reference": None,
    }

    # ── Level 0: 用户 COPR project ──────────────────────────────────────────
    user_result = _check_user_copr_project(
        pkgname, copr_url, copr_owner, copr_project, copr_login, copr_token
    )
    if user_result:
        result.update(user_result)
        return result

    # ── Level 1: EUR fulltext search ─────────────────────────────────────────
    eur_projects = _eur_fulltext_search(pkgname)
    if eur_projects:
        eur_match = _scan_eur_results(eur_projects, pkgname, target_chroot=target, target_version=version)
        if eur_match:
            result["level"] = 1
            result["decision"] = "reuse_eur_srpm"
            result["match"] = eur_match
            return result

    # ── Level 2: openEuler 目标版本 ─────────────────────────────────────────
    target_match = _check_target_version(
        pkgname, lang, target, version, requirement
    )
    if target_match:
        result.update(target_match)
        return result

    # ── Level 3: gitcode src-openeuler ──────────────────────────────────────
    gitcode_match = _check_src_openeuler(pkgname, lang)
    if gitcode_match:
        result.update(gitcode_match)
        return result

    # ── Level 4: 全新包 ─────────────────────────────────────────────────────
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="4 级级联包存在性检查"
    )
    parser.add_argument("pkgname", help="包名")
    parser.add_argument("--lang", default="", help="语言：python/go/rust/c/cpp/nodejs/java")
    parser.add_argument("--version", default="", help="目标版本号")
    parser.add_argument("--requirement", default="", help="版本约束，如 >= 1.0")
    parser.add_argument("--target", default="",
                        help="目标 openEuler 版本，如 openEuler-24.03-LTS-SP3")
    parser.add_argument("-o", "--output", default="", help="输出 JSON 文件路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    args = parser.parse_args()

    result = check_package_existence(
        args.pkgname,
        lang=args.lang.strip().lower(),
        version=args.version.strip(),
        requirement=args.requirement.strip(),
        target=args.target.strip(),
    )

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if args.json or not args.output:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    # 退出码：0=命中（L1/L2），3=有参考源（L3），4=全新（L4）
    if result["decision"] in ("reuse_eur_srpm", "reuse_official"):
        return 0
    elif result["decision"] == "introduce_new_with_ref":
        return 3
    else:
        return 4


if __name__ == "__main__":
    sys.exit(main())
