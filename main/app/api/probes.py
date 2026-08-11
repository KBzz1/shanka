"""运行观测探针（structure-contract 8.2）：/healthz 存活、/readyz 就绪（DB + 存储），豁免 X-Device-ID 鉴权。"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["observability"])
logger = logging.getLogger(__name__)


@router.get("/healthz", status_code=200, response_model=dict[str, str])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", response_model=dict[str, str])
def readyz(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}
    db_ok = True
    try:
        with request.app.state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001  # readyz 必须兜底，DB 不可用时返回 503 而非崩溃
        db_ok = False
        logger.warning("readyz database check failed: %s", exc)
    checks["database"] = "ok" if db_ok else "error"
    storage_ok = request.app.state.storage.check_writable()
    checks["storage"] = "ok" if storage_ok else "error"
    if not (db_ok and storage_ok):
        return JSONResponse(status_code=503, content={"status": "unavailable", "checks": checks})
    return JSONResponse(content={"status": "ready", "checks": checks})
