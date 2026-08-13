"""services.cards.rewrite：单卡重写用例（V6 5.13：原地替换/失败保留/ReviewState 重置/Rubric 记录）。

- 原地替换同一 Card 行（同事务）：内容字段覆盖更新；generation_item_id 换新标识
  （PRD 5.13：新版本用新标识，旧标识随覆盖作废——database-design 256 行）；
  source/target_difficulty/knowledge_point_ids 保留原值；code 不变；
  version 递增（database-design 2.9）；updated_at 刷新（created_at 不变）。
- ReviewState 原子重置为新建卡初始值（2.10；difficulty=1.0 满足 ORM CHECK 1~10）。
- 失败保留：卡不存在/跨用户 CARD_NOT_FOUND（404）；无 Key API_KEY_NOT_SET（422）；
  解密失败 API_KEY_UNAVAILABLE（502）；响应解析/Schema 违约 REWRITE_SCHEMA_INVALID（422，
  保留原卡不做任何写）；LLM 调用异常由 adapter 抛 GENERATION_FAILED/API_KEY_UNAVAILABLE。
- Rubric 评分字段（evidence/correctness/difficulty/learning_value/rubric_total）由
  SCORING 阶段回写，重写期留 NULL（T10 起 fake 评分退役，brief Step 4）。
- 8.3 观测：chat 成功后 observe_llm_call 上报（批次生成与单卡重写共用 llm_metrics）。
- 账本纪律（spec §9）：chat 前 create_attempt(STARTED, stage="REWRITE", scope=CARD,
  task_id 空) + 独立 session.commit()（§9 硬规则优先于 handler 幂等包装原子性的必要
  例外，final review 收尾裁决——报告已注明）；失败（上游/AppError/未预期）→
  finish_failed + 独立 commit 后 re-raise（原卡保留语义不变）；成功 → finish_success
  与卡替换同事务（handler 最终 commit），normalized_result 不写入（红线 4）。
- 事务归 services：成功路径本函数不做 commit，调用方（handler 幂等包装）commit。
"""

import hashlib
import json
import re
import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.db.models import ApiKey, Card, LlmCallAttempt, ReviewState
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


def _resolve_api_key(session: Session, *, user_id: str, settings: Settings) -> str:
    """Key 解析（红线 4：仅本调用路径解密；明文不落日志/响应）。

    无加密配置或 api_keys 无 AVAILABLE 行 → API_KEY_NOT_SET（422，契约 ch7「样卡/任务启动时未
    保存 Key」语义）；解密失败 → API_KEY_UNAVAILABLE（502——502 留给解密失败/上游不可用）。
    P4-4：按 user 域查询（列投影 Core select——ApiKey 用户域行对 ORM 不可见，
    P3 mapper 过渡遗留，Task 5 移除）。
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
    """Rewrite 双消息组装（spec §5.7 Rewrite 行）：稳定 system（rewrite v3 → 原始
    generator-output schema v2 原文）+ 动态 user（<REWRITE_INPUT> 安全 JSON 信封）。

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
    """失败终态独立落库（§9 + final review 收尾裁决）：finish_failed + commit，
    调用方随后 re-raise——原卡保留语义不变（卡字段未被触碰，无业务副作用）。"""
    finish_failed(session, attempt, error_code=error_code, now=now)
    session.commit()


def rewrite_card(
    session: Session,
    *,
    user_id: str,
    card_id: str,
    custom_requirements: str | None,
    now: str,
    settings: Settings,
    idempotency_key: str | None = None,
    client_factory: Callable[[str], DeepSeekClient] | None = None,
) -> Card:
    """单卡重写用例（V6 流程 11 步）：归属查卡 → ReviewState 防御 → Key 解析 → 账本
    STARTED 占位 + 独立 commit（§9 硬规则）→ 双消息 chat（max_tokens Settings 化）→
    解析/校验 → 原地替换 + ReviewState 原子重置 + finish_success 同事务。
    返回替换后的 Card（调用方 commit）。

    失败路径均在写入前抛错（保留原卡）；账本 FAILED 独立 commit 落库后 re-raise。
    client_factory 测试注入（mock transport），生产缺省构造 DeepSeekClient（明文 Key
    仅存在于 client 实例——executor 模式）。
    idempotency_key：handler 传入 Idempotency-Key（operation_key 区分多次用户请求）；
    测试可传 None → hash("")。
    """
    card = session.scalar(select(Card).where(Card.card_id == card_id, Card.user_id == user_id))
    if card is None:
        raise AppError(ErrorCode.CARD_NOT_FOUND, "卡片不存在")
    review_state = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
    if review_state is None:
        # 防御：新建卡同事务插入初始行，历史数据缺失时创建（同 2.10 初始值）
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
    # §9 硬规则：任何外部 chat 调用前必须先有已提交的 STARTED 行。rewrite_card 的
    # 事务归 handler 幂等包装，此处 commit 是账本占位的必要例外（final review 裁决）。
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
    observe_llm_call(result)  # 8.3：单卡重写 chat 的 llm 指标上报（final review Important 1）
    try:
        cards = parse_cards_json(result["content"])
        if not cards:
            raise AppError(ErrorCode.REWRITE_SCHEMA_INVALID, "重写响应格式不符")
        internal = to_internal_card(cards[0])  # 多于 1 张只取首张——重写单卡语义
        if validate_card(internal, load_card_schema()):
            raise AppError(ErrorCode.REWRITE_SCHEMA_INVALID, "重写卡片未通过 Schema 校验")
    except AppError as exc:
        # 输出非法：调用已成功但未产出合法卡 → 账本 FAILED（保留原卡），幂等记录不落库
        _fail_rewrite_attempt(session, attempt, error_code=exc.code.value, now=now)
        raise
    except Exception:  # 解析/校验未预期异常同款脱敏记账
        _fail_rewrite_attempt(
            session, attempt, error_code=ErrorCode.GENERATION_FAILED.value, now=now
        )
        raise
    # 原地替换（同一 Card 行，同事务）；缺键清 None——类型切换时不残留旧类型字段
    card.front = internal["front"]
    card.back = internal["back"]
    card.card_type = internal["type"]
    card.question = internal.get("question")
    card.answer = internal.get("answer")
    card.statement = internal.get("statement")
    card.explanation = internal.get("explanation")
    answer_boolean = internal.get("answer_boolean")
    card.answer_boolean = int(answer_boolean) if isinstance(answer_boolean, bool) else None
    card.generation_item_id = str(uuid.uuid4())  # 新版本新标识，旧标识随覆盖作废（PRD 5.13）
    # Rubric 评分字段留 NULL 待 SCORING 回写（T10 起 fake 评分退役）；target_difficulty 保留原值
    card.version = _next_version(card.version)
    card.updated_at = now
    # ReviewState 原子重置（2.10 新建卡初始值）
    review_state.state = "NEW"
    review_state.stability = 0.0
    review_state.difficulty = 1.0
    review_state.due = now
    review_state.reps = 0
    review_state.lapses = 0
    review_state.last_review = None
    review_state.last_rating = None
    review_state.updated_at = now
    # 账本 SUCCESS 与卡替换同事务（handler 最终 commit；normalized_result 不写入——红线 4）
    finish_success(
        session,
        attempt,
        usage=result["usage"],
        http_status=result["http_status"],
        duration_ms=result["duration_ms"],
        now=now,
    )
    session.flush()
    return card
