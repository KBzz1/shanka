"""目标环境配置:local(本机)/ prod(生产隧道)与测试账号凭据 env 读取。

凭据纪律(DESIGN 8.1):凭据只来自 SHANKA_TEST_USERNAME / SHANKA_TEST_EMAIL /
SHANKA_TEST_PASSWORD 环境变量,不得出现在命令参数、console 或 JSONL;缺失报错且不自动注册。
"""

from __future__ import annotations

import os

ENVIRONMENTS: dict[str, str] = {
    "local": "http://localhost:8000",
    "prod": "https://shanka.kbzz1.top",
}

USERNAME_ENV = "SHANKA_TEST_USERNAME"
EMAIL_ENV = "SHANKA_TEST_EMAIL"
PASSWORD_ENV = "SHANKA_TEST_PASSWORD"


class MissingCredentialsError(RuntimeError):
    """测试账号凭据环境变量缺失(不自动注册)。"""


def resolve(name: str) -> str:
    if name not in ENVIRONMENTS:
        raise ValueError(f"未知环境: {name},可选 {list(ENVIRONMENTS)}")
    return ENVIRONMENTS[name]


def is_prod(name: str) -> bool:
    return name == "prod"


def credentials() -> tuple[str, str, str]:
    """读取测试账号凭据;缺失时抛 MissingCredentialsError(调用方决定退出码)。"""
    username = os.environ.get(USERNAME_ENV, "")
    email = os.environ.get(EMAIL_ENV, "")
    password = os.environ.get(PASSWORD_ENV, "")
    missing = [
        name
        for name, value in ((USERNAME_ENV, username), (EMAIL_ENV, email), (PASSWORD_ENV, password))
        if not value
    ]
    if missing:
        raise MissingCredentialsError(f"缺少测试账号凭据环境变量: {', '.join(missing)}(不自动注册)")
    return username, email, password
