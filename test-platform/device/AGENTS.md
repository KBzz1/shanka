# AGENTS.md

测试平台真机层：WSL2 编译前端 debug APK 与安装到已连真机。SDK/gradle 路径均参数化，默认 `~/android-sdk` 与 `~/gradle-dist/gradle-9.6.1`（`--sdk-dir`/`--gradle-dir`/`--project` 可覆盖）。

- `build/build_apk.sh` — 在 WSL2 内编译前端 debug APK（默认工程 `frontend-app/Front`，产出 `app/build/outputs/apk/debug/app-debug.apk`）；SDK/gradle/工程缺失时明确报错退出。
- `install/install.sh` — 安装 APK 到已连真机：自动探测 adb（本机 `~/android-sdk/platform-tools/adb` 或 Windows 侧 Android SDK，`--adb` 可覆盖）；无设备连接时提示跳过并退出 0，不误报失败。
- 联调链路：后端 `scripts/run.sh` 运行中 → 真机 App 默认走 `https://shanka.kbzz1.top`（公网）；本机高频迭代用 Windows 侧 `adb reverse tcp:8000 tcp:8000` 后走 `http://localhost:8000`，见 `docs/frontend/local-dev.md` 第 8 节。
- instrumented 测试（真机/模拟器上的 UI 与后端联调测试，独立于后端仓库）：`cd frontend-app/Front && ./gradlew connectedAndroidTest`（需 adb 已连设备/模拟器；报告在 `app/build/reports/androidTests/connected/`）。前端工程在独立仓库 `frontend-app/`（后端 git 忽略），本层只负责编译与安装，不掺前端测试代码。
