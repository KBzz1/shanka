"""services.cards.rewrite：单卡两阶段 AI 重写（V2.5 3.19/6.5：预览持久化 → CAS 应用/取消）。

阶段一 create_rewrite_preview（POST /cards/{card_id}/rewrite-previews）：可见卡（统一
可见谓词，404）→ 来源可用（3.19：GENERATED 且来源任务/章节仍在——source_task_id/
chapter_id 均为 SET NULL 外键，非空即来源仍存在，否则 409 CARD_REWRITE_UNAVAILABLE）
→ Key 解析（422/502）→ 账本 STARTED 占位 + 独立 commit（§9 硬规则）→ 双消息 chat
（max_tokens Settings 化）→ 解析/校验（422 REWRITE_SCHEMA_INVALID）→ 持久化预览行
（PENDING/base_card_version=卡当前版本/预览 JSON/custom_requirements/expires_at=now+24h）
+ finish_success 同事务——原卡零改动（不触碰正文/版本/ReviewState/学习记录）。
阶段二 apply_rewrite_preview（POST .../apply）：预览归属（404）→ 状态判定（非 PENDING
或过期 → 409 CARD_VERSION_CONFLICT，原卡不变）→ 卡可见性（统一可见谓词，404）→
版本 CAS（base_card_version 与当前版本不一致 → 409，原卡不变）→ 同事务原子替换
（正文/generation_item_id 换新/version 递增/ReviewState 重置）+ 预览 APPLIED。
零 LLM 调用、不解析 Key（应用是纯 DB 短事务）。
阶段三 cancel_rewrite_preview（DELETE ...）：PENDING → CANCELLED；过期 PENDING 先写
EXPIRED；重复取消/已应用/已过期 → no-op（可幂等 204）。

- 惰性过期：创建前清扫本用户过期 PENDING 预览（EXPIRED 落库，index pending expiry）；
  apply/cancel 对触碰的预览过期时先写状态再抛 409/返回（write-then-raise——错误路径
  回滚不持久化，与删除批次 finalizer 同款语义，成功路径补提交）。
- 失败保留：任何生成/应用失败路径原卡保留（不写卡字段）；账本 FAILED 独立 commit 后
  re-raise（GENERATION_FAILED/API_KEY_UNAVAILABLE 错误码由 llm 层/adapter 统一脱敏，
  红线 4：明文 Key 与原始异常不进响应/日志）。
- Rubric 评分字段（evidence/correctness/difficulty/learning_value/rubric_total）由
  SCORING 阶段回写，重写期留 NULL（T10 起 fake 评分退役）。
- 8.3 观测：chat 成功后 observe_llm_call 上报（预览创建与批次生成共用 llm_metrics）。
- 账本纪律（spec §9）：chat 前 create_attempt(STARTED, stage="REWRITE", scope=CARD,
  task_id 空) + 独立 session.commit()（§9 硬规则优先于 handler 幂等包装原子性的必要
  例外）；成功 → finish_success 与预览行同事务（handler 最终 commit），normalized_result
  不写入（红线 4）。
- 事务归 services：本模块不 commit，由调用方（handler 幂等包装）commit。
"""

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from domain.card import VISIBLE_PREDICATE_SQL
from domain.rewrite_preview import REWRITE_PREVIEW_EXPIRY_HOURS
from infra.db.models import ApiKey, Card, CardRewritePreview, LlmCallAttempt, ReviewState
from infra.db.session import format_utc
from infra.llm.crypto import decrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient, RetryableUpstreamError
from infra.llm.prompts import asset_versions, load_asset, safe_json_dumps
from services.generation.ledger import (
    attempt_count,
    create_attempt,
    finish_failed,
    finish_success,
)
from services.generation.llm_metrics import observe_llm_call
from services.generation.response_parse import parse_cards_json, to_internal_card
from services.generation.schema_validator import load_card_schema, validate_card

_REWRITE_STAGE = "REWRITE"


def _next_version(current: str) -> str:
    r"""version 递增（database-design 2.9）：`^v(\d+)$` → 数字 +1；其余（V1 手动卡 ISO 时间戳）→ v2。"""
    m = re.fullmatch(r"v(\d+)", current)
    if m is None:
        return "v2"
    return f"v{int(m.group(1)) + 1}"


def _parse_utc(value: str) -> datetime:
    """反解 format_utc 输出（固定形如 %Y-%m-%dT%H:%M:%S.%fZ 的 UTC 字符串；
    Python 3.11+ fromisoformat 原生接受 Z 后缀）。"""
    return datetime.fromisoformat(value)


