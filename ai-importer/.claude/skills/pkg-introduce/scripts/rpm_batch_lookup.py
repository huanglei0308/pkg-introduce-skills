#!/usr/bin/env python3
"""Compatibility wrapper forwarding to build-rpm/scripts/rpm_batch_lookup.py."""

from __future__ import annotations

import runpy
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "../../build-rpm/scripts" / "rpm_batch_lookup.py"
_globals = runpy.run_path(str(TARGET), run_name=__name__)
globals().update(_globals)
