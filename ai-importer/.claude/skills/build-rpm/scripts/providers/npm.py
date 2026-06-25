#!/usr/bin/env python3
"""npm registry provider for version enumeration in the resolution layer."""

from __future__ import annotations

import json
import re
import urllib.request


def normalize_version(value: str) -> str:
    text = (value or "").strip()
    return text[1:] if text.lower().startswith("v") else text


_PRERELEASE_RE = re.compile(
    r"[-.]?(alpha|beta|rc|pre|dev|snapshot|nightly|canary)\d*(\b|$)",
    re.IGNORECASE,
)


def list_stable_versions(name: str) -> list[str]:
    """Return stable versions for an npm package, newest first."""
    url = f"https://registry.npmjs.org/{name}"
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "resolve_dependency_versions/1.0"},
        )
        payload = json.loads(urllib.request.urlopen(request, timeout=15).read())
    except Exception:
        return []

    versions_info = payload.get("versions") or {}
    stable = []
    for raw_ver in versions_info:
        normalized = normalize_version(raw_ver)
        if not normalized:
            continue
        if _PRERELEASE_RE.search(normalized):
            continue
        try:
            parts = [int(x) for x in normalized.split(".")[:3]]
            if len(parts) < 3:
                parts += [0] * (3 - len(parts))
        except ValueError:
            continue
        stable.append((parts, normalized))

    stable.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in stable]
