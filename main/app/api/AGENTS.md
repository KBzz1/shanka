# AGENTS.md

路由模块，每资源一个文件（api_key / cards / decks / pdfs / review / samples / stats / tasks），另含运维端点 `observability.py` / `probes.py` / `metrics.py`；编号对应 structure-contract.md 接口清单章节。

- handler 保持薄：参数校验 → 调 services → 返回 schema 化响应。
- 鉴权、幂等、统一错误由 `../middleware/` 处理，路由不重复实现。
