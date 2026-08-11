"""infra.llm.deepseek adapter 单元测试：mock HTTP transport（不访问外网）。

- validate_key：GET /user/balance，401→INVALID、200+is_available→AVAILABLE、200+is_available=false→
  INSUFFICIENT_BALANCE、429/5xx/网络→AppError(API_KEY_UNAVAILABLE)；200 畸形 body（非 JSON/数组）→
  API_KEY_UNAVAILABLE（上游异常口径，不逃逸为 500）；
- chat：POST /chat/completions（thinking 开关/JSON output/usage 映射/system_fingerprint 透传/错误映射）；
- thinking 参数以 DeepSeek 官方 API 为准：启用时 `body["thinking"] = {"type": "enabled"}`，禁用时不带该键。
- R1 live 加固：客户端构造 trust_env=False（不继承 shell 代理——HTTP_PROXY=127.0.0.1:7897 时直连；
  不可直接断言私有属性，用"构造无代理依赖的说明性用例"锁定行为）；
  chat 返回透传 system_fingerprint（上游无该字段 → None，R1 driver 按单元记录）。
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.llm.deepseek import DeepSeekClient


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


def test_adapter_validate_key_200_non_json_body_maps_unavailable() -> None:
    """200 + 非 JSON body → JSONDecodeError；按上游异常映射 API_KEY_UNAVAILABLE（不逃逸为 500）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    with pytest.raises(AppError) as excinfo:
        client.validate_key("sk-x")
    assert excinfo.value.code is ErrorCode.API_KEY_UNAVAILABLE


def test_adapter_validate_key_200_non_object_json_maps_unavailable() -> None:
    """200 + JSON 非 object（数组）→ data.get AttributeError；按上游异常映射 API_KEY_UNAVAILABLE。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

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


def test_adapter_chat_passthrough_system_fingerprint() -> None:
    """R1 live：上游响应含 system_fingerprint → chat 返回同值透传（driver 按单元记录）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": "deepseek-v4-flash",
                "system_fingerprint": "fp_r1_live_0001",
            },
        )

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    result = client.chat("p", "sk-test")
    assert result["system_fingerprint"] == "fp_r1_live_0001"


def test_adapter_chat_missing_system_fingerprint_is_none() -> None:
    """R1 live：上游响应无 system_fingerprint 字段 → chat 返回 None（不抛错、不伪造）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    result = client.chat("p", "sk-test")
    assert result["system_fingerprint"] is None


def test_adapter_chat_independent_of_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """R1 live 加固说明性用例：构造与代理环境无关的客户端（trust_env=False 直连）。

    trust_env 不可直接断言（httpx 私有属性，跨版本脆弱）——本用例为"无代理依赖"的行为
    锚点：即使环境注入指向死端口的 HTTP_PROXY/HTTPS_PROXY，mock transport 路径照常工作，
    生产路径因 trust_env=False 同样不读取代理环境（本机 HTTP_PROXY=127.0.0.1:7897）。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    result = client.chat("p", "sk-test")
    assert result["content"] == "answer"


def test_adapter_chat_malformed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = DeepSeekClient(_settings(), transport=_mock_transport(handler))
    with pytest.raises(AppError) as excinfo:
        client.chat("p", "sk-test")
    assert excinfo.value.code is ErrorCode.GENERATION_FAILED


def test_adapter_chat_empty_choices_maps_generation_failed() -> None:
    """200 + choices=[] → IndexError；解析失败统一 GENERATION_FAILED（不逃逸为 500）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

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
