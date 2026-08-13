"""卡片路由（structure-contract 6.5；openapi /decks/{deck_id}/cards + /cards/{card_id}/rewrite）。

写操作幂等接线：handler 内 execute_idempotent(session, ...) → session.commit()，
幂等记录与业务副作用同事务。import 响应统一用 ImportResponse 模型构造
（openapi 的 import 请求体是内联 schema，无命名组件 → dict 解析 + 手动校验）；
rewrite 请求体同为内联 object（custom_requirements 可空，非 str → 手动 VALIDATION_ERROR）。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.schemas.cards import Card, CardCreate, CardUpdateRequest, ImportResponse, ImportResult
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from services.cards.rewrite import rewrite_card
from services.cards.service import (
    card_view,
    create_card,
    delete_card,
    import_cards,
    list_cards,
    update_card,
)

router = APIRouter(prefix="/decks/{deck_id}/cards", tags=["decks"])
router_rewrite = APIRouter(prefix="/cards", tags=["decks"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


@router.get("", response_model=dict[str, list[Card]])
def list_cards_endpoint(
    request: Request,
    deck_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    cards = list_cards(session, device_id=request.state.device_id, deck_id=deck_id)
    return JSONResponse(content={"items": [card_view(c) for c in cards]})


@router.post("", status_code=201, response_model=Card)
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


@router.post("/import", status_code=201, response_model=ImportResponse)
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


@router_rewrite.patch("/{card_id}", response_model=Card)
def update_card_endpoint(
    request: Request,
    card_id: str,
    payload: CardUpdateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """编辑卡片（structure-contract 6.5）：内容覆盖 + ReviewState 重置为新卡。"""
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = f"/cards/{card_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        card = update_card(
            session,
            device_id=device_id,
            card_id=card_id,
            front=payload.front,
            back=payload.back,
            now=_now(),
        )
        return 200, card_view(card)

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


@router_rewrite.delete("/{card_id}", status_code=204)
def delete_card_endpoint(
    request: Request,
    card_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> Response:
    """删除单卡（structure-contract 6.5）；级联 review_states/review_events。"""
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = f"/cards/{card_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        delete_card(session, device_id=device_id, card_id=card_id)
        return 204, {}

    _replayed, status, _body = execute_idempotent(
        session,
        device_id=device_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return Response(status_code=status)


@router_rewrite.post("/{card_id}/rewrite", response_model=Card)
def rewrite_card_endpoint(
    request: Request,
    card_id: str,
    session: Annotated[Session, Depends(get_db_session)],
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> JSONResponse:
    """单卡重写（structure-contract 6.7；openapi /cards/{card_id}/rewrite，V6）。

    幂等接线同 create_card（V1 模式）：execute_idempotent + session.commit() 同事务；
    错误路径（404/422/502）AppError 上抛由错误 handler 处理，session 依赖关闭回滚——
    非 2xx 不落幂等记录（execute_idempotent 契约 1.3/2.12），不 commit。
    client_factory 从 app.state 注入（getattr 缺省 None → 生产构造真实 client；测试注入 mock）。
    """
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = f"/cards/{card_id}/rewrite"  # 与 openapi 路径一致，无 /v1 前缀（现有路由惯例）
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    payload = body or {}  # openapi 内联 object：custom_requirements 可空
    custom_requirements = payload.get("custom_requirements")
    if custom_requirements is not None and not isinstance(custom_requirements, str):
        # V1 import 同款手动校验（内联 object 无命名组件 → 非 str 手动 VALIDATION_ERROR）
        raise AppError(ErrorCode.VALIDATION_ERROR, "custom_requirements 必须为字符串")

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        card = rewrite_card(
            session,
            device_id=device_id,
            card_id=card_id,
            custom_requirements=custom_requirements,
            idempotency_key=key,
            now=_now(),
            settings=request.app.state.settings,
            client_factory=getattr(request.app.state, "client_factory", None),
        )
        return 200, card_view(card)

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
