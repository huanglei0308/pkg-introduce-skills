"""
Single-job execution（COPR 模式）。

job_runner 做三件事：
  1. 初始化 session 目录 + session.json + workflow_<pkgname>.json
  2. 循环：先用 step_supervisor 判断下一步，wait 时纯 Python sleep，
     其他 action 才启 claude -p /import-package-step
  3. 写回 Redis job 最终状态
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

JOB_PREFIX  = "job:ai:"
LOGS_PREFIX = "logs:ai:"

SKILLS_DIR    = os.environ.get("SKILLS_DIR", "/app/.claude/skills")
SESSIONS_BASE = Path(os.environ.get("SESSIONS_BASE", "/tmp/ai-sessions"))

SUPERVISOR    = Path(SKILLS_DIR) / "import-package-step/scripts/step_supervisor.py"
RUN_EVALUATE  = Path(SKILLS_DIR) / "import-package-step/scripts/run_evaluate_dep.py"

# timeline.py 写入接口（供 job_runner / step_supervisor / 脚本共用）
_SCRIPTS_DIR = str(Path(SKILLS_DIR) / "import-package-step/scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from timeline import write_event

MAX_JOB_SECONDS = int(os.environ.get("MAX_JOB_SECONDS", str(4 * 3600)))
MAX_LOOPS       = int(os.environ.get("MAX_LOOPS", "200"))


def _log(r, job_id, msg):
    r.rpush(f"{LOGS_PREFIX}{job_id}", json.dumps({"msg": msg, "t": time.time()}))


def _finish(r, job_id, status, error=""):
    _log(r, job_id, f"[引包] 完成  status={status}" + (f"  error={error}" if error else ""))
    r.hset(f"{JOB_PREFIX}{job_id}", "status", status)
    if error:
        r.hset(f"{JOB_PREFIX}{job_id}", "error", error)
    r.rpush(f"{LOGS_PREFIX}{job_id}", json.dumps({"done": True, "status": status}))

def _finish_with_timeline(r, job_id, session_dir, status, error="",
                          start_time: float | None = None):
    """_finish + 写 session.completed 事件。所有退出路径统一走这里。"""
    # 读 workflow 收集终态信息
    wf_files = list(session_dir.glob("workflow_*.json"))
    wf_info = {}
    if wf_files:
        try:
            wf = json.loads(wf_files[0].read_text())
            wf_info = {
                "built_pkgs": wf.get("built_pkgs", []),
                "reused_pkgs": wf.get("reused_pkgs", []),
                "loop_count": wf.get("loop_count", 0),
            }
        except Exception:
            pass
    write_event(session_dir, "session.completed", "", {
        "status": status,
        "error": error,
        "duration_s": round(time.time() - start_time, 1) if start_time else 0,
        **wf_info,
    })
    _finish(r, job_id, status, error)


def _init_workflow(session_dir: Path, pkgname: str) -> None:
    """初始化 workflow_<pkgname>.json，已存在则跳过（断点续跑）。"""
    p = session_dir / f"workflow_{pkgname}.json"
    if not p.exists():
        p.write_text(json.dumps({
            "pkgname":    pkgname,
            "goal":       "build_success",
            "loop_count": 0,
            "max_loops":  MAX_LOOPS,
            "built_pkgs":  [],
            "reused_pkgs": [],
            "error":       None,
        }, indent=2, ensure_ascii=False), encoding="utf-8")


def _extract_build_failure(session_dir: Path, pkgname: str, job_id: str = "") -> None:
    """构建失败时提取结构化错误报告（build_failure_<build_id>.json），供 pkg-fixer 诊断。
    best-effort，失败不影响主流程。"""
    try:
        extractor = Path(SKILLS_DIR) / "import-package-step/scripts/extract-build-failure.py"
        subprocess.run(
            [sys.executable, str(extractor),
             "--session-dir", str(session_dir), "--pkg", pkgname],
            check=False, capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        print(f"[sync_copr][{job_id}] extract-build-failure error: {e}", flush=True)


def _sync_copr_result(session_dir: Path, pkgname: str, job_id: str = "") -> None:
    """wait 结束后拉取 COPR build log，写入 build_rpm_result.json。"""
    if not pkgname:
        return

    br_path = session_dir / f"pkgs/{pkgname}/build_rpm_result.json"
    if not br_path.exists():
        return

    sync_start = time.time()
    try:
        import json as _json
        br = _json.loads(br_path.read_text())
        build_id = br.get("copr_build_id")
        copr_chroot = br.get("copr_chroot", "")

        # fallback：从 dep_registry.json 里找 build_id
        if not build_id:
            dep_reg_path = session_dir / "dep_registry.json"
            if dep_reg_path.exists():
                dep_reg = _json.loads(dep_reg_path.read_text())
                dep_entry = dep_reg.get(pkgname, {})
                build_id = dep_entry.get("copr_build_id")
                if not copr_chroot:
                    copr_chroot = dep_entry.get("copr_chroot", "")

        if not build_id or br.get("build_log"):
            return

        print(f"[sync_copr][{job_id}] pulling build log for {pkgname} build_id={build_id}", flush=True)

        # 直接用 docker/importer-worker 里的 copr_client（jobs 凭据）
        session = _json.loads((session_dir / "session.json").read_text())
        login = session.get("copr_login", "")
        token = session.get("copr_token", "")
        copr_url = session.get("copr_url", "http://copr-frontend:5000")
        owner = session.get("copr_owner", "")
        project = session.get("copr_project", "")
        if not copr_chroot:
            copr_chroot = session.get("copr_chroot", "")

        from copr_client import get_build, poll_build_until_done
        def _log_fn(msg): print(f"[sync_copr][{job_id}] {msg}", flush=True)

        # 查当前状态，如果还在跑就等完
        data = get_build(build_id, login, token)
        state = data.get("state", "unknown")

        # 校验包名：防止 pkg-builder 提交了错误的包
        # COPR 返回 source_package.name 是 RPM 包名（python-xxx / python3-xxx），
        # pkgname 是上游名（setuptools）。用 upstream_from_srpm_name 剥离
        # 语言前缀还原为上游名后再比对，兼容 python- 和 python3- 两种前缀。
        actual_pkg = data.get("source_package", {}).get("name", "")
        if actual_pkg and actual_pkg != pkgname:
            expected = pkgname
            try:
                import sys as _sys
                _scripts_dir = str(Path(SKILLS_DIR) / "build-rpm/scripts")
                if _scripts_dir not in _sys.path:
                    _sys.path.insert(0, _scripts_dir)
                from rpm_naming import upstream_from_srpm_name, rpm_name_from_gav
                gate_path = session_dir / f"pkgs/{pkgname}/gate_result_{pkgname}.json"
                lang = ""
                if gate_path.exists():
                    gate_data = _json.loads(gate_path.read_text())
                    lang = gate_data.get("lang", "") or gate_data.get("result", {}).get("lang", "")
                # 从 RPM 名剥离前缀还原上游名（python3-setuptools → setuptools）
                # lang 为空时默认 "python"——Python 是最常见的包语言
                normalized = upstream_from_srpm_name(actual_pkg, lang or "python")
                # Java：pkgname 是 Maven GAV（com.google.j2objc:j2objc-annotations），
                # SRPM 名是 artifactId（j2objc-annotations），expected 侧需归一
                if lang == "java":
                    expected = rpm_name_from_gav(pkgname)
            except Exception:
                normalized = actual_pkg
            if normalized != expected:
                br["status"] = "failed"
                br["failure_reason"] = (
                    f"Package name mismatch: build {build_id} "
                    f"is '{actual_pkg}', expected '{pkgname}'"
                )
                br_path.write_text(_json.dumps(br, indent=2, ensure_ascii=False))
                # MISMATCH 计数写入 fix_state.json：supervisor 对第 2 次 MISMATCH
                # 直接 fail（重生成一次仍 mismatch = 根因不在 spec 文本）
                try:
                    fs_path = session_dir / "pkgs" / pkgname / "fix_state.json"
                    fs = _json.loads(fs_path.read_text()) if fs_path.exists() else {}
                    fs["mismatch_count"] = int(fs.get("mismatch_count", 0) or 0) + 1
                    fs_path.parent.mkdir(parents=True, exist_ok=True)
                    fs_path.write_text(_json.dumps(fs, indent=2, ensure_ascii=False))
                except Exception as e:
                    print(f"[sync_copr][{job_id}] warn: mismatch_count 写入失败: {e}", flush=True)
                print(f"[sync_copr][{job_id}] MISMATCH: build {build_id} is {actual_pkg}, expected {pkgname}",
                      flush=True)
                _extract_build_failure(session_dir, pkgname, job_id)
                return

        terminal = {"succeeded", "failed", "canceled", "skipped"}
        if state not in terminal:
            state = poll_build_until_done(build_id, login, token, _log_fn)

        # 拉 builder-live.log
        backend_url = "http://copr-backend:5002"
        import urllib.request, re, gzip as _gzip
        dir_url = f"{backend_url}/results/{owner}/{project}/{copr_chroot}/"
        build_prefix = f"{build_id:08d}-"
        build_log = ""
        try:
            with urllib.request.urlopen(dir_url, timeout=10) as resp:
                content = resp.read().decode()
            dirs = re.findall(rf'href="({build_prefix}[^"]+/)"', content)
            if dirs:
                build_dir = dir_url + dirs[0]
                for log_name in ("builder-live.log.gz", "builder-live.log"):
                    try:
                        with urllib.request.urlopen(build_dir + log_name, timeout=30) as resp:
                            raw = resp.read()
                            build_log = (_gzip.decompress(raw) if log_name.endswith(".gz") else raw).decode("utf-8", errors="replace")
                            break
                    except Exception:
                        pass
        except Exception:
            pass

        br["copr_status"] = state
        br["build_log"] = build_log[-8000:] if build_log else ""
        br["build_log_tail"] = build_log[-2000:] if build_log else ""
        if state == "succeeded":
            br["status"] = "success"
        else:
            br["status"] = "failed"
            br["failure_reason"] = br.get("failure_reason") or f"copr build {state}"

        br_path.write_text(_json.dumps(br, indent=2, ensure_ascii=False))
        print(f"[sync_copr][{job_id}] {pkgname}: state={state} → build_rpm_result.status={br['status']}", flush=True)

        # ── 时间线：构建结束 ────────────────────────────────────────────
        write_event(session_dir, "build.completed", pkgname, {
            "build_id": str(build_id) if build_id else "",
            "status": state,
            "duration_s": round(time.time() - sync_start, 1),
            "copr_chroot": copr_chroot,
        })

        if br["status"] == "failed":
            _extract_build_failure(session_dir, pkgname, job_id)

    except Exception as e:
        print(f"[sync_copr][{job_id}] error: {e}", flush=True)


def _run_supervisor(session_dir: Path, job_id: str = "") -> dict:
    """直接调 step_supervisor.py（纯 Python，不启 claude），返回解析后的 dict。"""
    result = subprocess.run(
        [sys.executable, str(SUPERVISOR), "--session-dir", str(session_dir)],
        capture_output=True, text=True,
    )
    out = {}
    for line in result.stdout.splitlines():
        if "=" in line and line.split("=", 1)[0].isupper():
            k, _, v = line.partition("=")
            out[k.lower()] = v.strip("'")
        else:
            # 进度摘要行直接打印（print_progress 输出）
            if line.strip():
                print(f"[supervisor][{job_id}] {line}", flush=True)
    if result.returncode != 0 and result.stderr:
        print(f"[supervisor][{job_id}] stderr: {result.stderr[:200]}", flush=True)
    return out


def run_job(r, proj, job_id):
    job        = r.hgetall(f"{JOB_PREFIX}{job_id}")
    pkgname    = job["pkgname"]
    # 归一化：用户可能误传入 RPM 包名（python-numpy），剥离语言前缀还原为上游名
    for _pfx in ["python3-", "python-", "nodejs-"]:
        if pkgname.startswith(_pfx):
            _normalized = pkgname[len(_pfx):]
            _log(r, job_id, f"[归一化] pkgname '{pkgname}' → '{_normalized}'")
            pkgname = _normalized
            break
    url        = job["url"]
    version    = job.get("version", "")
    owner, coprname = proj.split("/", 1)
    copr_login  = job.get("copr_login", "")
    copr_token  = job.get("copr_token", "")
    copr_chroot = job.get("copr_chroot", "")

    # 防御：任务在排队期间被取消，直接退出
    if job.get("status") == "cancelled":
        _log(r, job_id, "Job was cancelled before start, exiting")
        _finish(r, job_id, "cancelled")
        return

    if not copr_login or not copr_token:
        _log(r, job_id, "ERROR: job 缺少 copr_login/copr_token")
        _finish(r, job_id, "failed", "missing credentials")
        return
    if not copr_chroot:
        _log(r, job_id, "ERROR: job 缺少 copr_chroot")
        _finish(r, job_id, "failed", "missing chroot")
        return

    r.hset(f"{JOB_PREFIX}{job_id}", "status", "running")
    _log(r, job_id, f"[引包] pkgname={pkgname}  url={url}"
                    + (f"  version={version}" if version else ""))
    _log(r, job_id, f"[引包] 目标: {proj}  chroot: {copr_chroot}")

    # ── 1. 初始化 session 目录 ────────────────────────────────────────────
    session_dir = SESSIONS_BASE / job_id
    for sub in ("pkgs", "sources", "srpms", "build_state"):
        (session_dir / sub).mkdir(parents=True, exist_ok=True)
    (session_dir / "pkgs" / pkgname).mkdir(parents=True, exist_ok=True)

    session_json = {
        "session_id":   job_id,
        "pkgname":      pkgname,
        "upstream_url": url,
        "version":      version,
        "copr_url":     os.environ.get("COPR_API_URL", "http://copr-frontend:5000"),
        "copr_owner":   owner,
        "copr_project": coprname,
        "copr_login":   copr_login,
        "copr_token":   copr_token,
        "copr_chroot":  copr_chroot,
        "repo_local":   str(session_dir / "repo"),
    }
    (session_dir / "session.json").write_text(
        json.dumps(session_json, ensure_ascii=False, indent=2)
    )
    if not (session_dir / "dep_registry.json").exists():
        (session_dir / "dep_registry.json").write_text("{}")
    if not (session_dir / "build_state" / "introduced.txt").exists():
        (session_dir / "build_state" / "introduced.txt").touch()

    _init_workflow(session_dir, pkgname)

    # ── 写 session.created 时间线事件 ──────────────────────────────────────
    write_event(session_dir, "session.created", "", {
        "job_id": job_id,
        "pkgname": pkgname,
        "url": url,
        "version": version,
        "copr_project": proj,
        "copr_chroot": copr_chroot,
    })

    # ── 1.5 异步预热 repo 缓存 + 生成构建工具链 manifest ────────────────────
    if copr_chroot:
        _warm_script = Path("/app/.claude/skills/build-rpm/scripts/warm_repo_cache.py")
        if _warm_script.exists():
            threading.Thread(
                target=lambda: subprocess.run(
                    [sys.executable, str(_warm_script), copr_chroot],
                    capture_output=False,
                    timeout=660,
                ),
                daemon=True,
            ).start()
        # 同时生成当前 chroot 的构建工具链版本清单，作为全局约束
        _toolchain_script = Path("/app/.claude/skills/build-rpm/scripts/chroot_toolchain.py")
        if _toolchain_script.exists():
            threading.Thread(
                target=lambda: subprocess.run(
                    [sys.executable, str(_toolchain_script), copr_chroot,
                     "--session-dir", str(session_dir)],
                    capture_output=False,
                    timeout=300,
                ),
                daemon=True,
            ).start()

    # ── 2. 公共环境变量 ───────────────────────────────────────────────────
    env = {
        **os.environ,
        "ANTHROPIC_API_KEY":  os.environ.get("ANTHROPIC_AUTH_TOKEN",
                              os.environ.get("ANTHROPIC_API_KEY", "")),
        "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", ""),
        "COPR_FRONTEND_URL":  session_json["copr_url"],
        "COPR_OWNER":         owner,
        "COPR_PROJECT":       coprname,
        "COPR_API_LOGIN":     copr_login,
        "COPR_API_TOKEN":     copr_token,
        "COPR_CHROOT":        copr_chroot,
        "SESSIONS_BASE":      str(SESSIONS_BASE),
    }

    # ── 3. Supervisor 先行 + claude 按需启动循环 ──────────────────────────
    start   = time.time()
    loop    = 0
    prompt  = f"/import-package-step {session_dir}"

    while True:
        # 超时保护
        elapsed = time.time() - start
        if elapsed > MAX_JOB_SECONDS:
            _finish_with_timeline(r, job_id, session_dir, "failed",
                                  f"timeout after {int(elapsed)}s", start)
            return
        if loop >= MAX_LOOPS:
            _finish_with_timeline(r, job_id, session_dir, "failed",
                                  f"max_loops {MAX_LOOPS} exceeded", start)
            return

        # ── 时间线：新一轮循环开始 ───────────────────────────────────────
        write_event(session_dir, "loop.start", "", {"loop": loop + 1})

        # 先用纯 Python 问 supervisor 下一步
        sv = _run_supervisor(session_dir, job_id)
        action = sv.get("action", "")
        delay  = sv.get("delay", "")

        print(f"[supervisor][{job_id}] loop={loop} action={action}({sv.get('target','')}) delay={delay}", flush=True)

        # ── 时间线：supervisor 决策 ─────────────────────────────────────
        write_event(session_dir, "loop.end", "", {
            "loop": loop + 1,
            "action": action,
            "target": sv.get("target", ""),
            "delay": delay,
        })

        if action == "done":
            # 从 workflow 读最终报告写回 Redis
            wf_files = list(session_dir.glob("workflow_*.json"))
            if wf_files:
                wf = json.loads(wf_files[0].read_text())
                pkgname = wf.get("pkgname", "")
                r.hset(f"{JOB_PREFIX}{job_id}", "built_pkgs",  " ".join(wf.get("built_pkgs", [])))
                r.hset(f"{JOB_PREFIX}{job_id}", "reused_pkgs", " ".join(wf.get("reused_pkgs", [])))
                r.hset(f"{JOB_PREFIX}{job_id}", "loop_count",  str(wf.get("loop_count", "")))
                r.hset(f"{JOB_PREFIX}{job_id}", "error",       "")
                # 读 summary 报告写入 Redis
                if pkgname:
                    report_path = session_dir / f"pkgs/{pkgname}/{pkgname}_introduction_report.md"
                    if report_path.exists():
                        report_content = report_path.read_text(encoding="utf-8", errors="replace")
                        r.hset(f"{JOB_PREFIX}{job_id}", "report", report_content[:8000])
            _finish_with_timeline(r, job_id, session_dir, "success", "", start)
            return

        if action == "fail":
            wf_files = list(session_dir.glob("workflow_*.json"))
            error = sv.get("target", "unknown failure")
            if wf_files:
                wf = json.loads(wf_files[0].read_text())
                pkgname = wf.get("pkgname", "")
                error = wf.get("error") or error
                r.hset(f"{JOB_PREFIX}{job_id}", "built_pkgs",  " ".join(wf.get("built_pkgs", [])))
                r.hset(f"{JOB_PREFIX}{job_id}", "reused_pkgs", " ".join(wf.get("reused_pkgs", [])))
                r.hset(f"{JOB_PREFIX}{job_id}", "loop_count",  str(wf.get("loop_count", "")))
                r.hset(f"{JOB_PREFIX}{job_id}", "error",       error)
                # 读失败 summary 报告写入 Redis
                if pkgname:
                    report_path = session_dir / f"pkgs/{pkgname}/{pkgname}_introduction_report.md"
                    if report_path.exists():
                        report_content = report_path.read_text(encoding="utf-8", errors="replace")
                        r.hset(f"{JOB_PREFIX}{job_id}", "report", report_content[:8000])
            _finish_with_timeline(r, job_id, session_dir, "failed", error, start)
            return

        if action == "wait":
            # COPR 构建中，每秒检查一次取消信号，到时再继续
            try:
                delay_s = int(delay) if delay else 60
            except ValueError:
                delay_s = 60
            # ── 时间线：进入等待 ──────────────────────────────────────────
            write_event(session_dir, "loop.wait", "", {
                "loop": loop + 1,
                "reason": "copr_running",
                "targets": [sv.get("target", "")] if sv.get("target") else [],
                "delay_s": delay_s,
            })
            _log(r, job_id, f"[wait] COPR 构建中，{delay_s}s 后轮询")
            for _ in range(delay_s):
                time.sleep(1)
                cur = r.hget(f"{JOB_PREFIX}{job_id}", "status")
                if cur in ("cancelled", "failed", "success"):
                    _finish_with_timeline(r, job_id, session_dir,
                                          cur if cur else "cancelled", "", start)
                    return
            loop += 1
            continue

        # wait 结束后，对所有 failed 状态的 dep 都拉取 build log
        # 不只是当前 action 对应的包，避免低优先级包的日志一直拉不到
        dep_reg_path = session_dir / "dep_registry.json"
        if dep_reg_path.exists():
            import json as _jr_json
            dep_reg = _jr_json.loads(dep_reg_path.read_text())
            for dep_name, dep_info in dep_reg.items():
                if dep_info.get("status") == "build_failed":
                    _sync_copr_result(session_dir, dep_name, job_id)
        # 主包失败时也拉日志
        if action in ("fix_failure", "fix_failure_dep"):
            target_pkg = sv.get("pkgname", "") if action == "fix_failure" else sv.get("target", "")
            _sync_copr_result(session_dir, target_pkg, job_id)

        # ── 脚本先行：evaluate / evaluate_main 优先用脚本，不启 Claude ──
        # run_check.py + run_gate.py 本身是纯 Python 脚本，95%+ 的 dep 不需要 AI。
        # 脚本返回 needs_ai 时才 fall through 到 Claude agent。
        if action in ("evaluate", "evaluate_main"):
            target = sv.get("target", "")
            mode = "top-level" if action == "evaluate_main" else "dependency"
            constraint = sv.get("constraint", "")
            # 读取 upstream URL
            url = ""
            version = ""
            if mode == "dependency":
                _dep_path = session_dir / "dep_registry.json"
                if _dep_path.exists():
                    _dep_reg = json.loads(_dep_path.read_text())
                    url = _dep_reg.get(target, {}).get("url", "")
            else:
                _sess_path = session_dir / "session.json"
                if _sess_path.exists():
                    _sess = json.loads(_sess_path.read_text())
                    url = _sess.get("upstream_url", "")
                    version = _sess.get("version", "")

            if url:
                _log(r, job_id, f"[script] trying direct evaluate for {target}")
                try:
                    rc = subprocess.run(
                        [sys.executable, str(RUN_EVALUATE),
                         "--pkg", target, "--mode", mode,
                         "--url", url, "--constraint", constraint,
                         "--version", version,
                         "--session-dir", str(session_dir)],
                        capture_output=True, text=True, timeout=300,
                    )
                    if rc.returncode == 0:
                        result = json.loads(rc.stdout)
                        st = result.get("status", "")
                        if st == "done":
                            _log(r, job_id, f"[script] {target} evaluate done (no Claude)")
                            write_event(session_dir, "loop.skip", "", {
                                "loop": loop + 1,
                                "action": action,
                                "target": target,
                                "reason": "script_direct_evaluate",
                                "script_result": "done",
                            })
                            loop += 1
                            continue
                        if st == "failed":
                            _log(r, job_id, f"[script] {target} evaluate failed: {result.get('reason', '')}")
                            write_event(session_dir, "loop.skip", "", {
                                "loop": loop + 1,
                                "action": action,
                                "target": target,
                                "reason": "script_direct_evaluate",
                                "script_result": "failed",
                            })
                            loop += 1
                            continue
                        # st == "needs_ai" → fall through to Claude
                        _log(r, job_id, f"[script] {target} needs_ai, falling back to Claude")
                    else:
                        _log(r, job_id, f"[script] {target} script error (rc={rc.returncode}), falling back to Claude")
                except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
                    _log(r, job_id, f"[script] {target} exception: {e}, falling back to Claude")

        if not action:
            _finish_with_timeline(r, job_id, session_dir, "failed",
                                  "supervisor returned no action", start)
            return

        # 需要 claude 的 action：启动 claude -p /import-package-step
        action_start = time.time()
        target_pkg = sv.get("target", "") or sv.get("pkgname", "")
        write_event(session_dir, "action.start", target_pkg, {
            "action": action,
            "loop": loop + 1,
        })
        _log(r, job_id, f"[step] action={action}")
        cmd = [
            "claude",
            "--model", "claude-sonnet-4-6",
            "--add-dir", "/app",
            "--allowedTools", "Bash,Read,Write,Edit,Agent,Skill",
            "--output-format", "stream-json",
            "--verbose",
            "-p", prompt,
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd="/app",
        )

        # 实时把 stderr 打印到 worker stdout
        def _stream_stderr(p=proc):
            for line in iter(p.stderr.readline, ""):
                line = line.rstrip()
                if line and not line.startswith(("{", "[")):
                    print(f"[dbg][{job_id}] {line}", flush=True)
        stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
        stderr_thread.start()

        # watchdog：用户取消时强杀 claude
        def _watchdog(p=proc):
            while p.poll() is None:
                status = r.hget(f"{JOB_PREFIX}{job_id}", "status")
                if status in ("success", "failed", "cancelled"):
                    p.terminate()
                    try:
                        p.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        p.kill()
                    return
                time.sleep(5)
        watcher = threading.Thread(target=_watchdog, daemon=True)
        watcher.start()

        # 解析 stream-json，打印可读日志
        for raw in iter(proc.stdout.readline, ""):
            raw = raw.rstrip()
            if not raw:
                continue
            try:
                evt   = json.loads(raw)
                etype = evt.get("type", "")
                if etype == "assistant":
                    for block in evt.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            for line in block["text"].splitlines():
                                if line.strip():
                                    print(f"[claude][{job_id}] {line}", flush=True)
                                _log(r, job_id, line)
                        elif block.get("type") == "tool_use":
                            tool = block.get("name", "")
                            inp  = block.get("input", {})
                            desc = str(inp.get("command", inp.get("description", inp.get("prompt", ""))))[:120]
                            print(f"[tool][{job_id}] {tool}: {desc}", flush=True)
                elif etype == "tool_result":
                    # 打印脚本输出中的关键日志行
                    for content in evt.get("content", []):
                        if isinstance(content, dict) and content.get("type") == "text":
                            for line in content["text"].splitlines():
                                line = line.strip()
                                if line and any(kw in line for kw in (
                                    "[copr]", "[register-", "[read-", "ERROR", "error:",
                                    "status=", "build_id=", "added:", "decision=",
                                )):
                                    print(f"[script][{job_id}] {line}", flush=True)
                                    _log(r, job_id, line)
                elif etype == "result":
                    for line in evt.get("result", "").splitlines():
                        if line.strip():
                            print(f"[result][{job_id}] {line}", flush=True)
                            _log(r, job_id, line)
            except Exception:
                pass

        stderr_thread.join(timeout=5)
        proc.wait()
        action_duration = round(time.time() - action_start, 1)
        print(f"[claude][{job_id}] exit={proc.returncode}", flush=True)

        # ── 时间线：action 完成 ─────────────────────────────────────────
        write_event(session_dir, "action.end", target_pkg, {
            "action": action,
            "exit_code": proc.returncode,
            "duration_s": action_duration,
        })

        # 检查用户是否取消
        cur_status = r.hget(f"{JOB_PREFIX}{job_id}", "status")
        if cur_status in ("success", "failed", "cancelled"):
            _finish_with_timeline(r, job_id, session_dir,
                                  str(cur_status) if cur_status else "cancelled", "", start)
            return

        loop += 1
