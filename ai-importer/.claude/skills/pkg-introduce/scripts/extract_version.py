#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from java_metadata import extract_java_version
from nodejs_metadata import extract_nodejs_version
from python_metadata import extract_python_version
from rust_metadata import extract_rust_version


Extractor = Callable[[str], str]

_GO_VERSION_RE = re.compile(
    r"""(?m)^[ \t]*(?:var|const)\s+[Vv]ersion\s*(?:string\s*)?=\s*['"`]([0-9][^'"`\s]*)['"`]"""
)


def extract_generic_version(source_dir: str) -> str:
    src = Path(source_dir)
    version_file = src / "VERSION"
    if version_file.exists():
        version = version_file.read_text(encoding="utf-8", errors="ignore").strip()
        # Strip leading 'v' and take first line only
        version = version.splitlines()[0].strip().lstrip("v") if version else ""
        if version:
            return version
    return ""


def extract_go_version(source_dir: str) -> str:
    src = Path(source_dir)

    # 1. VERSION file
    v = extract_generic_version(source_dir)
    if v:
        return v

    # 2. Scan top-level and cmd/ Go source files for version constants
    #    e.g. var version = "0.72" or const Version = "1.2.3"
    scan_dirs = [src, src / "cmd", src / "main"]
    for d in scan_dirs:
        if not d.is_dir() and d != src:
            continue
        for go_file in (src.glob("*.go") if d == src else d.glob("*.go")):
            try:
                content = go_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = _GO_VERSION_RE.search(content)
            if m:
                return m.group(1).strip()

    # 3. git describe --tags (works for packages that inject version via ldflags)
    try:
        result = subprocess.run(
            ["git", "-C", str(src), "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            tag = result.stdout.strip().lstrip("v")
            if tag:
                return tag
    except Exception:
        pass

    return ""


def extract_c_version(source_dir: str) -> str:
    return extract_generic_version(source_dir)


def extract_cpp_version(source_dir: str) -> str:
    return extract_generic_version(source_dir)


def extract_ruby_version(source_dir: str) -> str:
    return extract_generic_version(source_dir)


EXTRACTORS: dict[str, Extractor] = {
    "python": extract_python_version,
    "rust": extract_rust_version,
    "nodejs": extract_nodejs_version,
    "java": extract_java_version,
    "go": extract_go_version,
    "c": extract_c_version,
    "cpp": extract_cpp_version,
    "ruby": extract_ruby_version,
}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: extract_version.py <lang> <source-dir>", file=sys.stderr)
        sys.exit(2)

    lang = sys.argv[1].strip().lower()
    source_dir = sys.argv[2]
    extractor = EXTRACTORS.get(lang)
    if extractor is None:
        print(f"unsupported language: {lang}", file=sys.stderr)
        sys.exit(2)

    print(extractor(source_dir))
