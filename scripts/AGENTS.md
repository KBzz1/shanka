# AGENTS.md

运维脚本：`run.sh`（启动）与 `stop.sh`（停止）。

- 语义与 `docs/Architecture/deployment.md` 契约 4.1 一致：本应用已在运行则幂等退出；端口被其他程序占用则换 8001 并提示同步 Cloudflare Tunnel 回源端口。
- 停止只匹配本应用（uvicorn + app.main:app，含 conda run 包装进程），不误杀其他监听进程；未发现运行中实例时提示并正常退出。

演示脚本：`gen_sample_cards.py`（样卡真实生成，结果打印终端）。

- 真实调用 DeepSeek：样书目标章（默认第 1 章）→ Planner 规划学习目标 → Generator 锚定单卡，复用 `agent_evolution` 版本化 Prompt/Schema 与 quota/校验逻辑；零 DB、不落盘。
- 红线 4：`DEEPSEEK_API_KEY` 仅从仓库根 `.env` 运行时读取（`--env-file` 可覆盖），进程内使用，任何输出不出现明文。
- 用法：`conda run -n shanka-backend python scripts/gen_sample_cards.py [--count 10] [--ratio 4:4:2] [--difficulty APPLICATION] [--model deepseek-v4-pro]`。
- 已知欠同步：`--difficulty` 仍接受旧枚举 `APPLICATION`（V2.5 域枚举已改名 `DEEP_QUESTION`），脚本枚举待跟进，使用时注意。
- 终端只打印代表 prompt（Planner 第一组 + Generator 第一单元），其余同构省略。

验收与评估脚本：

- `run_b5_acceptance.py`：密度制真实验收（B5 门禁，V25-D-26）。生产 HTTP 链路跑完整制卡流程并打印数量/难度分布验收证据；凭据只从 `.env` 读取。
- `task_quality_report.py`：单任务质量量化报告（对应 `docs/Architecture/generation-quality-metrics.md`）。只读评估：给定 task_id 输出 A 质量 / B 编排 / C 效率成本指标与参考值对照，零写入。
