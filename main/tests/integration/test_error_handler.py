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


def test_auth_errors_carry_www_authenticate() -> None:
    """D-05/契约 1.4：受保护接口 401 一律携带 WWW-Authenticate: Bearer——
    handler 层抛出的 AUTH_REQUIRED/AUTH_INVALID 由 error_handler 统一补头
    （覆盖 me 窄竞态：token 在 resolve 后被撤销时 service 抛 AUTH_INVALID 的 401）。"""
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/invalid")
    def invalid() -> None:
        raise AppError(ErrorCode.AUTH_INVALID, "会话无效、已撤销或已过期")

    @probe.get("/required")
    def required() -> None:
        raise AppError(ErrorCode.AUTH_REQUIRED, "缺少 Bearer 凭证")

    with TestClient(probe) as client:
        for path in ("/invalid", "/required"):
            resp = client.get(path)
            assert resp.status_code == 401
            assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_invalid_credentials_401_without_www_authenticate() -> None:
    """INVALID_CREDENTIALS（login 失败）不带 WWW-Authenticate（DESIGN §4.3）。"""
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/creds")
    def creds() -> None:
        raise AppError(ErrorCode.INVALID_CREDENTIALS, "用户名或密码错误")

    with TestClient(probe) as client:
        resp = client.get("/creds")
    assert resp.status_code == 401
    assert "WWW-Authenticate" not in resp.headers
