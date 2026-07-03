#!/usr/bin/env python3
"""向 dep_registry.json 注册一个需要引入的依赖。

用法：
  python3 register-dep.py --session-dir . --pkg meson \
    --url https://github.com/mesonbuild/meson \
    --constraint ">= 1.4.0" \
    --required-by python-numpy
"""
import argparse
import json
import re
import sys
from pathlib import Path

# 可信的 git 仓库主机
_TRUSTED_GIT_HOSTS = (
    "github.com",
    "gitlab.com",
    "gitee.com",
    "codeberg.org",
    "bitbucket.org",
    "salsa.debian.org",
    "pagure.io",
    "git.sr.ht",
)

# 明确不是 git 仓库的 URL 特征
_NON_REPO_PATTERNS = re.compile(
    r"(pypi\.org|npmjs\.com|crates\.io|rubygems\.org"
    r"|mesonbuild\.com|cmake\.org|gnu\.org/software"
    r"|readthedocs|docs\.|wiki\.|/releases/download/)"
)


def is_git_repo_url(url: str) -> tuple[bool, str]:
    """检查 URL 是否是可 git clone 的仓库地址。

    返回 (ok, reason)。
    """
    if not url:
        return False, "URL 为空"

    # 拒绝明显不是 git 仓库的地址
    if _NON_REPO_PATTERNS.search(url):
        return False, f"URL 看起来是官网/文档/包注册表，不是 git 仓库: {url}"

    # 必须是 https:// 或 http://
    if not (url.startswith("https://") or url.startswith("http://")):
        return False, f"URL 必须以 https:// 或 http:// 开头: {url}"

    # 提取主机名
    try:
        host = url.split("//", 1)[1].split("/")[0].lower()
        path = "/".join(url.split("//", 1)[1].split("/")[1:])
    except IndexError:
        return False, f"URL 格式无效: {url}"

    # 检查主机是否可信
    trusted = any(host == h or host.endswith("." + h) for h in _TRUSTED_GIT_HOSTS)
    if not trusted:
        # 未知主机时，要求路径至少有 owner/repo 两段
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            return False, f"未知主机且路径不像 owner/repo 格式: {url}"
        # 未知主机但路径格式合理，警告但允许
        return True, f"警告：未知主机 {host}，请确认是 git 仓库"

    # 可信主机下，路径需要有 owner/repo 两段
    parts = [p for p in path.rstrip("/").split("/") if p]
    if len(parts) < 2:
        return False, f"路径缺少 owner/repo，不像 git 仓库: {url}"

    return True, "ok"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--pkg", required=True, help="要引入的包名")
    parser.add_argument("--url", default="", help="upstream git 仓库 URL")
    parser.add_argument("--constraint", default="", help="版本约束，如 '>= 1.4.0'")
    parser.add_argument("--required-by", default="", help="哪个包需要它")
    parser.add_argument("--skip-url-check", action="store_true", help="跳过 URL 格式校验（慎用）")
    args = parser.parse_args()

    # URL 校验
    if args.url and not args.skip_url_check:
        ok, reason = is_git_repo_url(args.url)
        if not ok:
            print(f"[register-dep] ERROR: URL 校验失败 — {reason}", file=sys.stderr)
            print(f"[register-dep] 请提供可 git clone 的仓库地址（如 https://github.com/owner/repo）", file=sys.stderr)
            sys.exit(1)
        if reason != "ok":
            print(f"[register-dep] {reason}", file=sys.stderr)

    # constraint 为空时警告
    if not args.constraint:
        print(f"[register-dep] WARNING: --constraint 未指定，evaluator 将选最新稳定版而非最小满足版本。"
              f"建议明确指定版本约束（如 '>= 1.4.0'）。", file=sys.stderr)

    reg_path = Path(args.session_dir) / "dep_registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {}

    if args.pkg in reg:
        old = reg[args.pkg]
        changed = []
        if args.url and not old.get("url"):
            # 已有条目补充 URL 时也校验
            if not args.skip_url_check:
                ok, reason = is_git_repo_url(args.url)
                if not ok:
                    print(f"[register-dep] ERROR: URL 校验失败 — {reason}", file=sys.stderr)
                    sys.exit(1)
            old["url"] = args.url
            changed.append("url")
        if args.constraint and not old.get("constraint"):
            old["constraint"] = args.constraint
            changed.append("constraint")
        if changed:
            reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[register-dep] updated {args.pkg}: {changed}")
        else:
            print(f"[register-dep] {args.pkg} already registered, no change")
        return

    reg[args.pkg] = {
        "url": args.url,
        "constraint": args.constraint,
        "status": "pending_evaluate",
        "required_by": args.required_by,
    }
    reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[register-dep] registered {args.pkg} (url={args.url!r}, constraint={args.constraint!r})")


if __name__ == "__main__":
    main()
