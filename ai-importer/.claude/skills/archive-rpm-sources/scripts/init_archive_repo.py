#!/usr/bin/env python3
"""
初始化归档仓：clone 或 pull 远端仓库到本地，确保 dist/ 目录存在。
将 repo_local 路径写入 session.json。

用法：
  python3 init_archive_repo.py --session-json ./session.json
  python3 init_archive_repo.py --session-json ./session.json --config /path/to/config.json
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def load_config(config_path: Path) -> dict:
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"[ERROR] 配置文件不存在: {config_path}", file=sys.stderr)
        sys.exit(1)


def auth_url(remote_url: str, username: str, token: str) -> str:
    if not token or "://" not in remote_url:
        return remote_url
    scheme, rest = remote_url.split("://", 1)
    return f"{scheme}://{username}:{token}@{rest}"


def init_or_update_repo(local_dir: Path, authed_url: str, branch: str) -> None:
    if (local_dir / ".git").exists():
        print(f"[INFO] 拉取最新代码: {local_dir}")
        subprocess.run(["git", "pull", "origin", branch], cwd=str(local_dir), check=False)
        return
    print(f"[INFO] 克隆仓库 → {local_dir}")
    result = subprocess.run(
        ["git", "clone", "--branch", branch, authed_url, str(local_dir)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("[INFO] 克隆失败（空仓库），初始化本地仓库")
        local_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=str(local_dir), check=True)
        subprocess.run(["git", "checkout", "-b", branch], cwd=str(local_dir), check=True)
        subprocess.run(["git", "remote", "add", "origin", authed_url], cwd=str(local_dir), check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化归档仓")
    parser.add_argument("--session-json", required=True, help="session.json 路径")
    parser.add_argument("--config", default="", help="archive config.json 路径（默认自动查找）")
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else Path(__file__).resolve().parents[1] / "config.json"
    cfg = load_config(config_path)
    git_cfg = cfg.get("gitcode") or cfg.get("github") or {}
    token = git_cfg.get("token", "")
    username = git_cfg.get("username", "oauth2")
    remote = cfg["repo"]["remote_url"]
    branch = cfg["repo"]["branch"]
    local_dir = Path(cfg["repo"]["local_dir"])

    authed = auth_url(remote, username, token)
    init_or_update_repo(local_dir, authed, branch)
    (local_dir / "dist").mkdir(parents=True, exist_ok=True)

    session_path = Path(args.session_json)
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["repo_local"] = str(local_dir)
    session_path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"REPO_LOCAL={local_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
