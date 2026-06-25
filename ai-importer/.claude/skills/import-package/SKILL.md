---
name: import-package
description: openEuler 包引入入口。文件状态机 + Supervisor Loop 驱动，通过 spawn agent 隔离上下文。
argument-hint: "<pkgname> <upstream_url> [--version <ver>]"
allowed-tools:
  - Bash
  - Read
  - Agent
  - Skill
---

你是 openEuler 包引入流程的 lead（Supervisor）。

## 核心原则

1. **文件是唯一状态来源** — 所有决策基于读文件，不信任 agent 返回值
2. **通过 spawn agent 调用所有 skill** — 保护 lead 上下文不膨胀
3. **Supervisor Loop 驱动** — 读状态 → 找阻塞点 → 派发最小行动 → 重复
4. **无 team 无消息传递** — Agent 前台 spawn（同步等待），完成即退出

## Agent 定义位置

```
AGENTS_DIR="/app/.claude/agents/pkg-introduce"
  pkg-evaluator.md   ← check + gate 合二为一，输出 gate_result
  pkg-builder.md     ← build-rpm skill 封装，输出 build_rpm_result
  pkg-reviewer.md    ← review-rpm skill 封装，输出 review 报告
```

## 状态文件

```
session_dir/
  workflow_<pkgname>.json   ← 整体目标状态（lead 维护）
  dep_registry.json         ← 所有依赖状态（lead + builder 共同写入）
  pkgs/*/
    gate_result_<pkg>.json  ← evaluator 输出
    build_rpm_result.json   ← builder 输出
    critique_*.json         ← reviewer 输出
```

### workflow_<pkgname>.json

```json
{
  "pkgname": "yellhorn-mcp",
  "goal": "build_success",
  "loop_count": 0,
  "max_loops": 20,
  "built_pkgs": [],
  "reused_pkgs": [],
  "error": null
}
```

### dep_registry.json

每个依赖的状态流转：

```
pending_evaluate
  ├─→ reused          ← evaluator 决策 reuse_official/reuse_copr_project（已有满足版本）
  └─→ evaluate_done   ← evaluator 决策 introduce_new（需新引入）
           ├─→ build_done    ← builder 构建成功（SRPM 上传 COPR + 构建通过）
           └─→ build_failed  ← builder 构建失败
```

`reused` 和 `build_done` 对 lead 语义等价——该依赖已就绪，不需要再处理。

```json
{
  "mcp": {
    "url": "https://github.com/modelcontextprotocol/python-sdk",
    "constraint": ">=1.0",
    "required_by": "yellhorn-mcp",
    "status": "pending_evaluate | reused | evaluate_done | build_done | build_failed"
  }
}
```

## 入口参数

```bash
# 唯一调用方式：外层传入已创建好的 session 目录
SESSION_DIR="<arguments>"
```

## § 1  Session 读取

```bash
SKILLS_DIR="/app/.claude/skills"
SCRIPTS_DIR="$SKILLS_DIR/import-package-step/scripts"

[ -d "$SESSION_DIR" ] || { echo "ERROR: session_dir not found: $SESSION_DIR"; exit 1; }
cd "$SESSION_DIR"

# 从 session.json 读取所有参数
eval "$(python3 $SCRIPTS_DIR/read-session.py --session-dir .)"
PKGNAME="$SESSION_PKGNAME"
UPSTREAM_URL="$SESSION_UPSTREAM_URL"
VERSION="$SESSION_VERSION"

echo "[import-package] session=$SESSION_DIR  pkgname=$PKGNAME  version=${VERSION:-latest}"
```

session 目录由外层（`job_runner.py` 或手动）预先创建，包含：
- `session.json`：pkgname、upstream_url、version、COPR 凭据、copr_chroot
- `pkgs/<pkgname>/`、`sources/`、`srpms/`、`build_state/` 目录结构
- `dep_registry.json = {}`、`build_state/introduced.txt`

初始化 workflow（已存在则读取断点继续）：

```bash
python3 "/app/.claude/skills/import-package/scripts/init-workflow.py" \
  --session-dir "$SESSION_DIR" --pkgname "$PKGNAME"
```

## § 2  主包 evaluate（每次进入时执行）

spawn pkg-evaluator（前台同步）：

```python
Agent(
  subagent_type="pkg-evaluator",
  prompt=f"pkgname: {PKGNAME}\nmode: top-level\nsession_dir: {SESSION_DIR}"
)
```

读 `gate_result_<pkgname>.json` 验证：

```bash
eval "$(python3 "/app/.claude/skills/import-package/scripts/read-gate-result.py" \
  --session-dir "$SESSION_DIR" --pkgname "$PKGNAME")" \
  || { echo "ERROR: gate check failed"; exit 1; }
```

按 `$GATE_DECISION` 路由：
- `reuse_official` → 记录 reused_pkgs，终止（官方源已有）
- `reuse_copr_project` → 记录 reused_pkgs，终止（COPR project 已有）
- `introduce_new` → 进入 § 3

## § 3  启动 Supervisor Loop

初始化完成后，通过 `/loop` 动态模式启动单步执行循环：

```python
# /import-package-step 每次只执行一步（evaluate/build/critique/feedback/done）
# 完成后由 ScheduleWakeup 安排下次唤醒，直到目标达成或失败自动停止
# 归档、summary、容器清理均在 done 分支内完成
Skill(
  skill="loop",
  args=f"/import-package-step {SESSION_DIR}"
)
```
