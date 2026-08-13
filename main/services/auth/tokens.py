"""opaque session token（DESIGN §4.3：256-bit 随机；DB 只存 SHA-256 摘要）。"""

import hashlib
import secrets


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
