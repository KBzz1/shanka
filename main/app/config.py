"""单一配置入口（project-structure 6）：pydantic-settings 单层配置类。

规则：默认值进代码；密钥/令牌走环境变量；禁止散落硬编码。
敏感项清单（不得写入日志、响应、任务明细或测试报告）：
- `DEEPSEEK_API_KEY` → `deepseek_api_key`（`repr=False`，仅 infra/llm 调用路径可读取）
- `API_KEY_ENCRYPTION_KEY` → `api_key_encryption_key`（`repr=False`，仅 infra/llm 调用路径可解密）

运行位置约定：开发/验收在 `main/` 下运行（env_file=".env" 相对工作目录，
仓库根 .env 不存在于 main/，故测试不会意外加载）；测试一律显式传参构造。
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "shanka-backend"
    version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./shanka.db"
    storage_path: Path = Path("./storage")
    # 限流阈值（structure-contract 1.6；可运维调整，客户端不得硬编码）
    rate_limit_write_per_minute: int = 60
    rate_limit_ip_per_second: int = 5
    rate_limit_api_key_per_hour: int = 10
    rate_limit_samples_per_hour: int = 20
    rate_limit_pdf_per_hour: int = 10
    # PDF 上传限制（structure-contract 6.1；可运维调整）
    pdf_max_size_bytes: int = 50 * 1024 * 1024
    pdf_max_pages: int = 500
    # PDF 扫描器后台循环间隔（lifespan daemon 线程轮询；测试不依赖，显式 scan_once）
    pdf_scan_interval_seconds: float = 1.0
    # 任务执行器后台循环间隔（lifespan daemon 线程轮询；测试不依赖，显式 scan_once）
    task_scan_interval_seconds: float = 1.0
    # 敏感项：禁止打印、复制、写入日志/响应/任务明细；`repr=False` 防意外入日志
    deepseek_api_key: str | None = Field(default=None, repr=False)
    # API Key 加密密钥（database-design 2.2：环境变量，32 字节 hex；缺失时 PUT /api-key 不可用）
    api_key_encryption_key: str | None = Field(default=None, repr=False)
    # DeepSeek 模型与 thinking 单一配置入口（R-09：默认冻结 deepseek-v4-flash + thinking disabled，可替换）
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_thinking: bool = False
    deepseek_timeout_seconds: float = 60.0
