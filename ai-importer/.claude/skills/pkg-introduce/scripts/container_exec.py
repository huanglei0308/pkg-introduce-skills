#!/usr/bin/env python3
"""
容器命令执行工具 - 只负责执行，不做任何判断

AI Skill 通过调用此脚本在容器内执行任意命令，获取结果。

用法：
  python3 container_exec.py "dnf install -y gcc"
  python3 container_exec.py "go build ./..." --workdir /build/source
  python3 container_exec.py "cat /etc/os-release" --json
"""

import argparse
import json
import subprocess
import sys

DEFAULT_CONTAINER = "oe-build-env"
DEFAULT_WORKDIR   = "/build/source"


def exec_cmd(container: str, cmd: str, workdir: str) -> dict:
    r = subprocess.run(
        ["docker", "exec", "-w", workdir, container, "bash", "-c", cmd],
        capture_output=True, text=True
    )
    return {
        "returncode": r.returncode,
        "stdout": r.stdout,
        "stderr": r.stderr,
        "success": r.returncode == 0,
    }


def main():
    parser = argparse.ArgumentParser(description="在 openEuler 容器内执行命令")
    parser.add_argument("cmd", help="要执行的 shell 命令")
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--workdir", default=DEFAULT_WORKDIR)
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="以 JSON 格式输出结果（供 AI Skill 解析）")
    args = parser.parse_args()

    result = exec_cmd(args.container, args.cmd, args.workdir)

    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result["stdout"]:
            print(result["stdout"], end="")
        if result["stderr"]:
            print(result["stderr"], end="", file=sys.stderr)

    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
