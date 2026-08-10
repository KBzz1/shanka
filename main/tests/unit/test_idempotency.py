"""幂等原语单元测试（structure-contract 1.3；database-design 2.12）。"""

import hashlib
import uuid

import pytest
from starlette.requests import Request
from starlette.types import Scope

from app.errors import AppError, ErrorCode
from app.middleware.idempotency import get_idempotency_key, request_body_hash


def test_idempotency_request_body_hash_deterministic() -> None:
    body = b'{"name": "deck"}'
    expected = hashlib.sha256(body).hexdigest()
    assert request_body_hash(body) == expected


def test_idempotency_request_body_hash_differs_for_diff_body() -> None:
    assert request_body_hash(b'{"a":1}') != request_body_hash(b'{"a":2}')


def _request_with_headers(headers: dict[str, str]) -> Request:
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/decks",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_idempotency_key_missing_rejected() -> None:
    with pytest.raises(AppError) as excinfo:
        get_idempotency_key(_request_with_headers({}))
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR


def test_idempotency_key_invalid_uuid_rejected() -> None:
    with pytest.raises(AppError) as excinfo:
        get_idempotency_key(_request_with_headers({"Idempotency-Key": "not-a-uuid"}))
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR


def test_idempotency_key_normalized_uuid() -> None:
    key = str(uuid.uuid4())
    assert get_idempotency_key(_request_with_headers({"Idempotency-Key": key})) == key
    # 大写 UUID 归一化为小写规范形式
    assert get_idempotency_key(_request_with_headers({"Idempotency-Key": key.upper()})) == key
