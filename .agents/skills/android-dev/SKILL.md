---
name: android-dev
description: WSL2 工作区的 android-dev 指针。android-emulator 插件的桌面模拟器流程在本机不可用（Linux 宿主，android_preflight 直接失败）。凡是 /android-dev、"构建并运行 Android 应用"、"模拟器里跑起来/截图" 类请求，都改走 android-device-debugging skill 的 Windows 侧模拟器流程。
---

# android-dev（本机指针 → android-device-debugging）

本机是 WSL2 宿主。`android-emulator` 插件的桌面模拟器工作流不支持 Linux——`android_preflight` 的 Host OS 检查必然失败，后续 MCP 工具（android_build_and_run / android_screenshot / android_ui_*）均不可用。不要按插件的 INSTALL_ENVIRONMENT.md 安装环境（那只覆盖 macOS/Windows），也不要在 WSL 内另装模拟器。若命令模板要求先跑 `android_preflight`：跑一次确认失败即可，失败本身就确认了下面的路由，不要继续插件流程。

执行 android-dev 类请求（构建 APK、启动模拟器、安装、截图、UI 自动化）时，改为加载工作区 skill **android-device-debugging**，按其「Windows-side emulator (WSL host)」一节执行：

1. WSL 里构建 APK：`frontend/Front` 下 `./gradlew assembleDebug`（debug 包名 `com.qiuzhao.flashcards.debug`）。
2. Windows 侧启动模拟器：`emulator.exe -avd Medium_Phone_API_36.1 -gpu swiftshader_indirect -memory 4096 -no-snapshot -no-boot-anim`。**`-gpu swiftshader_indirect` 必须带**——本机 SDK 缺 `opengl32sw.dll`，默认 GPU 栈会让 guest 黑屏卡死、adb 永远 unauthorized。
3. 设备操作一律用 Windows `adb.exe -s emulator-5554`（小米真机常连，必须显式序列号）；APK 先 `cp` 到 `/mnt/c` 再 install。
4. 截图验证用 `adb exec-out screencap -p`；UI 自动化用 `adb shell input`（模拟器无厂商限制）。

完整命令序列与故障分流见 [.agents/skills/android-device-debugging/references/windows-wsl-playbook.md](../android-device-debugging/references/windows-wsl-playbook.md) §9。USB 真机调试、adb reverse、instrumentation 按该 skill 原有流程，与本指针不冲突。
