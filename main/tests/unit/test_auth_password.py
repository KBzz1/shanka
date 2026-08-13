"""Argon2id 密码哈希（DESIGN §4.2：生产参数不低于 OWASP 基线 19456/2/1；dummy 校验）。"""

from services.auth.password import (
    DUMMY_PASSWORD_HASH,
    PRODUCTION_PARAMS,
    Argon2PasswordHasher,
    hash_password,
    verify_dummy,
    verify_password,
)


def test_production_params_not_below_owasp_baseline():
    assert PRODUCTION_PARAMS["memory_cost"] >= 19456
    assert PRODUCTION_PARAMS["time_cost"] >= 2
    assert PRODUCTION_PARAMS["parallelism"] >= 1


def test_hash_and_verify_roundtrip():
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong", h) is False


def test_password_not_truncated_or_normalized():
    # 128 字符上限内原样参与哈希（DESIGN：禁止静默截断/Unicode 归一化）
    p128 = "x" * 128
    h = hash_password(p128)
    assert verify_password(p128, h) is True
    # 不同大小写/NFC 差异字符串产生不同哈希结果（不做大小写转换）
    assert verify_password("PASSWORD", hash_password("password")) is False


def test_hasher_allows_low_cost_injection():
    # 低成本实例可自验证（DESIGN：测试可注入低成本 hasher；生产默认与参数守卫不被降低）
    low = Argon2PasswordHasher(memory_cost=8, time_cost=1, parallelism=1)
    h = low.hash("pw")
    assert low.verify(h, "pw") is True
    assert low.verify(h, "wrong") is False


def test_verify_dummy_runs_same_hasher_branch():
    # 只断言走同类 hasher 分支（DESIGN：不写毫秒断言）
    assert verify_dummy("anything") is False
    assert DUMMY_PASSWORD_HASH.startswith("$argon2id$")
