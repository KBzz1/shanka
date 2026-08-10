"""样卡接口（structure-contract 6.3；openapi /samples）。

无副作用、不落库——豁免幂等键（契约 1.3：仅本接口豁免；POST /samples 限流由
RateLimitMiddleware 单独维度覆盖）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.schemas.samples import SampleRequest
from infra.db.session import get_db_session
from services.generation.samples import generate_samples

router = APIRouter(prefix="/samples", tags=["samples"])


@router.post("")
def generate_samples_endpoint(
    request: Request,
    payload: SampleRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """生成 3 张样卡（1 基础 + 1 理解 + 1 应用；2 问答 + 1 判断），不入库。"""
    cards = generate_samples(
        session,
        device_id=request.state.device_id,
        file_id=payload.file_id,
        chapter_ids=payload.chapter_ids,
        config=payload.generation_config.model_dump(),
    )
    return JSONResponse(content={"sample_cards": cards})
