#!/usr/bin/env bash
# 停止脚本（契约 4.1 与 run.sh 对称）：仅停止本应用（uvicorn + app.main:app）。
# 判定与 run.sh 的 is_our_app 同源：只按命令行匹配本应用，不误杀其他监听进程。
# 未发现运行中的本应用 → 提示并正常退出（幂等，exit 0）。
set -euo pipefail

stopped=0
pids="$(ss -tlnpH 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)"
for pid in ${pids}; do
  if ps -p "${pid}" -o args= 2>/dev/null | grep -q 'uvicorn.*app\.main:app'; then
    echo "[stop.sh] 停止本应用 pid=${pid}"
    # 一并停止 conda run 包装进程（命令行含 conda run 的父进程），避免残留
    ppid="$(ps -o ppid= -p "${pid}" 2>/dev/null | tr -d ' ' || true)"
    if [ -n "${ppid}" ] && ps -p "${ppid}" -o args= 2>/dev/null | grep -q 'conda run'; then
      kill "${ppid}" 2>/dev/null || true
    fi
    kill "${pid}" 2>/dev/null || true
    stopped=1
  fi
done

if [ "${stopped}" = "0" ]; then
  echo "[stop.sh] 未发现运行中的本应用，无需停止" >&2
  exit 0
fi

sleep 1
if ss -tlnp 2>/dev/null | grep -qE 'pid=' && pgrep -f 'uvicorn.*app\.main:app' > /dev/null 2>&1; then
  echo "[stop.sh] 警告：仍有本应用进程残留，请检查" >&2
  exit 1
fi
echo "[stop.sh] 已停止"
