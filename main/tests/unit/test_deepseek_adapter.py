"""infra.llm.deepseek adapter 单元测试：mock HTTP transport（不访问外网）。

- validate_key：GET /user/balance，401→INVALID、200+is_available→AVAILABLE、200+is_available=false→
  INSUFFICIENT_BALANCE、429/5xx/网络→AppError(API_KEY_UNAVAILABLE)；
- chat：POST /chat/completions（thinking 开关/JSON output/usage 映射/错误映射）；
- thinking 参数以 DeepSeek 官方 API 为准：启用时 `body["thinking"] = {"type": "enabled"}`，禁用时不带该键。
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.llm.deepseek import DeepSeekClient, _masked_key


def _settings(**kw: Any) -> Settings:
    defaults: dict[str, Any] = {
        "api_key_encryption_key": "aa" * 32,
        "deepseek_model": "deepseek-v4-flash",
        "deepseek_thinking": False,
    }
    defaults.update(kw)
    return Settings(**defaults)


def _mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_adapter_validate_key_available() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer ")
        assert request.url.path == "/user/balance"
        return httpx.Response(200, json={"is_available": True, "balance_infos": []})

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    assert client.validate_key("sk-test-abc") == "AVAILABLE"


def test_adapter_validate_key_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    assert client.validate_key("sk-bad") == "INVALID"


def test_adapter_validate_key_insufficient_balance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"is_available": False, "balance_infos": [{"total_balance": "0.00"}]}
        )

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    assert client.validate_key("sk-low") == "INSUFFICIENT_BALANCE"


def test_adapter_validate_key_upstream_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    with pytest.raises(AppError) as excinfo:
        client.validate_key("sk-x")
    assert excinfo.value.code is ErrorCode.API_KEY_UNAVAILABLE


def test_adapter_validate_key_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    with pytest.raises(AppError) as excinfo:
        client.validate_key("sk-x")
    assert excinfo.value.code is ErrorCode.API_KEY_UNAVAILABLE


def test_adapter_chat_request_shape_thinking_off_json() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 10,
                },
            },
        )

    client = DeepSeekClient(_settings(deepseek_thinking=False), transport=_mock_transport(handler))
    result = client.chat("请生成卡片", "sk-test")
    assert captured["url"] == "/chat/completions"
    body = captured["json"]
    assert body["model"] == "deepseek-v4-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert "thinking" not in body  # thinking disabled → 请求不带该参数
    assert result["content"] == '{"ok": true}'
    assert result["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 10,
    }


def test_adapter_chat_thinking_enabled() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = DeepSeekClient(_settings(deepseek_thinking=True), transport=_mock_transport(handler))
    client.chat("p", "sk-test")
    assert captured["json"]["thinking"] == {"type": "enabled"}  # DeepSeek 官方 thinking 参数


def test_adapter_chat_malformed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    with pytest.raises(AppError) as excinfo:
        client.chat("p", "sk-test")
    assert excinfo.value.code is ErrorCode.GENERATION_FAILED


def test_adapter_chat_upstream_error_maps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    with pytest.raises(AppError) as excinfo:
        client.chat("p", "sk-test")
    # chat 时 Key 已保存但可能失效：401 → API_KEY_UNAVAILABLE（非 NOT_SET）
    assert excinfo.value.code is ErrorCode.API_KEY_UNAVAILABLE


def test_adapter_masked_key() -> None:
    assert _masked_key("sk-abcdefghijkl1234") == "sk-****1234"
    assert _masked_key("short") == "sk-****hort"  # len>4 → 显示后 4 位
    assert _masked_key("abc") == "sk-****"  # 短于 4 → 全掩码
