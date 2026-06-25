#!/usr/bin/env python3
"""预热目标 chroot 的 dnf repo 缓存。

在 session 初始化后异步调用，使 rpm_batch_lookup.py 后续查询直接命中本地缓存。

用法：
  python3 warm_repo_cache.py openeuler-24.03_LTS_SP2-x86_64
"""
import sys
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from rpm_batch_lookup import chroot_to_repofrompath, run_batch_lookup  # noqa: E402


def main() -> None:
    chroot = sys.argv[1] if len(sys.argv) > 1 else ""
    if not chroot:
        print("[warm_cache] no chroot specified, skipping", file=sys.stderr)
        return

    rfp = chroot_to_repofrompath(chroot)
    if not rfp:
        print(f"[warm_cache] unknown chroot {chroot}, skipping", file=sys.stderr)
        return

    # 检查缓存是否已存在（所有 repo 都有 repomd.xml）
    cache_root = Path("/var/cache/dnf")
    repo_ids = [rid for rid, _ in rfp]
    all_cached = all(
        any(cache_root.glob(f"{rid}-*/repodata/repomd.xml"))
        for rid in repo_ids
    )
    if all_cached:
        print(f"[warm_cache] cache already fresh for {chroot}, skipping", file=sys.stderr)
        return

    print(f"[warm_cache] warming cache for {chroot} ...", file=sys.stderr)
    # 用最轻量的查询触发 repodata 下载
    tasks = [{"queries": [{"kind": "name", "value": "python3"}]}]
    try:
        run_batch_lookup(tasks, chroot=chroot, timeout=600)
        print(f"[warm_cache] done for {chroot}", file=sys.stderr)
    except Exception as e:
        print(f"[warm_cache] warning: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
