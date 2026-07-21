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

MAX_JOB_SECONDS = int(os.environ.get("MAX_JOB_SECONDS", str(4 * 3600)))
MAX_LOOPS       = int(os.environ.get("MAX_LOOPS", "100"))


def _log(r, job_id, msg):
    r.rpush(f"{LOGS_PREFIX}{job_id}", json.dumps({"msg": msg, "t": time.time()}))


def _finish(r, job_id, status, error=""):
    _log(r, job_id, f"[引包] 完成  status={status}" + (f"  error={error}" if error else ""))
    r.hset(f"{JOB_PREFIX}{job_id}", "status", status)
    if error:
        r.hset(f"{JOB_PREFIX}{job_id}", "error", error)
    r.rpush(f"{LOGS_PREFIX}{job_id}", json.dumps({"done": True, "status": status}))


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


def _sync_copr_result(session_dir: Path, pkgname: str, job_id: str = "") -> None:
    """wait 结束后拉取 COPR build log，写入 build_rpm_result.json。"""
    if not pkgname:
        return

    br_path = session_dir / f"pkgs/{pkgname}/build_rpm_result.json"
    if not br_path.exists():
        return

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
            try:
                import sys as _sys
                _scripts_dir = str(Path(SKILLS_DIR) / "build-rpm/scripts")
                if _scripts_dir not in _sys.path:
                    _sys.path.insert(0, _scripts_dir)
                from rpm_naming import upstream_from_srpm_name
                gate_path = session_dir / f"reports/gate_result_{pkgname}.json"
                lang = ""
                if gate_path.exists():
                    gate_data = _json.loads(gate_path.read_text())
                    lang = gate_data.get("lang", "") or gate_data.get("result", {}).get("lang", "")
                # 从 RPM 名剥离前缀还原上游名（python3-setuptools → setuptools）
                normalized = upstream_from_srpm_name(actual_pkg, lang) if lang else actual_pkg
            except Exception:
                normalized = actual_pkg
            if normalized != pkgname:
                br["status"] = "failed"
                br["failure_reason"] = (
                    f"Package name mismatch: build {build_id} "
                    f"is '{actual_pkg}', expected '{pkgname}'"
                )
                br_path.write_text(_json.dumps(br, indent=2, ensure_ascii=False))
                print(f"[sync_copr][{job_id}] MISMATCH: build {build_id} is {actual_pkg}, expected {pkgname}",
                      flush=True)
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

    # ── 1.5 异步预热 repo 缓存 ────────────────────────────────────────────
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
            _finish(r, job_id, "failed", f"timeout after {int(elapsed)}s")
            return
        if loop >= MAX_LOOPS:
            _finish(r, job_id, "failed", f"max_loops {MAX_LOOPS} exceeded")
            return

        # 先用纯 Python 问 supervisor 下一步
        sv = _run_supervisor(session_dir, job_id)
        action = sv.get("action", "")
        delay  = sv.get("delay", "")

        print(f"[supervisor][{job_id}] loop={loop} action={action}({sv.get('target','')}) delay={delay}", flush=True)

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
            _finish(r, job_id, "success")
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
            _finish(r, job_id, "failed", error)
            return

        if action == "wait":
            # COPR 构建中，每秒检查一次取消信号，到时再继续
            try:
                delay_s = int(delay) if delay else 60
            except ValueError:
                delay_s = 60
            _log(r, job_id, f"[wait] COPR 构建中，{delay_s}s 后轮询")
            for _ in range(delay_s):
                time.sleep(1)
                cur = r.hget(f"{JOB_PREFIX}{job_id}", "status")
                if cur in ("cancelled", "failed", "success"):
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
        if action in ("analyze_failure", "analyze_failure_dep"):
            target_pkg = sv.get("pkgname", "") if action == "analyze_failure" else sv.get("target", "")
            _sync_copr_result(session_dir, target_pkg, job_id)

        if not action:
            _finish(r, job_id, "failed", "supervisor returned no action")
            return

        # 需要 claude 的 action：启动 claude -p /import-package-step
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
        print(f"[claude][{job_id}] exit={proc.returncode}", flush=True)

        # 检查用户是否取消
        if r.hget(f"{JOB_PREFIX}{job_id}", "status") in ("success", "failed", "cancelled"):
            return

        loop += 1
