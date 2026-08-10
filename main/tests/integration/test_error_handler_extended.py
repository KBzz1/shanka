"""统一错误包装扩展集成测试（structure-contract 1.4；VALIDATION_ERROR/INTERNAL_ERROR）。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.error_handler import register_exception_handlers
from app.middleware.request_id import RequestIDMiddleware


def test_validation_error_returns_400_contract_shape() -> None:
    from pydantic import BaseModel

    class Payload(BaseModel):
        name: str

    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.post("/echo")
    def echo(payload: Payload) -> dict[str, str]:
        return {"name": payload.name}

    with TestClient(probe) as client:
        resp = client.post("/echo", json={"name": 123})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["localization_key"] == "error.validation_error"
    assert body["error"]["message"]


def test_unexpected_exception_returns_500_internal_error() -> None:
    probe = FastAPI()
    register_exception_handlers(probe)
    probe.add_middleware(RequestIDMiddleware)

    @probe.get("/boom")
    def boom() -> None:
        raise RuntimeError("内部细节 secret-internals")

    # raise_server_exceptions=False：未处理异常的 500 由最外层 ServerErrorMiddleware 兜底
    # 直发（按 FastAPI 装配，Exception handler 成为其 handler），之后仍会向上重抛原始异常
    # （便于服务器/测试客户端记录）；此断言覆盖的是兜底 handler 产出的 500 响应
    with TestClient(probe, raise_server_exceptions=False) as client:
        resp = client.get("/boom", headers={"X-Request-ID": "req-abc-123"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["localization_key"] == "error.internal_error"
    # 内部细节不得出现在响应
    assert "secret-internals" not in str(body)
    # final review I-1：500 兜底直发绕过了 RequestIDMiddleware 的响应头写入，
    # handler 必须自行补 X-Request-ID（契约 1.4 以 request_id 关联日志）
    assert resp.headers["X-Request-ID"] == "req-abc-123"
