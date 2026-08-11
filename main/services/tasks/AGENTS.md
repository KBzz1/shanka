# AGENTS.md

生成任务用例：创建/查询/取消/resume + 状态机 + 批次执行。

- `service.py`：任务 CRUD 与状态机（DB 条件更新）；Task 创建与 KnowledgePoint 规划同事务。
- `executor.py`：进程内 DB 驱动（4.4 定式），V5A adapter 分批执行；批次状态 + 游标 + 心跳同事务（崩溃后已完成批次保留、未完成可恢复）；系统级错误 → 任务 FAILED，批次级失败 → 批次 SKIPPED 任务继续。
- API Key 只在 infra/llm 路径解密（根 AGENTS.md 红线 4）；任务明细/日志不得出现明文。
