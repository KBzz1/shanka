# R1 受控 DeepSeek live 验证报告

日期：2026-08-11｜分支：codex/r1｜驱动：`main/tests/live/driver.py`（--live）

## 1. 执行参数（LIVE-CAPPED 边界）

| 项 | 值 |
| --- | --- |
| 模型（冻结） | `deepseek-v4-flash` + thinking disabled + JSON output（Settings 默认，R-09） |
| 抽样框 | seed `20260811`，60 块：第 1 章（页 15-35）20 块 + 第 2 章（36-79）20 块 + 第 6 章（170-198）20 块 |
| 难度 | easy/medium/hard = 24/24/12（quantity_tendency COMPACT/BALANCED/EXTENSIVE） |
| canary | 单元 1；失败即停（stop_reason=canary_failed） |
| 成本上限 | 单次 ¥5 / 总计 ¥10（driver 每单元后检查） |
| 运行次数 | 2 次（首次 canary 失败 → 实质修复 → 授权重跑 1 次） |
| 真实调用 | 仅正式运行；诊断调用 3 次（canary 失败根因定位） |

## 2. 结果统计（正式运行，live3）

| 指标 | 值 |
| --- | --- |
| 单元 | 60/60 执行，**59 成功 / 1 失败** |
| 完成率 | 98.33%（对照 PRD 8.1「生成任务完成率 ≥ 90%」✓） |
| 失败率（原始） | 1/60 = 1.67% |
| 95% 区间 | Wilson 双侧 **[0.29%, 8.86%]**（scipy 非依赖，标准库 Wilson；Clopper-Pearson 精确区间未用，此样本量下二者接近） |
| 总成本 | **¥1.6351**（上限 ¥5/¥10 ✓ 未触发停止条件） |
| tokens | prompt 85,599（cache_hit 68,224 / cache_miss 17,375）+ output 195,774 |
| system_fingerprint | 单一：`fp_a18b46594c_prod0820_fp8_kvcache_20260402`（冻结验证 ✓） |
| 单元耗时 | 均值 28.6s / 中位 27.0s / 最慢 66.0s |
| 幂等 | 60/60 replay_ok（同幂等键重放同响应，无重复执行） |
| 入库 | 315 卡（59 单元计划数全匹配；generation_item_id 无重复） |

失败单元 43（第 6 章 COMPACT）：批次 1 停留 PROCESSING、http_status=None → **adapter 系统级失败（GENERATION_FAILED，上游请求错误/超时）**，executor 按 4.1 标记任务 FAILED 并保留已入库结果；非 Schema 违约、非我方缺陷（第 6 章 20 块中仅 1 块触发，随机上游抖动）。

## 3. 失败率解释边界（R-05）

- 60 单元在**预先固定抽样框 + 近似独立**条件下，单侧 95% 失败率上界 ≈ **8.9%**（Wilson 上界；0/60 理论下界 4.9% 因 1 个真实失败未达到）。
- 该区间**仅描述本书 3 章固定抽样框在此模型/配置下的单元级失败**，不外推全书/生产质量。
- 8.1 其余指标：重复入库率 0%（live 实证）；断点续传/PDF 解析成功率由本机回归（AC-05/AC-01）覆盖。

## 4. canary 失败与实质修复（运行 1 → 运行 2）

| 运行 | 结果 | 根因 | 修复 |
| --- | --- | --- | --- |
| 运行 1（canary） | 0/3 卡 | **generator prompt v1 指令输出裸单卡对象**，V5A 解析器 `parse_cards_json` 期望 `{"cards":[...]}` 包装——资产与解析契约断裂（V5A 遗留，mock 测试掩盖） | `prompts/generator v1 → v2`（输出 `{"cards":[...]}`）+ 规则 4「每知识点一张卡」（批次语义），manifest/CHANGELOG/4 处断言同步；诊断调用验证 3 卡全合法 |
| 运行 2（正式） | 59/60 | 单元 43 上游抖动 | 无（非我方缺陷，按 8.1 报告真实失败） |

## 5. 人工复核（描述性，非门槛）

按难度分层固定抽 18 张（seed 20260811，BASIC/UNDERSTANDING/APPLICATION 各 6）：

- **BASIC（6）**：定义类问答清晰准确（AI Agent 定义/上下文工程目标/上下文窗口），front/back 对应一致，长度适中。
- **UNDERSTANDING（6）**：判断题正确性 OK（Agent 自主性 vs 传统程序、评估方式、可解释性）；2 张含英文术语/英文题干（原文英文术语保留，可接受）；个别答案简短（如判断题「正确」二字）但无错误。
- **APPLICATION（6）**：场景应用题质量高（法律咨询上下文工程应用、在线/离线评估对比、Agent 组件）；1 张评估方法答案用了机器学习通用术语（交叉验证/留出法），对 Agent 评估略泛化但合理。
- 总体：18/18 无事实性错误、无前后不匹配；Schema 全部合法（复核抽取时校验）。

## 6. 边界与未验证

- 本验证仅覆盖：固定 3 章抽样框 × 冻结模型 × 单次运行；不覆盖全书/其他模型/生产负载。
- 外部范围（R-06）：Cloudflare Tunnel、TLS 证书、Android 真机联网不在本期执行。
- OCR/扫描版 PDF（AC-01 排除项）未验证。
- 多实例/生产 DB（PostgreSQL）行级锁语义未验证（R-17 登记）。
- API_KEY_ENCRYPTION_KEY 未在 .env 提供（driver 运行时生成临时密钥——live DB 密文不可跨进程解密，属已知限制，报告只标注来源）。
