"""样卡接口（structure-contract 6.3；openapi /samples）。

无副作用、不落库——豁免幂等键（契约 1.3：仅本接口豁免；POST /samples 限流由
RateLimitMiddleware 单独维度覆盖）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.schemas.samples import SampleCard, SampleRequest
from infra.db.session import get_db_session
from services.generation.samples import generate_samples

router = APIRouter(prefix="/samples", tags=["samples"])


def _to_sample_card(card: dict[str, object]) -> SampleCard:
    """fake/生成器返回字段 → SampleCard 轻量组件（显式映射，剔除落库/归属/版本字段）。"""
    return SampleCard(
        card_id=str(card["card_id"]),
        front=str(card["front"]),
        back=str(card["back"]),
        card_type=str(card["card_type"]),
        statement=card.get("statement"),  # type: ignore[arg-type]
        answer_boolean=card.get("answer_boolean"),  # type: ignore[arg-type]
        explanation=card.get("explanation"),  # type: ignore[arg-type]
        target_difficulty=card.get("target_difficulty"),  # type: ignore[arg-type]
    )


@router.post("", response_model=dict[str, list[SampleCard]])
def generate_samples_endpoint(
    request: Request,
    payload: SampleRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """生成 3 张样卡（1 基础 + 1 理解 + 1 应用；2 问答 + 1 判断），不入库。"""
    cards = generate_samples(
        session,
        user_id=request.state.principal.user_id,
        file_id=payload.file_id,
        chapter_ids=payload.chapter_ids,
        config=payload.generation_config,
    )
    return JSONResponse(content={"sample_cards": [_to_sample_card(c).model_dump() for c in cards]})
