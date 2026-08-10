"""infra.llm.crypto AES-256-GCM 加密单元测试（database-design 2.2）。"""

import base64

import pytest

from app.config import Settings
from infra.llm.crypto import decrypt_key, encrypt_key, key_from_settings

# 测试固定 32 字节密钥（hex）
_TEST_KEY_HEX = "aa" * 32


def test_crypto_roundtrip() -> None:
    key = bytes.fromhex(_TEST_KEY_HEX)
    payload = encrypt_key("sk-test-1234567890", key)
    assert decrypt_key(payload, key) == "sk-test-1234567890"


def test_crypto_iv_random_per_encryption() -> None:
    key = bytes.fromhex(_TEST_KEY_HEX)
    p1 = encrypt_key("sk-same-value", key)
    p2 = encrypt_key("sk-same-value", key)
    assert p1 != p2  # 随机 IV → 密文不同


def test_crypto_payload_contains_iv_and_ciphertext() -> None:
    key = bytes.fromhex(_TEST_KEY_HEX)
    payload = encrypt_key("sk-x", key)
    raw = base64.b64decode(payload)
    assert len(raw) > 12 + 16  # IV(12) + tag(16) + 密文
    assert raw[:12] != raw[12:24]  # IV 与密文不同


def test_crypto_wrong_key_fails() -> None:
    key_a = bytes.fromhex("aa" * 32)
    key_b = bytes.fromhex("bb" * 32)
    payload = encrypt_key("sk-secret", key_a)
    with pytest.raises(Exception):  # noqa: B017 — 不关心具体异常类，仅断言解密失败
        decrypt_key(payload, key_b)


def test_crypto_key_from_settings() -> None:
    settings = Settings(api_key_encryption_key=_TEST_KEY_HEX)
    assert key_from_settings(settings) == bytes.fromhex(_TEST_KEY_HEX)


def test_crypto_key_from_settings_invalid_returns_none() -> None:
    assert key_from_settings(Settings(api_key_encryption_key="not-hex")) is None
    assert key_from_settings(Settings()) is None
