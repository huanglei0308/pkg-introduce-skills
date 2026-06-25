#!/usr/bin/env python3
"""
上游仓库合规检查脚本

检查两项：
  1. 主流平台：上游 URL 必须来自已知主流 Git 代码托管平台
  2. 活跃度  ：仓库在最近 5 年内（1825 天）必须有过更新

支持平台：
  GitHub / GitLab / Gitee / Gitcode+AtomGit（同平台）/ Bitbucket

注意：Gitcode 和 AtomGit 是同一平台，共用 /api/v5 接口和 config.json 中的 token。

用法：
  python3 check_repo.py <upstream_url>
  python3 check_repo.py <upstream_url> -o result.json
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from urllib.parse import urlparse


INACTIVE_DAYS = 365 * 5  # 默认 5 年，可通过 config.json repo_check.inactive_days_threshold 覆盖

# 域名 → (平台类型, 人读名称)
# gitcode.com 和 atomgit.com 是同一平台，映射到同一个 checker
SUPPORTED_PLATFORMS = {
    "github.com":    ("github",    "GitHub"),
    "gitlab.com":    ("gitlab",    "GitLab"),
    "gitee.com":     ("gitee",     "Gitee"),
    "gitcode.com":   ("gitcode",   "Gitcode / AtomGit"),
    "atomgit.com":   ("gitcode",   "Gitcode / AtomGit"),
    "bitbucket.org": ("bitbucket", "Bitbucket"),
}


def _load_config() -> dict:
    """从 config.json 读取完整配置。"""
    config_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_token() -> str | None:
    """从 config.json 读取 Gitcode PRIVATE-TOKEN。"""
    return _load_config().get("gitcode", {}).get("token")


def _http_get(url: str, token: str | None = None, timeout: int = 10) -> dict | list | None:
    headers = {"User-Agent": "import-package-checker/1.0", "Accept": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _days_since(iso_str: str) -> int | None:
    """计算从 ISO 时间字符串到现在的天数。"""
    if not iso_str:
        return None
    iso_str = iso_str.rstrip("Z").split(".")[0]
    try:
        dt = datetime.fromisoformat(iso_str).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).days


def _parse_owner_repo(path: str) -> tuple[str, str] | None:
    parts = path.strip("/").split("/")
    if len(parts) < 2:
        return None
    return parts[0], parts[1].removesuffix(".git")


# ── 各平台活跃度查询 ───────────────────────────────────────────────────────

def _check_github(parsed) -> tuple[int | None, str]:
    pr = _parse_owner_repo(parsed.path)
    if not pr:
        return None, "无法解析 owner/repo"
    owner, repo = pr
    data = _http_get(f"https://api.github.com/repos/{owner}/{repo}")
    if not data or "pushed_at" not in data:
        return None, "API 请求失败或仓库不存在"
    return _days_since(data["pushed_at"]), data["pushed_at"][:10]


def _check_gitlab(parsed) -> tuple[int | None, str]:
    path = parsed.path.strip("/").removesuffix(".git")
    encoded = urllib.parse.quote(path, safe="")
    data = _http_get(f"https://gitlab.com/api/v4/projects/{encoded}")
    if not data or "last_activity_at" not in data:
        return None, "API 请求失败或项目不存在"
    return _days_since(data["last_activity_at"]), data["last_activity_at"][:10]


def _check_gitee(parsed) -> tuple[int | None, str]:
    pr = _parse_owner_repo(parsed.path)
    if not pr:
        return None, "无法解析 owner/repo"
    owner, repo = pr
    data = _http_get(f"https://gitee.com/api/v5/repos/{owner}/{repo}")
    if not data or "pushed_at" not in data:
        return None, "API 请求失败或仓库不存在"
    return _days_since(data["pushed_at"]), data["pushed_at"][:10]


def _check_gitcode(parsed) -> tuple[int | None, str]:
    """Gitcode 与 AtomGit 同平台，均使用 /api/v5/repos/ 端点，token 来自 config.json。"""
    pr = _parse_owner_repo(parsed.path)
    if not pr:
        return None, "无法解析 owner/repo"
    owner, repo = pr
    token = _load_token()
    host = parsed.netloc  # gitcode.com 或 atomgit.com，两个域名同一套 API
    data = _http_get(f"https://{host}/api/v5/repos/{owner}/{repo}", token=token)
    if not data or "pushed_at" not in data:
        return None, "API 请求失败或仓库不存在（请确认 config.json 中 token 已配置）"
    return _days_since(data["pushed_at"]), data["pushed_at"][:10]


def _check_bitbucket(parsed) -> tuple[int | None, str]:
    pr = _parse_owner_repo(parsed.path)
    if not pr:
        return None, "无法解析 workspace/repo"
    workspace, repo = pr
    data = _http_get(f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}")
    if not data or "updated_on" not in data:
        return None, "API 请求失败或仓库不存在"
    return _days_since(data["updated_on"]), data["updated_on"][:10]


CHECKERS = {
    "github":    _check_github,
    "gitlab":    _check_gitlab,
    "gitee":     _check_gitee,
    "gitcode":   _check_gitcode,
    "bitbucket": _check_bitbucket,
}


# ── 主逻辑 ────────────────────────────────────────────────────────────────

def check_repo(upstream_url: str) -> dict:
    parsed = urlparse(upstream_url)
    host = parsed.netloc.lower().removeprefix("www.")

    # ── 1. 平台白名单 ──
    platform = SUPPORTED_PLATFORMS.get(host)
    if not platform:
        return {
            "platform_name": host,
            "platform_type": "unknown",
            "is_mainstream": False,
            "days_inactive": None,
            "last_updated": "N/A",
            "blocking": True,
            "message": (
                f"{host} 不在支持的平台列表中"
                "（仅支持 GitHub / GitLab / Gitee / Gitcode / AtomGit / Bitbucket），阻断"
            ),
        }

    ptype, pname = platform

    # ── 2. 活跃度 ──
    days, detail = CHECKERS[ptype](parsed)

    if days is None:
        return {
            "platform_name": pname,
            "platform_type": ptype,
            "is_mainstream": True,
            "days_inactive": None,
            "last_updated": detail,
            "blocking": False,
            "message": f"{pname} 平台活跃度查询失败（{detail}），请人工确认",
        }

    # 从 config.json 读取阈值和 blocking 开关，兜底使用常量默认值
    cfg = _load_config().get("repo_check", {})
    inactive_threshold = int(cfg.get("inactive_days_threshold", INACTIVE_DAYS))
    blocking_enabled   = bool(cfg.get("blocking", True))

    stale    = days > inactive_threshold
    blocking = stale and blocking_enabled
    years, days_rem = divmod(days, 365)
    threshold_years, threshold_days_rem = divmod(inactive_threshold, 365)

    if stale and blocking:
        msg = f"{pname} 仓库已 {years} 年 {days_rem} 天未更新，超过 {threshold_years} 年阈值，疑似废弃，阻断"
    elif stale and not blocking:
        msg = f"{pname} 仓库已 {years} 年 {days_rem} 天未更新，超过 {threshold_years} 年阈值，但 blocking=false，仅警告"
    elif years >= 1:
        msg = f"{pname} 仓库 {years} 年 {days_rem} 天未更新，仍在阈值内，通过"
    else:
        msg = f"{pname} 仓库 {days} 天前有更新，活跃，通过"

    return {
        "platform_name": pname,
        "platform_type": ptype,
        "is_mainstream": True,
        "days_inactive": days,
        "last_updated": detail,
        "blocking": blocking,
        "message": msg,
    }


def print_report(result: dict, url: str = "") -> None:
    if result["blocking"]:
        status = "❌ 阻断"
    elif result["days_inactive"] is None:
        status = "⚠️  警告"
    else:
        status = "✅ 通过"

    print("\n仓库合规检查报告")
    if url:
        print(f"URL       : {url}")
    print("─" * 50)
    print(f"状态      : {status}")
    print(f"平台      : {result['platform_name']}"
          + ("（非主流）" if not result["is_mainstream"] else ""))
    print(f"最后更新  : {result['last_updated']}")
    if result["days_inactive"] is not None:
        print(f"距今天数  : {result['days_inactive']} 天")
    print(f"说明      : {result['message']}")
    print("─" * 50)


def main() -> None:
    parser = argparse.ArgumentParser(description="检查上游仓库是否主流且活跃")
    parser.add_argument("url", help="上游仓库 URL")
    parser.add_argument("-o", "--output", help="输出 JSON 文件路径")
    args = parser.parse_args()

    result = check_repo(args.url)
    print_report(result, args.url)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"结果已写入：{args.output}")

    sys.exit(1 if result["blocking"] else 0)


if __name__ == "__main__":
    main()
