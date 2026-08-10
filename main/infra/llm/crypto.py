"""API Key 加密（database-design 2.2；红线 4：仅 infra/llm 调用路径可解密）。

AES-256-GCM：随机 12 字节 IV 随密文保存，payload = base64(iv + ciphertext + tag)。
解密密钥来自环境变量（Settings.api_key_encryption_key，32 字节 hex）。
明文 Key 不得进入日志/响应/任务明细（调用方保证）。
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import Settings

_IV_LEN = 12


def key_from_settings(settings: Settings) -> bytes | None:
    raw = settings.api_key_encryption_key
    if raw is None:
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    if len(key) != 32:
        return None
    return key


def encrypt_key(plaintext: str, key: bytes) -> str:
    iv = os.urandom(_IV_LEN)
    ciphertext = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return base64.b64encode(iv + ciphertext).decode("ascii")


def decrypt_key(payload: str, key: bytes) -> str:
    raw = base64.b64decode(payload)
    iv, ciphertext = raw[:_IV_LEN], raw[_IV_LEN:]
    plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
    return plaintext.decode("utf-8")
