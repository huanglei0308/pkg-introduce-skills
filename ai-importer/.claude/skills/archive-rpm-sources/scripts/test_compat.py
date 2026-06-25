#!/usr/bin/env python3
"""
验证 publish_rpm.py 中 compat 相关逻辑的测试脚本。

覆盖：
  - parse_rpm_nvra         ← RPM 文件名解析
  - get_version_change_type ← 版本变更类型
  - detect_package_type    ← 包类型检测
  - resolve_dist_conflicts ← 冲突处理（模拟升级场景，不依赖 Docker）

用法：
  python3 test_compat.py
  python3 test_compat.py --integration  # 含需要 Docker 的集成测试
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 将脚本目录加入 path，直接 import publish_rpm
sys.path.insert(0, str(Path(__file__).parent))
import publish_rpm as p

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
SKIP = "\033[33m-\033[0m"

_errors = 0


def check(desc: str, got, expected):
    global _errors
    if got == expected:
        print(f"  {PASS} {desc}")
    else:
        print(f"  {FAIL} {desc}")
        print(f"       expected: {expected!r}")
        print(f"       got:      {got!r}")
        _errors += 1


# ─────────────────────────────────────────────
# 1. parse_rpm_nvra
# ─────────────────────────────────────────────

def test_parse_rpm_nvra():
    print("\n[1] parse_rpm_nvra")
    cases = [
        ("python3-requests-2.28.0-1.noarch.rpm",
         {"name": "python3-requests", "version": "2.28.0", "release": "1", "arch": "noarch"}),
        ("openssl-libs-3.0.1-2.aarch64.rpm",
         {"name": "openssl-libs", "version": "3.0.1", "release": "2", "arch": "aarch64"}),
        # 含 ~ 的 release（epoch 分隔）
        ("python3-ruyi-0.48.0.alpha.20260317-1.noarch.rpm",
         {"name": "python3-ruyi", "version": "0.48.0.alpha.20260317", "release": "1", "arch": "noarch"}),
        # 无效格式
        ("not-an-rpm.tar.gz", None),
        ("missing-arch.rpm",  None),
    ]
    for fname, expected in cases:
        check(fname, p.parse_rpm_nvra(fname), expected)


# ─────────────────────────────────────────────
# 2. get_version_change_type
# ─────────────────────────────────────────────

def test_version_change_type():
    print("\n[2] get_version_change_type")
    cases = [
        ("1.0.0",  "2.0.0",  "major"),
        ("1.2.0",  "1.3.0",  "minor"),
        ("1.2.3",  "1.2.4",  "patch"),
        ("2.0.0",  "2.0.1",  "patch"),
        # pre-1.0 包：major=0 不视为 major，按 minor 处理
        ("0.48.0", "0.49.0", "minor"),
        ("0.48.0.alpha.20260317", "0.49.0", "minor"),
        # 无法解析的版本
        ("abc",    "def",    "unknown"),
    ]
    for old, new, expected in cases:
        check(f"{old} → {new}", p.get_version_change_type(old, new), expected)


# ─────────────────────────────────────────────
# 3. detect_package_type  （从 spec 内容 + 包名前缀）
# ─────────────────────────────────────────────

_SPEC_TEMPLATES = {
    "python": """\
Name: python3-requests
Version: 2.28.0
Release: 1
Summary: HTTP library
License: Apache-2.0
%description
%install
%{__python3} setup.py install --root %{buildroot}
%{python3_sitelib}/requests
""",
    "java": """\
Name: jackson-databind
Version: 2.14.0
Release: 1
Summary: Java JSON library
License: Apache-2.0
BuildRequires: maven-local
%description
%install
%mvn_install
install -m 644 target/jackson-databind-2.14.0.jar %{buildroot}%{_javadir}/
""",
    "ruby": """\
Name: rubygem-rake
Version: 13.0.6
Release: 1
Summary: Ruby build tool
License: MIT
%description
%install
%gem_install
""",
    "nodejs": """\
