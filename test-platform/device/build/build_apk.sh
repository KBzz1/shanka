#!/usr/bin/env bash
# 本机编译前端 debug APK(测试平台 device 层)。WSL2 内编译,SDK/gradle 路径参数化。
# 用法: ./device/build/build_apk.sh [--sdk-dir DIR] [--gradle-dir DIR] [--project DIR]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SDK_DIR="${SDK_DIR:-$HOME/android-sdk}"
GRADLE_DIR="${GRADLE_DIR:-$HOME/gradle-dist/gradle-9.6.1}"
# 统一仓库后前端工程位于 frontend/Front（旧独立仓库路径 frontend-app/Front 已不存在）。
PROJECT="${PROJECT:-$REPO_ROOT/frontend/Front}"

while [ $# -gt 0 ]; do
  case "$1" in
    --sdk-dir) SDK_DIR="$2"; shift 2 ;;
    --gradle-dir) GRADLE_DIR="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[ -d "$SDK_DIR" ] || { echo "Android SDK 不存在: $SDK_DIR(参考 test-platform/AGENTS.md 环境准备)" >&2; exit 1; }
[ -x "$GRADLE_DIR/bin/gradle" ] || { echo "gradle 不存在: $GRADLE_DIR" >&2; exit 1; }
[ -f "$PROJECT/settings.gradle.kts" ] || { echo "前端工程不存在: $PROJECT" >&2; exit 1; }

[ -f "$PROJECT/local.properties" ] || echo "sdk.dir=$SDK_DIR" > "$PROJECT/local.properties"

cd "$PROJECT"
"$GRADLE_DIR/bin/gradle" assembleDebug --no-daemon
echo "APK: $PROJECT/app/build/outputs/apk/debug/app-debug.apk"