def _preview_expires_at(now: str) -> str:
    """预览过期时刻（3.19：24 小时，实现常量统一）。"""
    return format_utc(_parse_utc(now) + timedelta(hours=REWRITE_PREVIEW_EXPIRY_HOURS))


def _resolve_api_key(session: Session, *, user_id: str, settings: Settings) -> str:
    """Key 解析（红线 4：仅本调用路径解密；明文不落日志/响应）。

    无加密配置或 api_keys 无 AVAILABLE 行 → API_KEY_NOT_SET（422，契约 ch7「样卡/任务启动时未
    保存 Key」语义）；解密失败 → API_KEY_UNAVAILABLE（502——502 留给解密失败/上游不可用）。
    P4-4：按 user 域查询（列投影 Core select——只取 encrypted_key 列）。
    """
    key = key_from_settings(settings)
    encrypted = session.scalar(
        select(ApiKey.encrypted_key).where(ApiKey.user_id == user_id, ApiKey.status == "AVAILABLE")
    )
    if key is None or encrypted is None:
        raise AppError(ErrorCode.API_KEY_NOT_SET, "未保存 API Key 或加密配置缺失")
    try:
        return decrypt_key(encrypted, key)
    except Exception:  # noqa: BLE001 —— 解密失败（畸形 payload/密钥不符）统一 API_KEY_UNAVAILABLE
        raise AppError(ErrorCode.API_KEY_UNAVAILABLE, "API Key 解密失败") from None


def _card_content(card: Card) -> dict[str, object]:
    """重写输入携带的原卡内容字段（按卡型；指纹内容 hash 与 user 载荷共用）。"""
    if card.card_type == "TRUE_FALSE":
        return {
            "type": card.card_type,
            "front": card.front,
            "back": card.back,
            "statement": card.statement,
            "answer_boolean": (None if card.answer_boolean is None else bool(card.answer_boolean)),
            "explanation": card.explanation,
        }
    return {
        "type": card.card_type,
        "front": card.front,
        "back": card.back,
        "question": card.question,
        "answer": card.answer,
    }


def _build_rewrite_prompts(card: Card, *, custom_requirements: str | None) -> tuple[str, str]:
    """Rewrite 双消息组装（spec §5.7 Rewrite 行）：稳定 system（rewrite v4 → 原始
    generator-output schema v3 原文）+ 动态 user（<REWRITE_INPUT> 安全 JSON 信封）。

    与 Generator/Planner/Scoring 同款定式：system 只承载可版本化稳定资产（缓存前缀
    逐字节稳定），user 只承载本次动态 JSON；完整 Prompt 不落日志（红线 4）。
    """
    system_prompt = (
        f"{load_asset('prompts', 'rewrite')}\n\n<GENERATOR_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'generator_output')}\n</GENERATOR_OUTPUT_SCHEMA>"
    )
    payload: dict[str, object] = {
        "card_id": card.card_id,
        "version": card.version,
        "card": _card_content(card),
        "target_difficulty": card.target_difficulty,
        "source_chunks": [],
        "custom_requirements": custom_requirements,
    }
    user_prompt = f"<REWRITE_INPUT>{safe_json_dumps(payload)}</REWRITE_INPUT>"
    return system_prompt, user_prompt


def _rewrite_operation_key(card: Card, *, idempotency_key: str | None) -> str:
    """重写 operation_key（spec §9）：rewrite:{card_id}:{card_version}:{幂等键 hash 前 16 位}
    ——必须区分同一卡片的多次用户请求，历史失败不得影响后续合法重写。"""
    key_hash = hashlib.sha256((idempotency_key or "").encode("utf-8")).hexdigest()[:16]
    return f"rewrite:{card.card_id}:{card.version}:{key_hash}"


