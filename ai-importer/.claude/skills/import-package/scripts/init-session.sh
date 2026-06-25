#!/usr/bin/env bash
# 为一次包引入任务创建隔离的工作目录和 session.json（COPR 模式，无 Docker）
#
# 用法：
#   eval "$(bash init-session.sh <pkgname> <upstream_url> [--version <ver>])"
#
# 输出环境变量：SESSION_DIR

set -euo pipefail

PKG="${1:-}"
if [[ -z "$PKG" ]]; then
  echo "[ERROR] 用法: $0 <pkgname> [<upstream_url>] [--version <ver>]" >&2
  exit 1
fi

# ── 解析参数 ──────────────────────────────────────────────────────────────────
SESSION_KEY=""
UPSTREAM_URL=""
VERSION=""
shift  # 消费 pkgname

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="${2:-}"
      [[ -z "$VERSION" ]] && { echo "[ERROR] --version 需要版本号" >&2; exit 1; }
      shift 2
      ;;
    --*)
      echo "[ERROR] 未知参数: $1" >&2; exit 1
      ;;
    *)
      UPSTREAM_URL="$1"
      shift
      ;;
  esac
done

# ── 推导 session key ──────────────────────────────────────────────────────────
# key = hash(url + version + chroot)，三者任一不同则新建 session
COPR_CHROOT_FOR_KEY="${COPR_CHROOT:-}"
if [[ -n "$UPSTREAM_URL" ]]; then
  SESSION_KEY=$(echo "${UPSTREAM_URL}|${VERSION}|${COPR_CHROOT_FOR_KEY}" | sha256sum | head -c8)
  echo "[init-session] key source: url+version+chroot (${SESSION_KEY})" >&2
elif [[ -n "$VERSION" ]]; then
  SESSION_KEY=$(echo "${PKG}@${VERSION}|${COPR_CHROOT_FOR_KEY}" | sha256sum | head -c8)
  echo "[init-session] key source: pkgname@version+chroot (${SESSION_KEY})" >&2
else
  TIMESTAMP=$(date +%Y%m%d-%H%M%S)
  RAND=$(head -c4 /dev/urandom | xxd -p)
  SESSION_KEY="${TIMESTAMP}-${RAND}"
fi

SESSION_ID="${PKG}-${SESSION_KEY}"
SESSION_DIR="${SESSIONS_BASE:-/tmp/claude-ws}/${SESSION_ID}"

# ── 复用已有 session ──────────────────────────────────────────────────────────
if [[ -d "${SESSION_DIR}" ]] && [[ -f "${SESSION_DIR}/session.json" ]]; then
  echo "[init-session] reusing existing session: ${SESSION_DIR}" >&2
  # 更新 copr_chroot（可能因新 job 使用不同 chroot 而变化）
  COPR_CHROOT="${COPR_CHROOT:-${COPR_DEFAULT_CHROOT:-}}"
  if [[ -n "$COPR_CHROOT" ]]; then
    python3 - <<PYEOF
import json, pathlib
p = pathlib.Path("${SESSION_DIR}/session.json")
d = json.loads(p.read_text())
d["copr_chroot"] = "${COPR_CHROOT}"
p.write_text(json.dumps(d, indent=2, ensure_ascii=False))
PYEOF
    echo "[init-session] updated copr_chroot: ${COPR_CHROOT}" >&2
  fi
  echo "SESSION_DIR=${SESSION_DIR}"
  exit 0
fi

# ── 新建 session ──────────────────────────────────────────────────────────────
mkdir -p \
  "${SESSION_DIR}" \
  "${SESSION_DIR}/pkgs/${PKG}" \
  "${SESSION_DIR}/sources" \
  "${SESSION_DIR}/srpms" \
  "${SESSION_DIR}/build_state"

# COPR 凭据从环境变量读取
COPR_URL="${COPR_FRONTEND_URL:-http://copr-frontend:5000}"
COPR_OWNER="${COPR_OWNER:-}"
COPR_PROJECT="${COPR_PROJECT:-}"
COPR_LOGIN="${COPR_API_LOGIN:-}"
COPR_TOKEN="${COPR_API_TOKEN:-}"
COPR_CHROOT="${COPR_CHROOT:-}"

cat > "${SESSION_DIR}/session.json" <<EOF
{
  "session_id":    "${SESSION_ID}",
  "pkgname":       "${PKG}",
  "upstream_url":  "${UPSTREAM_URL}",
  "version":       "${VERSION}",
  "copr_url":      "${COPR_URL}",
  "copr_owner":    "${COPR_OWNER}",
  "copr_project":  "${COPR_PROJECT}",
  "copr_login":    "${COPR_LOGIN}",
  "copr_token":    "${COPR_TOKEN}",
  "copr_chroot":   "${COPR_CHROOT}",
  "repo_local":    "${SESSION_DIR}/repo"
}
EOF

# 初始化 build_state 文件
touch "${SESSION_DIR}/build_state/introduced.txt"
echo "{}" > "${SESSION_DIR}/dep_registry.json"

# ── 注册 pkgname → session_dir 映射 ──────────────────────────────────────────
REGISTRY=$(python3 -c "
import pathlib, sys
for p in [pathlib.Path('$PWD')] + list(pathlib.Path('$PWD').parents):
    c = p / '.claude' / 'skills' / 'pkg-introduce' / 'session_registry.json'
    if c.parent.exists():
        print(c); break
" 2>/dev/null || true)

if [[ -n "$REGISTRY" ]]; then
  python3 - <<PYEOF
import json, os
reg = {}
if os.path.exists("${REGISTRY}"):
    try:
        reg = json.load(open("${REGISTRY}"))
    except Exception:
        reg = {}
reg["${PKG}"] = "${SESSION_DIR}"
json.dump(reg, open("${REGISTRY}", "w"), indent=2, ensure_ascii=False)
PYEOF
fi

echo "[init-session] session_id : ${SESSION_ID}" >&2
echo "[init-session] work_dir   : ${SESSION_DIR}" >&2

echo "SESSION_DIR=${SESSION_DIR}"
