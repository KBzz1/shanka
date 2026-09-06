"""运行指标对象（structure-contract 8.3；与 app/api/metrics.py 共享 prometheus REGISTRY）。

llm/generation/batch 指标定义在本层：上报点在 services（batches/executor），
分层依赖 app → services → infra 单向，services 不得反向依赖 app——
故指标对象不能定义在 app/api/metrics.py（/metrics 端点仍在 app/api/metrics.py，
generate_latest(REGISTRY) 天然包含本模块注册的指标）。

duration histogram 桶为契约 8.3 约定：DeepSeek 单调用典型秒~分钟级、生成任务典型
分钟级，默认桶（上限 10s）会使大部分观测落 +Inf、分位数失真，故按量级定制。
"""

from prometheus_client import REGISTRY, Counter, Histogram

# 单次 DeepSeek 调用：样卡/重写秒级，批次 chat 常见 5~60s（质量基线每卡墙钟 ~7.7s）。
_LLM_DURATION_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300)
# 生成任务全生命周期（started_at→ended_at）：批次×8 单元，分钟到十分钟级。
_GENERATION_DURATION_BUCKETS = (5, 10, 30, 60, 120, 300, 600, 1800)

LLM_REQUESTS_TOTAL = Counter(
    "llm_requests_total", "DeepSeek 请求总数", ["model", "http_status"], registry=REGISTRY
)
LLM_REQUEST_DURATION_SECONDS = Histogram(
    "llm_request_duration_seconds",
    "DeepSeek 请求耗时",
    ["model"],
    buckets=_LLM_DURATION_BUCKETS,
    registry=REGISTRY,
)
LLM_TOKENS_TOTAL = Counter("llm_tokens_total", "DeepSeek token 消耗", ["kind"], registry=REGISTRY)
GENERATION_TASKS_TOTAL = Counter(
    "generation_tasks_total", "生成任务结果", ["result"], registry=REGISTRY
)
GENERATION_TASKS_DURATION_SECONDS = Histogram(
    "generation_tasks_duration_seconds",
    "生成任务耗时",
    buckets=_GENERATION_DURATION_BUCKETS,
    registry=REGISTRY,
)
BATCH_RETRY_TOTAL = Counter("batch_retry_total", "批次重试次数", registry=REGISTRY)
