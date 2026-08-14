"""认证主体（structure-contract 1.1：显式 AuthPrincipal，身份不得由其他字段伪装）。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    user_id: str
    session_id: str
