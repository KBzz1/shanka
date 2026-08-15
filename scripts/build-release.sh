#!/usr/bin/env bash
# build-release.sh — Release 变体构建 + 校验 + 原子产出正式 APK（NV-07 / Task 13）
#
# 流程：
#   1. Gradle 构建 assembleRelease（签名凭据由 Gradle 从 Front/keystore.properties 读取，
#      本脚本不接触、不打印任何凭据，也不接收任何凭据参数）。
#   2. 校验构建产物：Release 变体标识（签名证书非 Android Debug）、签名有效
#      （apksigner verify）、包名与 versionName == 2.5.0、产物位于期望输出路径。
#   3. 计算 SHA-256。
#   4. 全部通过后才原子替换 /home/kbzz1/shanka_backend/releases/app-release.apk
#      （先写同目录临时文件再 mv）并写入同名 .sha256 校验文件。
#   5. 任何一步失败 → 非零退出，既有 APK 与校验文件保持原样。
set -euo pipefail

EXPECTED_VERSION="2.5.0"
EXPECTED_PACKAGE="com.qiuzhao.flashcards"
RELEASES_DIR="/home/kbzz1/shanka_backend/releases"
TARGET_APK="$RELEASES_DIR/app-release.apk"
TARGET_SHA="$RELEASES_DIR/app-release.apk.sha256"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONT_DIR="$REPO_ROOT/Front"
BUILD_APK="$FRONT_DIR/app/build/outputs/apk/release/app-release.apk"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

# --- 定位 Android SDK 与构建工具 ------------------------------------------------------------
SDK_DIR=""
if [[ -f "$FRONT_DIR/local.properties" ]]; then
    SDK_DIR="$(sed -n 's/^sdk\.dir=//p' "$FRONT_DIR/local.properties" | head -n1)"
fi
if [[ -z "${SDK_DIR:-}" && -n "${ANDROID_HOME:-}" ]]; then SDK_DIR="$ANDROID_HOME"; fi
if [[ -z "${SDK_DIR:-}" && -n "${ANDROID_SDK_ROOT:-}" ]]; then SDK_DIR="$ANDROID_SDK_ROOT"; fi
[[ -z "${SDK_DIR:-}" ]] && die "找不到 Android SDK（Front/local.properties 或 ANDROID_HOME）"

BUILD_TOOLS="$(ls -1 "$SDK_DIR/build-tools" 2>/dev/null | sort -V | tail -n1)"
[[ -z "$BUILD_TOOLS" ]] && die "SDK $SDK_DIR 下没有 build-tools"
APKSIGNER="$SDK_DIR/build-tools/$BUILD_TOOLS/apksigner"
AAPT="$SDK_DIR/build-tools/$BUILD_TOOLS/aapt"
# 签名验证工具缺失 = 门禁缺失：如实失败，不得伪造成功。
[[ -x "$APKSIGNER" ]] || die "缺少 apksigner（$APKSIGNER）：签名验证门禁不可用"
[[ -x "$AAPT" ]] || die "缺少 aapt（$AAPT）：版本/包名校验门禁不可用"

# --- 1. 构建 Release 变体 --------------------------------------------------------------------
echo "== Gradle assembleRelease（签名凭据由 Gradle 从 keystore.properties 读取）"
"$FRONT_DIR/gradlew" -p "$FRONT_DIR" assembleRelease --console=plain
[[ -f "$BUILD_APK" ]] || die "Release 变体产物缺失：$BUILD_APK"

# --- 2. 校验构建产物 ------------------------------------------------------------------------
echo "== 校验 $BUILD_APK"
# 2a. 签名有效 + Release 变体标识（证书 DN 不得是 Android Debug）
CERT_DN="$("$APKSIGNER" verify --print-certs "$BUILD_APK" | sed -n 's/^Signer #1 certificate DN: //p')"
[[ -n "$CERT_DN" ]] || die "apksigner 未输出证书 DN：签名无效"
if [[ "$CERT_DN" == *"Android Debug"* ]]; then
    die "签名证书是 Android Debug（$CERT_DN）——产物不是 Release 变体"
fi
echo "   签名有效；证书 DN: $CERT_DN"

# 2b. 包名 + 版本（versionName == 2.5.0）
BADGING="$("$AAPT" dump badging "$BUILD_APK")"
APK_PACKAGE="$(echo "$BADGING" | sed -n "s/^package: name='\([^']*\)'.*/\1/p")"
APK_VERSION="$(echo "$BADGING" | sed -n "s/^package: name='[^']*' versionCode='[^']*' versionName='\([^']*\)'.*/\1/p")"
[[ "$APK_PACKAGE" == "$EXPECTED_PACKAGE" ]] || die "包名不符：期望 $EXPECTED_PACKAGE，实际 $APK_PACKAGE"
[[ "$APK_VERSION" == "$EXPECTED_VERSION" ]] || die "版本不符：期望 $EXPECTED_VERSION，实际 $APK_VERSION"
echo "   包名 $APK_PACKAGE；版本 $APK_VERSION"

# 2c. SHA-256
SHA256="$(sha256sum "$BUILD_APK" | awk '{print $1}')"
echo "   SHA-256: $SHA256"

# --- 3. 原子替换（先写同目录临时文件再 mv；失败时既有 APK 原样保留） ---------------------------
mkdir -p "$RELEASES_DIR"
TMP_APK="$RELEASES_DIR/.app-release.apk.$$.tmp"
TMP_SHA="$RELEASES_DIR/.app-release.apk.sha256.$$.tmp"
trap 'rm -f "$TMP_APK" "$TMP_SHA"' EXIT

cp "$BUILD_APK" "$TMP_APK"
printf '%s  %s\n' "$SHA256" "app-release.apk" > "$TMP_SHA"
mv -f "$TMP_APK" "$TARGET_APK"
mv -f "$TMP_SHA" "$TARGET_SHA"
trap - EXIT

echo "== OK: $TARGET_APK（$APK_VERSION，SHA-256 $SHA256）"
