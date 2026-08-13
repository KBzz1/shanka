"""ORM 模型（database-design 2.1~2.12 一一对应 + 本包新增表，契约守卫 2 校验）。

类型映射（database-design §0）：UUID→TEXT、时间→TEXT(ISO 8601 UTC)、JSON→TEXT、
布尔→INTEGER(0/1)、小数→REAL、枚举→TEXT。
枚举值域由 domain/enums 与应用层校验保证，DB 层仅存字符串。
"""

from sqlalchemy import (
    REAL,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Device(Base):
    """2.1 devices：匿名设备 ID 数据主体。"""

    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    last_active_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ApiKey(Base):
    """2.2 api_keys：一设备一 Key，加密存储（V3B 使用）。"""

    __tablename__ = "api_keys"

    device_id: Mapped[str] = mapped_column(
        String, ForeignKey("devices.device_id", ondelete="CASCADE"), primary_key=True
    )
    encrypted_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False
    )  # AVAILABLE/INVALID/INSUFFICIENT_BALANCE/UNKNOWN
    masked_key: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class PdfFile(Base):
    """2.3 pdf_files：PDF 元数据，storage_key 为随机 UUID 存储路径。"""

    __tablename__ = "pdf_files"
    __table_args__ = (Index("ix_pdf_files_device_created", "device_id", "created_at"),)

    file_id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, ForeignKey("devices.device_id"), nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # PENDING/PARSING/PARSED/FAILED
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class Chapter(Base):
    """2.4 chapters：章节（用户可改 name/start_page/end_page）。"""

    __tablename__ = "chapters"
    __table_args__ = (Index("ix_chapters_file_id", "file_id"),)

    chapter_id: Mapped[str] = mapped_column(String, primary_key=True)
    file_id: Mapped[str] = mapped_column(
        String, ForeignKey("pdf_files.file_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    start_page: Mapped[int] = mapped_column(Integer, nullable=False)
    end_page: Mapped[int] = mapped_column(Integer, nullable=False)


class Task(Base):
    """2.5 tasks：生成任务（file_id/deck_id 删除后 SET NULL 保留任务）。"""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_device_created", "device_id", "created_at"),
        Index("ix_tasks_task_device", "task_id", "device_id"),
    )

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, ForeignKey("devices.device_id"), nullable=False)
    file_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("pdf_files.file_id", ondelete="SET NULL"), nullable=True
    )
    deck_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("decks.deck_id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_chapters: Mapped[str] = mapped_column(Text, nullable=False)  # JSON 快照
    generation_config: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    generated_card_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_batch_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_batch_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resumable: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completion_reason: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # 空单元三分支：NO_GENERATION_UNITS 等
    skipped_planning_group_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class KnowledgePoint(Base):
    """2.6 knowledge_points：知识点规划。"""

    __tablename__ = "knowledge_points"
    __table_args__ = (Index("ix_knowledge_points_task_id", "task_id"),)

    knowledge_point_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_chunk_id: Mapped[str] = mapped_column(String, nullable=False)
    topic: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # PENDING/PROCESSED/SKIPPED
    target_difficulty: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # BASIC/UNDERSTANDING/APPLICATION（规划锚定）
    card_type: Mapped[str | None] = mapped_column(String, nullable=True)  # QUESTION/TRUE_FALSE
    source_chunk_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # TEXT JSON 数组


class Batch(Base):
    """2.7 batches：生成批次（游标完整性 UNIQUE(task_id, batch_index)）。"""

    __tablename__ = "batches"
    __table_args__ = (
        UniqueConstraint("task_id", "batch_index", name="uq_batches_task_index"),
        UniqueConstraint("task_id", "generation_unit_id", name="uq_batches_task_unit"),
    )

    batch_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    generated_item_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    coverage_rate: Mapped[float | None] = mapped_column(REAL, nullable=True)
    duplicate_rate: Mapped[float | None] = mapped_column(REAL, nullable=True)
    difficulty_distribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    chapter_distribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_type_distribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty_deviation: Mapped[float | None] = mapped_column(REAL, nullable=True)
    cache_hit_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_miss_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String, nullable=True)
    rubric_version: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)
    generation_unit_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("knowledge_points.knowledge_point_id", ondelete="SET NULL"),
        nullable=True,
    )


