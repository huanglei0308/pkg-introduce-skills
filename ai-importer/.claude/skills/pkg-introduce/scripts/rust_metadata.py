#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        try:
            from pip._vendor import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]


def load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None or not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_rust_version(source_dir: str) -> str:
    cargo_toml = Path(source_dir) / "Cargo.toml"
    data = load_toml(cargo_toml)
    if not data:
        return ""
    package = data.get("package")
    if not isinstance(package, dict):
        return ""
    version = package.get("version")
    return version.strip() if isinstance(version, str) else ""


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "version":
        print("usage: rust_metadata.py version <source-dir>", file=sys.stderr)
        sys.exit(2)
    print(extract_rust_version(sys.argv[2]))
