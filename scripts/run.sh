#!/usr/bin/env bash
# 启动脚本（契约 4.1：端口检测 → 被占用换 8001 并提示同步 Tunnel 回源路由）
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8000}"

port_in_use() {
  ss -tln 2>/dev/null | awk '{print $4}' | grep -q ":${1}$"
}

if port_in_use "${PORT}"; then
  echo "[run.sh] 端口 ${PORT} 被占用，切换到 8001" >&2
  echo "[run.sh] 提示：需在 Cloudflare Dashboard 将公共主机名回源端口同步改为 8001" >&2
  PORT=8001
fi

if port_in_use "${PORT}"; then
  echo "[run.sh] 错误：端口 8000 与 8001 均被占用，无法启动" >&2
  exit 1
fi

cd "${REPO_ROOT}/main"
set -a
# shellcheck disable=SC1091
source ../.env
set +a
exec conda run -n shanka-backend uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
