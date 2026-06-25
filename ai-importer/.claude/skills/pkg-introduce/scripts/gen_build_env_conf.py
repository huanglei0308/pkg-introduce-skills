#!/usr/bin/env python3
"""
根据目标 openEuler 版本动态生成 build-env.conf.json。
Build Job 启动时调用，避免所有任务共用同一个静态 conf 文件导致冲突。
"""

import argparse
import json
import sys

# OE 版本 → Docker 镜像 tag + platform
# platform 当前全部用 linux/amd64，支持 aarch64 时加节点后按需切换
OE_VERSION_MAP = {
    "openEuler-25.09":       {"tag": "openeuler-25.09:latest",        "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-25.03":       {"tag": "openeuler-25.03:latest",        "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-24.09":       {"tag": "openeuler-24.09:latest",        "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-24.03-LTS-SP3": {"tag": "openeuler-24.03-lts-sp3:latest", "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-24.03-LTS-SP2": {"tag": "openeuler-24.03-lts-sp2:latest", "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-24.03-LTS-SP1": {"tag": "openeuler-24.03-lts-sp1:latest", "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-24.03-LTS":   {"tag": "openeuler-24.03-lts:latest",    "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-22.03-LTS-SP4": {"tag": "openeuler-22.03-lts-sp4:latest", "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-22.03-LTS-SP3": {"tag": "openeuler-22.03-lts-sp3:latest", "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-22.03-LTS-SP2": {"tag": "openeuler-22.03-lts-sp2:latest", "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-22.03-LTS-SP1": {"tag": "openeuler-22.03-lts-sp1:latest", "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-22.03-LTS":   {"tag": "openeuler-22.03-lts:latest",    "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-20.03-LTS-SP4": {"tag": "openeuler-20.03-lts-sp4:latest", "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-20.03-LTS-SP3": {"tag": "openeuler-20.03-lts-sp3:latest", "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-20.03-LTS-SP2": {"tag": "openeuler-20.03-lts-sp2:latest", "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-20.03-LTS-SP1": {"tag": "openeuler-20.03-lts-sp1:latest", "arch": "x86_64", "platform": "linux/amd64"},
    "openEuler-20.03-LTS":   {"tag": "openeuler-20.03-lts:latest",    "arch": "x86_64", "platform": "linux/amd64"},
}

# OE 版本名 → repo.openeuler.org 的 branch 路径
OE_BRANCH_MAP = {
    "openEuler-25.09":         "openEuler-25.09",
    "openEuler-25.03":         "openEuler-25.03",
    "openEuler-24.09":         "openEuler-24.09",
    "openEuler-24.03-LTS-SP3": "openEuler-24.03-LTS-SP3",
    "openEuler-24.03-LTS-SP2": "openEuler-24.03-LTS-SP2",
    "openEuler-24.03-LTS-SP1": "openEuler-24.03-LTS-SP1",
    "openEuler-24.03-LTS":     "openEuler-24.03-LTS",
    "openEuler-22.03-LTS-SP4": "openEuler-22.03-LTS-SP4",
    "openEuler-22.03-LTS-SP3": "openEuler-22.03-LTS-SP3",
    "openEuler-22.03-LTS-SP2": "openEuler-22.03-LTS-SP2",
    "openEuler-22.03-LTS-SP1": "openEuler-22.03-LTS-SP1",
    "openEuler-22.03-LTS":     "openEuler-22.03-LTS",
    "openEuler-20.03-LTS-SP4": "openEuler-20.03-LTS-SP4",
    "openEuler-20.03-LTS-SP3": "openEuler-20.03-LTS-SP3",
    "openEuler-20.03-LTS-SP2": "openEuler-20.03-LTS-SP2",
    "openEuler-20.03-LTS-SP1": "openEuler-20.03-LTS-SP1",
    "openEuler-20.03-LTS":     "openEuler-20.03-LTS",
}


def generate(oe_version: str, issue_number: str) -> dict:
    entry = OE_VERSION_MAP.get(oe_version)
    if not entry:
        print(f"[gen_build_env_conf] 未知 OE 版本：{oe_version}", file=sys.stderr)
        print(f"[gen_build_env_conf] 支持的版本：{list(OE_VERSION_MAP.keys())}", file=sys.stderr)
        sys.exit(1)

    branch = OE_BRANCH_MAP.get(oe_version, oe_version)
    # 每个 Job 用独立 container name，避免并发冲突
    container_name = f"oe-build-env-{issue_number}"

    return {
        "image": {
            "base_url": "https://repo.openeuler.org",
            "branch": branch,
            "build": "",
            "arch": entry["arch"],
            "tag": entry["tag"],
            "filename_prefix": "openEuler-docker",
        },
        "container": {
            "name": container_name,
            "source_mount": "/build/source",
            "platform": entry["platform"],
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oe-version", required=True, help="目标 openEuler 版本")
    parser.add_argument("--issue-number", default="local", help="Issue 编号，用于生成唯一容器名")
    parser.add_argument("--output", required=True, help="输出 JSON 文件路径")
    args = parser.parse_args()

    conf = generate(args.oe_version, args.issue_number)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=2, ensure_ascii=False)
    print(f"[gen_build_env_conf] 已生成：{args.output} (oe={args.oe_version}, container={conf['container']['name']})")


if __name__ == "__main__":
    main()
