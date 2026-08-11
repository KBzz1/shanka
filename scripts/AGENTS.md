# AGENTS.md

运维脚本：`run.sh`（启动）与 `stop.sh`（停止）。

- 语义与 `docs/Architecture/deployment.md` 契约 4.1 一致：本应用已在运行则幂等退出；端口被其他程序占用则换 8001 并提示同步 Cloudflare Tunnel 回源端口。
- 停止只匹配本应用（uvicorn + app.main:app，含 conda run 包装进程），不误杀其他监听进程；未发现运行中实例时提示并正常退出。
