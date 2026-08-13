"""认证主体（structure-contract 1.1：显式 AuthPrincipal，禁止 device_id 伪装身份）。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    user_id: str
    session_id: str
