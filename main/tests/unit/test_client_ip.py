"""限流客户端 IP 解析守卫（app/middleware/client_ip.py；CF-Connecting-IP 优先，R25-07 同批）。"""

from starlette.requests import Request

from app.middleware.client_ip import resolve_client_ip


def _request(
    headers: dict[str, str] | None = None, client: tuple[str, int] | None = None
) -> Request:
    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    }
    return Request(scope)


def test_client_ip_cf_connecting_ip_takes_precedence() -> None:
    """Tunnel 部署：cloudflared 回环直连 + CF 注入头 → 限流键用真实客户端 IP。"""
    req = _request({"CF-Connecting-IP": "203.0.113.7"}, client=("127.0.0.1", 50000))
    assert resolve_client_ip(req) == "203.0.113.7"


def test_client_ip_falls_back_to_transport_peer() -> None:
    """本地直连（无 CF 头）：回退 transport 层 client.host（uvicorn proxy headers 已重写）。"""
    req = _request(client=("10.0.0.2", 50000))
    assert resolve_client_ip(req) == "10.0.0.2"


def test_client_ip_blank_cf_header_ignored() -> None:
    req = _request({"CF-Connecting-IP": "   "}, client=("10.0.0.3", 1))
    assert resolve_client_ip(req) == "10.0.0.3"


def test_client_ip_unknown_without_peer() -> None:
    assert resolve_client_ip(_request()) == "unknown"
