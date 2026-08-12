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
    rate_limit_write_per_minute: int = 60
    rate_limit_ip_per_second: int = 5
    rate_limit_api_key_per_hour: int = 10
    rate_limit_samples_per_hour: int = 20
    rate_limit_pdf_per_hour: int = 10
    # PDF 上传限制（structure-contract 6.1；可运维调整）
    # 100MB 与 Cloudflare 免费版上传上限对齐（2026-08-11 决策：教材扫描件常超 50MB）
    pdf_max_size_bytes: int = 100 * 1024 * 1024
    pdf_max_pages: int = 1000
    # PDF 扫描器后台循环间隔（lifespan daemon 线程轮询；测试不依赖，显式 scan_once）
    pdf_scan_interval_seconds: float = 1.0
    # 任务执行器后台循环间隔（lifespan daemon 线程轮询；测试不依赖，显式 scan_once）
    task_scan_interval_seconds: float = 1.0
    # 分批生成（V5A 4.2/5.7：可运维调整，客户端不得硬编码）
    batch_size: int = 3  # 每批知识点数
    generation_retry_limit: int = (
        2  # 批次 Schema 校验失败重试上限（重试 2 次，共 3 次尝试；达上限批次 SKIPPED）
    )
    # 孤儿 RUNNING 任务恢复阈值（V5B 4.5：超过该分钟数无心跳视为孤儿，Task 2 恢复消费）
    orphan_timeout_minutes: int = 30
    # 敏感项：禁止打印、复制、写入日志/响应/任务明细；`repr=False` 防意外入日志
    deepseek_api_key: str | None = Field(default=None, repr=False)
    # API Key 加密密钥（database-design 2.2：环境变量，32 字节 hex；缺失时 PUT /api-key 不可用）
    api_key_encryption_key: str | None = Field(default=None, repr=False)
    # DeepSeek 模型与 thinking 单一配置入口（R-09：默认冻结 deepseek-v4-flash + thinking disabled，可替换）
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_thinking: bool = False
    deepseek_timeout_seconds: float = 60.0
