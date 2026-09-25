"""单一配置入口（project-structure 6）：pydantic-settings 单层配置类。

规则：默认值进代码；密钥/令牌走环境变量；禁止散落硬编码。
敏感项清单（不得写入日志、响应、任务明细或测试报告）：
- `DEEPSEEK_API_KEY` → `deepseek_api_key`（`repr=False`，仅 infra/llm 调用路径可读取）
- `API_KEY_ENCRYPTION_KEY` → `api_key_encryption_key`（`repr=False`，仅 infra/llm 调用路径可解密）

运行位置约定：开发/验收在 `main/` 下运行（env_file 相对工作目录：
".env" = main/.env、"../.env" = 仓库根 .env（优先级更高，run.sh source ../.env 同源））。
测试一律显式传参构造（断言默认值处用 `Settings(_env_file=None)`，不受仓库根 .env 加载影响）。
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore"：仓库根 .env 含 run.sh 运维变量（如 CLOUDFLARED_SERVICE_INSTALL_TOKEN），
    # 不属于 Settings 字段，必须忽略而非 forbid。
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "shanka-backend"
    version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_dir: Path = Path("./data/logs")
    database_url: str = "sqlite:///./shanka.db"
    storage_path: Path = Path("./storage")
    # 限流阈值（structure-contract 1.6；可运维调整，客户端不得硬编码）
    # IP 总闸门：rate_limit_ip_per_second = 持续 refill 速率（不变）；
    # rate_limit_ip_burst = 桶容量（同 IP 初始可立即消费的短突发上限）。两者必须为正。
    rate_limit_write_per_minute: int = 60
    rate_limit_ip_per_second: float = Field(default=5, gt=0)
    rate_limit_ip_burst: int = Field(default=10, gt=0)
    rate_limit_api_key_per_hour: int = 10
    rate_limit_samples_per_hour: int = 20
    rate_limit_pdf_per_hour: int = 10
    # /metrics 是否豁免 Bearer（默认收紧需认证；本地调试/抓取器经 env METRICS_AUTH_EXEMPT=true 打开）
    metrics_auth_exempt: bool = False
    # 账号认证（DESIGN §4.2/§4.3：可运维调整，客户端不得硬编码）
    auth_session_ttl_days: int = 30
    rate_limit_auth_per_hour: int = 20
    rate_limit_login_email_per_hour: int = 10
    # PDF 上传限制（structure-contract 6.1；可运维调整）
    # 100MB 与 Cloudflare 免费版上传上限对齐（2026-08-11 决策：教材扫描件常超 50MB）
    pdf_max_size_bytes: int = 100 * 1024 * 1024
    pdf_max_pages: int = 1000
    # ZIP 笔记包上传限制（V25-D-35；可运维调整）：总字符帽兼作 zip 炸弹防线
    zip_max_size_bytes: int = 20 * 1024 * 1024
    zip_max_files: int = 500
    zip_max_total_chars: int = 300_000
    # PDF 扫描器后台循环间隔（lifespan daemon 线程轮询；测试不依赖，显式 scan_once）
    pdf_scan_interval_seconds: float = 1.0
    # 任务执行器后台循环间隔（lifespan daemon 线程轮询；测试不依赖，显式 scan_once）
    task_scan_interval_seconds: float = 1.0
    # 分批生成（V5A 4.2/5.7：可运维调整，客户端不得硬编码）
    batch_size: int = 3  # 每批知识点数
    # 单轮任务 worker 最多处理的生成批次数；避免大任务独占进程，让其它任务获得调度机会
    generation_work_quantum_batches: int = 4
    generation_retry_limit: int = (
        2  # 批次 Schema 校验失败重试上限（重试 2 次，共 3 次尝试；达上限批次 SKIPPED）
    )
    # 样卡单难度调用重试上限；每个难度独立记账，避免已成功难度因整任务重试而重复付费
    sample_retry_limit: int = 1
    # 孤儿 RUNNING 任务恢复阈值（V5B 4.5：超过该分钟数无心跳视为孤儿，Task 2 恢复消费）
    orphan_timeout_minutes: int = 30
    # LLM 硬上限与预算（spec §10 全局硬上限、§8 scoring、§6.2/§6.3 planning；可运维调整）
    # 规划两阶段（V2.5.2）：精规划按主题打包，批输入字符上限沿用 planner_max_input_chars；
    # 粗规划分段上限与输出 token 预算耦合：每段主题数 ≈ 字符×密度锚点×1.2 悬顶，
    # 每主题输出 ≈ 64 token（标题+tier+UUID 引用），24k 字符/段保证 EXTENSIVE 即使
    # 超发 40% 也在 planner_coarse_max_output_tokens 内（2026-09-10 生产 EXTENSIVE
    # 33.5k 字整章单段 5k+ token 被 4096 截断 → JSON 解析失败 ×3 → 任务 FAILED）
    planner_max_input_chars: int = 20_000
    planner_coarse_max_input_chars: int = 24_000
    # 精规划单批主题数上限（输出 token 预算护栏：8 主题 × ≤3 单元在 2048 token 内）
    planner_fine_topics_per_call: int = 8
    # 精规划批输入页 = 主题声明页 ∪ ±1 页上下文余量
    planner_fine_page_margin: int = 1
    # 60：密度制后全书任务（12 章 33 组）也放行（V25-D-25）；V2.5.2 起口径=精规划批数
    max_planner_groups_per_task: int = 60
    # 生成预算（§10 POST 校验）：任务预算超上限直接 VALIDATION_ERROR；单元页数与原文输入双限
    max_generation_units_per_task: int = 300
    max_source_pages_per_unit: int = 8
    generator_max_input_chars: int = 10_000
    # 密度制锚点（V25-D-25）：每 1 万字目标卡数，按覆盖模式；规划目标区间由此推导
    text_chunk_target_chars: int = 3000  # V25-D-32：粘贴文本段落打包目标块大小
    cards_per_10k_compact: float = 6.0
    cards_per_10k_balanced: float = 12.0
    cards_per_10k_extensive: float = 20.0
    # V25-D-43 问答直通预算密度：每 1 万字估算问答对数（题库远密于教材；用于创建期
    # 预算守卫，超 max_generation_units_per_task 明确拒绝而非静默截断用户题库）
    qa_pairs_per_10k_chars: float = 40.0
    # 评分（§8 分层抽样）：组批受卡片数与输入字符双限，调用数超限按确定性抽样缩减
    max_scoring_calls_per_task: int = 60
    scoring_max_cards_per_call: int = 12
    scoring_max_input_chars: int = 15_000
    # 规划重试（§6.3 账本为权威）：每组预算 2 次重试（共 3 次尝试），超限组 SKIPPED
    planning_retry_limit: int = 2
    # AI 章节规划（V25-D-36 无目录 PDF）：输入分段/输出 token 对齐粗规划；段数上限 =
    # 千页书护栏（1000 页 ÷ 约 20 页/段），超限 FAILED 提示整本降级；重试预算同规划
    ai_chapter_max_input_chars: int = 24_000
    ai_chapter_max_output_tokens: int = 4096
    ai_chapter_max_segments: int = 48
    ai_chapter_retry_limit: int = 2
    ai_chapter_max_boundaries_per_segment: int = 12
    # 确定性分诊阈值（V25-D-38）：资料总字符 ≤ 该值 → 直接单章（source=AUTO，零模型）。
    # 锚点：对齐 ai_chapter_max_input_chars——更小资料的章节对生成零影响（粗规划本就单段
    # 拿全文），只影响用户选范围；闪卡问答类笔记（~9k 字）自动落此分支。
    single_chapter_max_chars: int = 24_000
    # HTML 资料限制（V25-D-38；可运维调整）：与 ZIP 同款量级
    html_max_size_bytes: int = 20 * 1024 * 1024
    html_max_total_chars: int = 300_000
    # Markdown 单文件资料限制（V25-D-40；可运维调整）：与 HTML 同款量级
    markdown_max_size_bytes: int = 20 * 1024 * 1024
    markdown_max_total_chars: int = 300_000
    # 输出上限（§5.7 JSON 截断防线 / §10：可运维调整，不是制卡字数规则；
    # Scoring 每次仍按 item 数计算更小的实际值 min(上限, 256 + 128 × items)）
    planner_max_output_tokens: int = 2048
    # 粗规划输出主题清单可较长（充分模式整段 40~60 主题，每主题 ≈64 token）；
    # 8192 为实测接受值（2026-09-10 于 deepseek-v4-flash 探针验证；2026-09-12 于
    # deepseek-flash 复核通过：两轮整章粗规划无截断）
    planner_coarse_max_output_tokens: int = 8192
    generator_max_output_tokens: int = 768
    rewrite_max_output_tokens: int = 768
    scoring_max_output_tokens: int = 4096
    # 敏感项：禁止打印、复制、写入日志/响应/任务明细；`repr=False` 防意外入日志
    deepseek_api_key: str | None = Field(default=None, repr=False)
    # API Key 加密密钥（database-design 2.2：环境变量，32 字节 hex；缺失时 PUT /api-key 不可用）
    api_key_encryption_key: str | None = Field(default=None, repr=False)
    # DeepSeek 模型与 thinking 单一配置入口（2026-09-12：默认 deepseek-flash = 官方
    # 2026-09-10 发布的 V4.1-Flash；旧名 deepseek-v4-flash 已下线、仅临时兼容路由；
    # R-09 thinking disabled 沿用，可替换）
    deepseek_model: str = "deepseek-flash"
    deepseek_thinking: bool = False
    deepseek_timeout_seconds: float = 60.0
