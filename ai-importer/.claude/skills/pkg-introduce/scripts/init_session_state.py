#!/usr/bin/env python3
"""初始化 pkg-introduce 顶层会话状态。"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def reset_directory(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def initialize_session_state(build_state_dir: Path, reports_dir: Path, sources_dir: Path) -> int:
    building_file = build_state_dir / "building.txt"
    if building_file.exists():
        residual = [line.strip() for line in building_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if residual:
            print("[警告] building.txt 有残留，上次可能异常退出，涉及包：")
            for item in residual:
                print(item)

    reset_directory(build_state_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    sources_dir.mkdir(parents=True, exist_ok=True)

    (build_state_dir / "building.txt").write_text("", encoding="utf-8")
    (build_state_dir / "introduced.txt").write_text("", encoding="utf-8")

    state_script = Path(__file__).resolve().parents[2] / "build-rpm" / "scripts" / "dependency_resolution_state.py"
    command = [
        sys.executable,
        str(state_script),
        "init",
        "--build-state-dir",
        str(build_state_dir),
    ]
    proc = subprocess.run(command, check=False)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 pkg-introduce 顶层会话状态")
    parser.add_argument("--build-state-dir", default="./build_state", help="状态目录，默认 ./build_state")
    parser.add_argument("--reports-dir", default="./reports", help="报告目录，默认 ./reports")
    parser.add_argument("--sources-dir", default="./sources", help="源码目录，默认 ./sources")
    args = parser.parse_args()

    return initialize_session_state(
        Path(args.build_state_dir),
        Path(args.reports_dir),
        Path(args.sources_dir),
    )


if __name__ == "__main__":
    sys.exit(main())
