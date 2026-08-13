"""Argon2id 密码哈希（DESIGN §4.2 冻结契约）。

生产默认参数 = OWASP 当前最低基线 memory_cost=19456 KiB / time_cost=2 / parallelism=1；
测试可注入低成本 hasher，但生产默认与参数守卫不得被降低（Task 1 守卫测试强制）。
"""

from argon2 import PasswordHasher as _Argon2PasswordHasher
from argon2.exceptions import VerifyMismatchError

PRODUCTION_PARAMS = {"memory_cost": 19456, "time_cost": 2, "parallelism": 1}

_PRODUCER = _Argon2PasswordHasher(
    memory_cost=PRODUCTION_PARAMS["memory_cost"],
    time_cost=PRODUCTION_PARAMS["time_cost"],
    parallelism=PRODUCTION_PARAMS["parallelism"],
)

# 固定 dummy 哈希：以生产参数（19456/2/1）哈希固定假密码后一次性硬编码
# （生成脚本见 task-1-report），避免每次进程启动重算 19 MiB。
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=19456,t=2,p=1$oHJZP5CsRHQHof4NLC3I7g$"
    "PimK7DZbH4jqHbYv1MGgmkSDt7YA0X9YVIRxdIsfzsc"
)


class Argon2PasswordHasher:
    """可注入低成本参数的 hasher（测试专用入口）；hash/verify 同源，便于守卫。"""

    def __init__(self, *, memory_cost: int, time_cost: int, parallelism: int) -> None:
        self._hasher = _Argon2PasswordHasher(
            memory_cost=memory_cost, time_cost=time_cost, parallelism=parallelism
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            self._hasher.verify(password_hash, password)
            return True
        except VerifyMismatchError:
            return False


def hash_password(password: str) -> str:
    return _PRODUCER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _PRODUCER.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def verify_dummy(password: str) -> bool:
    """用户不存在时执行固定 dummy 校验，抹平账号存在性时序差（DESIGN §4.2）。"""
    try:
        _PRODUCER.verify(DUMMY_PASSWORD_HASH, password)
        return True
    except VerifyMismatchError:
        return False
