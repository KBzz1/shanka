"""services.cards.rewrite：单卡重写用例（V6 5.13：原地替换/失败保留/ReviewState 重置/Rubric 记录）。

- 原地替换同一 Card 行（同事务）：内容字段覆盖更新；generation_item_id 换新标识
  （PRD 5.13：新版本用新标识，旧标识随覆盖作废——database-design 256 行）；
  source/target_difficulty/knowledge_point_ids 保留原值；code 不变；
  version 递增（database-design 2.9）；updated_at 刷新（created_at 不变）。
- ReviewState 原子重置为新建卡初始值（2.10；difficulty=1.0 满足 ORM CHECK 1~10）。
- 失败保留：卡不存在/跨设备 CARD_NOT_FOUND（404）；无 Key API_KEY_NOT_SET（422）；
  解密失败 API_KEY_UNAVAILABLE（502）；响应解析/Schema 违约 REWRITE_SCHEMA_INVALID（422，
  保留原卡不做任何写）；LLM 调用异常由 adapter 抛 GENERATION_FAILED/API_KEY_UNAVAILABLE。
- Rubric 只观测（AC-06：低分照常替换）：score_card 5 字段落卡，不影响替换结果。
- 事务归 services：本函数不做 commit，调用方（handler 幂等包装）commit。
"""

import json
import re
import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.db.models import ApiKey, Card, ReviewState
from infra.llm.crypto import decrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from infra.llm.prompts import load_asset
from services.generation.response_parse import parse_cards_json, to_internal_card
from services.generation.rubric import score_card
from services.generation.schema_validator import load_card_schema, validate_card


def _next_version(current: str) -> str:
    r"""version 递增（database-design 2.9）：`^v(\d+)$` → 数字 +1；其余（V1 手动卡 ISO 时间戳）→ v2。"""
    m = re.fullmatch(r"v(\d+)", current)
    if m is None:
        return "v2"
    return f"v{int(m.group(1)) + 1}"


def _resolve_api_key(session: Session, *, device_id: str, settings: Settings) -> str:
    """Key 解析（红线 4：仅本调用路径解密；明文不落日志/响应）。

    无加密配置或 api_keys 无 AVAILABLE 行 → API_KEY_NOT_SET（422，契约 ch7「样卡/任务启动时未
    保存 Key」语义）；解密失败 → API_KEY_UNAVAILABLE（502——502 留给解密失败/上游不可用）。
    """
    key = key_from_settings(settings)
    row = session.scalar(
        select(ApiKey).where(ApiKey.device_id == device_id, ApiKey.status == "AVAILABLE")
    )
    if key is None or row is None:
        raise AppError(ErrorCode.API_KEY_NOT_SET, "未保存 API Key 或加密配置缺失")
    try:
        return decrypt_key(row.encrypted_key, key)
    except Exception:  # noqa: BLE001 —— 解密失败（畸形 payload/密钥不符）统一 API_KEY_UNAVAILABLE
        raise AppError(ErrorCode.API_KEY_UNAVAILABLE, "API Key 解密失败") from None


def _build_rewrite_prompt(card: Card, *, custom_requirements: str | None) -> str:
    """rewrite 资产填充原卡字段（类型结构化字段按 card_type 选填）→ 拼接 JSON Schema 要求。

    完整 Prompt 不落日志（红线 4/AC-08）。资产含字面 JSON 花括号，用 replace 而非 format 填充。
    """
    if card.card_type == "TRUE_FALSE":
        structured = (
            f"- 陈述（statement）：{card.statement}\n"
            f"- 判断（answer_boolean）：{card.answer_boolean}\n"
            f"- 解释（explanation）：{card.explanation}"
        )
    else:
        structured = f"- 问题（question）：{card.question}\n- 答案（answer）：{card.answer}"
    prompt = (
        load_asset("prompts", "rewrite")
        .replace("{card_type}", card.card_type)
        .replace("{front}", card.front)
        .replace("{back}", card.back)
        .replace("{structured_fields}", structured)
    )
    if custom_requirements:
        prompt += f"\n附加要求：{custom_requirements}"
    prompt += (
        f"\n请严格按以下 JSON Schema 输出：\n{json.dumps(load_card_schema(), ensure_ascii=False)}"
    )
    return prompt


def rewrite_card(
    session: Session,
    *,
    device_id: str,
    card_id: str,
    custom_requirements: str | None,
    now: str,
    settings: Settings,
    client_factory: Callable[[str], DeepSeekClient] | None = None,
) -> Card:
    """单卡重写用例（V6 流程 11 步）：归属查卡 → ReviewState 防御 → Key 解析 → chat →
    解析/校验 → 原地替换 + ReviewState 原子重置。返回替换后的 Card（调用方 commit）。

    失败路径均在写入前抛错（保留原卡）；client_factory 测试注入（mock transport），
    生产缺省构造 DeepSeekClient（明文 Key 仅存在于 client 实例——executor 模式）。
    """
    card = session.scalar(select(Card).where(Card.card_id == card_id, Card.device_id == device_id))
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
    api_key = _resolve_api_key(session, device_id=device_id, settings=settings)
    client = (
        client_factory(api_key)
        if client_factory is not None
        else DeepSeekClient(settings, api_key=api_key)
    )
    try:
        result = client.chat(_build_rewrite_prompt(card, custom_requirements=custom_requirements))
    finally:
        client.close()
    cards = parse_cards_json(result["content"])
    if not cards:
        raise AppError(ErrorCode.REWRITE_SCHEMA_INVALID, "重写响应格式不符")
    internal = to_internal_card(cards[0])  # 多于 1 张只取首张——重写单卡语义
    if validate_card(internal, load_card_schema()):
        raise AppError(ErrorCode.REWRITE_SCHEMA_INVALID, "重写卡片未通过 Schema 校验")
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
    # Rubric 观测（AC-06：低分照常替换）；target_difficulty 保留原值
    scores = score_card(
        {
            "type": card.card_type,
            "question": card.question,
            "answer": card.answer,
            "statement": card.statement,
            "explanation": card.explanation,
            "target_difficulty": card.target_difficulty,
        }
    )
    card.evidence_score = scores["evidence_score"]
    card.correctness_score = scores["correctness_score"]
    card.difficulty_score = scores["difficulty_score"]
    card.learning_value_score = scores["learning_value_score"]
    card.rubric_total_score = scores["rubric_total_score"]
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
    session.flush()
    return card
