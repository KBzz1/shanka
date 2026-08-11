"""复习路由（structure-contract 6.6；openapi /decks/{id}/review、/review-events）。handler 只做 HTTP 映射。

双幂等接线（1.3）：
- 键层：execute_idempotent（Idempotency-Key 优先）。评级无资源 ID 路径——path 固定
  `/review-events`，body hash（含 card_id/client_event_id）区分不同请求（契约 1.3）。
- 兜底层：client_event_id 去重由 service biz 内处理（Task 2），键层未命中时生效。
- 重放口径：键层重放 = 首次完整快照（幂等记录）；client_event_id 兜底重放 = 当前
  review_state 视图（R-12）。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.schemas.review import ReviewEventRequest, ReviewQueueItem, ReviewState
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from services.review.service import review_queue, submit_review

router = APIRouter(tags=["review"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


@router.get("/decks/{deck_id}/review", response_model=dict[str, list[ReviewQueueItem]])
def get_review_queue_endpoint(
    request: Request,
    deck_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    items = review_queue(session, device_id=request.state.device_id, deck_id=deck_id, now=_now())
    return JSONResponse(content={"items": items})


@router.post("/review-events", response_model=ReviewState)
def submit_review_endpoint(
    request: Request,
    payload: ReviewEventRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = "/review-events"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        view = submit_review(
            session,
            device_id=device_id,
            card_id=payload.card_id,
            rating=payload.rating,
            client_event_id=payload.client_event_id,
            device_timezone=payload.device_timezone,
            now=_now(),
        )
        return 200, view

    _replayed, status, body = execute_idempotent(
        session,
        device_id=device_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)
