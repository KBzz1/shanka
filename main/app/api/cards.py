"""卡片路由（structure-contract 6.5；openapi /decks/{deck_id}/cards）。

写操作幂等接线：handler 内 execute_idempotent(session, ...) → session.commit()，
幂等记录与业务副作用同事务。import 响应统一用 ImportResponse 模型构造
（openapi 的 import 请求体是内联 schema，无命名组件 → dict 解析 + 手动校验）。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.schemas.cards import CardCreate, ImportResponse, ImportResult
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from services.cards.service import card_view, create_card, import_cards, list_cards

router = APIRouter(prefix="/decks/{deck_id}/cards", tags=["decks"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


@router.get("")
def list_cards_endpoint(
    request: Request,
    deck_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    cards = list_cards(session, device_id=request.state.device_id, deck_id=deck_id)
    return JSONResponse(content={"items": [card_view(c) for c in cards]})


@router.post("", status_code=201)
def create_card_endpoint(
    request: Request,
    deck_id: str,
    payload: CardCreate,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = f"/decks/{deck_id}/cards"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        card = create_card(
            session,
            device_id=device_id,
            deck_id=deck_id,
            front=payload.front,
            back=payload.back,
            now=_now(),
        )
        return 201, card_view(card)

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


@router.post("/import", status_code=201)
def import_cards_endpoint(
    request: Request,
    deck_id: str,
    payload: dict[str, list[dict[str, str]]],
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = f"/decks/{deck_id}/cards/import"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    raw_cards = payload.get("cards", [])
    if not raw_cards:
        raise AppError(ErrorCode.IMPORT_PARSE_ERROR, "导入列表不能为空")
    for i, c in enumerate(raw_cards):
        if not c.get("front") or not c.get("back"):
            raise AppError(ErrorCode.IMPORT_PARSE_ERROR, f"第 {i} 张卡片 front/back 不能为空")

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        results = import_cards(
            session,
            device_id=device_id,
            deck_id=deck_id,
            cards=[(c["front"], c["back"]) for c in raw_cards],
            now=_now(),
        )
        response = ImportResponse(results=[ImportResult.model_validate(r) for r in results])
        # exclude_none：CREATED result 的 error 为 None，openapi error 非 nullable，不得输出 null
        return 201, response.model_dump(exclude_none=True)

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
