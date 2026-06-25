#!/usr/bin/env python3
"""PyPI provider for version enumeration in the resolution layer."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

try:
    from packaging.version import InvalidVersion, Version
except Exception:  # pragma: no cover - optional runtime dependency
    Version = None
    InvalidVersion = Exception


def normalize_version(value: str) -> str:
    text = (value or "").strip()
    return text[1:] if text.lower().startswith("v") else text


def list_stable_versions(name: str) -> list[str]:
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{name}/json",
        headers={"User-Agent": "resolve_dependency_versions/1.0"},
    )
    payload = json.loads(urllib.request.urlopen(request, timeout=15).read())
    releases = payload.get("releases") or {}
    stable: list[tuple[Any, str]] = []
    if Version is None:
        return []
    for raw_version, files in releases.items():
        normalized = normalize_version(raw_version)
        if not normalized or not files:
            continue
        try:
            parsed = Version(normalized)
        except InvalidVersion:
            continue
        if parsed.is_prerelease or parsed.is_devrelease:
            continue
        stable.append((parsed, normalized))
    stable.sort(reverse=True)
    return [item[1] for item in stable]
