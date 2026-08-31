# AGENTS.md

运维脚本：`run.sh`（启动）与 `stop.sh`（停止）。

- 语义与 `docs/Architecture/deployment.md` 契约 4.1 一致：本应用已在运行则幂等退出；端口被其他程序占用则换 8001 并提示同步 Cloudflare Tunnel 回源端口。
- 停止只匹配本应用（uvicorn + app.main:app，含 conda run 包装进程），不误杀其他监听进程；未发现运行中实例时提示并正常退出。

演示脚本：`gen_sample_cards.py`（样卡真实生成，结果打印终端）。

- 真实调用 DeepSeek：样书目标章（默认第 1 章）→ Planner 规划学习目标 → Generator 锚定单卡，复用 `agent_evolution` 版本化 Prompt/Schema 与 quota/校验逻辑；零 DB、不落盘。
- 红线 4：`DEEPSEEK_API_KEY` 仅从仓库根 `.env` 运行时读取（`--env-file` 可覆盖），进程内使用，任何输出不出现明文。
- 用法：`conda run -n shanka-backend python scripts/gen_sample_cards.py [--count 10] [--ratio 4:4:2] [--difficulty APPLICATION] [--model deepseek-v4-pro]`。
- 终端只打印代表 prompt（Planner 第一组 + Generator 第一单元），其余同构省略。
