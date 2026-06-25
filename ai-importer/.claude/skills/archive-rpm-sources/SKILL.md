---
name: archive-rpm-sources
description: 将 AI 引包过程的中间产物（spec、日志、报告）归档到本地持久化目录，方便后续查看。RPM 产物已在 COPR project 中，无需额外归档。由 import-package-step 的 done/fail 分支调用。
argument-hint: "--pkgs <pkg1> [pkg2...] --session-dir <dir> [--reports-dir <dir>]"
allowed-tools:
  - Bash
  - Read
---

> **调用方式：Skill 工具（`/archive-rpm-sources`）。**

你负责将 AI 引包的中间产物归档到本地持久化目录。
RPM 产物已在 COPR project 中，用户直接通过 COPR repo 安装，无需额外归档。

## 归档内容

```
/var/lib/ai-importer/archives/<pkgname>-<version>-<YYYYMMDD>/
  ├── session.json                        ← COPR 信息、凭据引用
  ├── dep_registry.json                   ← 依赖状态
  ├── pkgs/<pkgname>/
  │   ├── <pkgname>.spec                  ← 最终 spec
  │   ├── gate_result_<pkgname>.json      ← 评估决策
  │   ├── build_rpm_result.json           ← 构建结果
  │   ├── copr_build_result.json          ← COPR 构建 ID 和状态
  │   ├── build.log                       ← 构建日志
  │   ├── rpmlint.txt                     ← spec 静态检查
  │   └── critique_round*.json            ← reviewer 报告（若有）
  └── build_state/
      └── introduced.txt
```

## 主流程

```bash
SESSION_DIR="<session_dir 参数>"
PKGNAME="<第一个 pkg>"
ARCHIVE_BASE="${ARCHIVE_BASE:-/var/lib/ai-importer/archives}"
SCRIPTS_DIR="/app/.claude/skills/import-package-step/scripts"

# 读取版本（从 gate_result）
VERSION="$(python3 $SCRIPTS_DIR/read-gate-fields.py --session-dir "$SESSION_DIR" --pkg "$PKGNAME" --field version 2>/dev/null || echo unknown)"

DATE=$(date +%Y%m%d)
DEST="${ARCHIVE_BASE}/${PKGNAME}-${VERSION}-${DATE}"

mkdir -p "${DEST}"

# 复制整个 session 产物目录
cp -r "${SESSION_DIR}/." "${DEST}/"

echo "[archive] 中间产物已归档到: ${DEST}"
echo "[archive] COPR 构建 ID:"
for f in "${SESSION_DIR}"/pkgs/*/copr_build_result.json; do
  [ -f "$f" ] || continue
  pkg=$(basename "$(dirname "$f")")
  python3 $SCRIPTS_DIR/read-dep-registry.py --session-dir "$SESSION_DIR" --pkg "$pkg" --field copr_build_id 2>/dev/null \
    || python3 -c "import json; d=json.load(open('$f')); print(f'  $pkg: build_id={d.get(\"copr_build_id\",\"?\")} status={d.get(\"status\",\"?\")}')";
done
```

## 查看方式

归档完成后，用户可以：

```bash
# 列出所有归档
ls /var/lib/ai-importer/archives/

# 查看某次引包的 spec
cat /var/lib/ai-importer/archives/<pkgname>-<version>-<date>/pkgs/<pkgname>/<pkgname>.spec

# 查看 COPR 构建结果
cat /var/lib/ai-importer/archives/<pkgname>-<version>-<date>/pkgs/<pkgname>/copr_build_result.json

# 查看 reviewer 意见
cat /var/lib/ai-importer/archives/<pkgname>-<version>-<date>/pkgs/<pkgname>/critique_round1_<pkgname>.json
```

## 注意事项

- 归档失败不阻断主流程，仅输出警告
- `session.json` 中含 COPR token，归档目录权限应设为 700
- RPM 产物通过 COPR project repo 地址提供给用户，不在本地归档
