"""牌组路由（structure-contract 6.5；openapi /decks）。handler 只做 HTTP 映射。

写操作幂等接线：handler 内 execute_idempotent(session, ...) → session.commit()，
幂等记录与业务副作用同事务（get_db_session 只负责创建/关闭，不提交）。
路径无 /v1 前缀：/v1 语义由部署层 openapi servers url 承担（与 probes 同理）。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.schemas.decks import Deck, DeckCreate, DeckUpdateRequest
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from services.decks.service import create_deck, delete_deck, get_deck, list_decks, rename_deck

router = APIRouter(prefix="/decks", tags=["decks"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


@router.get("", response_model=dict[str, list[Deck]])
def list_decks_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    project_id: Annotated[str | None, Query()] = None,
) -> JSONResponse:
    """牌组列表：支持 project_id 归属过滤（openapi listDecks；跨用户 → 404）。"""
    items = list_decks(
        session,
        user_id=request.state.principal.user_id,
        now=_now(),
        project_id=project_id,
    )
    return JSONResponse(content={"items": items})


@router.post("", status_code=201, response_model=Deck)
def create_deck_endpoint(
    request: Request,
    payload: DeckCreate,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = request.url.path
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        deck = create_deck(
            session,
            user_id=user_id,
            name=payload.name,
            now=_now(),
            project_id=payload.project_id,  # V2.5 归属项目（归属校验在 service 内）
        )
        session.flush()
        # 响应走真实进度视图（_to_deck_view 含 project_id），与 GET 详情同形
        return 201, get_deck(session, user_id=user_id, deck_id=deck.deck_id, now=_now())

    _replayed, status, body = execute_idempotent(
        session,
        user_id=user_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.get("/{deck_id}", response_model=Deck)
def get_deck_endpoint(
    request: Request,
    deck_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    data = get_deck(session, user_id=request.state.principal.user_id, deck_id=deck_id, now=_now())
    return JSONResponse(content=data)


@router.patch("/{deck_id}", response_model=Deck)
def rename_deck_endpoint(
    request: Request,
    deck_id: str,
    payload: DeckUpdateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/decks/{deck_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        rename_deck(session, user_id=user_id, deck_id=deck_id, name=payload.name, now=_now())
        # 返回真实进度视图（改名可能发生在有卡片的牌组上）
        return 200, get_deck(session, user_id=user_id, deck_id=deck_id, now=_now())

    _replayed, status, body = execute_idempotent(
        session,
        user_id=user_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.delete("/{deck_id}", status_code=204)
def delete_deck_endpoint(
    request: Request,
    deck_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> Response:
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/decks/{deck_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        delete_deck(session, user_id=user_id, deck_id=deck_id)
        return 204, {}

    _replayed, status, _body = execute_idempotent(
        session,
        user_id=user_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return Response(status_code=status)
