# AGENTS.md

R1 live 验证支撑设施：60 文本块抽样框（`sample_frame.py`）+ live 执行驱动（`driver.py`）。详细用法见 `README.md`（前置条件、参数、停止条件、Key 安全），本文不重复。

- live 执行由主 Agent 在 `--live` 下进行；本目录默认零网络（dry-run 注入 mock transport）。
- 关键约束以 README.md 为准：样书只读引用、单次运行保护（`--allow-rerun` 显式授权）、成本上限停止、报告/日志只出现 `masked()` Key（根 AGENTS.md 红线 4）。
