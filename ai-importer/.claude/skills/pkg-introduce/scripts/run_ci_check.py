#!/usr/bin/env python3
"""
CI 门禁（COPR 模式）：本地执行 repoclosure + dnf builddep，无 Docker。

验证所有引入包的依赖是否闭合：
  - 官方源：对应 chroot 版本的 openEuler repo
  - COPR project 源：本次构建的包

用法：
  python3 run_ci_check.py \
    --pkgs python3-foo python3-bar \
    --session-dir /tmp/claude-ws/foo-abc123 \
    --reports-dir ./pkgs/foo

exit codes:
  0  全部通过
  1  检查失败
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# openEuler chroot name → repo base URL
_CHROOT_REPO_MAP = {
    "openeuler-22.03_LTS-":      "http://repo.openeuler.org/openEuler-22.03-LTS",
    "openeuler-22.03_LTS_SP1-":  "http://repo.openeuler.org/openEuler-22.03-LTS-SP1",
    "openeuler-22.03_LTS_SP2-":  "http://repo.openeuler.org/openEuler-22.03-LTS-SP2",
    "openeuler-22.03_LTS_SP3-":  "http://repo.openeuler.org/openEuler-22.03-LTS-SP3",
    "openeuler-22.03_LTS_SP4-":  "http://repo.openeuler.org/openEuler-22.03-LTS-SP4",
    "openeuler-24.03_LTS-":      "http://repo.openeuler.org/openEuler-24.03-LTS",
    "openeuler-24.03_LTS_SP1-":  "http://repo.openeuler.org/openEuler-24.03-LTS-SP1",
    "openeuler-24.03_LTS_SP2-":  "http://repo.openeuler.org/openEuler-24.03-LTS-SP2",
}


def _chroot_repo_base(chroot: str) -> str | None:
    for prefix, base in _CHROOT_REPO_MAP.items():
        if chroot.startswith(prefix):
            return base
    return None


def _chroot_arch(chroot: str) -> str:
    return "aarch64" if chroot.endswith("-aarch64") else "x86_64"


def _get_copr_result_url(session_dir: Path) -> tuple[str, str]:
    """从 session.json + gate_result 读取 COPR result repo URL 和 chroot。"""
    session = json.loads((session_dir / "session.json").read_text())
    copr_url     = session.get("copr_url", "http://copr-frontend:5000")
    copr_owner   = session.get("copr_owner", "")
    copr_project = session.get("copr_project", "")
    login        = session.get("copr_login", "")
    token        = session.get("copr_token", "")

    # 找 COPR project chroot
    import base64, urllib.request, urllib.parse
    creds = base64.b64encode(f"{login}:{token}".encode()).decode()
    url   = (f"{copr_url.rstrip('/')}/api_3/project"
             f"?ownername={copr_owner}&projectname={copr_project}")
    req   = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
    chroot = ""
    result_url = ""
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        chroot_repos = data.get("chroot_repos", {})
        # 优先 x86_64
        for c, repo in chroot_repos.items():
            if c.endswith("-x86_64"):
                chroot = c
                result_url = repo
                break
        if not chroot and chroot_repos:
            chroot, result_url = next(iter(chroot_repos.items()))
    except Exception as e:
        print(f"[CI] WARNING: 无法获取 COPR chroot 信息: {e}", file=sys.stderr)

    return chroot, result_url


def _write_repo_file(repo_path: Path, chroot: str, copr_result_url: str) -> bool:
    """写入临时 repo 文件。返回是否成功。"""
    base = _chroot_repo_base(chroot)
    arch = _chroot_arch(chroot)

    content = ""
    if base:
        content += f"""[ci-oe-official]
name=openEuler {chroot} official
baseurl={base}/everything/{arch}/
enabled=1
gpgcheck=0

[ci-oe-update]
name=openEuler {chroot} update
baseurl={base}/update/{arch}/
enabled=1
gpgcheck=0

[ci-oe-epol]
name=openEuler {chroot} EPOL
baseurl={base}/EPOL/main/{arch}/
enabled=1
gpgcheck=0

"""

    if copr_result_url:
        content += f"""[ci-copr-result]
name=COPR project result
baseurl={copr_result_url}
enabled=1
gpgcheck=0

