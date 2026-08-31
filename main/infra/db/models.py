"""ORM 模型（database-design 2.1~2.16 一一对应 + 本包新增表，契约守卫 2 校验）。

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
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApiKey(Base):
    """2.2 api_keys：一用户一 Key（V2.2），加密存储（V3B 使用）。

    V2.2：user_id 为数据主体隔离键与主键（mapper 身份键同 user_id 元数据主键）；
    V2.3：设备架构彻底清除——device_id 遗留列/约束随不可逆迁移删除。
    """

    __tablename__ = "api_keys"
    __table_args__ = (PrimaryKeyConstraint("user_id", name="pk_api_keys"),)

    user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.user_id"), nullable=True)
    encrypted_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False
    )  # AVAILABLE/INVALID/INSUFFICIENT_BALANCE/UNKNOWN
    masked_key: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class PdfFile(Base):
    """2.3 pdf_files：PDF 元数据，storage_key 为随机 UUID 存储路径。

    V2.2：user_id 为数据主体隔离键；V2.3：device_id 遗留列随不可逆迁移删除。
    """

    __tablename__ = "pdf_files"
    __table_args__ = (Index("ix_pdf_files_user_created", "user_id", "created_at"),)

    file_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.user_id"), nullable=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # PENDING/PARSING/PARSED/FAILED
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    # 解析租约：解析在事务外执行，完成发布前必须匹配令牌和版本，防止删除/替换后的
    # 迟到扫描结果重新写入章节与文本块。
    parse_lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    parse_lease_until: Mapped[str | None] = mapped_column(String, nullable=True)
    parse_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
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


class GenerationOperation(Base):
    """生成操作唯一性记录。

    ``idempotency_keys`` 只覆盖单个 HTTP 请求；本表覆盖跨请求、跨进程和重启后的同一
    生成意图。``task_id`` 故意是可空的非外键快照，避免 operation/task 互相依赖导致历史
    数据删除时的循环 FK；任务侧通过 ``operation_id`` 反向关联。
    """

    __tablename__ = "generation_operations"
    __table_args__ = (
        UniqueConstraint("user_id", "operation_key", name="uq_generation_operations_user_key"),
        CheckConstraint(
            "status IN ('ACTIVE','COMPLETED','FAILED','ABANDONED')",
            name="ck_generation_operations_status_domain",
        ),
        Index("ix_generation_operations_user_status", "user_id", "status", "updated_at"),
        Index(
            "ix_generation_operations_active_input",
            "user_id",
            "input_fingerprint",
            "operation_key",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )

    operation_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.user_id"), nullable=False)
    operation_key: Mapped[str] = mapped_column(String, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )  # ACTIVE/COMPLETED/FAILED/ABANDONED
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)


class Task(Base):
    """2.5 tasks：生成任务（V2.5 七态；file_id/deck_id/project_id 删除后 SET NULL 保留任务）。

    V2.2：user_id 为数据主体隔离键；V2.3：device_id 遗留列随不可逆迁移删除。
    V2.5：status 七态（DRAFT/SAMPLE_GENERATING/AWAITING_SAMPLE_CONFIRMATION/GENERATING/
    COMPLETED/FAILED/ABANDONED）；stage 列改名 internal_stage 语义（仅运行期内部观测）；
    project_id/retry_of_task_id 归属与重试关联；sample_cards 持久化样卡。
    """

    __tablename__ = "tasks"
    __table_args__ = (
        # Keep legacy values accepted by ``Base.metadata.create_all`` fixtures; the Alembic
        # migration normalizes them before installing the production-only V2.5 domain check.
        CheckConstraint(
            "status IN ('DRAFT','SAMPLE_GENERATING','AWAITING_SAMPLE_CONFIRMATION',"
            "'GENERATING','COMPLETED','FAILED','ABANDONED',"
            "'PENDING','RUNNING','PAUSED')",
            name="ck_tasks_status_domain",
        ),
        CheckConstraint(
            "stage IS NULL OR stage IN ('PLANNING','GENERATING','SCORING','PUBLISHING')",
            name="ck_tasks_stage_domain",
        ),
        CheckConstraint(
            "(claimed_by IS NULL AND lease_token IS NULL AND lease_until IS NULL)"
            " OR (claimed_by IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_tasks_lease_fields_together",
        ),
        CheckConstraint("lease_version >= 0", name="ck_tasks_lease_version_nonnegative"),
        CheckConstraint("attempt_count >= 0", name="ck_tasks_attempt_count_nonnegative"),
        Index("ix_tasks_user_created", "user_id", "created_at"),
        Index("ix_tasks_project_id", "project_id"),
        # Worker scans and deletion preflight both filter active status by resource.  Keep the
        # status/stage/heartbeat access path explicit so queue growth does not turn every poll
        # into a full tasks-table scan.
        Index("ix_tasks_status_stage_updated", "status", "stage", "updated_at"),
        Index("ix_tasks_project_status_updated", "project_id", "status", "updated_at"),
        Index("ix_tasks_deck_status_updated", "deck_id", "status", "updated_at"),
    )

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.user_id"), nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("learning_projects.project_id", ondelete="SET NULL"), nullable=True
    )  # V2.5 归属项目；新写入必填，NULL 只兼容迁移前已失去 PDF 的终态任务
    file_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("pdf_files.file_id", ondelete="SET NULL"), nullable=True
    )
    deck_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("decks.deck_id", ondelete="SET NULL"), nullable=True
    )
    retry_of_task_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tasks.task_id", ondelete="SET NULL"), nullable=True
    )  # V2.5 只指向同用户失败任务
    operation_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("generation_operations.operation_id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String, nullable=False)  # V2.5 七态
    stage: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # V2.5 改名 internal_stage 语义
    selected_chapters: Mapped[str] = mapped_column(Text, nullable=False)  # JSON 快照
    generation_config: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    sample_cards: Mapped[str | None] = mapped_column(Text, nullable=True)  # V2.5 样卡 JSON
    sample_config_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # V2.5 配置指纹
    sample_confirmed_at: Mapped[str | None] = mapped_column(String, nullable=True)  # V2.5 确认时间
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
    claimed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    lease_until: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[str | None] = mapped_column(String, nullable=True)


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
    )  # BASIC/UNDERSTANDING/DEEP_QUESTION（规划锚定；历史 APPLICATION 经迁移映射）
    card_type: Mapped[str | None] = mapped_column(String, nullable=True)  # QUESTION/TRUE_FALSE
    coverage_tier: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # CORE/IMPORTANT/LOW_FREQUENCY（Planner 标注；V25-D-25 起落库并传给生成 spec）
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
    """2.8 decks：牌组（V2.5：project_id 归属学习项目；source 枚举补 GENERATED）。

    V2.2：user_id 为数据主体隔离键；V2.3：device_id 遗留列随不可逆迁移删除。
    """

    __tablename__ = "decks"
    __table_args__ = (
        Index("ix_decks_user_updated", "user_id", "updated_at"),
        Index("ix_decks_project_id", "project_id"),
    )

    deck_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.user_id"), nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("learning_projects.project_id", ondelete="SET NULL"), nullable=True
    )  # V2.5 归属项目；NULL = 手动/独立牌组
    name: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # MANUAL/IMPORTED/GENERATED
    version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class Card(Base):
    """2.9 cards：卡片（部分唯一索引 generation_item_id；UNIQUE(deck_id, position)）。

    V2.2：user_id 为数据主体隔离键；V2.3：device_id 遗留列随不可逆迁移删除。
    V2.5：publication_state=STAGED/PUBLISHED + delete_batch_id（10 秒撤销批次），
    统一可见谓词 `publication_state='PUBLISHED' AND delete_batch_id IS NULL`（契约 3.9）；
    source_task_id/chapter_id 生成来源；索引 (publication_state, delete_batch_id)。
    """

    __tablename__ = "cards"
    __table_args__ = (
        UniqueConstraint("deck_id", "position", name="uq_cards_deck_position"),
        Index("ix_cards_user_deck", "user_id", "deck_id"),
        Index("ix_cards_source_task", "source_task_id"),
        Index("ix_cards_chapter_id", "chapter_id"),
        Index("ix_cards_publication_delete", "publication_state", "delete_batch_id"),
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
    user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.user_id"), nullable=True)
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
    source_task_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tasks.task_id", ondelete="SET NULL"), nullable=True
    )  # V2.5 生成来源任务；删历史保留卡时置空
    chapter_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("chapters.chapter_id", ondelete="SET NULL"), nullable=True
    )  # V2.5 源章节；null = 未归属章节
    publication_state: Mapped[str] = mapped_column(
        String, nullable=False, default="PUBLISHED", server_default=text("'PUBLISHED'")
    )  # V2.5 STAGED/PUBLISHED；历史卡均迁为 PUBLISHED
    delete_batch_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("card_deletion_batches.delete_batch_id", ondelete="SET NULL"),
        nullable=True,
    )  # V2.5 非空 = 10 秒待删除批次
    pending_delete_at: Mapped[str | None] = mapped_column(String, nullable=True)  # V2.5 服务端计时
    undo_until: Mapped[str | None] = mapped_column(String, nullable=True)  # V2.5 服务端撤销窗口
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
    """2.11 review_events：不可变复习事件（UNIQUE(user_id, client_event_id)）。

    V2.2：user_id 为数据主体隔离键；V2.3：device_id 遗留列/约束随不可逆迁移删除
    （device_timezone 为负载字段，保留）。
    """

    __tablename__ = "review_events"
    __table_args__ = (
        UniqueConstraint("user_id", "client_event_id", name="uq_review_events_user_client"),
        Index("ix_review_events_user_reviewed", "user_id", "reviewed_at"),
        Index("ix_review_events_card_id", "card_id"),
    )

    review_event_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.user_id"), nullable=True)
    card_id: Mapped[str] = mapped_column(
        String, ForeignKey("cards.card_id", ondelete="CASCADE"), nullable=False
    )
    client_event_id: Mapped[str] = mapped_column(String, nullable=False)
    rating: Mapped[str] = mapped_column(String, nullable=False)  # AGAIN/HARD/GOOD/EASY
    reviewed_at: Mapped[str] = mapped_column(String, nullable=False)
    device_timezone: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # V2.5 降级为可空审计字段，不参与权威统计（1.2）
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class IdempotencyKey(Base):
    """2.12 idempotency_keys：幂等（V2.2 主键 user_id+path+key）。

    V2.3：设备架构彻底清除——device_id 遗留列与 UNIQUE (device_id, path,
    idempotency_key) 随不可逆迁移删除。
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "path", "idempotency_key", name="pk_idempotency_keys"),
    )

    # 复合主键列序对齐 database-design 2.12 `PRIMARY KEY (user_id, path, idempotency_key)`。
    user_id: Mapped[str | None] = mapped_column(String, primary_key=True, nullable=True)
    path: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
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
    """llm_call_attempts：LLM 调用账本（spec §9；调用前 STARTED 占位，重试/上限/成本权威）。

    V2.2：user_id 为数据主体隔离键；V2.3：device_id 遗留列随不可逆迁移删除。
    """

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
        Index("ix_llm_call_attempts_user_created", "user_id", "created_at"),
        Index("ix_llm_call_attempts_task_stage_operation", "task_id", "stage", "operation_key"),
        Index("ix_llm_call_attempts_operation", "operation_id", "stage", "operation_key"),
        CheckConstraint(
            "stage IN ('SAMPLE','PLANNING','GENERATING','SCORING','REWRITE')",
            name="ck_llm_call_attempts_stage_domain",
        ),
    )

    call_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.user_id"), nullable=True)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)  # TASK/CARD
    scope_id: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tasks.task_id", ondelete="SET NULL"), nullable=True
    )
    operation_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("generation_operations.operation_id", ondelete="SET NULL"),
        nullable=True,
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


