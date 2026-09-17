# AGENTS.md

运维脚本：`run.sh`（启动）与 `stop.sh`（停止）。

- 语义与 `docs/Architecture/deployment.md` 契约 4.1 一致：本应用已在运行则幂等退出；端口被其他程序占用则换 8001 并提示同步 Cloudflare Tunnel 回源端口。
- 停止只匹配本应用（uvicorn + app.main:app，含 conda run 包装进程），不误杀其他监听进程；未发现运行中实例时提示并正常退出。

演示脚本：`gen_sample_cards.py`（样卡真实生成，结果打印终端）。

- 真实调用 DeepSeek：样书目标章（默认第 1 章）→ 粗规划主题盘点 → 精规划学习单元 → Generator 锚定单卡，复用 `agent_evolution` 版本化 Prompt/Schema 与 quota/校验逻辑；零 DB、不落盘。
- 红线 4：`DEEPSEEK_API_KEY` 仅从仓库根 `.env` 运行时读取（`--env-file` 可覆盖），进程内使用，任何输出不出现明文。
- 用法：`conda run -n shanka-backend python scripts/gen_sample_cards.py [--count 10] [--ratio 4:4:2] [--difficulty APPLICATION] [--model deepseek-v4-pro] [--custom-requirements "生成不要有英文"]`。
- `--custom-requirements` 透传用户自定义提示词到粗规划/精规划/制卡三阶段（同生产 `generation_config.custom_requirements`），用于验证语言/侧重类偏好的遵守情况。
- 旧名兼容：`--difficulty APPLICATION` 显式映射到 `DEEP_QUESTION`（V2.5 域枚举改名后的兼容入口，非欠同步）。
- 终端只打印代表 prompt（粗规划首段 + 精规划首批 + Generator 第一单元），其余同构省略。

验收与评估脚本：

- `run_b5_acceptance.py`：密度制真实验收（B5 门禁，V25-D-26）。生产 HTTP 链路跑完整制卡流程并打印数量/难度分布验收证据；凭据只从 `.env` 读取。
- `task_quality_report.py`：单任务质量量化报告（对应 `docs/Architecture/generation-quality-metrics.md`）。只读评估：给定 task_id 输出 A 质量 / B 编排 / C 效率成本指标与参考值对照，零写入。

消融实验工作区：`planning_ablation/`（两阶段规划 V2.5.2 的定稿依据，2026-09-09）。

- `report.md` 是结论证据：v7 采用 baseline 纯锚点变体，`density_assessment` 自评变体消融后弃用；`assets/` 为实验草稿，与 `agent_evolution/` 定稿分离，勿互改。
- `run/` 是子代理原始输出（含样书正文切块），git-ignored、可由 harness 脚本 + `main/data/shanka.db` 重建；不入 Git、不外传。
- 零 API：所有模型判断由 ZCode 子代理完成，脚本不读 `.env`、不调 DeepSeek；确定性指标（区间命中/tier 过滤/B3 覆盖/去重）由脚本本地计算。

章节规划评测工作区：`chapter_planning_eval/`（V25-D-36 AI 章节边界规划的质量量化，2026-09-16）。

- 方法论：ground truth = 书自带目录（`parse_pdf` outline 章节）；剔目录只喂页文本给生产章节规划链路，对比 AI 边界 vs 目录边界。指标：边界 precision/recall/F1（±1 页容差、一对一贪心）、章节区间 IoU（均值/中位/≥0.5 覆盖率）、匹配对标题 bigram Dice、退化标记（0 边界整本降级）与确定性校验丢弃计数。
- 三步流水线：`build_payloads.py`（零 API，产出 GT/页文本/与生产逐字节一致的分段消息；无 outline 但结构肉眼可见的书走 `--ground-truth` 手工标注路径，样例 `fixtures/interview-qa-flashcards*`）→ `run_live.py`（读仓库根 `.env` 的 `DEEPSEEK_API_KEY` 真实调用，回复留档 `replies/`，中断可续）→ `make_report.py`（零 API 重算：生产 `validate_boundaries`/`merge_boundaries` + `metrics.py` 指标，写 `report.md`/`metrics.json`，可反复重算不重复付费；run/ 多书时须 `--book`）。
- `metrics.py --selftest` 跑内置手算用例；`run/` git-ignored（正文切块与回复留档可由脚本 + 原 PDF 重建）；与 `planning_ablation/` 同规：报告为结论证据、绝对值指示性（页粒度 GT、单书样本）。
