#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def extract_nodejs_version(source_dir: str) -> str:
    package_json = Path(source_dir) / "package.json"
    if not package_json.exists():
        return ""
    try:
        data = json.loads(package_json.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return ""
    version = data.get("version")
    return version.strip() if isinstance(version, str) else ""


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "version":
        print("usage: nodejs_metadata.py version <source-dir>", file=sys.stderr)
        sys.exit(2)
    print(extract_nodejs_version(sys.argv[2]))