Name: nodejs-semver
Version: 7.3.8
Release: 1
Summary: Semantic versioning
License: ISC
%description
%install
mkdir -p %{buildroot}%{nodejs_sitelib}/semver
cp -r lib/* %{buildroot}%{nodejs_sitelib}/semver/
""",
    "perl": """\
Name: perl-JSON
Version: 4.10
Release: 1
Summary: Perl JSON module
License: GPL+
%description
%install
make install DESTDIR=%{buildroot}
find %{buildroot}%{perl_vendorlib} -name '*.pm'
""",
    "other": """\
Name: zlib
Version: 1.3.1
Release: 1
Summary: Compression library
License: zlib
%description
%install
make install DESTDIR=%{buildroot}
""",
}


def test_detect_package_type():
    print("\n[3] detect_package_type")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        expected_map = {
            "python3-requests": "python",
            "jackson-databind":  "java",
            "rubygem-rake":      "ruby",
            "nodejs-semver":     "nodejs",
            "perl-JSON":         "perl",
            "zlib":              "other",
        }
        for pkg_name, lang in _SPEC_TEMPLATES.items():
            # 提取真正的包名（第一行 Name:）
            real_name = [l.split(":", 1)[1].strip()
                         for l in lang.splitlines() if l.startswith("Name:")][0]
            pkg_dir = tmpdir / real_name
            pkg_dir.mkdir()
            (pkg_dir / f"{real_name}.spec").write_text(lang)

        for real_name, expected in expected_map.items():
            got = p.detect_package_type(real_name, str(tmpdir))
            check(f"{real_name} → {expected}", got, expected)

    # 纯包名前缀检测（无 spec 文件）
    print("  [prefix-only]")
    prefix_cases = [
        ("python3-foo",    "python"),
        ("perl-Digest-MD5","perl"),
        ("lua-socket",     "lua"),
        ("php-mbstring",   "php"),
        ("golang-github-foo", "other"),   # golang 前缀未在 map 里，走 other
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for pkg_name, expected in prefix_cases:
            got = p.detect_package_type(pkg_name, tmp)
            check(f"  prefix: {pkg_name}", got, expected)


# ─────────────────────────────────────────────
# 4. resolve_dist_conflicts 模拟测试（无 Docker）
#    使用 --force-upgrade 路径，不触发 Docker 调用
# ─────────────────────────────────────────────

def _make_fake_rpm(dist_dir: Path, name: str, ver: str, rel: str = "1", arch: str = "noarch"):
    """创建空 RPM 占位文件（仅用于文件名解析测试）。"""
    f = dist_dir / f"{name}-{ver}-{rel}.{arch}.rpm"
    f.touch()
    return f


def test_resolve_conflicts_force_upgrade():
    print("\n[4] resolve_dist_conflicts (--force-upgrade, no Docker)")
    with tempfile.TemporaryDirectory() as tmp:
        dist = Path(tmp) / "dist"
        dist.mkdir()

        # 已有旧版本
        old = _make_fake_rpm(dist, "openssl-libs", "1.1.1", "1", "aarch64")
        # 新版本（模拟刚拷入）
        new = _make_fake_rpm(dist, "openssl-libs", "3.0.1", "1", "aarch64")

        removed, notes = p.resolve_dist_conflicts(
            dist, [new], container="",
            repo_dir=tmp, force_upgrade=True
        )
        check("旧版本被移除", not old.exists(), True)
        check("新版本保留",   new.exists(),     True)
        check("有升级记录",   len(notes) > 0,   True)
        print(f"       note: {notes[0]}")


def test_resolve_conflicts_same_version():
    print("\n[5] resolve_dist_conflicts (相同版本，无冲突)")
    with tempfile.TemporaryDirectory() as tmp:
        dist = Path(tmp) / "dist"
        dist.mkdir()

        # 同名同版本：不应视为冲突
        existing = _make_fake_rpm(dist, "python3-arpy", "2.3.0", "1", "noarch")
        new      = existing   # 同一个文件

        removed, notes = p.resolve_dist_conflicts(
            dist, [new], container="",
            repo_dir=tmp, force_upgrade=False
        )
        check("无删除操作", len(removed) == 0, True)
        check("无升级记录", len(notes)   == 0, True)


def test_resolve_conflicts_no_compat_type():
    print("\n[6] resolve_dist_conflicts (Python 包 major 升级 → 应报错中止)")
    with tempfile.TemporaryDirectory() as tmp:
        dist  = Path(tmp) / "dist"
        repo  = Path(tmp) / "repo"
        (dist).mkdir()
        (repo).mkdir()

        # 创建 spec 以便 detect_package_type 能识别
        pkg_name = "python3-requests"
        pkg_dir  = repo / pkg_name
        pkg_dir.mkdir()
        (pkg_dir / f"{pkg_name}.spec").write_text(
            "Name: python3-requests\n%{python3_sitelib}/requests\n"
        )

        old = _make_fake_rpm(dist, pkg_name, "2.28.0", "1", "noarch")
        new = _make_fake_rpm(dist, pkg_name, "3.0.0",  "1", "noarch")

        error_raised = False
        error_msg    = ""
        try:
            # 无反向依赖时 minor 不触发 compat；major 升级才触发
            # 这里 2.x → 3.x 是 major，且无 Docker 可查依赖 → find_rpm_dependents 返回 []
            # → needs_compat = True（major）→ python 类型 → 报错
            p.resolve_dist_conflicts(
                dist, [new], container="nonexistent_container",
                repo_dir=str(repo), force_upgrade=False
            )
        except (RuntimeError, subprocess.CalledProcessError, Exception) as e:
            error_msg   = str(e)
            error_raised = True

        check("major 升级触发报错", error_raised, True)
        check("新版本被回滚（删除）", not new.exists(), True)
        check("旧版本保留",          old.exists(),     True)
        if error_raised:
            print(f"       错误信息: {error_msg[:120].strip()}...")


# ─────────────────────────────────────────────
# 5. 集成测试（需要 Docker + 真实 RPM）
# ─────────────────────────────────────────────

REAL_RPMS_DIR = Path("/root/.claude/skills/rpm-repo/dist")
CONTAINER     = "oe-build-env"

def test_integration_detect_type():
    """用 dist/ 里真实 RPM 验证包名前缀检测。"""
    print("\n[INT-1] 真实 RPM 包名 → detect_package_type（仅前缀，无 spec）")
    with tempfile.TemporaryDirectory() as tmp:
        rpms = list(REAL_RPMS_DIR.glob("*.rpm"))
        if not rpms:
            print(f"  {SKIP} dist/ 无 RPM，跳过")
            return
        for rpm in sorted(rpms):
            info = p.parse_rpm_nvra(rpm.name)
            if not info:
                continue
            pkg_type = p.detect_package_type(info["name"], tmp)
            print(f"  {PASS} {info['name']:40s} → {pkg_type}")


def test_integration_repoclosure():
    """检查当前 dist/ 能否通过 repoclosure（容器内）。"""
    print(f"\n[INT-2] repoclosure 检查 dist/ (容器: {CONTAINER})")
    if not REAL_RPMS_DIR.exists():
        print(f"  {SKIP} dist/ 不存在，跳过")
        return
    try:
        p.run_ci_gate(REAL_RPMS_DIR, CONTAINER)
        print(f"  {PASS} repoclosure 通过")
    except RuntimeError as e:
        print(f"  {FAIL} repoclosure 失败: {e}")
        global _errors
        _errors += 1


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--integration", action="store_true",
                    help="同时运行需要 Docker 的集成测试")
    args = ap.parse_args()

    print("=" * 55)
    print("  publish_rpm.py compat 逻辑验证")
    print("=" * 55)

    test_parse_rpm_nvra()
    test_version_change_type()
    test_detect_package_type()
    test_resolve_conflicts_force_upgrade()
    test_resolve_conflicts_same_version()
    test_resolve_conflicts_no_compat_type()

    if args.integration:
        test_integration_detect_type()
        test_integration_repoclosure()

    print("\n" + "=" * 55)
    if _errors == 0:
        print(f"  {PASS} 全部通过")
    else:
        print(f"  {FAIL} {_errors} 项失败")
    print("=" * 55)
    sys.exit(0 if _errors == 0 else 1)


if __name__ == "__main__":
    main()
