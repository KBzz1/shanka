#!/usr/bin/env bash
# 安装 APK 到已连真机(device 层)。无设备时提示跳过(退出 0)。
# 用法: ./device/install/install.sh [--adb PATH] [--apk PATH]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
APK="${APK:-$REPO_ROOT/frontend-app/Front/app/build/outputs/apk/debug/app-debug.apk}"
ADB=""

while [ $# -gt 0 ]; do
  case "$1" in
    --adb) ADB="$2"; shift 2 ;;
    --apk) APK="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$ADB" ]; then
  for candidate in "$HOME/android-sdk/platform-tools/adb" \
                   "/mnt/c/Users/$(id -un)/AppData/Local/Android/Sdk/platform-tools/adb.exe" \
                   "/mnt/c/Program Files/Android/Android Studio/platform-tools/adb.exe"; do
    if [ -x "$candidate" ]; then ADB="$candidate"; break; fi
  done
fi
[ -n "$ADB" ] || { echo "未找到 adb,请用 --adb 指定(Windows 侧: \$env:LOCALAPPDATA\\Android\\Sdk\\platform-tools\\adb.exe)" >&2; exit 2; }
[ -f "$APK" ] || { echo "APK 不存在: $APK(先跑 device/build/build_apk.sh)" >&2; exit 1; }

DEVICES="$("$ADB" devices | awk 'NR>1 && $2=="device" {print $1}')"
if [ -z "$DEVICES" ]; then
  echo "未检测到已连接设备,跳过安装(可先执行: $ADB devices)"
  exit 0
fi
echo "安装到: $DEVICES"
"$ADB" -s "$(echo "$DEVICES" | head -1)" install -r "$APK"
