#!/usr/bin/env python3
"""Phase 2：引入门禁（COPR 模式，无 Docker）

前提：run_check.py 已成功跑完（overall_status=done），
      check_result_<pkgname>.json 中有确认的 lang/version。

步骤：existing_check（直接本地 dnf + COPR API，无需容器）

输出 gate_result_<pkgname>.json，格式：
  overall_status: "done" | "failed"
  result.decision: "reuse_official" | "reuse_copr_project" | "introduce_new"

exit codes:
  0  overall_status=done
  1  overall_status=failed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_PKG_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_PKG_SCRIPTS))

from check_existing_package import check_existing_package  # noqa: E402

GATE_STEPS = ["existing_check"]


def _get_project_chroots(copr_url: str, owner: str, project: str,
                          login: str, token: str) -> list:
    """查询 COPR project 的 chroot 列表，返回 x86_64 chroot 名（优先）。"""
    import base64, urllib.request, urllib.error
    creds = base64.b64encode(f"{login}:{token}".encode()).decode()
    url = f"{copr_url.rstrip('/')}/api_3/project?ownername={owner}&projectname={project}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        chroots = list(d.get("chroot_repos", {}).keys())
        # 优先取 x86_64，否则取第一个
        x86 = [c for c in chroots if c.endswith("-x86_64")]
        return x86 if x86 else chroots
    except Exception:
        return []


def _save(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _already_done(step_data: dict) -> bool:
    return step_data.get("status") in ("done", "skipped")


def run_gate(args: argparse.Namespace) -> int:
    reports_dir      = Path(args.reports_dir)
    check_report_path = reports_dir / f"check_result_{args.pkg}.json"
    gate_report_path  = reports_dir / f"gate_result_{args.pkg}.json"

    if not check_report_path.exists():
        print(f"[ERROR] check_result_{args.pkg}.json not found; run run_check.py first",
              file=sys.stderr)
        return 1

    check = json.loads(check_report_path.read_text(encoding="utf-8"))
    if check.get("overall_status") != "done":
        print(f"[ERROR] check phase not done (status={check.get('overall_status')})",
              file=sys.stderr)
        return 1

    cr      = check.get("result") or {}
    lang    = args.lang    or cr.get("lang", "")
    version = args.version or cr.get("version", "")

    if not lang or not version:
        print(f"[ERROR] lang or version missing (lang={lang!r}, version={version!r})",
              file=sys.stderr)
        return 1

    # Load or init gate report
    if gate_report_path.exists():
        report = json.loads(gate_report_path.read_text(encoding="utf-8"))
    else:
        report = {
            "pkgname":        args.pkg,
            "lang":           lang,
            "version":        version,
            "overall_status": "pending",
            "steps":          {step: {"status": "pending"} for step in GATE_STEPS},
            "result":         None,
        }

    steps = report["steps"]

    # COPR 凭据（优先参数，其次环境变量）
    copr_url = args.copr_url or os.environ.get("COPR_FRONTEND_URL", "http://copr-frontend:5000")
    owner    = args.copr_owner  or os.environ.get("COPR_OWNER", "")
    project  = args.copr_project or os.environ.get("COPR_PROJECT", "")
    login    = args.copr_login  or os.environ.get("COPR_API_LOGIN", "")
    token    = args.copr_token  or os.environ.get("COPR_API_TOKEN", "")

    # chroot 优先从 session.json 读，避免重新查 COPR API
    chroot = args.copr_chroot or os.environ.get("COPR_CHROOT", "")
    if not chroot:
        session_json = Path(args.reports_dir).parents[1] / "session.json"
        if not session_json.exists():
            # 兜底：向上查找 session.json
            for p in Path(args.reports_dir).parents:
                candidate = p / "session.json"
                if candidate.exists():
                    session_json = candidate
                    break
        if session_json.exists():
            try:
                chroot = json.loads(session_json.read_text(encoding="utf-8")).get("copr_chroot", "")
            except Exception:
                pass
    if not chroot:
        # 最后兜底：从 COPR API 查（向后兼容）
        chroots = _get_project_chroots(copr_url, owner, project, login, token)
        chroot  = chroots[0] if chroots else ""

    try:
        if not _already_done(steps["existing_check"]):
            result = check_existing_package(
                args.pkg,
                version=version,
                requirement=args.constraint,
                lang=lang,
                copr_url=copr_url,
                owner=owner,
                project=project,
                login=login,
                token=token,
                chroot=chroot,
            )
            decision = result["decision"]
            steps["existing_check"] = {
                "status":   "done",
                "decision": decision,
                "reason":   result.get("reason", ""),
                "chroot":   chroot,
            }
            _save(report, gate_report_path)

    except Exception as exc:
        report["overall_status"] = "failed"
        steps["existing_check"] = {"status": "failed", "reason": str(exc)}
        _save(report, gate_report_path)
        print(json.dumps({"status": "failed", "report": str(gate_report_path)},
                         ensure_ascii=False))
        return 1

    all_done = all(s.get("status") in ("done", "skipped") for s in steps.values())
    report["overall_status"] = "done" if all_done else "failed"
    report["result"] = {
        "lang":       lang,
        "version":    version,
        "decision":   steps["existing_check"].get("decision", ""),
        "reason":     steps["existing_check"].get("reason", ""),
        "copr_owner": owner,
        "copr_project": project,
    }
    _save(report, gate_report_path)

    print(json.dumps({"status": report["overall_status"], "report": str(gate_report_path)},
                     ensure_ascii=False))
    return 0 if report["overall_status"] == "done" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="pkg-introduce Phase 2: 引入门禁（COPR 模式）")
    parser.add_argument("--pkg",          required=True)
    parser.add_argument("--url",          default="", dest="upstream_url")
    parser.add_argument("--lang",         default="")
    parser.add_argument("--version",      default="")
    parser.add_argument("--constraint",   default="")
    parser.add_argument("--mode",         default="top-level", choices=["top-level", "dependency"])
    parser.add_argument("--reports-dir",  default="./reports", dest="reports_dir")
    parser.add_argument("--pkg-dir",      default=None, dest="pkg_dir")
    # COPR 凭据（可通过环境变量替代）
    parser.add_argument("--copr-url",     default="", dest="copr_url")
    parser.add_argument("--copr-owner",   default="", dest="copr_owner")
    parser.add_argument("--copr-project", default="", dest="copr_project")
    parser.add_argument("--copr-login",   default="", dest="copr_login")
    parser.add_argument("--copr-token",   default="", dest="copr_token")
    parser.add_argument("--copr-chroot",  default="", dest="copr_chroot",
                        help="目标 chroot（优先级：参数 > session.json > COPR API 查询）")
    args = parser.parse_args()
    if args.pkg_dir:
        args.reports_dir = args.pkg_dir
    return run_gate(args)


if __name__ == "__main__":
    sys.exit(main())
