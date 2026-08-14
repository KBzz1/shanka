# AGENTS.md

账号用例：注册/登录/登出/当前用户/principal 解析（DESIGN §4.2/§4.3）。

- `password.py`：Argon2id 哈希——生产参数 memory_cost=19456 / time_cost=2 / parallelism=1 为冻结契约，生产默认与参数守卫不得降低；测试可注入低成本 hasher（Argon2PasswordHasher），仅测试使用。
- `tokens.py`：256-bit opaque token（secrets.token_urlsafe(32)）；DB 只存 SHA-256 摘要；明文 token 只出现在 register/login 成功响应，不进日志。
- `service.py`：登录失败统一 401 INVALID_CREDENTIALS（不暴露用户存在性）；用户不存在先执行固定 dummy 校验（verify_dummy，DUMMY_PASSWORD_HASH 硬编码）抹平时序差；损坏 PHC 哈希视为校验失败，绝不 500。
- logout 幂等：条件更新 revoked_at（已撤销/重放不重复副作用，多会话并存只撤销当前 session）。
- resolve_principal 只查 auth_sessions（行内即含 user_id，无需 JOIN users），撤销/过期 → None 供中间件 401。
- 分层：本包不依赖 app（RateLimiter 仅 TYPE_CHECKING 鸭子类型调用 check）；错误码抛 AppError（VALIDATION_ERROR/EMAIL_TAKEN/INVALID_CREDENTIALS/RATE_LIMITED）。
