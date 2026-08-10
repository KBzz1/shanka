"""统一错误包装扩展集成测试（structure-contract 1.4；VALIDATION_ERROR/INTERNAL_ERROR）。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.error_handler import register_exception_handlers


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

    @probe.get("/boom")
    def boom() -> None:
        raise RuntimeError("内部细节 secret-internals")

    # raise_server_exceptions=False：Starlette ServerErrorMiddleware 处理后仍会向上重抛原始异常
    # （便于服务器/测试客户端记录），此处断言的是 handler 产出的 500 JSON 响应体
    with TestClient(probe, raise_server_exceptions=False) as client:
        resp = client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["localization_key"] == "error.internal_error"
    # 内部细节不得出现在响应
    assert "secret-internals" not in str(body)
