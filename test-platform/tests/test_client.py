"""shanka.client 单元测试:用 stdlib http.server 起本地测试服务。"""
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


class Handler(BaseHTTPRequestHandler):
    def _respond(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Request-ID", "req-test-1")
        if code == 429:
            self.send_header("Retry-After", "1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record_headers(self) -> None:
        REQ_HEADERS[self.path] = {
            "X-Device-ID": self.headers.get("X-Device-ID", ""),
            "Idempotency-Key": self.headers.get("Idempotency-Key", ""),
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
        self._respond(201, {"deck_id": "deck-1"})

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

    def test_get_with_device_and_log(self) -> None:
        c = ShankaClient(f"http://127.0.0.1:{self.port}", pace=0)
        shlogging.set_context(suite="t", scenario="t", device_id=c.device_id)
        r = c.request("GET", "/ok", step="probe")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.request_id, "req-test-1")
        self.assertEqual(REQ_HEADERS["/ok"]["X-Device-ID"], c.device_id)
        self.assertIn("request complete", (Path(self.tmp.name) / "t.log").read_text())

    def test_429_retry_then_success(self) -> None:
        global RETRY_COUNT
        RETRY_COUNT = 0
        c = ShankaClient(f"http://127.0.0.1:{self.port}", pace=0)
        r = c.request("GET", "/flaky", step="flaky")
        self.assertEqual(r.status, 200)  # 重试后成功
        self.assertGreaterEqual(HITS["/flaky"], 2)

    def test_idempotent_headers(self) -> None:
        c = ShankaClient(f"http://127.0.0.1:{self.port}", pace=0)
        r = c.request("POST", "/decks", body={"name": "x"}, idempotent=True, step="deck")
        self.assertEqual(r.status, 201)
        self.assertNotEqual(REQ_HEADERS["/decks"]["Idempotency-Key"], "")

    def test_non_json_response_is_tolerated(self) -> None:
        c = ShankaClient(f"http://127.0.0.1:{self.port}", pace=0)
        r = c.request("GET", "/gateway-error", step="gw")
        self.assertEqual(r.status, 502)  # 非 JSON 响应体不抛异常
        self.assertIsNone(r.json)
        self.assertIn('"level": "WARN"', (Path(self.tmp.name) / "t.log").read_text())


if __name__ == "__main__":
    unittest.main()
