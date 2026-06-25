#!/usr/bin/env python3
"""Prepare source tarball and upload build inputs into the RPM build container."""

import argparse
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def run(cmd: list[str], *, capture_output: bool = False, cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=True, text=True, capture_output=capture_output, cwd=cwd)
    except subprocess.CalledProcessError as exc:
        if capture_output and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        raise


def run_in_container(container: str, shell_cmd: str) -> subprocess.CompletedProcess:
    return run(["docker", "exec", container, "bash", "-c", shell_cmd])


def ensure_go_vendor(source_dir: Path, container: str) -> None:
    """Run go mod vendor inside container if vendor/ directory is missing on host."""
    if (source_dir / "vendor").exists():
        print("[INFO] Go vendor directory already exists, skipping go mod vendor")
        return
    if not (source_dir / "go.mod").exists():
        return
    print(f"[INFO] Running go mod vendor via container {container}")
    # Copy source into container temp dir, run vendor, copy back
    tmp_dir = f"/tmp/vendor-src-{source_dir.name}"
    run(["docker", "cp", str(source_dir), f"{container}:{tmp_dir}"])
    run_in_container(container, f"cd {tmp_dir} && go mod vendor")
    # Copy vendor dir back to host
    run(["docker", "cp", f"{container}:{tmp_dir}/vendor", str(source_dir / "vendor")])
    run_in_container(container, f"rm -rf {tmp_dir}")
    print("[INFO] go mod vendor completed")


def ensure_rust_vendor(source_dir: Path, container: str) -> None:
    """Run cargo vendor inside container if vendor/ directory is missing, and write .cargo/config.toml."""
    if (source_dir / "vendor").exists():
        print("[INFO] Rust vendor directory already exists, skipping cargo vendor")
        _write_cargo_config(source_dir)
        return
    if not (source_dir / "Cargo.toml").exists():
        return
    print(f"[INFO] Running cargo vendor via container {container}")
    tmp_dir = f"/tmp/vendor-src-{source_dir.name}"
    run(["docker", "cp", str(source_dir), f"{container}:{tmp_dir}"])
    run_in_container(container, f"cd {tmp_dir} && cargo vendor vendor")
    run(["docker", "cp", f"{container}:{tmp_dir}/vendor", str(source_dir / "vendor")])
    run_in_container(container, f"rm -rf {tmp_dir}")
    _write_cargo_config(source_dir)
    print("[INFO] cargo vendor completed")


def _write_cargo_config(source_dir: Path) -> None:
    cargo_dir = source_dir / ".cargo"
    cargo_dir.mkdir(exist_ok=True)
    config_path = cargo_dir / "config.toml"
    if not config_path.exists():
        config_path.write_text(
            "[source.crates-io]\nreplace-with = \"vendored-sources\"\n\n"
            "[source.vendored-sources]\ndirectory = \"vendor\"\n",
            encoding="utf-8",
        )
        print(f"[INFO] Written .cargo/config.toml for offline build")


def detect_lang(source_dir: Path) -> str:
    """Detect primary language by characteristic files (priority order)."""
    if (source_dir / "go.mod").exists():
        return "go"
    if (source_dir / "Cargo.toml").exists():
        return "rust"
    return "other"


def ensure_vendor(source_dir: Path, container: str) -> None:
    """Auto-vendor dependencies based on detected language."""
    lang = detect_lang(source_dir)
    if lang == "go":
        ensure_go_vendor(source_dir, container)
    elif lang == "rust":
        ensure_rust_vendor(source_dir, container)


def create_tarball(pkg: str, version: str, source_dir: Path) -> Path:
    staging_dir = Path("/tmp") / f"{pkg}-{version}"
    tarball = Path("/tmp") / f"{pkg}-{version}.tar.gz"

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    if tarball.exists():
        tarball.unlink()

    shutil.copytree(source_dir, staging_dir)
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(staging_dir, arcname=staging_dir.name)
    shutil.rmtree(staging_dir)
    return tarball


def prepare_container(container: str, pkg: str, tarball: Path, spec_path: Path) -> None:
    run([
        "docker", "exec", container, "bash", "-c",
        "mkdir -p ~/rpmbuild/{SPECS,SOURCES,BUILD,RPMS,SRPMS}",
    ])
    run([
        "docker", "exec", container, "bash", "-c",
        f"rm -f ~/rpmbuild/SOURCES/{pkg}-*.tar.gz",
    ])
    run(["docker", "cp", str(tarball), f"{container}:/tmp/"])
    run([
        "docker", "exec", container, "bash", "-c",
        f"cp /tmp/{tarball.name} ~/rpmbuild/SOURCES/",
    ])
    run(["docker", "cp", str(spec_path), f"{container}:/root/rpmbuild/SPECS/"])



def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare source tarball and upload build inputs")
    parser.add_argument("--pkg", required=True, help="Package name")
    parser.add_argument("--version", required=True, help="Package version")
    parser.add_argument("--source-dir", required=True, help="Source directory")
    parser.add_argument("--spec", required=True, help="Spec file path")
    parser.add_argument("--container", default="oe-build-env", help="Container name")
    parser.add_argument("--no-vendor", action="store_true", help="Skip auto-vendor step")
    args = parser.parse_args()

    source_dir = Path(args.source_dir).resolve()
    spec_path = Path(args.spec).resolve()

    if not source_dir.exists():
        print(f"[ERROR] Source directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)
    if not spec_path.exists():
        print(f"[ERROR] Spec file not found: {spec_path}", file=sys.stderr)
        sys.exit(1)

    if not args.no_vendor:
        ensure_vendor(source_dir, args.container)

    tarball = create_tarball(args.pkg, args.version, source_dir)
    prepare_container(args.container, args.pkg, tarball, spec_path)

    print(f"TARBALL={tarball}")
    print(f"CONTAINER={args.container}")


if __name__ == "__main__":
    main()