class Deck(Base):
    """2.8 decks：牌组。

    契约观察（登记 Progress R-11，V4 裁决）：structure-contract 3.8 Deck.source 为
    MANUAL/IMPORTED/GENERATED，而 database-design 2.8 只列 MANUAL/IMPORTED——
    字段权威在 structure-contract，database-design 派生遗漏 GENERATED 枚举说明。
    F1 建表用 TEXT 无 DB CHECK，不受影响；V4 创建 GENERATED 牌组时若需确认落点再更新 database-design。
    """

    __tablename__ = "decks"
    __table_args__ = (Index("ix_decks_device_updated", "device_id", "updated_at"),)

    deck_id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, ForeignKey("devices.device_id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # MANUAL/IMPORTED
    version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class Card(Base):
    """2.9 cards：卡片（部分唯一索引 generation_item_id；UNIQUE(deck_id, position)）。"""

    __tablename__ = "cards"
    __table_args__ = (
        UniqueConstraint("deck_id", "position", name="uq_cards_deck_position"),
        Index("ix_cards_device_deck", "device_id", "deck_id"),
        Index(
            "ix_cards_gen_item_partial",
            "generation_item_id",
            unique=True,
            sqlite_where=text("source = 'GENERATED' AND generation_item_id IS NOT NULL"),
        ),
    )

    card_id: Mapped[str] = mapped_column(String, primary_key=True)
    deck_id: Mapped[str] = mapped_column(
        String, ForeignKey("decks.deck_id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # GENERATED/MANUAL/IMPORTED
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    front: Mapped[str] = mapped_column(Text, nullable=False)
    back: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str | None] = mapped_column(String, nullable=True)
    card_type: Mapped[str] = mapped_column(String, nullable=False)  # QUESTION/TRUE_FALSE
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_boolean: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_difficulty: Mapped[str | None] = mapped_column(String, nullable=True)
    knowledge_point_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 数组
    evidence_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correctness_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    difficulty_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    learning_value_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rubric_total_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ReviewState(Base):
    """2.10 review_states：FSRS 排程状态快照（与卡片一对一）。"""

    __tablename__ = "review_states"
    __table_args__ = (
        CheckConstraint("stability >= 0", name="ck_review_states_stability"),
        CheckConstraint("difficulty >= 1 AND difficulty <= 10", name="ck_review_states_difficulty"),
        Index("ix_review_states_due", "due"),
    )

    review_state_id: Mapped[str] = mapped_column(String, primary_key=True)
    card_id: Mapped[str] = mapped_column(
        String, ForeignKey("cards.card_id", ondelete="CASCADE"), unique=True, nullable=False
    )
    state: Mapped[str] = mapped_column(String, nullable=False)  # NEW/LEARNING/REVIEW/RELEARNING
    stability: Mapped[float] = mapped_column(REAL, nullable=False)
    difficulty: Mapped[float] = mapped_column(REAL, nullable=False)
    due: Mapped[str] = mapped_column(String, nullable=False)
    last_review: Mapped[str | None] = mapped_column(String, nullable=True)
    reps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lapses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_rating: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ReviewEvent(Base):
    """2.11 review_events：不可变复习事件（UNIQUE(device_id, client_event_id)）。"""

    __tablename__ = "review_events"
    __table_args__ = (
        UniqueConstraint("device_id", "client_event_id", name="uq_review_events_device_client"),
        Index("ix_review_events_device_reviewed", "device_id", "reviewed_at"),
        Index("ix_review_events_card_id", "card_id"),
    )

    review_event_id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, ForeignKey("devices.device_id"), nullable=False)
    card_id: Mapped[str] = mapped_column(
        String, ForeignKey("cards.card_id", ondelete="CASCADE"), nullable=False
    )
    client_event_id: Mapped[str] = mapped_column(String, nullable=False)
    rating: Mapped[str] = mapped_column(String, nullable=False)  # AGAIN/HARD/GOOD/EASY
    reviewed_at: Mapped[str] = mapped_column(String, nullable=False)
    device_timezone: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class IdempotencyKey(Base):
    """2.12 idempotency_keys：幂等（复合主键 device_id+path+key；F1 Task 8 增加 request_body_hash 列）。"""

    __tablename__ = "idempotency_keys"

    # 复合主键列序对齐 database-design 2.12 `PRIMARY KEY (device_id, path, idempotency_key)`：
    # SQLite 复合主键前导列为 device_id，device_id 过滤查询可命中 rowid 索引。
    device_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[str] = mapped_column(Text, nullable=False)  # JSON 快照
    request_body_hash: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 首次请求体 SHA-256(hex)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class TextChunk(Base):
    """text_chunks：页文本一页一行、与章节解耦（spec §4.1；file_id 删除级联清理）。"""

    __tablename__ = "text_chunks"
    __table_args__ = (
        UniqueConstraint("file_id", "page_number", name="uq_text_chunks_file_page"),
        Index("ix_text_chunks_file_page", "file_id", "page_number"),
    )

    chunk_id: Mapped[str] = mapped_column(String, primary_key=True)
    file_id: Mapped[str] = mapped_column(
        String, ForeignKey("pdf_files.file_id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class LlmCallAttempt(Base):
    """llm_call_attempts：LLM 调用账本（spec §9；调用前 STARTED 占位，重试/上限/成本权威）。"""

    __tablename__ = "llm_call_attempts"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_id",
            "stage",
            "operation_key",
            "attempt_no",
            name="uq_llm_call_attempts_scope_attempt_no",
        ),
        Index("ix_llm_call_attempts_device_created", "device_id", "created_at"),
        Index("ix_llm_call_attempts_task_stage_operation", "task_id", "stage", "operation_key"),
    )

    call_id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, ForeignKey("devices.device_id"), nullable=False)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)  # TASK/CARD
    scope_id: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=True
    )
    stage: Mapped[str] = mapped_column(
        String, nullable=False
    )  # PLANNING/GENERATING/SCORING/REWRITE
    operation_key: Mapped[str] = mapped_column(String, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    prompt_name: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    schema_name: Mapped[str | None] = mapped_column(String, nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String, nullable=True)
    rubric_version: Mapped[str | None] = mapped_column(String, nullable=True)
    cache_hit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_miss: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="STARTED")
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    normalized_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
