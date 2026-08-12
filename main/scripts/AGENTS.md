# AGENTS.md

原 API 连通性冒烟脚本 `smoke_api.py` 已迁移至 `test-platform/scenarios/baseline/api_smoke.py`（2026-08-12）。

- 日常冒烟走测试平台：后端运行中执行 `cd test-platform && python3 scenarios/baseline/api_smoke.py [--base-url] [--device-id]`（由 `test-platform/runner/run.sh` 调度）；退出码 = 失败步骤数。
- 本目录保留 `live_estimate_smoke.py`（R1 历史验收资产，不动）；部署脚本 run.sh/stop.sh 在仓库根 `scripts/`。
- 冒烟脚本不含任何真实密钥（生成链路需 PUT /api-key）；与单元/集成测试层（`main/tests/`）独立，只验证运行中实例。
