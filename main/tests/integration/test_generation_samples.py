"""样卡生成集成测试（V2.5 Task 5 + V5A 样卡真实生成）：构成/启用难度/指纹/校验。

V2.5 起样卡经 POST /tasks/{task_id}/samples → worker 真实 LLM 生成 → 持久化于任务
（旧 /samples 纯函数路径移除）。本文件覆盖 services/generation/samples.py 的
sample_cards_llm（注入 StubClient，不触网）：
- 1~3 张样卡与启用难度一一对应（比例为 0 的难度不生成，契约 3.5）；
- 每张样卡为轻量组件（3.13）且 V2.5 三档均 QUESTION（契约 3.6 组合规则）；
- generator 双消息信封（§5.7）：system=generator prompt + generator-output schema，
  user=<GENERATION_SPEC> 安全 JSON（难度/章节文本/自定义要求）；
- Card v1 校验是唯一入库门槛（§5.6）：非法输出 → GENERATION_FAILED，不降格；
- sample_config_hash 配置指纹确定性；配置校验（validate_config，INVALID_PREFERENCES）。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import Base, Task, TextChunk
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.deepseek import RetryableUpstreamError
from services.generation.samples import config_fingerprint, sample_cards_llm
from services.generation.validate import validate_config

_NOW = "2026-08-15T00:00:00.000Z"

_SETTINGS = Settings(  # type: ignore[call-arg]  # pydantic-settings 运行时支持 _env_file
    deepseek_api_key="stub",
    api_key_encryption_key="aa" * 32,
    _env_file=None,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _config(basic: int = 40, understanding: int = 40, deep: int = 20) -> GenerationConfig:
    return GenerationConfig(
        coverage_mode="BALANCED",
        difficulty_ratio=DifficultyRatio(
            basic=basic, understanding=understanding, deep_question=deep
        ),
    )


def _envelope(user_prompt: str) -> dict[str, Any]:
    """三区块信封合并视图（V25-D-27）：规范 + 原文 + 用户要求平铺便于断言。"""

    def block(marker: str) -> dict[str, Any]:
        raw = user_prompt.split(f"<{marker}>")[1].split(f"</{marker}>")[0]
        return cast("dict[str, Any]", json.loads(raw))

    merged: dict[str, Any] = {}
    merged.update(block("GENERATION_SPEC"))
    merged["source_material"] = block("SOURCE_MATERIAL")
    merged.update(block("USER_REQUIREMENTS"))
    return merged


class StubClient:
    """注入 sample_cards_llm 的假 LLM：按信封 target_difficulty 返回合规 QUESTION 卡。

    - invalid=True：所有调用返回非 JSON（Card 校验必须拒绝，契约 §5.6）；
    - fail_difficulty：该难度调用抛注入的 AppError（上游错误传播语义）。
    """

    def __init__(
        self,
        *,
        invalid: bool = False,
        fail_difficulty: str | None = None,
        transient_failures: int = 0,
    ):
        self.calls: list[str] = []
        self.invalid = invalid
        self.fail_difficulty = fail_difficulty
        self.transient_failures = transient_failures

    def close(self) -> None:
        pass

    def chat(
        self,
        prompt: str,
        api_key: str = "",
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload = _envelope(prompt)
        difficulty = str(payload["target_difficulty"])
        self.calls.append(difficulty)
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise RetryableUpstreamError(
                ErrorCode.GENERATION_FAILED,
                "注入的短暂上游失败",
                retryable=True,
            )
        if self.fail_difficulty == difficulty:
            raise AppError(ErrorCode.GENERATION_FAILED, "注入的上游失败")
        if self.invalid:
            return {
                "content": "这不是 JSON",
                "usage": {},
                "model": "deepseek-v4-flash",
                "http_status": 200,
                "duration_ms": 1,
            }
        return {
            "content": json.dumps(
                {
                    "cards": [
                        {
                            "type": "QUESTION",
                            "question": f"样卡问题-{difficulty}",
                            "answer": f"样卡答案-{difficulty}",
                        }
                    ]
                }
            ),
            "usage": {"prompt_cache_miss_tokens": 5, "completion_tokens": 3},
            "model": "deepseek-v4-flash",
            "http_status": 200,
            "duration_ms": 1,
        }


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'v25_samples.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _seed_file(session: Session, *, file_id: str) -> None:
    """V25-D-29 基座：PDF 行伴随 LearningProject + Material（chunk 归属 materials）。"""
    from infra.db.models import LearningProject, Material, PdfFile, User

    user_id = _uuid()
    session.add(
        User(
            user_id=user_id,
            username=f"u-{user_id[:8]}",
            email=f"u-{user_id[:8]}@example.com",
            password_hash="x",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.flush()
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        name="样卡项目",
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(project)
    session.flush()
    session.add(
        PdfFile(
            file_id=file_id,
            user_id=user_id,
            filename="b.pdf",
            storage_key=_uuid(),
            size_bytes=10,
            status="PARSED",
            created_at=_NOW,
        )
    )
    session.flush()
    session.add(
        Material(
            material_id=file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
            project_id=project.project_id,
            type="PDF",
            name="b.pdf",
            status=None,
            size_bytes=10,
            created_at=_NOW,
        )
    )
    session.flush()


def _seed_chunks(session: Session, *, file_id: str, pages: int = 3) -> None:
    _seed_file(session, file_id=file_id)
    for page in range(1, pages + 1):
        session.execute(
            insert(TextChunk).values(
                chunk_id=_uuid(),
                file_id=file_id,
                material_id=file_id,
                chunk_seq=page,
                page_number=page,
                char_count=20,
                content_sha256="0" * 64,
                content=f"第 {page} 页：深度求索上下文工程的核心概念与工具调用规范。",
                created_at=_NOW,
            )
        )
    session.flush()


def _task(config: GenerationConfig, *, file_id: str) -> Task:
    return Task(
        task_id=_uuid(),
        user_id=_uuid(),
        file_id=file_id,
        status="SAMPLE_GENERATING",
        selected_chapters=json.dumps(
            [
                {
                    "chapter_id": _uuid(),
                    "name": "第 2 章 上下文工程",
                    "start_page": 1,
                    "end_page": 2,
                }
            ]
        ),
        generation_config=config.model_dump_json(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _generate(
    session: Session,
    config: GenerationConfig,
    *,
    client: StubClient,
    file_id: str | None = None,
) -> list[dict[str, object]]:
    # 每个测试独立 DB：调用即种帧（(file_id, page) 唯一；同会话复用须换 file_id）
    file_id = file_id or _uuid()
    _seed_chunks(session, file_id=file_id)
    task = _task(config, file_id=file_id)
    return sample_cards_llm(session, task=task, config=config, client=client, settings=_SETTINGS)


def test_samples_llm_three_cards_all_difficulties(
    session_factory: Callable[[], Session],
) -> None:
    """三档全启用 → 3 张（1 基础 + 1 理解 + 1 深问；V2.5 难度改名）；三档均 QUESTION。"""
    with session_factory() as session:
        client = StubClient()
        cards = _generate(session, _config(), client=client, file_id="f-1")
    assert len(cards) == 3
    assert [c["target_difficulty"] for c in cards] == [
        "BASIC",
        "UNDERSTANDING",
        "DEEP_QUESTION",
    ]
    assert client.calls == ["BASIC", "UNDERSTANDING", "DEEP_QUESTION"]
    assert sum(1 for c in cards if c["card_type"] == "QUESTION") == 3
    assert sum(1 for c in cards if c["card_type"] == "TRUE_FALSE") == 0
    # R-14：SampleCard 轻量组件（structure-contract 3.13）——无落库/归属/版本占位字段
    for card in cards:
        assert {"card_id", "front", "back", "card_type"} <= set(card)
        assert {"deck_id", "position", "created_at", "updated_at"} & set(card) == set()
    # 内容来自 LLM（非 fake 模板）：front/back 为注入的问题/答案
    assert cards[0]["front"] == "样卡问题-BASIC"
    assert cards[0]["back"] == "样卡答案-BASIC"


def test_samples_llm_only_enabled_difficulties(
    session_factory: Callable[[], Session],
) -> None:
    """比例为 0 的难度不生成（契约 3.5）：禁用理解档 → 2 张；仅基础档 → 1 张。"""
    with session_factory() as session:
        client = StubClient()
        cards = _generate(session, _config(40, 0, 60), client=client, file_id="f-2")
        assert len(cards) == 2
        assert [c["target_difficulty"] for c in cards] == ["BASIC", "DEEP_QUESTION"]
        assert client.calls == ["BASIC", "DEEP_QUESTION"]
        only_basic = _generate(session, _config(100, 0, 0), client=client, file_id="f-2b")
        assert len(only_basic) == 1
        assert only_basic[0]["target_difficulty"] == "BASIC"


def test_samples_llm_user_envelope_carries_material_and_config(
    session_factory: Callable[[], Session],
) -> None:
    """user 信封携带章节源文本（按页）与配置；断言每次调用独立构建（难度逐档）。"""
    captured: list[dict[str, Any]] = []

    class Capture(StubClient):
        def chat(  # 捕获签名子集（缺省参数缺省即可）
            self,
            user_prompt: str,
            system_prompt: str | None = None,
            max_tokens: int | None = None,
        ) -> dict[str, Any]:
            captured.append(_envelope(user_prompt))
            return super().chat(user_prompt, system_prompt=system_prompt, max_tokens=max_tokens)

    with session_factory() as session:
        _seed_chunks(session, file_id="f-1")
        _generate(session, _config(), client=Capture(), file_id="f-4")
    assert len(captured) == 3
    assert captured[0]["source_material"], "信封必须携带章节源文本"
    assert captured[0]["learning_objective"] == "第 2 章 上下文工程"
    assert captured[0]["card_type"] == "QUESTION"
    assert captured[0]["custom_requirements"] is None


def test_samples_llm_rejects_invalid_output(
    session_factory: Callable[[], Session],
) -> None:
    """非 JSON 输出 → GENERATION_FAILED（Schema 唯一门槛，不降格不重试）。"""
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        _generate(session, _config(), client=StubClient(invalid=True), file_id="f-5")
    assert excinfo.value.code is ErrorCode.GENERATION_FAILED


def test_samples_llm_upstream_error_propagates(
    session_factory: Callable[[], Session],
) -> None:
    """上游（Key 401 等）AppError 原样传播 → 由 executor 映射任务 FAILED + error_code。"""
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        _generate(
            session,
            _config(),
            client=StubClient(fail_difficulty="UNDERSTANDING"),
            file_id="f-6",
        )
    assert excinfo.value.code is ErrorCode.GENERATION_FAILED


def test_samples_llm_retries_transient_upstream_error(
    session_factory: Callable[[], Session],
) -> None:
    """短暂网络/5xx 故障只重试当前难度，不能让整次样卡任务直接失败。"""
    with session_factory() as session:
        client = StubClient(transient_failures=1)
        cards = _generate(session, _config(), client=client, file_id="f-7")

    assert len(cards) == 3
    assert client.calls == ["BASIC", "BASIC", "UNDERSTANDING", "DEEP_QUESTION"]


def test_samples_fingerprint_deterministic_and_sensitive() -> None:
    """配置指纹：同配置同值、不同配置不同值；dict/模型两路径同值（start 校验口径）。"""
    assert config_fingerprint(_config()) == config_fingerprint(_config())
    assert config_fingerprint(_config()) == config_fingerprint(_config().model_dump())
    different = GenerationConfig(
        coverage_mode="EXTENSIVE",
        difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
    )
    assert config_fingerprint(different) != config_fingerprint(_config())


def test_samples_validate_config() -> None:
    validate_config(_config())  # 合法
    # V2.5：非法比例/非法 coverage_mode 由 Pydantic 模型层拦截（构造即 ValidationError）
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        DifficultyRatio(basic=50, understanding=50, deep_question=20)  # 合计 120 非法
    with pytest.raises(pydantic.ValidationError):
        GenerationConfig(
            coverage_mode="HUGE",
            difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
        )
    # service 层兜底（model_construct 绕过模型 validator 的防御路径）：
    # 比例语义非法 / coverage_mode 值域非法 → INVALID_PREFERENCES（V2.5 语义）
    bypassed_ratio = GenerationConfig.model_construct(
        coverage_mode="BALANCED",
        difficulty_ratio=DifficultyRatio.model_construct(
            basic=50, understanding=50, deep_question=20
        ),
    )
    with pytest.raises(AppError) as excinfo:
        validate_config(bypassed_ratio)
    assert excinfo.value.code is ErrorCode.INVALID_PREFERENCES
    bypassed_mode = GenerationConfig.model_construct(
        coverage_mode="HUGE",
        difficulty_ratio=DifficultyRatio.model_construct(
            basic=40, understanding=40, deep_question=20
        ),
    )
    with pytest.raises(AppError) as excinfo:
        validate_config(bypassed_mode)
    assert excinfo.value.code is ErrorCode.INVALID_PREFERENCES
