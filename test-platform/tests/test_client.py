"""shanka.client 单元测试:用 stdlib http.server 起本地测试服务。

账号化覆盖:set_token 后普通请求带 Bearer 且无 X-Device-ID;未 set_token 不带头;
register/login 不带头/不重试/不落日志;logout 带 Bearer 并清空本地 token;敏感值不进 JSONL。
"""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import logging as shlogging
from shanka.client import ShankaClient

HITS: dict[str, int] = {}
REQ_HEADERS: dict[str, dict[str, str]] = {}  # path -> 请求头快照(供断言)
RETRY_COUNT = 0
FAIL_AUTH = False   # True 时 register/login 返回 429(验证不自动重试)
FAIL_LOGOUT = False  # True 时 logout 返回 401(验证失败也清空 token)

SESSION_BODY = {
    "user": {"user_id": "u-test-1", "username": "tester", "created_at": "2026-08-14T00:00:00Z"},
    "access_token": "tok-secret-abc",
    "token_type": "Bearer",
    "expires_at": "2026-09-13T00:00:00Z",
}


class Handler(BaseHTTPRequestHandler):
    def _respond(self, code: int, body: dict | None, extra_headers: dict | None = None) -> None:
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Request-ID", "req-test-1")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        if code == 429:
            self.send_header("Retry-After", "1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _record_headers(self) -> None:
        REQ_HEADERS[self.path] = {
            "Authorization": self.headers.get("Authorization", ""),
            "Idempotency-Key": self.headers.get("Idempotency-Key", ""),
            "X-Device-ID": self.headers.get("X-Device-ID", ""),
        }

    def do_GET(self) -> None:
        global RETRY_COUNT
        HITS[self.path] = HITS.get(self.path, 0) + 1
        self._record_headers()
        if self.path == "/flaky" and RETRY_COUNT == 0:
            RETRY_COUNT += 1
            self._respond(429, {"error": {"code": "RATE_LIMITED"}})
            return
        if self.path == "/gateway-error":  # 模拟网关 502 HTML 页(非 JSON)
            data = b"<html>502 Bad Gateway</html>"
            self.send_response(502)
            self.send_header("Content-Type", "text/html")
            self.send_header("X-Request-ID", "req-test-1")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._respond(200, {"status": "ok", "path": self.path})

    def do_POST(self) -> None:
        HITS[self.path] = HITS.get(self.path, 0) + 1
        self._record_headers()
        if self.path == "/auth/register":
            if FAIL_AUTH:
                self._respond(429, {"error": {"code": "RATE_LIMITED"}})
                return
            self._respond(201, SESSION_BODY)
            return
        if self.path == "/auth/login":
            if FAIL_AUTH:
                self._respond(429, {"error": {"code": "RATE_LIMITED"}})
                return
            self._respond(200, SESSION_BODY)
            return
        if self.path == "/auth/logout":
            if FAIL_LOGOUT:
                self._respond(401, {"error": {"code": "AUTH_INVALID"}})
                return
            self._respond(204, None)
            return
        if self.path == "/always-500":
            self._respond(500, {"error": {"code": "INTERNAL"}})
            return
        self._respond(201, {"deck_id": "deck-1"})

    def do_PUT(self) -> None:
        HITS[self.path] = HITS.get(self.path, 0) + 1
        self._record_headers()
        self._respond(200, {"status": "ok"})

    def log_message(self, *args):  # 静默访问日志
        pass


class ClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        shlogging.init_logger("run-test", Path(cls.tmp.name) / "t.log")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        global FAIL_AUTH, FAIL_LOGOUT
        FAIL_AUTH = False
        FAIL_LOGOUT = False
        HITS.clear()
        REQ_HEADERS.clear()

    def _client(self) -> ShankaClient:
        # localhost 而非 127.0.0.1:urllib proxy_bypass('localhost')==True(实测),
        # 而 NO_PROXY 的 127.* 不匹配 IP 字面量(proxy_bypass('127.0.0.1')==False),
        # 环境带 HTTP_PROXY 时 127.0.0.1 会间歇走代理 502——localhost 直连稳定
        return ShankaClient(f"http://localhost:{self.port}", pace=0)

    def _log(self) -> str:
        return (Path(self.tmp.name) / "t.log").read_text()

    def test_get_with_token_and_log(self) -> None:
        c = self._client()
        c.set_token("tok-1")
        shlogging.set_context(suite="t", scenario="t", user_id="")
        r = c.request("GET", "/ok", step="probe")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.request_id, "req-test-1")
        self.assertEqual(REQ_HEADERS["/ok"]["Authorization"], "Bearer tok-1")
        self.assertIn("request complete", self._log())

    def test_no_token_no_auth_header(self) -> None:
        c = self._client()
        r = c.request("GET", "/ok", step="probe")
        self.assertEqual(r.status, 200)
        self.assertEqual(REQ_HEADERS["/ok"]["Authorization"], "")  # 未设置不带头
        self.assertEqual(REQ_HEADERS["/ok"]["X-Device-ID"], "")  # 设备头已彻底移除

    def test_set_token_bearer_without_device_header(self) -> None:
        c = self._client()
        c.set_token("tok-secret-abc")
        r = c.request("GET", "/ok", step="probe")
        self.assertEqual(r.status, 200)
        self.assertEqual(REQ_HEADERS["/ok"]["Authorization"], "Bearer tok-secret-abc")
        self.assertEqual(REQ_HEADERS["/ok"]["X-Device-ID"], "")

    def test_register_no_auth_no_log(self) -> None:
        c = self._client()
        n_before = len(self._log().splitlines())
        r = c.register("tester", "pw-secret-123")
        self.assertEqual(r.status, 201)
        self.assertEqual(r.json["access_token"], "tok-secret-abc")
        self.assertEqual(REQ_HEADERS["/auth/register"]["Authorization"], "")  # 不带头
        self.assertEqual(REQ_HEADERS["/auth/register"]["Idempotency-Key"], "")  # 不带幂等键
        log = self._log()
        self.assertEqual(len(log.splitlines()), n_before)  # 敏感路径不落事件
        self.assertNotIn("pw-secret-123", log)
        self.assertNotIn("tok-secret-abc", log)

    def test_login_no_auth_header_no_log(self) -> None:
        c = self._client()
        n_before = len(self._log().splitlines())
        r = c.login("tester", "pw-secret-123")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json["access_token"], "tok-secret-abc")
        self.assertEqual(REQ_HEADERS["/auth/login"]["Authorization"], "")
        log = self._log()
        self.assertEqual(len(log.splitlines()), n_before)
        self.assertNotIn("pw-secret-123", log)
        self.assertNotIn("tok-secret-abc", log)

    def test_login_does_not_hold_token(self) -> None:
        """token 由 set_token 持有;login 成功不自动生效。"""
        c = self._client()
        c.login("tester", "pw-secret-123")
        c.request("GET", "/ok", step="probe")
        self.assertEqual(REQ_HEADERS["/ok"]["Authorization"], "")

    def test_register_login_strip_auth_header_after_set_token(self) -> None:
        """brief 硬性语义:register/login 恒不带头——即使先 set_token 也不发送 Authorization。"""
        c = self._client()
        c.set_token("tok-held")
        r = c.register("tester", "pw-secret-123")
        self.assertEqual(r.status, 201)
        self.assertEqual(REQ_HEADERS["/auth/register"]["Authorization"], "")
        r = c.login("tester", "pw-secret-123")
        self.assertEqual(r.status, 200)
        self.assertEqual(REQ_HEADERS["/auth/login"]["Authorization"], "")
        # 对照:普通请求仍携带已持有的 token(剥离仅作用于凭据路径)
        c.request("GET", "/ok", step="probe")
        self.assertEqual(REQ_HEADERS["/ok"]["Authorization"], "Bearer tok-held")

    def test_register_login_single_attempt_on_429(self) -> None:
        """DESIGN 4.4:register/login 不自动重试,防网络重放静默创建多条会话。"""
        global FAIL_AUTH
        FAIL_AUTH = True
        c = self._client()
        r = c.register("tester", "pw-secret-123")
        self.assertEqual(r.status, 429)
        self.assertEqual(HITS["/auth/register"], 1)
        HITS.clear()
        r = c.login("tester", "pw-secret-123")
        self.assertEqual(r.status, 429)
        self.assertEqual(HITS["/auth/login"], 1)
        # 对照:普通请求仍按 _MAX_RETRY 重试 429
        global RETRY_COUNT
        RETRY_COUNT = 0
        r = c.request("GET", "/flaky", step="flaky")
        self.assertEqual(r.status, 200)
        self.assertGreaterEqual(HITS["/flaky"], 2)

    def test_logout_bearer_and_clears_token(self) -> None:
        c = self._client()
        c.set_token("tok-secret-abc")
        r = c.logout()
        self.assertEqual(r.status, 204)
        self.assertEqual(REQ_HEADERS["/auth/logout"]["Authorization"], "Bearer tok-secret-abc")
        self.assertNotEqual(REQ_HEADERS["/auth/logout"]["Idempotency-Key"], "")  # 写接口幂等键
        c.request("GET", "/ok", step="after-logout")
        self.assertEqual(REQ_HEADERS["/ok"]["Authorization"], "")  # logout 后清空本地 token

    def test_logout_clears_token_even_on_failure(self) -> None:
        global FAIL_LOGOUT
        FAIL_LOGOUT = True
        c = self._client()
        c.set_token("tok-secret-abc")
        r = c.logout()
        self.assertEqual(r.status, 401)
        c.request("GET", "/ok", step="after-logout")
        self.assertEqual(REQ_HEADERS["/ok"]["Authorization"], "")

    def test_429_retry_then_success(self) -> None:
        global RETRY_COUNT
        RETRY_COUNT = 0
        c = self._client()
        r = c.request("GET", "/flaky", step="flaky")
        self.assertEqual(r.status, 200)  # 重试后成功
        self.assertGreaterEqual(HITS["/flaky"], 2)

    def test_idempotent_headers(self) -> None:
        c = self._client()
        r = c.request("POST", "/decks", body={"name": "x"}, idempotent=True, step="deck")
        self.assertEqual(r.status, 201)
        self.assertNotEqual(REQ_HEADERS["/decks"]["Idempotency-Key"], "")

    def test_idempotency_key_explicit_reuse(self) -> None:
        """idempotency_key 显式指定时复用该键(跨用户幂等复用场景),不生成新键。"""
        c = self._client()
        r = c.request("POST", "/decks", body={"name": "x"}, idempotent=True,
                      idempotency_key="key-shared-1", step="deck")
        self.assertEqual(r.status, 201)
        self.assertEqual(REQ_HEADERS["/decks"]["Idempotency-Key"], "key-shared-1")

    def test_api_key_put_not_logged(self) -> None:
        """红线 4:非 api-key 路径落事件,PUT /api-key 不落(凭据脱敏)。"""
        c = self._client()
        n_before = len(self._log().splitlines())
        r = c.request("POST", "/decks", body={"name": "x"}, step="deck")
        self.assertEqual(r.status, 201)
        n_post = len(self._log().splitlines())
        self.assertGreater(n_post, n_before)  # 非 api-key 路径有请求事件
        r = c.request("PUT", "/api-key", body={"api_key": "sk-test-secret"}, step="api-key")
        self.assertEqual(r.status, 200)
        log = self._log()
        self.assertEqual(len(log.splitlines()), n_post)  # PUT /api-key 不新增事件
        self.assertNotIn("sk-test-secret", log)  # 明文永不落日志

    def test_non_json_response_is_tolerated(self) -> None:
        c = self._client()
        r = c.request("GET", "/gateway-error", step="gw")
        self.assertEqual(r.status, 502)  # 非 JSON 响应体不抛异常
        self.assertIsNone(r.json)
        self.assertIn('"level": "WARN"', self._log())


if __name__ == "__main__":
    unittest.main()
