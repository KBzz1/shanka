# R1 live 驱动设施（task-3 brief）

R1 本机受控 live 验证的支撑设施：60 文本块抽样框（`sample_frame.py`）+ live 执行驱动
（`driver.py`）。live 执行本身由 T4 主 Agent 在 `--live` 下进行；本目录默认全程零网络
（dry-run 注入 mock transport）。

## 前置条件

- Conda 环境 `shanka-backend`（Python 3.12）；工作目录 `main/`。
- 样书：`/home/kbzz1/shanka_backend/res/AI-Agents-in-Depth-zh-CN.pdf`（只读引用，不复制不提交）。
- 根目录 `.env`（权限 600、Git 忽略）：`DEEPSEEK_API_KEY`（仅 live 必需）；
  `API_KEY_ENCRYPTION_KEY` 可选（本机 .env 未提供时，driver 运行时生成临时密钥，
  仅进程内存内有效，不落盘不落日志——报告只标注来源 env/ephemeral-random）。
  driver 只运行时读取，任何文件/日志/输出不含明文（红线 4）。注意：.env 不进 Git，
  工作树（.claude/worktrees/）内没有——从工作树运行时须显式
  `--env-file /home/kbzz1/shanka_backend/.env`。

## 1. 抽样框（60 文本块，固定 seed）

```bash
cd main && conda run -n shanka-backend python -m tests.live.sample_frame --out /tmp/r1-frame.json
```

- 读真实 PDF 第 1/2/6 章（名称前缀匹配）→ 章内按固定 seed（20260811）分散取 20 块/章 →
  60 块 JSON；每块含 `chapter_name / start_page / end_page / file_id（占位空串）/ difficulty
  （easy/medium/hard 循环）/ index / seed`。
- 纯本地文件操作（pypdf 文本层 + 书签），无网络、无 OCR；seed 固定即结果确定。
- 抽样框在 live 前由主 Agent 审阅固定（task-3 brief Step 2），driver 只消费不修改。

## 2. 执行驱动（默认 dry-run，零网络）

```bash
cd main && conda run -n shanka-backend python -m tests.live.driver \
  --frame /tmp/r1-frame.json --db /tmp/r1-live.db --storage /tmp/r1-storage \
  [--limit N] [--max-cost-yuan 5] [--max-total-yuan 10] [--dry-run]
```

正式链路（driver 单元流程 5 步）：

1. 上传真实 PDF（POST /pdfs，multipart）→ 显式 `scan_once` 解析（V3A 三重校验）→
   解析章节替换为抽样框 60 块章节（DB 直插，非 HTTP——章节配置接口由 AC-02 覆盖，
   live 关注生成链路）。
2. 建牌组（POST /decks）。
3. 保存 Key：live 走 PUT /api-key（真实校验）；dry-run 直接落 api_keys 行（避免触网），
   日志/报告只用 `masked()`（`sk-****`）展示。
4. 逐单元：POST /tasks（chapter_ids=[块章]、quantity_tendency 按难度映射
   COMPACT/BALANCED/EXTENSIVE、幂等键 K）→ 显式 `scan_once`（settings 注入
   `task_scan_interval_seconds=3600.0` 禁用后台自动循环）→ 任务 COMPLETED 验证 →
   卡 Schema 合法（`validate_card` 直读 DB）→ 入库计数 = 计划数 → 幂等重放（同键重发 →
   同响应 + 不重复执行）→ 记录 usage/model/fingerprint/价格 → 成本累计检查。
5. 报告：JSON（每单元 task_id/model/fingerprint/tokens 4 键/价格/状态/耗时 +
   汇总成功/失败/总成本/停止原因）+ stdout 人类可读摘要；无明文 Key。

难度映射：`easy → COMPACT`、`medium → BALANCED`、`hard → EXTENSIVE`。

## 输出

- 报告 JSON：`<db>.report.json`（可 `--report` 指定）。每单元含
  `task_id / model / fingerprint / fingerprints / tokens{prompt,cache_hit,cache_miss,output}
  / cost_yuan{cache_hit,cache_miss,output,total} / status / failures / duration_ms / wall_ms
  / planned_cards / inserted_cards / replay_ok / batches`；汇总含 `units_succeeded /
  units_failed / total_cost_yuan / stop_reason`。
- stdout 摘要逐单元一行（掩码 Key、无明文）。

## 停止条件

- 成本：`--max-cost-yuan`（默认 5 元/单元）与 `--max-total-yuan`（默认 10 元累计）——
  每单元后检查（含第 1 单元），超限立即停止并保留真实失败（`stop_reason` 记录
  `max_cost_yuan_exceeded` / `max_total_yuan_exceeded`）。
- 单元失败（任务未 COMPLETED / 卡 Schema 违约 / 入库计数 ≠ 计划数 / 幂等重放失败）：
  记为 FAILED 并继续后续单元（真实失败保留）；setup 阶段失败（上传/解析/建组/Key）直接退出。
- `--limit N`：只执行前 N 个单元（T4 分片执行用）。

## Key 安全（红线 4）

- driver 源码无真实 Key 字面量；`.env` 只运行时读取（`--env-file`，默认仓库根）。
- 报告 JSON / stdout / 日志只出现 `masked()` 输出（`sk-****XXXX`）；明文 Key 只存在于
  进程内调用栈（PUT /api-key → service → adapter/crypto）。
- 禁止把 `.env`、明文 Key 写入任何计划/报告/命令参数。
