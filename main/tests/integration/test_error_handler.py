"""统一错误 handler 集成测试（structure-contract 1.4 / 红线 3）。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import AppError, ErrorCode
from app.middleware.error_handler import register_exception_handlers


def test_error_handler_returns_contract_shape() -> None:
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/boom")
    def boom() -> None:
        raise AppError(ErrorCode.DECK_NOT_FOUND, "未找到牌组")

    with TestClient(probe) as client:
        resp = client.get("/boom")
    assert resp.status_code == 404
    assert resp.json() == {
        "error": {
            "code": "DECK_NOT_FOUND",
            "message": "未找到牌组",
            "localization_key": "error.deck_not_found",
        }
    }
