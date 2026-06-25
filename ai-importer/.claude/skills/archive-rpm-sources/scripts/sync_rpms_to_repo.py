#!/usr/bin/env python3
"""
将容器内新构建的 RPM 同步到归档仓 dist/，并更新 repodata。
仅同步与指定包名匹配的 RPM（复用 publish_rpm.py 的匹配逻辑）。

用法：
  python3 sync_rpms_to_repo.py \
    --pkg python3-foo \
    --container oe-build-env \
    --repo-local /root/.claude/skills/rpm-repo-github
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path


def normalize_name_token(value: str) -> str:
    return re.sub(r"[-_.]+", "_", value.lower())


def sync_rpms(pkg_name: str, container: str, repo_local: Path) -> list[Path]:
    dist_dir = repo_local / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    _LANG_PREFIXES = (
        "python3-", "python-", "ros-humble-", "ros-",
        "java-", "maven-", "rubygem-", "nodejs-", "npm-",
        "perl-", "lua-", "php8-", "php-",
    )
    base_name = pkg_name
    for pfx in _LANG_PREFIXES:
        if base_name.startswith(pfx):
            base_name = base_name[len(pfx):]
            break
    normalized_base = normalize_name_token(base_name)

    # 从 spec 中提取 %package -n 子包名
    extra_normalized: list[str] = []
    spec_result = subprocess.run(
        ["docker", "exec", container, "cat", f"/root/rpmbuild/SPECS/{pkg_name}.spec"],
        capture_output=True, text=True,
    )
    if spec_result.returncode == 0:
        names = re.findall(r"^%package\s+-n\s+(\S+)", spec_result.stdout, re.MULTILINE)
        extra_normalized = [normalize_name_token(n) for n in names]

    result = subprocess.run(
        ["docker", "exec", container, "bash", "-c",
         "find /root/rpmbuild/RPMS /root/rpmbuild/SRPMS -name '*.rpm' 2>/dev/null"],
        capture_output=True, text=True,
    )

    synced: list[Path] = []
    before = set(dist_dir.glob("*.rpm"))

    for rpm_path in result.stdout.strip().splitlines():
        rpm_path = rpm_path.strip()
        if not rpm_path:
            continue
        rpm_name = Path(rpm_path).name
        norm_rpm = normalize_name_token(rpm_name)

        all_bases = [normalized_base] + extra_normalized
        matched = any(
            re.search(r"(?:^|_)" + re.escape(nb) + r"(?:_|$|\d)", norm_rpm)
            for nb in all_bases
        )
        if not matched:
            continue

        # 跳过超过 100MB 的文件
        size_r = subprocess.run(
            ["docker", "exec", container, "stat", "-c", "%s", rpm_path],
            capture_output=True, text=True,
        )
        if size_r.returncode == 0 and int(size_r.stdout.strip()) > 100 * 1024 * 1024:
            print(f"[WARN] 跳过超大 RPM（>100MB）: {rpm_name}")
            continue

        subprocess.run(
            ["docker", "cp", f"{container}:{rpm_path}", str(dist_dir)], check=True
        )
        print(f"[INFO] 同步 RPM: {rpm_name} → dist/")

    after = set(dist_dir.glob("*.rpm"))
    synced = list(after - before)
    return synced


def update_repodata(dist_dir: Path) -> None:
    if subprocess.run(["which", "createrepo_c"], capture_output=True).returncode != 0:
        print("[WARN] createrepo_c 未安装，跳过 repodata 更新", file=sys.stderr)
        return
    result = subprocess.run(
        ["createrepo_c", "--update", str(dist_dir)], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"[ERROR] createrepo_c 失败:\n{result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    print("[INFO] repodata 更新完成")


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 RPM 到归档仓 dist/")
    parser.add_argument("--pkg", required=True, help="包名")
    parser.add_argument("--container", required=True, help="容器名")
    parser.add_argument("--repo-local", required=True, help="归档仓本地路径")
    args = parser.parse_args()

    repo_local = Path(args.repo_local)
    synced = sync_rpms(args.pkg, args.container, repo_local)
    update_repodata(repo_local / "dist")

    print(f"[INFO] 新增 RPM 数量: {len(synced)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
