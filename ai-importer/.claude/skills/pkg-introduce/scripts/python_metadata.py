#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
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


_VERSION_RE = re.compile(r"""^[ \t]*(?:__version__|version)\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE)
_CHANGELOG_VERSION_RE = re.compile(r"(?:^|\s)v?(\d+\.\d+[\w.\-]*)")


def _scan_version_in_file(path: Path) -> str:
    """Extract version from a source file via __version__ or version = '...' patterns."""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = _VERSION_RE.search(content)
    return m.group(1).strip() if m else ""


def _dynamic_version_fallback(src: Path, pkg_name: str) -> str:
    """Fallback version extraction for packages using dynamic version in pyproject.toml."""
    normalized = pkg_name.replace("-", "_").lower()

    # 1. __init__.py candidates
    init_candidates = [
        src / "src" / normalized / "__init__.py",
        src / normalized / "__init__.py",
    ]
    for path in init_candidates:
        v = _scan_version_in_file(path)
        if v:
            return v

    # 2. Single-file module (e.g. multipart.py)
    v = _scan_version_in_file(src / f"{normalized}.py")
    if v:
        return v

    # 3. _version.py variants
    version_file_candidates = [
        src / "src" / normalized / "_version.py",
        src / normalized / "_version.py",
        src / "_version.py",
        src / "src" / "_version.py",
    ]
    for path in version_file_candidates:
        v = _scan_version_in_file(path)
        if v:
            return v

    # 4. src/ glob fallback: first __init__.py found under src/
    src_dir = src / "src"
    if src_dir.is_dir():
        for init_path in sorted(src_dir.glob("*/__init__.py")):
            v = _scan_version_in_file(init_path)
            if v:
                return v

    # 5. CHANGELOG first-line version
    for changelog_name in ("CHANGELOG.md", "CHANGELOG.rst", "CHANGELOG", "CHANGES.md", "CHANGES"):
        changelog = src / changelog_name
        if not changelog.exists():
            continue
        try:
            first_lines = changelog.read_text(encoding="utf-8", errors="ignore").splitlines()[:10]
        except OSError:
            continue
        for line in first_lines:
            m = _CHANGELOG_VERSION_RE.search(line)
            if m:
                return m.group(1).strip()

    return ""


def extract_python_version(source_dir: str) -> str:
    src = Path(source_dir)

    pyproject = src / "pyproject.toml"
    data = load_toml(pyproject)
    if data:
        project = data.get("project", {})
        if isinstance(project, dict):
            # Static version field
            version = project.get("version")
            if isinstance(version, str) and version.strip():
                return version.strip()
            # Dynamic version declared — try static fallback before giving up
            dynamic = project.get("dynamic", [])
            if "version" in (dynamic or []):
                pkg_name = project.get("name", src.name)
                # Check hatch version path first (e.g. [tool.hatch.version] path = "pkg/__version__.py")
                hatch_path = (
                    data.get("tool", {}).get("hatch", {}).get("version", {}).get("path", "")
                    if isinstance(data.get("tool"), dict)
                    else ""
                )
                if hatch_path:
                    v = _scan_version_in_file(src / hatch_path)
                    if v:
                        return v
                v = _dynamic_version_fallback(src, pkg_name)
                if v:
                    return v
        tool = data.get("tool")
        if isinstance(tool, dict):
            poetry = tool.get("poetry")
            if isinstance(poetry, dict):
                version = poetry.get("version")
                if isinstance(version, str) and version.strip():
                    return version.strip()

    version_file = src / "VERSION"
    if version_file.exists():
        version = version_file.read_text(encoding="utf-8", errors="ignore").strip()
        if version:
            return version

    setup_py = src / "setup.py"
    if setup_py.exists():
        content = setup_py.read_text(encoding="utf-8", errors="ignore")
        patterns = [
            r'version\s*=\s*["\']([^"\']+)["\']',
            r'__version__\s*=\s*["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                return match.group(1).strip()

    return ""


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "version":
        print("usage: python_metadata.py version <source-dir>", file=sys.stderr)
        sys.exit(2)
    print(extract_python_version(sys.argv[2]))
