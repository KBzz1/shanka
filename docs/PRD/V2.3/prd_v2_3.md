# 闪卡 App 产品需求文档（PRD）v2.3

| 文档属性 | 内容 |
| --- | --- |
| 产品版本 | MVP v2.3 |
| 文档状态 | 已确认：设备架构彻底清除（决策翻转 D-06→V2.3，2026-08-14） |
| 更新日期 | 2026-08-14 |
| 核心链路 | （v2.2 核心链路全继承）账号注册/登录 → PDF 上传 → 章节选择 → 样卡确认 → 正式生成 → 写入牌组 → 断点续传 → 复习与数据统计 |

---

## 0. 版本继承与变更总览

### 0.1 继承声明

本 PRD **完整继承** [PRD v2.2](../V2.2/prd_v2_2.md) 的全部业务需求（含 v2.2 继承的 v2.1 全文、
FR-01~FR-19、AC-01~AC-12、已确认决策 D-01~D-06）。**本文件只声明 V2.3 的增量与替换性表述**；
未在本文件出现的章节一律以 v2.2 原文为准；两文件冲突时以本文件为准。

### 0.2 变更清单

| 编号 | 变更 | 性质 |
| --- | --- | --- |
| 1 | D-06 撤销（决策翻转）：旧设备数据连同旧架构全部物理删除（devices 表、8 表 device_id 列、device 版约束/索引、代码与契约残留）；删除不可逆，downgrade 显式拒绝 | 破坏性（结构删除） |
| 2 | 结构变更：devices 表删除；pdf_files/tasks/decks/cards/review_events/llm_call_attempts/api_keys/idempotency_keys 删除 device_id 列与 `CHECK (device_id IS NOT NULL OR user_id IS NOT NULL)`；owner 恒为 user_id | 破坏性（结构删除） |
| 3 | 排除项：不回滚；device_timezone 字段保留（负载字段非设备实体） | 范围明确 |

---

## 1. 产品概述（V2.3 增量）

### 1.1 产品目标（V2.3 增量）

在 v2.2 目标「以账号体系确立服务端数据主体」基础上：**彻底清除匿名设备时代的数据与架构残留**。
V2.2 曾按决策 D-06 将旧 device_id 数据「不迁不删、无访问路径」保留；本版本决策翻转——旧设备数据
连同旧架构全部物理删除，数据模型归一到「owner 恒为 user_id」的单一归属语义，消除双归属（user/device）
的长期维护负担与语义歧义。

### 1.2 产品原则（V2.3 增量）

- **删除即终结**：设备架构清除为不可逆操作（downgrade 显式拒绝），回退只能依赖升级前备份；
  不提供任何兼容过渡窗口。
- **单一归属**：所有业务数据只认 `user_id`；设备不再作为数据主体出现在数据库、接口或代码中。

## 2. 变更详情

1. **D-06 撤销（决策翻转）**：旧设备数据连同旧架构全部物理删除——`devices` 表、8 张 owner 表
   （pdf_files/tasks/decks/cards/review_events/llm_call_attempts/api_keys/idempotency_keys）的
   `device_id` 列、device 版约束/索引、代码与契约中的设备残留；删除不可逆，downgrade 显式拒绝。
2. **结构变更**：`devices` 表删除；8 表删除 `device_id` 列与
   `CHECK (device_id IS NOT NULL OR user_id IS NOT NULL)`；owner 恒为 `user_id`。
3. **排除项**：不回滚；`review_events.device_timezone` 字段保留（复习事件负载字段——IANA 时区
   字符串，仅看板分桶用，非设备实体引用）。

## 3. 验收标准

- 空库 7 revisions 升级全链；`alembic check` 零漂移；V2.3 downgrade 拒绝。
- 全仓 `device_id` 运行时引用为零（迁移文件与「V2.1 历史」标注除外）。
- 契约三处一致：database-design 与 ORM 一致（红线 2 守卫全绿）；structure-contract §9 指向 V2.3。
