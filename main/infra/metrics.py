"""运行指标对象（structure-contract 8.3；与 app/api/metrics.py 共享 prometheus REGISTRY）。

llm/generation/batch 指标定义在本层：上报点在 services（batches/executor），
分层依赖 app → services → infra 单向，services 不得反向依赖 app——
故指标对象不能定义在 app/api/metrics.py（/metrics 端点仍在 app/api/metrics.py，
generate_latest(REGISTRY) 天然包含本模块注册的指标）。
"""

from prometheus_client import REGISTRY, Counter, Histogram

LLM_REQUESTS_TOTAL = Counter(
    "llm_requests_total", "DeepSeek 请求总数", ["model", "http_status"], registry=REGISTRY
)
LLM_REQUEST_DURATION_SECONDS = Histogram(
    "llm_request_duration_seconds", "DeepSeek 请求耗时", ["model"], registry=REGISTRY
)
LLM_TOKENS_TOTAL = Counter("llm_tokens_total", "DeepSeek token 消耗", ["kind"], registry=REGISTRY)
GENERATION_TASKS_TOTAL = Counter(
    "generation_tasks_total", "生成任务结果", ["result"], registry=REGISTRY
)
GENERATION_TASKS_DURATION_SECONDS = Histogram(
    "generation_tasks_duration_seconds", "生成任务耗时", registry=REGISTRY
)
BATCH_RETRY_TOTAL = Counter("batch_retry_total", "批次重试次数", registry=REGISTRY)