def _rewrite_input_fingerprint(
    card: Card, *, custom_requirements: str | None, versions: dict[str, str]
) -> str:
    """重写输入指纹（spec §9）：card_id/version/卡内容 hash/用户要求/资产版本。
    完整原文与完整 Prompt 不进入指纹载荷或账本（红线 4）。"""
    content_hash = hashlib.sha256(
        json.dumps(
            _card_content(card), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "card_id": card.card_id,
        "version": card.version,
        "content_sha256": content_hash,
        "custom_requirements": custom_requirements,
        "rewrite_prompt_version": versions["rewrite_prompt_version"],
        "generator_output_schema_version": versions["schema_version"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fail_rewrite_attempt(
    session: Session, attempt: LlmCallAttempt, *, error_code: str, now: str
) -> None:
    """失败终态独立落库（§9）：finish_failed + commit，调用方随后 re-raise——
    原卡保留语义不变（卡字段未被触碰，无业务副作用）。"""
    finish_failed(session, attempt, error_code=error_code, now=now)
    session.commit()


def _expire_user_previews(session: Session, *, user_id: str, now: str) -> None:
    """惰性过期清扫（3.19/index pending expiry）：本用户过期 PENDING 预览 → EXPIRED。

    幂等：重复执行不再命中；调用点：预览创建（无列表端点，创建是唯一全量路径）。
    """
    for row in session.scalars(
        select(CardRewritePreview).where(
            CardRewritePreview.user_id == user_id,
            CardRewritePreview.status == "PENDING",
        )
    ).all():
        if row.expires_at <= now:
            row.status = "EXPIRED"
            row.updated_at = now


def _owned_preview(
    session: Session, *, user_id: str, card_id: str, rewrite_id: str
) -> CardRewritePreview:
    """归属查预览（apply/cancel 用；跨用户/不存在/卡不匹配统一 404，契约 1.1）。"""
    preview = session.scalar(
        select(CardRewritePreview).where(
            CardRewritePreview.rewrite_id == rewrite_id,
            CardRewritePreview.user_id == user_id,
            CardRewritePreview.card_id == card_id,
        )
    )
    if preview is None:
        raise AppError(ErrorCode.CARD_NOT_FOUND, "重写预览不存在")
    return preview


def _preview_payload(card: Card, internal: dict[str, Any]) -> dict[str, object]:
    """预览存储 JSON（database-design 2.21：front/back/card_type/target_difficulty +
    apply 需要的完整内部卡字段；不含完整 Prompt）。"""
    return {**internal, "target_difficulty": card.target_difficulty}


def preview_view(preview: CardRewritePreview) -> dict[str, object]:
    """预览响应视图（openapi CardRewritePreview；structure-contract 3.19）。"""
    data = json.loads(preview.preview)
    return {
        "rewrite_id": preview.rewrite_id,
        "card_id": preview.card_id,
        "base_card_version": preview.base_card_version,
        "front": data["front"],
        "back": data["back"],
        "card_type": data["type"],
        "target_difficulty": data.get("target_difficulty"),
        "custom_requirements": preview.custom_requirements,
        "status": preview.status,
        "expires_at": preview.expires_at,
        "created_at": preview.created_at,
    }


def create_rewrite_preview(
    session: Session,
    *,
    user_id: str,
    card_id: str,
    custom_requirements: str | None,
    now: str,
    settings: Settings,
    idempotency_key: str | None = None,
    client_factory: Callable[[str], DeepSeekClient] | None = None,
) -> CardRewritePreview:
    """创建重写预览（3.19/6.5 POST rewrite-previews）：只持久化预览，不改原卡。

    校验顺序：可见卡（统一可见谓词，404）→ 来源可用（3.19：GENERATED 且来源任务/
    章节仍在——非生成卡/来源已删 409 CARD_REWRITE_UNAVAILABLE，不触网）→ Key 解析
    （422/502）→ 账本 STARTED 占位 + 独立 commit（§9 硬规则）→ 双消息 chat → 解析/
    校验（422 REWRITE_SCHEMA_INVALID）→ 预览行 + finish_success 同事务。
    任何失败均在写入前抛错（原卡零改动）；账本 FAILED 独立 commit 落库后 re-raise。
    返回持久化预览行（调用方 commit）。
    client_factory 测试注入（mock transport），生产缺省构造 DeepSeekClient（明文 Key
    仅存在于 client 实例——executor 模式）。
    """
    _expire_user_previews(session, user_id=user_id, now=now)
    # 统一可见谓词（3.9）：STAGED/删除批次卡对用户单卡操作同样不可见（4.1）
    card = session.scalar(
        select(Card).where(
            Card.card_id == card_id,
            Card.user_id == user_id,
            text(VISIBLE_PREDICATE_SQL),
        )
    )
    if card is None:
        raise AppError(ErrorCode.CARD_NOT_FOUND, "卡片不存在")
    # 来源可用（3.19）：只允许来源项目、PDF、章节和来源页仍存在的 GENERATED 卡。
    # source_task_id/chapter_id 均为 SET NULL 外键——非空即来源链（任务→项目/PDF→章节→页）
    # 仍存在；删历史保留卡/删章节后任一为 NULL → 不可重写（FR-08 不得伪造来源）。
    if card.source != "GENERATED" or card.source_task_id is None or card.chapter_id is None:
        raise AppError(ErrorCode.CARD_REWRITE_UNAVAILABLE, "来源已失效或非生成卡，不可重写")
    api_key = _resolve_api_key(session, user_id=user_id, settings=settings)
    client = (
        client_factory(api_key)
        if client_factory is not None
        else DeepSeekClient(settings, api_key=api_key)
    )
    versions = asset_versions()
    operation_key = _rewrite_operation_key(card, idempotency_key=idempotency_key)
    attempt_no = (
        attempt_count(session, task_id=None, stage=_REWRITE_STAGE, operation_key=operation_key) + 1
    )
    attempt = create_attempt(
        session,
        user_id=user_id,
        scope_type="CARD",
        scope_id=card_id,
        task_id=None,
        stage=_REWRITE_STAGE,
        operation_key=operation_key,
        input_fingerprint=_rewrite_input_fingerprint(
            card, custom_requirements=custom_requirements, versions=versions
        ),
        attempt_no=attempt_no,
        model=settings.deepseek_model,
        prompt_name="rewrite",
        prompt_version=versions["rewrite_prompt_version"],
        schema_name="generator_output",
        schema_version=versions["schema_version"],
        now=now,
    )
    # §9 硬规则：任何外部 chat 调用前必须先有已提交的 STARTED 行。预览创建的事务
    # 归 handler 幂等包装，此处 commit 是账本占位的必要例外。
    session.commit()
    system_prompt, user_prompt = _build_rewrite_prompts(
        card, custom_requirements=custom_requirements
    )
    try:
        result = client.chat(
            user_prompt,
            system_prompt=system_prompt,
            max_tokens=settings.rewrite_max_output_tokens,
        )
    except RetryableUpstreamError as exc:
        _fail_rewrite_attempt(session, attempt, error_code=exc.code.value, now=now)
        raise
    except AppError as exc:
        _fail_rewrite_attempt(session, attempt, error_code=exc.code.value, now=now)
        raise
    except Exception:  # 未预期异常统一脱敏记账后上抛（原卡保留语义不变）
        _fail_rewrite_attempt(
            session, attempt, error_code=ErrorCode.GENERATION_FAILED.value, now=now
        )
        raise
    finally:
        client.close()
    observe_llm_call(result)  # 8.3：预览创建 chat 的 llm 指标上报
    try:
        cards = parse_cards_json(result["content"])
        if not cards:
            raise AppError(ErrorCode.REWRITE_SCHEMA_INVALID, "重写响应格式不符")
        internal = to_internal_card(cards[0])  # 多于 1 张只取首张——重写单卡语义
        if validate_card(internal, load_card_schema()):
            raise AppError(ErrorCode.REWRITE_SCHEMA_INVALID, "重写卡片未通过 Schema 校验")
    except AppError as exc:
        # 输出非法：调用已成功但未产出合法卡 → 账本 FAILED（保留原卡），无预览行
        _fail_rewrite_attempt(session, attempt, error_code=exc.code.value, now=now)
        raise
    except Exception:  # 解析/校验未预期异常同款脱敏记账
        _fail_rewrite_attempt(
            session, attempt, error_code=ErrorCode.GENERATION_FAILED.value, now=now
        )
        raise
    # 持久化预览行（原卡零改动）；不保存完整 Prompt（红线 4）
    preview = CardRewritePreview(
        rewrite_id=str(uuid.uuid4()),
        user_id=user_id,
        card_id=card_id,
        base_card_version=card.version,
        preview=json.dumps(_preview_payload(card, internal), ensure_ascii=False),
        custom_requirements=custom_requirements,
        status="PENDING",
        expires_at=_preview_expires_at(now),
        created_at=now,
        updated_at=now,
    )
    session.add(preview)
    # 账本 SUCCESS 与预览行同事务（handler 最终 commit；normalized_result 不写入——红线 4）
    finish_success(
        session,
        attempt,
        usage=result["usage"],
        http_status=result["http_status"],
        duration_ms=result["duration_ms"],
        now=now,
    )
    session.flush()
    return preview


def _apply_preview_content(card: Card, data: dict[str, Any], *, now: str) -> None:
    """预览 JSON → 原地替换同一 Card 行（同事务；FR-08 原子替换）。

    与旧单步同语义：内容字段覆盖更新；generation_item_id 换新标识（新版本新标识，
    旧标识随覆盖作废）；缺键清 None——类型切换时不残留旧类型字段；version 递增
    （database-design 2.9）；updated_at 刷新（created_at 不变）；source/target_
    difficulty/knowledge_point_ids 保留原值；Rubric 评分字段留 NULL 待 SCORING 回写。
    """
    card.front = data["front"]
    card.back = data["back"]
    card.card_type = data["type"]
    card.question = data.get("question")
    card.answer = data.get("answer")
    card.statement = data.get("statement")
    card.explanation = data.get("explanation")
    answer_boolean = data.get("answer_boolean")
    card.answer_boolean = int(answer_boolean) if isinstance(answer_boolean, bool) else None
    card.generation_item_id = str(uuid.uuid4())
    card.version = _next_version(card.version)
    card.updated_at = now


def _reset_review_state(review_state: ReviewState, *, now: str) -> None:
    """ReviewState 原子重置（2.10 新建卡初始值；difficulty=1.0 满足 ORM CHECK 1~10）。"""
    review_state.state = "NEW"
    review_state.stability = 0.0
    review_state.difficulty = 1.0
    review_state.due = now
    review_state.reps = 0
    review_state.lapses = 0
    review_state.last_review = None
    review_state.last_rating = None
    review_state.updated_at = now


def apply_rewrite_preview(
    session: Session,
    *,
    user_id: str,
    card_id: str,
    rewrite_id: str,
    now: str,
) -> Card:
    """应用重写预览（6.5 POST apply；CAS 原子替换）：版本一致才替换，原卡不变。

    校验顺序：预览归属（404）→ 状态判定（过期/非 PENDING → 409 CARD_VERSION_CONFLICT，
    原卡不变）→ 卡可见性（统一可见谓词，404——已删除/STAGED/批内卡不可见即不存在）
    → 版本 CAS（base_card_version 与当前版本不一致 → 409，原卡不变）→ 同事务：
    正文替换 + ReviewState 重置 + 预览 APPLIED。零 LLM 调用、不解析 Key——纯 DB 短事务。
    返回替换后的 Card（调用方 commit）。
    """
    preview = _owned_preview(session, user_id=user_id, card_id=card_id, rewrite_id=rewrite_id)
    if preview.status == "PENDING" and preview.expires_at <= now:
        # 24h TTL 到达（左闭右开）：先写 EXPIRED 再抛（write-then-raise，错误路径
        # 回滚不持久化，后续 cancel/创建清扫的成功路径补提交——删除批次同款语义）
        preview.status = "EXPIRED"
        preview.updated_at = now
        raise AppError(ErrorCode.CARD_VERSION_CONFLICT, "重写预览已过期")
    if preview.status != "PENDING":
        # 已应用/已取消：预览不再可应用（重复 apply 只产生一次有效替换，FR-08）
        raise AppError(ErrorCode.CARD_VERSION_CONFLICT, "重写预览已应用或已取消")
    # 统一可见谓词（3.9）：预览创建后卡被删除/入批 → 不可见即不存在
    card = session.scalar(
        select(Card).where(
            Card.card_id == card_id,
            Card.user_id == user_id,
            text(VISIBLE_PREDICATE_SQL),
        )
    )
    if card is None:
        raise AppError(ErrorCode.CARD_NOT_FOUND, "卡片不存在")
    if card.version != preview.base_card_version:
        # CAS 失败（并发直接编辑/其他重写已应用）：原卡不变，预览保留可取消
        raise AppError(ErrorCode.CARD_VERSION_CONFLICT, "原卡版本已变化（CAS 失败）")
    _apply_preview_content(card, json.loads(preview.preview), now=now)
    review_state = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
    if review_state is None:
        # 防御：历史数据缺失时创建（同 2.10 初始值；正常路径创建卡时已插入）
        review_state = ReviewState(
            review_state_id=str(uuid.uuid4()),
            card_id=card_id,
            state="NEW",
            stability=0.0,
            difficulty=1.0,
            due=now,
            reps=0,
            lapses=0,
            updated_at=now,
        )
        session.add(review_state)
    else:
        _reset_review_state(review_state, now=now)
    preview.status = "APPLIED"
    preview.updated_at = now
    session.flush()
    return card


def cancel_rewrite_preview(
    session: Session,
    *,
    user_id: str,
    card_id: str,
    rewrite_id: str,
    now: str,
) -> None:
    """取消重写预览（6.5 DELETE；可幂等）：PENDING → CANCELLED；过期 PENDING 先写
    EXPIRED；APPLIED/CANCELLED/EXPIRED → no-op（重复取消/取消已应用均 204）。
    跨用户/不存在 → 404（不暴露存在性）。"""
    preview = _owned_preview(session, user_id=user_id, card_id=card_id, rewrite_id=rewrite_id)
    if preview.status == "PENDING":
        if preview.expires_at <= now:
            preview.status = "EXPIRED"
        else:
            preview.status = "CANCELLED"
        preview.updated_at = now
