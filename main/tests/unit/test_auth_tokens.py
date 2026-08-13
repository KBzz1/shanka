"""opaque session token（DESIGN §4.3：256-bit 随机，DB 只存 SHA-256 摘要）。"""

import hashlib

from services.auth.tokens import generate_session_token, hash_session_token


def test_token_is_256_bit_random():
    t1, t2 = generate_session_token(), generate_session_token()
    assert t1 != t2
    # token_urlsafe(32) = 256-bit → 43 字符 base64url（末位无填充）
    assert len(t1) == 43


def test_hash_is_sha256_hex_of_plaintext():
    t = generate_session_token()
    assert hash_session_token(t) == hashlib.sha256(t.encode()).hexdigest()


def test_hash_does_not_reveal_token():
    t = generate_session_token()
    h = hash_session_token(t)
    assert t not in h and h not in t
