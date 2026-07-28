#!/usr/bin/env python3
"""
Run evaluate (check + gate) for a single dep without AI.

Replaces the pkg-evaluator agent for deterministic cases:
  - run_check.py exit 0 → run_gate.py → update dep_registry → done
  - run_check.py exit 2 → needs_ai (fall back to Claude)
  - run_check.py exit 1 → hard failure

Exit codes:
  0 — evaluate completed (status in JSON output is "done" or "failed")
  1 — script error (bad args, file not found)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_PKG_INTRODUCE_SCRIPTS = (
    Path(__file__).resolve().parents[2] / "pkg-introduce" / "scripts"
)
_RUN_CHECK = _PKG_INTRODUCE_SCRIPTS / "run_check.py"
_RUN_GATE = _PKG_INTRODUCE_SCRIPTS / "run_gate.py"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(session_dir: Path, pkgname: str, mode: str, url: str,
        constraint: str = "", version: str = "") -> dict:
    """Run check + gate. Returns {"status": "done"|"needs_ai"|"failed", ...}."""
    reports_dir = session_dir / "pkgs" / pkgname
    sources_dir = session_dir / "sources"
    build_state_dir = session_dir / "build_state"

    # ── run_check.py ──────────────────────────────────────────────────
    check_cmd = [
        sys.executable, str(_RUN_CHECK),
        "--pkg", pkgname,
        "--url", url,
        "--mode", mode,
        "--pkg-dir", str(reports_dir),
        "--sources-dir", str(sources_dir),
        "--build-state-dir", str(build_state_dir),
    ]
    if version:
        check_cmd += ["--version", version]
    if constraint:
        check_cmd += ["--constraint", constraint]

    check_proc = subprocess.run(check_cmd, capture_output=True, text=True, timeout=300)
    check_rc = check_proc.returncode

    if check_rc == 1:
        # Hard failure
        return {
            "status": "failed",
            "stage": "check",
            "reason": (check_proc.stderr.strip() or check_proc.stdout.strip()
                       or "run_check.py failed"),
        }

    if check_rc == 2:
        # needs_ai — return signal for Claude to handle
        check_result_path = reports_dir / f"check_result_{pkgname}.json"
        return {
            "status": "needs_ai",
            "stage": "check",
            "check_result": str(check_result_path),
            "reason": "run_check.py returned needs_ai, requires LLM to resolve "
                      "license or version",
        }

    # check_rc == 0 — all steps passed, proceed to gate
    # ── run_gate.py ───────────────────────────────────────────────────
    session = _read_json(session_dir / "session.json")

    gate_cmd = [
        sys.executable, str(_RUN_GATE),
        "--pkg", pkgname,
        "--url", url,
        "--mode", mode,
        "--pkg-dir", str(reports_dir),
        "--copr-url", session.get("copr_url", ""),
        "--copr-owner", session.get("copr_owner", ""),
        "--copr-project", session.get("copr_project", ""),
        "--copr-login", session.get("copr_login", ""),
        "--copr-token", session.get("copr_token", ""),
        "--copr-chroot", session.get("copr_chroot", ""),
    ]
    if constraint:
        gate_cmd += ["--constraint", constraint]

    gate_proc = subprocess.run(gate_cmd, capture_output=True, text=True, timeout=120)

    if gate_proc.returncode != 0:
        return {
            "status": "failed",
            "stage": "gate",
            "reason": gate_proc.stderr.strip() or gate_proc.stdout.strip()
                      or "run_gate.py failed",
        }

    # Gate succeeded — read result and update dep_registry
    gate_result_path = reports_dir / f"gate_result_{pkgname}.json"
    if gate_result_path.exists():
        gate = _read_json(gate_result_path)
        decision = (gate.get("result") or {}).get("decision", "")
        lang = (gate.get("result") or {}).get("lang", "")
        version_detected = (gate.get("result") or {}).get("version", "")

        # Update dep_registry
        reg_path = session_dir / "dep_registry.json"
        if reg_path.exists() and mode == "dependency":
            reg = _read_json(reg_path)
            if pkgname in reg:
                reg[pkgname]["status"] = "evaluate_done"
                if lang:
                    reg[pkgname]["lang"] = lang
                _write_json(reg_path, reg)

        return {
            "status": "done",
            "decision": decision,
            "lang": lang,
            "version": version_detected,
            "gate_result": str(gate_result_path),
        }

    return {"status": "failed", "stage": "gate", "reason": "gate_result not found"}


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run evaluate (check + gate) for a dep without AI"
    )
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--mode", default="dependency", choices=["top-level", "dependency"])
    parser.add_argument("--url", required=True)
    parser.add_argument("--constraint", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--session-dir", required=True)
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    result = run(
        session_dir=session_dir,
        pkgname=args.pkg,
        mode=args.mode,
        url=args.url,
        constraint=args.constraint,
        version=args.version,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in ("done", "failed") else 1


if __name__ == "__main__":
    sys.exit(main())
