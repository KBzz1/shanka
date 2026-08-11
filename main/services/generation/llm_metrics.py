"""services.generation.llm_metrics：8.3 llm 指标上报（批次生成与单卡重写共用）。

- 上报点：批次生成编排点（services/generation/batches.py process_next_batch，usage 落
  Batch 观测列之前）与单卡重写编排点（services/cards/rewrite.py rewrite_card，chat 返回后、
  响应解析前）。
- llm_requests_total(model, http_status) / llm_request_duration_seconds(model) /
  llm_tokens_total(kind: cache_hit/cache_miss/output)；缺失字段不计数（observe 0 亦无意义）。
- 8.3 观测范围仅 DeepSeek API：任何 chat 调用成功返回后都必须走本上报（失败路径由
  adapter 抛错，不产生观测）。
"""

from typing import Any

from infra.metrics import (
    LLM_REQUEST_DURATION_SECONDS,
    LLM_REQUESTS_TOTAL,
    LLM_TOKENS_TOTAL,
)


def observe_llm_call(result: dict[str, Any]) -> None:
    """8.3 llm 指标上报（批次与单卡重写共用；参数 = client.chat 返回的 result dict）。

    调用方在 chat 返回后、usage 落库（Batch 观测列）或响应解析前调用；成功一次
    chat 即 inc/observe 一组：llm_requests_total + llm_request_duration_seconds +
    llm_tokens_total（cache_hit/cache_miss/output 按 usage 计数）。
    """
    model = str(result.get("model") or "unknown")
    LLM_REQUESTS_TOTAL.labels(model=model, http_status=str(result.get("http_status") or 0)).inc()
    duration_ms = result.get("duration_ms")
    if isinstance(duration_ms, (int, float)):
        LLM_REQUEST_DURATION_SECONDS.labels(model=model).observe(duration_ms / 1000.0)
    usage = result.get("usage")
    if isinstance(usage, dict):
        for kind, key in (
            ("cache_hit", "prompt_cache_hit_tokens"),
            ("cache_miss", "prompt_cache_miss_tokens"),
            ("output", "completion_tokens"),
        ):
            tokens = usage.get(key)
            if isinstance(tokens, int) and tokens > 0:
                LLM_TOKENS_TOTAL.labels(kind=kind).inc(tokens)