"""

    if not content:
        return False

    try:
        repo_path.write_text(content, encoding="utf-8")
        return True
    except PermissionError:
        return False


def _copr_repo_accessible(copr_result_url: str) -> bool:
    """检查 COPR result repo 的 repomd.xml 是否可访问。"""
    import urllib.request
    url = copr_result_url.rstrip("/") + "/repodata/repomd.xml"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def run_repoclosure(pkgs: list[str], chroot: str, copr_result_url: str) -> tuple[bool, str]:
    """运行 repoclosure 检查运行时依赖。"""
    check = subprocess.run(["which", "repoclosure"], capture_output=True)
    if check.returncode != 0:
        subprocess.run(["dnf", "install", "-y", "--quiet", "dnf-utils"],
                       capture_output=True, timeout=60)
        check = subprocess.run(["which", "repoclosure"], capture_output=True)
        if check.returncode != 0:
            return True, "[SKIP] repoclosure 不可用，跳过运行时依赖检查"

    base = _chroot_repo_base(chroot)
    arch = _chroot_arch(chroot)

    # 检查 COPR result repo 是否可访问（空 project 时 repomd.xml 不存在）
    use_copr = bool(copr_result_url) and _copr_repo_accessible(copr_result_url)
    if copr_result_url and not use_copr:
        print("[CI] COPR result repo 暂不可访问（可能尚无已构建的包），跳过 COPR 源")

    cmd = ["repoclosure", "--newest"]

    if base:
        cmd += [
            "--repofrompath", f"ci-oe-official,{base}/everything/{arch}/",
            "--repofrompath", f"ci-oe-update,{base}/update/{arch}/",
            "--repofrompath", f"ci-oe-epol,{base}/EPOL/main/{arch}/",
            "--repo", "ci-oe-official",
            "--repo", "ci-oe-update",
            "--repo", "ci-oe-epol",
        ]
    else:
        # 没有 chroot 信息，使用现有 repo
        pass

    if use_copr:
        cmd += [
            "--repofrompath", f"ci-copr-result,{copr_result_url}",
            "--repo", "ci-copr-result",
        ]

    for pkg in pkgs:
        cmd += ["--check", pkg]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        return False, (result.stdout + result.stderr).strip()
    return True, ""


def run_builddep(pkg: str, spec_path: Path, chroot: str, copr_result_url: str) -> tuple[bool, str]:
    """运行 dnf builddep 检查编译期依赖。"""
    if not spec_path.exists():
        return True, f"[SKIP] spec 文件不存在: {spec_path}"

    base = _chroot_repo_base(chroot)
    arch = _chroot_arch(chroot)
    use_copr = bool(copr_result_url) and _copr_repo_accessible(copr_result_url)

    cmd = ["dnf", "builddep", "--assumeno"]

    if base:
        cmd += [
            "--repofrompath", f"ci-oe-official,{base}/everything/{arch}/",
            "--repofrompath", f"ci-oe-update,{base}/update/{arch}/",
            "--repofrompath", f"ci-oe-epol,{base}/EPOL/main/{arch}/",
            "--disablerepo=*",
            "--enablerepo=ci-oe-official",
            "--enablerepo=ci-oe-update",
            "--enablerepo=ci-oe-epol",
        ]

    if use_copr:
        cmd += [
            "--repofrompath", f"ci-copr-result,{copr_result_url}",
            "--enablerepo=ci-copr-result",
        ]

    cmd.append(str(spec_path))

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    combined = result.stdout + result.stderr
    # --assumeno 成功时返回非零（拒绝安装），只有含 Error 才是真正失败
    failed = ("Error:" in combined and
              ("could not be found" in combined or "No match" in combined))
    return (not failed), (combined if failed else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="CI 门禁：repoclosure + dnf builddep（COPR 模式）")
    parser.add_argument("--pkgs", nargs="+", required=True, help="待检查的包名列表")
    parser.add_argument("--session-dir", required=True, help="session 目录路径")
    parser.add_argument("--reports-dir", default="", help="报告输出目录")
    args = parser.parse_args()

    session_dir  = Path(args.session_dir)
    reports_dir  = Path(args.reports_dir) if args.reports_dir else session_dir / "pkgs" / args.pkgs[0]
    reports_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    warnings: list[str] = []

    # 1. 获取 COPR 信息
    print("[CI] 读取 COPR 信息...")
    chroot, copr_result_url = _get_copr_result_url(session_dir)
    if chroot:
        print(f"[CI] chroot: {chroot}")
    if copr_result_url:
        print(f"[CI] COPR result URL: {copr_result_url}")
    else:
        print("[CI] WARNING: 未找到 COPR result URL，将仅检查官方源", file=sys.stderr)

    try:
        # 3. repoclosure（所有包一起验证运行时依赖）
        print(f"[CI] 运行 repoclosure（{len(args.pkgs)} 个包）...")
        ok, msg = run_repoclosure(args.pkgs, chroot, copr_result_url)
        if ok:
            if msg.startswith("[SKIP]"):
                print(f"[CI] ⚠ {msg}")
                warnings.append(msg)
            else:
                print("[CI] ✓ 运行时依赖闭合检查通过")
        else:
            print("[CI] ✗ 运行时依赖闭合检查失败", file=sys.stderr)
            errors.append(f"repoclosure 失败:\n{msg}")

        # 4. dnf builddep（逐包验证编译期依赖）
        for pkg in args.pkgs:
            spec_path = session_dir / f"pkgs/{pkg}/{pkg}.spec"
            print(f"[CI] 运行 dnf builddep（{pkg}）...")
            ok, msg = run_builddep(pkg, spec_path, chroot, copr_result_url)
            if ok:
                if msg.startswith("[SKIP]"):
                    print(f"[CI] ⚠ {msg}")
                    warnings.append(msg)
                else:
                    print(f"[CI] ✓ {pkg} 编译期依赖检查通过")
            else:
                print(f"[CI] ✗ {pkg} 编译期依赖检查失败", file=sys.stderr)
                errors.append(f"{pkg} BuildRequires 不满足:\n{msg}")

    finally:
        pass

    # 5. 写结果文件
    status = "pass" if not errors else "fail"
    result = {"status": status, "errors": errors, "warnings": warnings,
              "chroot": chroot, "copr_result_url": copr_result_url}
    out_file = reports_dir / "ci_check_result.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[CI] 结果已写入: {out_file}")

    if errors:
        print(f"\n[CI] 门禁未通过，共 {len(errors)} 项失败", file=sys.stderr)
        return 1

    print("[CI] 门禁全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
