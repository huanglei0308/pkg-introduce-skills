#!/usr/bin/env python3
"""Check openEuler source repo (gitcode.com/src-openeuler) for existing RPM spec.

If a matching repo exists, fetch spec, yaml metadata, and patches as a
reference starting point for spec generation.  Falls back silently when the
repo does not exist or the network is unreachable — this is purely an
optimisation and must never block the build.

Usage:
  python3 fetch_reference_spec.py --pkgname snappy --output-dir ./pkgs/snappy/reference
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

GITCODE_HOST = "gitcode.com"
PKG_NAMESPACE = "src-openeuler"
CLONE_TIMEOUT = 30
LS_REMOTE_TIMEOUT = 10

# 重试配置
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # 秒，递增：2 → 4 → 6


# ── helpers ────────────────────────────────────────────────────────────────────

def _git_available() -> bool:
    """Return True if git is on PATH and executable."""
    return shutil.which("git") is not None


def _log(msg: str) -> None:
    """Print timestamped diagnostic message to stderr."""
    print(f"[fetch_ref] {msg}", file=sys.stderr)


def _git_env() -> dict:
    """Return environment for git subprocess — strip proxy so HTTPS to
    gitcode.com does not get routed through an HTTP-only proxy."""
    env = dict(os.environ)
    for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(v, None)
    return env


def _run_git(args: list[str], timeout: int, desc: str = "") -> subprocess.CompletedProcess:
    """Run a git command, logging stderr on failure."""
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env=_git_env())
    if result.returncode != 0:
        stderr = result.stderr.strip()
        label = desc or " ".join(args[:2])
        _log(f"git {label} FAILED (rc={result.returncode}): {stderr[:300]}")
    return result


def _try_git_ls_remote(pkgname: str) -> Optional[bool]:
    """Single attempt: check via git ls-remote whether the repo exists."""
    url = f"https://{GITCODE_HOST}/{PKG_NAMESPACE}/{pkgname}.git"
    try:
        result = _run_git(
            ["git", "ls-remote", "--heads", url],
            timeout=LS_REMOTE_TIMEOUT,
            desc=f"ls-remote {pkgname}",
        )
        if result.returncode == 0:
            return True
        stderr = result.stderr.lower()
        if any(kw in stderr for kw in ("not found", "could not read",
                                        "repository not found", "403", "404")):
            return False
        return None
    except subprocess.TimeoutExpired:
        _log(f"git ls-remote {pkgname} timed out after {LS_REMOTE_TIMEOUT}s")
        return None
    except Exception as exc:
        _log(f"git ls-remote {pkgname} exception: {exc}")
        return None


def _try_git_clone(pkgname: str, dest: Path) -> bool:
    """Single attempt: shallow clone the repo."""
    url = f"https://{GITCODE_HOST}/{PKG_NAMESPACE}/{pkgname}.git"
    try:
        result = _run_git(
            ["git", "clone", "--depth=1", url, str(dest)],
            timeout=CLONE_TIMEOUT,
            desc=f"clone {pkgname}",
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        _log(f"git clone {pkgname} timed out after {CLONE_TIMEOUT}s")
        return False
    except Exception as exc:
        _log(f"git clone {pkgname} exception: {exc}")
        return False


# ── retry wrapper ──────────────────────────────────────────────────────────────

def _repo_exists(pkgname: str) -> Optional[bool]:
    """Check whether the repo exists, with retry on transient failures."""
    if not _git_available():
        _log("git not available on PATH")
        return None

    for attempt in range(1, MAX_RETRIES + 1):
        result = _try_git_ls_remote(pkgname)
        if result is not None:
            return result
        if attempt < MAX_RETRIES:
            delay = RETRY_BASE_DELAY * attempt
            _log(f"ls-remote attempt {attempt}/{MAX_RETRIES} failed, retrying in {delay}s...")
            time.sleep(delay)

    _log(f"ls-remote failed after {MAX_RETRIES} attempts")
    return None


def _clone_and_extract(pkgname: str, output_dir: Path) -> bool:
    """Shallow-clone and copy spec / yaml / patches, with retry."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            tmp = Path(tempfile.mkdtemp(prefix=f"refspec_{pkgname}_"))
        except Exception as exc:
            _log(f"mkdtemp failed: {exc}")
            return False

        try:
            ok = _try_git_clone(pkgname, tmp)
            if not ok:
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * attempt
                    _log(f"clone attempt {attempt}/{MAX_RETRIES} failed, retrying in {delay}s...")
                    time.sleep(delay)
                continue

            # Find spec file — prefer <pkgname>.spec, accept any .spec
            spec_file = tmp / f"{pkgname}.spec"
            if not spec_file.exists():
                candidates = sorted(tmp.glob("*.spec"))
                if candidates:
                    spec_file = candidates[0]
                else:
                    _log(f"clone succeeded but no .spec found in repo {PKG_NAMESPACE}/{pkgname}")
                    return False

            output_dir.mkdir(parents=True, exist_ok=True)

            # Copy spec
            shutil.copy2(spec_file, output_dir / spec_file.name)

            # Copy yaml metadata if present
            for yaml_file in tmp.glob("*.yaml"):
                shutil.copy2(yaml_file, output_dir / yaml_file.name)

            # Copy all patch files
            for patch in tmp.glob("*.patch"):
                shutil.copy2(patch, output_dir / patch.name)

            # Copy .inc / .macros files (RPM helper includes, less common)
            for ext in (".inc", ".macros"):
                for f in tmp.glob(f"*{ext}"):
                    shutil.copy2(f, output_dir / f.name)

            files = sorted([f.name for f in output_dir.iterdir()])
            _log(f"fetched {len(files)} files from {GITCODE_HOST}/{PKG_NAMESPACE}/{pkgname}")
            return True

        except Exception as exc:
            _log(f"clone_and_extract({pkgname}) error: {exc}")
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * attempt
                _log(f"retrying in {delay}s...")
                time.sleep(delay)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    _log(f"clone_and_extract failed after {MAX_RETRIES} attempts")
    return False


