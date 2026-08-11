# AGENTS.md

API 连通性冒烟脚本（`smoke_api.py`）：对运行中服务的 HTTP 实链路验证——探针、鉴权（X-Device-ID 必须）、牌组 CRUD、幂等重放（C-04）、错误响应结构、openapi 契约、metrics。

- 用法见脚本 docstring（`python scripts/smoke_api.py [--base-url] [--device-id]`）；退出码 = 失败步骤数。
- 不含任何真实密钥（生成链路需 PUT /api-key）；与单元/集成测试层（`main/tests/`）独立，只验证运行中实例。
