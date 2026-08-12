#!/usr/bin/env bash
# 测试平台调度入口(薄壳):转发到 suites.py。用法:
#   ./runner/run.sh [--environment local|prod] [--suite quick|full|live] [--scenario NAME] [--confirm-cost] [--confirm-prod]
set -euo pipefail
cd "$(dirname "$0")"
exec python3 suites.py "$@"