# ── main entry point ───────────────────────────────────────────────────────────

def fetch_reference_spec(pkgname: str, output_dir: Path) -> dict:
    """Check gitcode.com for a reference spec and fetch it.

    Idempotent — if *output_dir* already contains a .spec file the function
    returns immediately without re-fetching.
    """
    # Idempotency: already fetched → skip
    if output_dir.exists():
        spec_files = list(output_dir.glob("*.spec"))
        if spec_files:
            _log(f"already cached ({len(spec_files)} spec(s))")
            return {
                "found": True,
                "cached": True,
                "files": [f.name for f in sorted(output_dir.iterdir())],
            }

    _log(f"checking {GITCODE_HOST}/{PKG_NAMESPACE}/{pkgname} ...")
    exists = _repo_exists(pkgname)

    if exists is None:
        _log(f"result: network_error (cannot reach {GITCODE_HOST})")
        return {"found": False, "reason": "network_error",
                "detail": f"Cannot reach {GITCODE_HOST} after {MAX_RETRIES} attempts"}

    if not exists:
        _log(f"result: repo_not_found")
        return {"found": False, "reason": "repo_not_found",
                "detail": f"Repo {PKG_NAMESPACE}/{pkgname} not found on {GITCODE_HOST}"}

    success = _clone_and_extract(pkgname, output_dir)
    if not success:
        return {"found": False, "reason": "no_spec_file",
                "detail": f"Repo exists but no .spec found for {pkgname}"}

    files = sorted([f.name for f in output_dir.iterdir()])
    return {"found": True, "cached": False, "files": files,
            "source": f"{GITCODE_HOST}/{PKG_NAMESPACE}"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch reference RPM spec from openEuler source repository")
    parser.add_argument("--pkgname", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output-json", default="",
                        help="Also write result JSON to this file")
    args = parser.parse_args()

    result = fetch_reference_spec(args.pkgname, Path(args.output_dir))

    if args.output_json:
        json_path = Path(args.output_json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
