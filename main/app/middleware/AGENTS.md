# AGENTS.md

横切关注点集中地：`device_id.py`（X-Device-ID 鉴权）、`idempotency.py`（Idempotency-Key 幂等）、`error_handler.py`（统一错误）、`rate_limit.py`（限流）、`request_id.py` + `logging.py`（请求追踪与访问日志）、`metrics_middleware.py`（指标）、`body_capture.py`（响应体捕获）。

- 根 AGENTS.md 红线 3：幂等键、设备 ID 头、错误码格式只能在本目录实现，禁止散落各处。
- 新增中间件或改错误码格式时，同步更新 `docs/Architecture/structure-contract.md` 总则章节。
