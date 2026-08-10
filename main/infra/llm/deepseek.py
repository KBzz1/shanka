"""DeepSeek 正式 adapter（红线 4：Key 明文只在本模块调用路径；异常脱敏）。

- validate_key：GET /user/balance → AVAILABLE/INVALID/INSUFFICIENT_BALANCE；上游不可用（含 200 畸形 body）抛 API_KEY_UNAVAILABLE；
- chat：POST /chat/completions（thinking 开关 + JSON output + 超时）→ usage 映射；
- 错误映射：validate 401→INVALID、chat 401→API_KEY_UNAVAILABLE（Key 已保存但可能失效）、429/5xx→API_KEY_UNAVAILABLE、
  解析失败→GENERATION_FAILED；日志仅上游状态码/异常类型（不记录异常链、不引用 Key 明文）。
- thinking 参数（DeepSeek 官方 API）：启用时请求体 `"thinking": {"type": "enabled"}`，禁用时不携带。
- transport 可注入（mock 测试）；生产用默认 httpx。
"""

import logging
from typing import Any

import httpx

from app.config import Settings
from app.errors import AppError, ErrorCode

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.deepseek.com"
_VALIDATE_PATH = "/user/balance"
_CHAT_PATH = "/chat/completions"
_UPSTREAM_DOWN = "DeepSeek 上游不可用"


class DeepSeekClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.Client(
            base_url=_BASE_URL,
            timeout=settings.deepseek_timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def validate_key(self, api_key: str) -> str:
        """校验 Key（balance 端点）：AVAILABLE/INVALID/INSUFFICIENT_BALANCE；上游不可用抛 API_KEY_UNAVAILABLE。"""
        try:
            resp = self._client.get(_VALIDATE_PATH, headers={"Authorization": f"Bearer {api_key}"})
        except httpx.HTTPError as exc:
            logger.warning("deepseek validate_key upstream error type=%s", type(exc).__name__)
            raise AppError(ErrorCode.API_KEY_UNAVAILABLE, _UPSTREAM_DOWN) from None
        if resp.status_code == 401:
            return "INVALID"
        if resp.status_code in (429, 500, 502, 503):
            logger.warning("deepseek validate_key upstream status=%s", resp.status_code)
            raise AppError(ErrorCode.API_KEY_UNAVAILABLE, _UPSTREAM_DOWN)
        if resp.status_code != 200:
            logger.warning("deepseek validate_key upstream status=%s", resp.status_code)
            raise AppError(ErrorCode.API_KEY_UNAVAILABLE, _UPSTREAM_DOWN)
        try:
            data = resp.json()
            is_available = data.get("is_available", False)
        except (ValueError, AttributeError, TypeError) as exc:
            # 200 但 body 非 JSON / 非 object（数组、null）→ 视为上游异常，统一 API_KEY_UNAVAILABLE
            logger.warning("deepseek validate_key malformed 200 body type=%s", type(exc).__name__)
            raise AppError(ErrorCode.API_KEY_UNAVAILABLE, _UPSTREAM_DOWN) from None
        if not is_available:
            return "INSUFFICIENT_BALANCE"
        return "AVAILABLE"

    def chat(self, prompt: str, api_key: str) -> dict[str, Any]:
        """chat 请求：thinking 开关 + JSON output + usage 映射。返回 {"content", "usage"}。"""
        body: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        if self.settings.deepseek_thinking:
            body["thinking"] = {"type": "enabled"}
        try:
            resp = self._client.post(
                _CHAT_PATH,
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.HTTPError as exc:
            logger.warning("deepseek chat request error type=%s", type(exc).__name__)
            raise AppError(ErrorCode.GENERATION_FAILED, "DeepSeek 请求失败") from None
        if resp.status_code == 401:
            raise AppError(ErrorCode.API_KEY_UNAVAILABLE, "API Key 无效或不可用")
        if resp.status_code in (429, 500, 502, 503):
            logger.warning("deepseek chat upstream status=%s", resp.status_code)
            raise AppError(ErrorCode.API_KEY_UNAVAILABLE, _UPSTREAM_DOWN)
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
        except (KeyError, TypeError, ValueError, IndexError):
            raise AppError(ErrorCode.GENERATION_FAILED, "DeepSeek 响应解析失败") from None
        return {
            "content": content,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
                "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
            },
        }