class User(Base):
    """2.15 users：账号数据主体（V2.2，决策 D-05；user_id 为数据主体隔离键）。

    email 为登录键（服务端 lowercase 规范化，UNIQUE）；username 为展示名
    （1-24 位中文/字母/数字/._-，可重名）；password_hash 为 Argon2id 输出，
    绝不进入日志/响应。V2.5：avatar_key 预设头像（mood_01~mood_12，默认 mood_01）。
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    avatar_key: Mapped[str] = mapped_column(
        String, nullable=False, default="mood_01", server_default=text("'mood_01'")
    )  # V2.5 预设头像，只接受内置预设
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class AuthSession(Base):
    """2.16 auth_sessions：登录会话（token 只存 SHA-256 摘要，绝不存明文）。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        Index("ix_auth_sessions_user_id", "user_id"),
    )

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)


class LearningProject(Base):
    """2.17 learning_projects（V2.5 新增）：学习项目；file_id 为 PDF↔项目唯一外键权威。

    项目状态由 PDF 状态与 chapters_confirmed_at 确定（契约 3.16，不建第二套状态列）。
    """

    __tablename__ = "learning_projects"
    __table_args__ = (Index("ix_learning_projects_user_updated", "user_id", "updated_at"),)

    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.user_id"), nullable=False)
    file_id: Mapped[str] = mapped_column(
        String, ForeignKey("pdf_files.file_id"), unique=True, nullable=False
    )  # 唯一外键权威（一个项目恰好一份当前 PDF）
    name: Mapped[str] = mapped_column(String, nullable=False)  # 1~60 字符，可重名
    chapters_confirmed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[str] = mapped_column(String, nullable=False)  # 缓存刷新与并发检查
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class UserPreferences(Base):
    """2.18 user_preferences（V2.5 新增）：账号偏好，一用户一行。

    比例约束（database-design 2.18）：三档 10% 整数档 0~100、合计 100；
    daily_goal 10~200 且 10 的倍数。
    """

    __tablename__ = "user_preferences"
    __table_args__ = (
        CheckConstraint(
            "basic_ratio % 10 = 0 AND basic_ratio BETWEEN 0 AND 100",
            name="ck_user_prefs_basic_ratio",
        ),
        CheckConstraint(
            "understanding_ratio % 10 = 0 AND understanding_ratio BETWEEN 0 AND 100",
            name="ck_user_prefs_understanding_ratio",
        ),
        CheckConstraint(
            "deep_question_ratio % 10 = 0 AND deep_question_ratio BETWEEN 0 AND 100",
            name="ck_user_prefs_deep_question_ratio",
        ),
        CheckConstraint(
            "basic_ratio + understanding_ratio + deep_question_ratio = 100",
            name="ck_user_prefs_ratio_total",
        ),
        CheckConstraint(
            "daily_goal BETWEEN 10 AND 200 AND daily_goal % 10 = 0",
            name="ck_user_prefs_daily_goal",
        ),
    )

    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.user_id"), primary_key=True
    )  # 一用户一行
    coverage_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="BALANCED", server_default=text("'BALANCED'")
    )  # COMPACT/BALANCED/EXTENSIVE
    basic_ratio: Mapped[int] = mapped_column(
        Integer, nullable=False, default=40, server_default=text("40")
    )
    understanding_ratio: Mapped[int] = mapped_column(
        Integer, nullable=False, default=40, server_default=text("40")
    )
    deep_question_ratio: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20, server_default=text("20")
    )
    daily_goal: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default=text("50")
    )  # 10~200，10 的倍数
    learning_timezone: Mapped[str] = mapped_column(String, nullable=False)  # 有效 IANA 时区
    current_project_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("learning_projects.project_id", ondelete="SET NULL"), nullable=True
    )  # 项目删除时置空
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ProjectStudySettings(Base):
    """2.19 project_study_settings：项目学习设置，一项目一行。

    旧章节字段保留用于已有生成/接口数据的读取兼容；新的今日学习计划使用双目标和
    ``project_study_decks`` 关联表，前端不再写章节范围。
    """

    __tablename__ = "project_study_settings"
    __table_args__ = (
        CheckConstraint(
            "daily_new_goal BETWEEN 0 AND 200 AND daily_new_goal % 10 = 0",
            name="ck_project_study_settings_daily_new_goal",
        ),
        CheckConstraint(
            "daily_review_goal BETWEEN 0 AND 200 AND daily_review_goal % 10 = 0",
            name="ck_project_study_settings_daily_review_goal",
        ),
        CheckConstraint(
            "daily_new_goal + daily_review_goal > 0",
            name="ck_project_study_settings_daily_goal_nonzero",
        ),
    )

    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_projects.project_id", ondelete="CASCADE"), primary_key=True
    )
    selected_chapter_ids: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default=text("'[]'")
    )  # 新卡章节范围 JSON；空数组 = 暂无新卡范围
    include_unassigned: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )  # 是否包含 chapter_id=null 的新卡（0/1）
    daily_new_goal: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default=text("10")
    )
    daily_review_goal: Mapped[int] = mapped_column(
        Integer, nullable=False, default=40, server_default=text("40")
    )
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ProjectStudyDeck(Base):
    """今日计划纳入的卡组（一个项目可选择多个卡组）。"""

    __tablename__ = "project_study_decks"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "deck_id", name="pk_project_study_decks"),
        Index("ix_project_study_decks_deck_id", "deck_id"),
    )

    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_projects.project_id", ondelete="CASCADE"), nullable=False
    )
    deck_id: Mapped[str] = mapped_column(
        String, ForeignKey("decks.deck_id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class CardDeletionBatch(Base):
    """2.20 card_deletion_batches（V2.5 新增）：卡片删除批次（10 秒撤销窗口）。"""

    __tablename__ = "card_deletion_batches"
    __table_args__ = (
        Index("ix_deletion_batches_user_status_undo", "user_id", "status", "undo_until"),
    )

    delete_batch_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.user_id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # PENDING/UNDONE/FINALIZED
    undo_until: Mapped[str] = mapped_column(String, nullable=False)  # 最后一次追加后 10 秒
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class CardRewritePreview(Base):
    """2.21 card_rewrite_previews（V2.5 新增）：单卡 AI 重写预览（两阶段；24 小时过期）。"""

    __tablename__ = "card_rewrite_previews"
    __table_args__ = (
        Index("ix_rewrite_previews_user_status_expires", "user_id", "status", "expires_at"),
    )

    rewrite_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.user_id"), nullable=False)
    card_id: Mapped[str] = mapped_column(
        String, ForeignKey("cards.card_id", ondelete="CASCADE"), nullable=False
    )
    base_card_version: Mapped[str] = mapped_column(String, nullable=False)  # 应用时 CAS
    preview: Mapped[str] = mapped_column(Text, nullable=False)  # 预览内容 JSON
    custom_requirements: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # 不保存完整 Prompt
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDING", server_default=text("'PENDING'")
    )  # PENDING/APPLIED/CANCELLED/EXPIRED
    expires_at: Mapped[str] = mapped_column(String, nullable=False)  # 24 小时（实现常量统一）
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
