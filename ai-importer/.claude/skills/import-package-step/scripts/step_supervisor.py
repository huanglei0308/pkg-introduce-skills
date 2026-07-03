#!/usr/bin/env python3
"""import-package-step 状态机。

读取 session 状态，输出下一步 action，并在 action 完成后更新状态。

用法：
  # 读状态，输出 action
  python3 step_supervisor.py --session-dir /path/to/session

  # action 完成后更新状态
  python3 step_supervisor.py --session-dir /path/to/session \
      --update-action build_dep --update-target dj-static \
      --build-result success --ci-status pass

  # 标记 dep 为 reused（evaluate 完成后）
  python3 step_supervisor.py --session-dir /path/to/session \
      --update-action evaluate --update-target static3 \
      --gate-decision reuse_official

输出 JSON：
  {"action": "build_dep", "target": "dj-static", "delay": 60, "loop": 6}
  {"action": "done", "target": "sites-faciles", "delay": null, "loop": 10}
  {"action": "fail", "target": "dep build_failed: [...]", "delay": null, "loop": 3}
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

# build_rpm_result.json 的合法终态
# precheck_done  — 预检通过但构建未完成（agent 中断），视为"待构建"
# interrupted    — agent 异常退出，视为"待构建"
# copr_running   — COPR 构建已提交但 wait_for_build 超时，build_id 已记录，supervisor 轮询
VALID_BUILD_STATUSES = {
    "success", "dep_needed", "failed", "ci_failed", "precheck_done", "interrupted", "copr_running"
}

# dep_registry 中表示"已就绪"的状态（等价于 build_done）
DEP_READY_STATUSES = {"build_done", "reused"}

# dep_registry 中表示"等待自身前置依赖就绪"的状态
DEP_WAITING_STATUS = "pending_deps"

# 编译慢的语言，用较长延迟
SLOW_LANGS = {"rust", "go", "c", "cpp"}



def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _poll_copr_build(build_id: int, sd: Path) -> str | None:
    """轮询 COPR build 状态，返回 'succeeded'/'failed'/'running'。
    读取 session.json 里的 COPR 凭据，失败时返回 None。
    """
    try:
        session = read_json(sd / "session.json")
        login = session.get("copr_login", "")
        token = session.get("copr_token", "")
        frontend = session.get("copr_url", "http://copr-frontend:5000")
        if not login or not token:
            return None
        import urllib.request, base64
        creds = base64.b64encode(f"{login}:{token}".encode()).decode()
        req = urllib.request.Request(
            f"{frontend}/api_3/build/{build_id}",
            headers={"Authorization": f"Basic {creds}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        state = data.get("state", "")
        terminal = {"succeeded", "failed", "canceled", "skipped"}
        if state == "succeeded":
            return "succeeded"
        if state in terminal:
            return "failed"
        return "running"
    except Exception as e:
        print(f"[warn] _poll_copr_build({build_id}) error: {e}", file=sys.stderr)
        return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_lang(sd: Path, pkgname: str) -> str:
    gate_f = sd / f"pkgs/{pkgname}/gate_result_{pkgname}.json"
    if gate_f.exists():
        return read_json(gate_f).get("result", {}).get("lang", "")
    return ""


def build_delay(lang: str) -> int:
    return 270 if lang in SLOW_LANGS else 60


MAX_DEP_DEPTH = 5


def compute_depth(dep_name: str, reg: dict, pkgname: str, _seen: frozenset = frozenset()) -> int:
    """从 required_by 链计算依赖深度。主包 = 0，直接依赖 = 1，以此类推。
    找不到上级时返回 1（降级处理，不阻断主流程）。"""
    entry = reg.get(dep_name, {})
    parent = entry.get("required_by", "")
    if not parent or parent == pkgname:
        return 1
    if parent not in reg:
        return 1  # 上级不在 registry，降级为直接依赖
    if parent in _seen:
        return 99  # 循环依赖保护：parent 已在访问链上
    return 1 + compute_depth(parent, reg, pkgname, _seen | {dep_name})


def determine_action(sd: Path, wf: dict, reg: dict) -> tuple[str, str, int | None]:
    """返回 (action, target, delay_seconds)。delay=None 表示停止循环。"""
    PKGNAME = wf["pkgname"]

    # 优先级 -1：evaluate_main 失败，等待 AI 分析
    if wf.get("evaluate_failed"):
        analysis_file = sd / f"pkgs/{PKGNAME}/evaluate_analysis_{PKGNAME}.json"
        if not analysis_file.exists():
            return ("analyze_evaluate_main", PKGNAME, 0)
        data = read_json(analysis_file)
        verdict = data.get("verdict", "abort")
        if verdict == "retry":
            wf.pop("evaluate_failed", None)
            wf_files = list(sd.glob("workflow_*.json"))
            if wf_files:
                write_json(wf_files[0], wf)
            (sd / f"pkgs/{PKGNAME}/gate_result_{PKGNAME}.json").unlink(missing_ok=True)
            analysis_file.unlink(missing_ok=True)
            return ("evaluate_main", PKGNAME, 60)
        return ("fail", data.get("reason", wf.get("evaluate_failed", "evaluate failed")), None)

    # 优先级 0：主包 gate_result 不存在或内容无效 → evaluate_main
    gate_path = sd / f"pkgs/{PKGNAME}/gate_result_{PKGNAME}.json"
    gate_valid = False
    if gate_path.exists():
        try:
            g = read_json(gate_path)
            decision = g.get("result", {}).get("decision", "")
            if g.get("overall_status") == "done" and \
               decision in ("introduce_new", "reuse_official", "reuse_copr_project"):
                gate_valid = True
            elif decision == "check_failed":
                # 网络/下载临时失败，删除后重试（不算损坏，delay 长一些）
                gate_path.unlink(missing_ok=True)
                return ("evaluate_main", PKGNAME, 120)
        except Exception:
            pass
    if not gate_valid:
        if gate_path.exists():
            gate_path.unlink(missing_ok=True)  # 损坏则删除，下次重跑
        return ("evaluate_main", PKGNAME, 60)

    # gate_result 已确认 reuse → 直接 done（goal_achieved 由 update 写入）
    if wf.get("goal_achieved") is True:
        return ("done", PKGNAME, None)

    # 读主包 build_rpm_result
    main_result_path = sd / f"pkgs/{PKGNAME}/build_rpm_result.json"
    main_result = None
    if main_result_path.exists():
        try:
            main_result = read_json(main_result_path)
        except Exception:
            pass
    main_status = main_result.get("status") if main_result else None
    if main_status and main_status not in VALID_BUILD_STATUSES:
        main_status = None
        main_result = None

    # build_rpm_result 为空/无效时，标记为 interrupted 触发重建
    if main_status is None and main_result_path.exists():
        try:
            raw = read_json(main_result_path)
            if raw.get("status") not in VALID_BUILD_STATUSES:
                raw["status"] = "interrupted"
                write_json(main_result_path, raw)
        except Exception:
            pass

    # 优先级 1a：有 dep evaluate 失败，等待 AI 分析
    failed_eval_deps = [k for k, v in reg.items() if v["status"] == "evaluate_failed"]
    if failed_eval_deps:
        dep = failed_eval_deps[0]
        analysis_file = sd / f"pkgs/{dep}/evaluate_analysis_{dep}.json"
        if not analysis_file.exists():
            return ("analyze_evaluate", dep, 0)
        data = read_json(analysis_file)
        verdict = data.get("verdict", "abort")
        if verdict == "retry":
            reg[dep]["status"] = "pending_evaluate"
            reg[dep].pop("error", None)
            write_json(sd / "dep_registry.json", reg)
            analysis_file.unlink(missing_ok=True)
            (sd / f"pkgs/{dep}/gate_result_{dep}.json").unlink(missing_ok=True)
            return ("evaluate", dep, 60)
        reason = data.get("reason", reg[dep].get("error", f"dep {dep} evaluate failed"))
        return ("fail", reason, None)

    # 优先级 1b：有 dep 待 evaluate
    pending_eval = [k for k, v in reg.items() if v["status"] == "pending_evaluate"]
    if pending_eval:
        over_depth = [k for k in pending_eval if compute_depth(k, reg, PKGNAME) > MAX_DEP_DEPTH]
        if over_depth:
            return ("fail", f"dep depth exceeded {MAX_DEP_DEPTH}: {over_depth}", None)
        return ("evaluate", pending_eval[0], 60)

    # 优先级 2：有 dep 待构建
    # pending_deps 状态：该 dep 曾返回 dep_needed，等待其前置依赖就绪后再重试
    # 若其所有前置依赖（required_by 链上新增的 dep）已就绪，则升回 evaluate_done
    reg_path_local = sd / "dep_registry.json"
    promoted = False
    for dep_name, dep_info in list(reg.items()):
        if dep_info["status"] == DEP_WAITING_STATUS:
            blockers = [k for k, v in reg.items()
                        if v.get("required_by") == dep_name
                        and v["status"] not in DEP_READY_STATUSES]
            if not blockers:
                reg[dep_name]["status"] = "evaluate_done"
                promoted = True
    if promoted:
        write_json(reg_path_local, reg)

    pending_build = [k for k, v in reg.items() if v["status"] == "evaluate_done"]
    if pending_build:
        over_depth = [k for k in pending_build if compute_depth(k, reg, PKGNAME) > MAX_DEP_DEPTH]
        if over_depth:
            return ("fail", f"dep depth exceeded {MAX_DEP_DEPTH}: {over_depth}", None)
        dep = pending_build[0]
        lang = get_lang(sd, dep)
        return ("build_dep", dep, build_delay(lang))

    # 优先级 2.5：有 dep 处于 copr_running，全量轮询所有，更新 dep_registry 状态
    # 注意：_finalize_copr_build（拉日志）不在这里调用，由 job_runner wait loop 负责
    copr_running_deps = [k for k, v in reg.items() if v["status"] == "copr_running"]
    if copr_running_deps:
        changed = False
        still_running = []
        for dep in copr_running_deps:
            build_id = reg[dep].get("copr_build_id")
            if not build_id:
                reg[dep]["status"] = "evaluate_done"
                changed = True
                continue
            copr_state = _poll_copr_build(build_id, sd)
            if copr_state == "succeeded":
                reg[dep]["status"] = "build_done"
                changed = True
            elif copr_state == "failed":
                reg[dep]["status"] = "build_failed"
                reg[dep]["error"] = f"copr build {build_id} failed"
                dep_result_path = sd / f"pkgs/{dep}/build_rpm_result.json"
                if dep_result_path.exists():
                    try:
                        br = read_json(dep_result_path)
                        if br.get("status") == "copr_running":
                            br["status"] = "failed"
                            br["failure_reason"] = f"copr build {build_id} failed"
                            write_json(dep_result_path, br)
                    except Exception:
                        pass
                changed = True
            else:
                still_running.append(dep)
        if changed:
            write_json(reg_path_local, reg)
        if still_running:
            return ("wait", f"{','.join(still_running)}(copr_running)", 60)
        # 所有 dep 状态已更新，重新检查 pending_deps 是否可晋升（2.5 结束后补跑一次 Priority 2 逻辑）
        promoted = False
        for dep_name, dep_info in list(reg.items()):
            if dep_info["status"] == DEP_WAITING_STATUS:
                blockers = [k for k, v in reg.items()
                            if v.get("required_by") == dep_name
                            and v["status"] not in DEP_READY_STATUSES]
                if not blockers:
                    reg[dep_name]["status"] = "evaluate_done"
                    promoted = True
        if promoted:
            write_json(reg_path_local, reg)
            pending_build = [k for k, v in reg.items() if v["status"] == "evaluate_done"]
            if pending_build:
                dep = pending_build[0]
                lang = get_lang(sd, dep)
                return ("build_dep", dep, build_delay(lang))

    # 优先级 3：有 dep 构建失败
    failed_deps = [k for k, v in reg.items() if v["status"] == "build_failed"]
    if failed_deps:
        dep = failed_deps[0]
        dep_build_id = reg[dep].get("copr_build_id")
        analysis_file = sd / f"pkgs/{dep}/failure_analysis_{dep}_{dep_build_id}.json" if dep_build_id else sd / f"pkgs/{dep}/failure_analysis_{dep}.json"
        # 兜底：agent 在 build_id 为空时可能写成 failure_analysis_{dep}_.json（尾部多下划线）
        if not analysis_file.exists():
            fallback = sd / f"pkgs/{dep}/failure_analysis_{dep}_.json"
            if fallback.exists():
                analysis_file = fallback
            else:
                return ("analyze_failure_dep", dep, 0)
        verdict = read_json(analysis_file).get("verdict", "abort")
        if verdict in ("rebuild", "retry"):
            reg[dep]["status"] = "evaluate_done"
            write_json(reg_path_local, reg)
            lang = get_lang(sd, dep)
            return ("build_dep", dep, build_delay(lang))
        reason = read_json(analysis_file).get("reason", f"dep {dep} build failed")
        return ("fail", reason, None)

    # 优先级 4：所有 dep 完成（或无 dep），处理主包
    all_deps_ready = all(v["status"] in DEP_READY_STATUSES for v in reg.values())
    if all_deps_ready or not reg:
        # 主包 copr_running：轮询 COPR API，只更新本地状态文件
        if main_status == "copr_running":
            build_id = main_result.get("copr_build_id") if main_result else None
            if build_id:
                copr_state = _poll_copr_build(build_id, sd)
                if copr_state == "succeeded":
                    main_result["status"] = "success"
                    write_json(main_result_path, main_result)
                    main_status = "success"
                elif copr_state == "failed":
                    main_result["status"] = "failed"
                    write_json(main_result_path, main_result)
                    main_status = "failed"
                else:
                    return ("wait", f"{PKGNAME}(build_id={build_id})", 60)
            else:
                main_result["status"] = "interrupted"
                write_json(main_result_path, main_result)
                main_status = "interrupted"

        if main_status in (None, "dep_needed", "precheck_done", "interrupted"):
            lang = get_lang(sd, PKGNAME)
            return ("build_main", PKGNAME, build_delay(lang))

        if main_status in ("failed", "ci_failed"):
            build_id = main_result.get("copr_build_id") if main_result else None
            analysis_file = sd / f"pkgs/{PKGNAME}/failure_analysis_{PKGNAME}_{build_id}.json" if build_id else sd / f"pkgs/{PKGNAME}/failure_analysis_{PKGNAME}.json"
            if not analysis_file.exists():
                return ("analyze_failure", PKGNAME, 0)
            verdict = read_json(analysis_file).get("verdict", "abort")
            if verdict == "rebuild":
                lang = get_lang(sd, PKGNAME)
                return ("rebuild", PKGNAME, build_delay(lang))
            if verdict == "retry":
                main_result_path.write_text(json.dumps({"status": "interrupted"}))
                lang = get_lang(sd, PKGNAME)
                return ("build_main", PKGNAME, build_delay(lang))
            reason = read_json(analysis_file).get("reason", f"main build {main_status}")
            return ("fail", reason, None)

        if main_status == "success":
            # 跳过 critique，直接 feedback → summary → done
            feedback_file = sd / f"pkgs/{PKGNAME}/feedback_{PKGNAME}.json"
            if not feedback_file.exists():
                return ("feedback", PKGNAME, 60)
            summary_file = sd / f"pkgs/{PKGNAME}/{PKGNAME}_introduction_report.md"
            if not summary_file.exists():
                return ("summary", PKGNAME, 60)
            return ("done", PKGNAME, None)

        return ("fail", f"unexpected main_status: {main_status}", None)

    return ("fail", "unexpected dep_registry state", None)


def _satisfies_constraint(version: str, constraint: str) -> bool:
    """检查 version 是否满足 constraint 约束字符串（如 '>= 1.0, != 1.2'）。"""
    if not version or not constraint:
        return True
    try:
        from packaging.version import Version
        from packaging.specifiers import SpecifierSet
        return Version(version) in SpecifierSet(constraint)
    except Exception:
        return True  # 无法解析时保守认为满足


def _get_resolved_version(sd: Path, pkgname: str) -> str:
    """从 gate_result 读取已解析版本，找不到返回空串。"""
    gate_f = sd / f"pkgs/{pkgname}/gate_result_{pkgname}.json"
    if gate_f.exists():
        return read_json(gate_f).get("result", {}).get("version", "")
    return ""


def _downgrade_stale_deps(sd: Path, reg: dict) -> bool:
    """扫描 dep_registry，将 resolved_version 不满足当前 constraint 的 ready dep 降回 pending_evaluate。

    返回 True 表示有 dep 被降级（调用方需写回文件）。
    """
    changed = False
    for pkg, entry in reg.items():
        if entry.get("status") not in DEP_READY_STATUSES:
            continue
        constraint = entry.get("constraint", "")
        if not constraint:
            continue
        resolved = entry.get("resolved_version") or _get_resolved_version(sd, pkg)
        if resolved and not _satisfies_constraint(resolved, constraint):
            entry["status"] = "pending_evaluate"
            entry.pop("resolved_version", None)
            changed = True
    return changed


def update_after_evaluate_main(sd: Path, wf: dict, wf_path: Path, gate_decision: str) -> None:
    """主包 evaluate_main 完成后更新 workflow。"""
    PKGNAME = wf["pkgname"]
    if gate_decision in ("reuse_official", "reuse_copr_project"):
        wf.setdefault("reused_pkgs", [])
        if PKGNAME not in wf["reused_pkgs"]:
            wf["reused_pkgs"].append(PKGNAME)
        wf["goal_achieved"] = True  # 下一轮 determine_action 直接返回 done
    elif gate_decision == "introduce_new":
        pass  # gate_result 文件已存在，下一轮走 build_main
    else:
        # gate 失败（空 decision 或未知值）：写入 evaluate_failed，等待 AI 分析
        wf["evaluate_failed"] = gate_decision or "evaluate_main gate failed"
    wf["loop_count"] = wf.get("loop_count", 0) + 1
    write_json(wf_path, wf)


def update_after_evaluate(sd: Path, reg: dict, reg_path: Path, target: str, gate_decision: str) -> None:
    """evaluate 完成后更新 dep_registry。"""
    if "reuse" in gate_decision:
        reg[target]["status"] = "reused"
        # 记录实际解析版本，用于后续约束降级检查
        v = _get_resolved_version(sd, target)
        if v:
            reg[target]["resolved_version"] = v
    elif gate_decision in ("introduce_new", "upgrade_user_repo"):
        reg[target]["status"] = "evaluate_done"
    else:
        # gate 失败：写入 evaluate_failed，等待 AI 分析
        reg[target]["status"] = "evaluate_failed"
        reg[target]["error"] = gate_decision or "evaluate gate failed"
    write_json(reg_path, reg)


def update_after_build(
    sd: Path, wf: dict, wf_path: Path, reg: dict, reg_path: Path,
    target: str, build_status: str, is_dep: bool
) -> None:
    """build_dep / build_main 完成后更新状态。"""
    if build_status == "success":
        if is_dep:
            reg[target]["status"] = "build_done"
            write_json(reg_path, reg)
        wf.setdefault("built_pkgs", [])
        if target not in wf["built_pkgs"]:
            wf["built_pkgs"].append(target)

    elif build_status == "copr_running":
        # COPR 构建已提交但 wait_for_build 超时，从 build_rpm_result.json 读 copr_build_id
        copr_build_id = None
        result_p = sd / f"pkgs/{target}/build_rpm_result.json"
        if result_p.exists():
            copr_build_id = read_json(result_p).get("copr_build_id")
        if is_dep:
            reg[target]["status"] = "copr_running"
            if copr_build_id:
                reg[target]["copr_build_id"] = copr_build_id
            write_json(reg_path, reg)
        # 主包的 copr_running 直接保留在 build_rpm_result.json 里，supervisor 轮询时读取

    elif build_status == "dep_needed":
        # 新 dep 已写入 dep_registry，重新读取；把当前 target 标为 pending_deps
        # 等其前置依赖全部就绪后，determine_action 会自动升回 evaluate_done
        reg_new = read_json(reg_path)
        reg.clear()
        reg.update(reg_new)
        if is_dep and target in reg:
            reg[target]["status"] = DEP_WAITING_STATUS
        # 扫描并降级：reused/build_done 但 resolved_version 不满足最新 constraint
        _downgrade_stale_deps(sd, reg)
        write_json(reg_path, reg)

    elif build_status in ("precheck_done", "interrupted") or build_status not in VALID_BUILD_STATUSES:
        # 构建未完成，保持 evaluate_done，下次重建
        print(f"[warn] {target} build_rpm_result.status={build_status!r}, will retry", file=sys.stderr)

    else:
        # failed / ci_failed
        if is_dep:
            reg[target]["status"] = "build_failed"
            reg[target]["error"] = build_status
            write_json(reg_path, reg)


_STATUS_LABEL: dict[str, str] = {
    "pending_evaluate": "待评估",
    "evaluate_done":    "待构建",
    "pending_deps":     "等待依赖",
    "reused":           "复用(跳过)",
    "build_done":       "构建完成",
    "build_failed":     "构建失败",
    "copr_running":     "COPR构建中",
}

_MAIN_STATUS_LABEL: dict[str, str] = {
    None:           "待构建",
    "dep_needed":   "缺少依赖",
    "precheck_done":"预检完成",
    "interrupted":  "中断(待重建)",
    "success":      "构建成功",
    "failed":       "构建失败",
    "ci_failed":    "CI失败",
}


def print_progress(sd: Path, wf: dict, reg: dict, next_action: str, next_target: str) -> None:
    """向 CLI 打印本轮进展摘要。"""
    PKGNAME = wf["pkgname"]
    loop = wf.get("loop_count", 0) + 1
    # 主包状态
    main_result_path = sd / f"pkgs/{PKGNAME}/build_rpm_result.json"
    if main_result_path.exists():
        raw = read_json(main_result_path).get("status")
        main_status = raw if raw in VALID_BUILD_STATUSES else None
    else:
        main_status = None
    main_label = _MAIN_STATUS_LABEL.get(main_status, main_status or "待构建")

    # feedback 状态
    feedback_file = sd / f"pkgs/{PKGNAME}/feedback_{PKGNAME}.json"
    review_label = "feedback完成" if feedback_file.exists() else "-"

    sep = "─" * 56
    print(f"\n{sep}")
    print(f"  包引入进展  [{PKGNAME}]  第 {loop} 步")
    print(sep)
    print(f"  主包  {PKGNAME:<30} {main_label}  review: {review_label}")

    total_deps = len(reg)
    if total_deps:
        done_deps = sum(1 for v in reg.values() if v["status"] in DEP_READY_STATUSES)
        failed_deps_list = [k for k, v in reg.items() if v["status"] == "build_failed"]
        print(f"  依赖  共 {total_deps} 个，已就绪 {done_deps} 个"
              + (f"，失败 {len(failed_deps_list)} 个: {failed_deps_list}" if failed_deps_list else ""))
        # 逐条打印非就绪依赖（减少噪音，只展示未完成的）
        pending_deps_list = [(k, v) for k, v in reg.items() if v["status"] not in DEP_READY_STATUSES]
        if pending_deps_list:
            print("  ┌─ 未完成依赖:")
            for dep_name, dep_info in pending_deps_list:
                label = _STATUS_LABEL.get(dep_info["status"], dep_info["status"])
                required_by = dep_info.get("required_by", "")
                by_str = f"  ← {required_by}" if required_by and required_by != PKGNAME else ""
                print(f"  │  {dep_name:<30} {label}{by_str}")
            print("  └─")
    else:
        print("  依赖  无")

    print(f"  → 下一步: {next_action}({next_target})")
    print(sep + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="import-package-step 状态机")
    parser.add_argument("--session-dir", required=True)

    # 更新模式参数
    parser.add_argument("--update-action", choices=["evaluate_main", "evaluate", "build_dep", "build_main", "rebuild", "done", "fail"])
    parser.add_argument("--update-target", default="")
    parser.add_argument("--gate-decision", default="")   # evaluate 完成后
    parser.add_argument("--build-result", default="")    # build 完成后

    args = parser.parse_args()
    sd = Path(args.session_dir)

    wf_files = list(sd.glob("workflow_*.json"))
    if not wf_files:
        print(json.dumps({"error": "no workflow file found"}))
        return 1
    wf_path = wf_files[0]
    wf = read_json(wf_path)
    PKGNAME = wf["pkgname"]

    reg_path = sd / "dep_registry.json"
    reg = read_json(reg_path) if reg_path.exists() else {}

    # ── 更新模式 ──────────────────────────────────────────────────────────────
    if args.update_action:
        if args.update_action == "evaluate_main":
            update_after_evaluate_main(sd, wf, wf_path, args.gate_decision)
            print(json.dumps({"updated": True}))
            return 0

        elif args.update_action == "evaluate":
            update_after_evaluate(sd, reg, reg_path, args.update_target, args.gate_decision)

        elif args.update_action in ("build_dep", "build_main"):
            is_dep = args.update_action == "build_dep"
            update_after_build(
                sd, wf, wf_path, reg, reg_path,
                args.update_target, args.build_result, is_dep
            )

        elif args.update_action == "done":
            wf["goal_achieved"] = True

        elif args.update_action == "fail":
            wf["goal_achieved"] = False
            wf["error"] = args.update_target

        wf["loop_count"] = wf.get("loop_count", 0) + 1
        write_json(wf_path, wf)
        print(json.dumps({"updated": True}))
        return 0

    # ── 读状态模式：输出下一步 action ─────────────────────────────────────────
    # 检查 dep 的非标准 status，打印警告
    for dep_name, dep_info in reg.items():
        if dep_info["status"] != "evaluate_done":
            continue
        dep_result_path = sd / f"pkgs/{dep_name}/build_rpm_result.json"
        if dep_result_path.exists():
            dep_status = read_json(dep_result_path).get("status")
            if dep_status and dep_status not in VALID_BUILD_STATUSES:
                print(f"[warn] dep {dep_name} non-standard status={dep_status!r}, will rebuild", file=sys.stderr)

    action, target, delay = determine_action(sd, wf, reg)
    loop = wf.get("loop_count", 0) + 1

    print_progress(sd, wf, reg, action, target)

    # evaluate action 时附带 constraint，供 evaluator 做版本选择
    constraint = ""
    if action == "evaluate" and target in reg:
        entry = reg[target]
        constraint = entry.get("constraint", "") if isinstance(entry, dict) else ""

    result = {"action": action, "target": target, "delay": delay, "loop": loop, "pkgname": PKGNAME, "constraint": constraint}
    for k, v in result.items():
        print(f"{k.upper()}={shlex.quote(str(v) if v is not None else '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
