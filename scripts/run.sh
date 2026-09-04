#!/usr/bin/env bash
# 启动脚本（契约 4.1：端口检测 → 本应用已在运行则幂等退出；被其他程序占用则换 8001 并提示同步 Tunnel 回源路由）
#
# 单进程约束（deployment.md）：限流器为进程内存状态、PDF 扫描器与任务执行器为进程内
# daemon 线程——uvicorn 必须单进程启动，禁止 --workers（多 worker 会双跑后台循环并使
# 限流失效）；扩容前须先把限流迁共享存储。--forwarded-allow-ips 显式信任回环 XFF
# （cloudflared 回源直连 127.0.0.1）；限流真实客户端 IP 另由 CF-Connecting-IP 承担
#（app/middleware/client_ip.py）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8000}"

port_in_use() {
  ss -tln 2>/dev/null | awk '{print $4}' | grep -q ":${1}$"
}

# 返回监听指定端口进程的 pid（同用户可见时；识别不到则输出空）
port_pid() {
  ss -tlnpH 2>/dev/null | awk -v port=":${1}$" '
    $4 ~ port { if (match($0, /pid=[0-9]+/)) { print substr($0, RSTART + 4, RLENGTH - 4); exit } }'
}

# 端口上的进程是否就是本应用（uvicorn + app.main:app）
is_our_app() {
  local pid
  pid="$(port_pid "${1}")"
  [ -n "${pid}" ] && ps -p "${pid}" -o args= 2>/dev/null | grep -q 'uvicorn.*app\.main:app'
}

if port_in_use "${PORT}"; then
  if is_our_app "${PORT}"; then
    echo "[run.sh] 端口 ${PORT} 上已在运行本应用（pid=$(port_pid "${PORT}")），无需重复启动" >&2
    exit 0
  fi
  echo "[run.sh] 端口 ${PORT} 被其他程序占用，切换到 8001" >&2
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
exec conda run -n shanka-backend uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" \
  --proxy-headers --forwarded-allow-ips=127.0.0.1
