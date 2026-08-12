# 测试平台第一期实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 `test-platform/` 自动化测试平台第一期——零依赖核心库、两个场景(api_smoke 迁移、live_flow 新建)、runner 调度(成本/环境闸门)、device 真机脚本、文档与登记。

**Architecture:** 顶层独立目录 `test-platform/`,纯 Python stdlib 零依赖;`shanka/` 核心库(HTTP 抽象/成本护栏/数据策略/环境/报告/JSON 日志),场景按域分组一文件一用户故事,runner 负责套件与闸门,device 管前端构建安装。

**Tech Stack:** Python 3 stdlib(urllib/http.server/unittest),Bash,无第三方依赖。

## Global Constraints

- 零依赖:全部 Python 用 stdlib,`python3` 直接运行,不创建 pyproject/venv,不用 main 的 conda 环境。
- 凭据红线:API Key 仅从 `/home/kbzz1/shanka_backend/.env` 的 `DEEPSEEK_API_KEY` 读取,不出现在命令行/日志/代码字面量;`PUT /api-key` 请求与响应永不落日志。
- 请求纪律:默认 0.3s/请求(IP 限流 5 req/s),429 按 Retry-After 重试(最多 3 次),避免平台自身触发限流。
- 成本闸门:场景声明 `LLM_CALLS`;runner 聚合,**> 3 次必须 `--confirm-cost`**。
- 环境闸门:prod 环境默认拒绝,必须 `--confirm-prod`。
- 日志:JSON Lines 输出到 `test-platform/logs/test-platform.log`(追加);必选字段 timestamp/level/run_id/message;请求事件含 suite/scenario/step/request_id/device_id/method/path/status/duration_ms/error_code;`request_id` 取自后端 X-Request-ID 响应头。
- 数据策略:默认随机设备 ID;场景创建的牌组结束自动清理;`--device-id` 固定可保留。
- 退出码 = 失败步骤数(0 = 全绿),继承 smoke_api 约定。
- 迁移:删 `main/scripts/smoke_api.py`;保留 `main/scripts/live_estimate_smoke.py`(R1 历史资产,不动)。
- 领域语言:中文注释与文档;代码标识符英文。
- 测试平台代码不进入 main/pyproject 的 mypy/ruff 范围。

---

### Task 1: shanka 基础层(environments / report / logging)

**Files:**
- Create: `test-platform/shanka/__init__.py`
- Create: `test-platform/shanka/environments.py`
- Create: `test-platform/shanka/report.py`
- Create: `test-platform/shanka/logging.py`
- Create: `test-platform/tests/__init__.py`
- Create: `test-platform/tests/test_logging.py`
- Create: `test-platform/tests/test_environments.py`

**Interfaces:**
- Produces (供后续任务使用):
  - `shanka.environments.ENVIRONMENTS: dict[str, str]` = `{"local": "http://localhost:8000", "prod": "https://shanka.kbzz1.top"}`
  - `shanka.environments.resolve(name: str) -> str`(未知名称抛 `ValueError`)
  - `shanka.environments.is_prod(name: str) -> bool`
  - `shanka.report.check(name: str, cond: bool, detail: str = "") -> None`(PASS/FAIL 打印 + 记 STEPS)
  - `shanka.report.summary() -> int`(返回失败数,打印 "N/M 通过")
  - `shanka.logging.init_logger(run_id: str, log_path: Path | None = None, console: bool = False) -> None`(全局初始化一次)
  - `shanka.logging.set_context(*, suite: str, scenario: str, device_id: str) -> None`
  - `shanka.logging.event(level: str, message: str, **fields) -> None`(JSON 行写入;字段值非 JSON 可序列化时自动 `str()`)

- [ ] **Step 1: 写失败测试**

`test-platform/tests/test_logging.py`:

```python
"""shanka.logging 的单元测试(stdlib unittest,零依赖)。"""
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import logging as shlogging


class LoggingTest(unittest.TestCase):
    def test_json_line_fields(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.log"
            shlogging.init_logger("run-123", path)
            shlogging.set_context(suite="quick", scenario="api_smoke", device_id="dev-1")
            shlogging.event("INFO", "请求完成", method="GET", path="/decks", status=200,
                            request_id="req-1", duration_ms=12, error_code="")
            line = json.loads(path.read_text().strip())
            self.assertEqual(line["run_id"], "run-123")
            self.assertEqual(line["level"], "INFO")
            self.assertEqual(line["message"], "请求完成")
            self.assertEqual(line["suite"], "quick")
            self.assertEqual(line["scenario"], "api_smoke")
            self.assertEqual(line["device_id"], "dev-1")
            self.assertEqual(line["request_id"], "req-1")
            self.assertEqual(line["status"], 200)
            self.assertIn("timestamp", line)

    def test_append_mode(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.log"
            shlogging.init_logger("run-1", path)
            shlogging.event("INFO", "a")
            shlogging.init_logger("run-2", path)  # 第二次初始化复用同一文件
            shlogging.event("INFO", "b")
            lines = path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[1])["run_id"], "run-2")


if __name__ == "__main__":
    unittest.main()
```

`test-platform/tests/test_environments.py`:

```python
"""shanka.environments 单元测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka.environments import ENVIRONMENTS, is_prod, resolve


class EnvironmentsTest(unittest.TestCase):
    def test_local_and_prod(self) -> None:
        self.assertEqual(resolve("local"), "http://localhost:8000")
        self.assertEqual(resolve("prod"), "https://shanka.kbzz1.top")

    def test_unknown_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve("nonexistent")

    def test_is_prod(self) -> None:
        self.assertFalse(is_prod("local"))
        self.assertTrue(is_prod("prod"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd test-platform && python3 -m unittest tests.test_logging tests.test_environments -v`
Expected: `ModuleNotFoundError: No module named 'shanka'`

- [ ] **Step 3: 实现基础层**

`test-platform/shanka/__init__.py`:

```python
"""测试平台核心库(纯 stdlib,零依赖)。"""
```

`test-platform/shanka/environments.py`:

```python
"""目标环境配置:local(本机)/ prod(生产隧道)。"""

ENVIRONMENTS: dict[str, str] = {
    "local": "http://localhost:8000",
    "prod": "https://shanka.kbzz1.top",
}


def resolve(name: str) -> str:
    if name not in ENVIRONMENTS:
        raise ValueError(f"未知环境: {name},可选 {list(ENVIRONMENTS)}")
    return ENVIRONMENTS[name]


def is_prod(name: str) -> bool:
    return name == "prod"
```

`test-platform/shanka/report.py`:

```python
"""统一报告:PASS/FAIL 步骤记录 + 退出码(失败步骤数)。"""

STEPS: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    STEPS.append((mark, name))
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))


def summary() -> int:
    failed = sum(1 for mark, _ in STEPS if mark == "FAIL")
    print(f"\n{len(STEPS) - failed}/{len(STEPS)} 通过, {failed} 失败")
    return failed
```

`test-platform/shanka/logging.py`:

```python
"""JSON Lines 事件日志:run_id 上下文/字段规范/脱敏(对齐后端 app.log 风格)。

事件字段:必选 timestamp/level/run_id/message;请求事件另含
suite/scenario/step/request_id/device_id/method/path/status/duration_ms/error_code。
脱敏纪律:调用方不得把 API Key 明文、设备 ID 混入 message 或附加字段。
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

_lock = threading.Lock()
_run_id = ""
_suite = ""
_scenario = ""
_device_id = ""
_file: TextIO | None = None
_console = False


def init_logger(run_id: str, log_path: Path | None = None, console: bool = False) -> None:
    """全局初始化一次;log_path 为 None 时仅 console。追加式,不截断;目录自动创建。"""
    global _run_id, _file, _console
    with _lock:
        _run_id = run_id
        _console = console
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _file = open(log_path, "a", encoding="utf-8")


def set_context(*, suite: str, scenario: str, device_id: str) -> None:
    global _suite, _scenario, _device_id
    with _lock:
        _suite, _scenario, _device_id = suite, scenario, device_id


def event(level: str, message: str, **fields: Any) -> None:
    row: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "level": level,
        "run_id": _run_id,
        "message": message,
    }
    if _suite:
        row["suite"] = _suite
    if _scenario:
        row["scenario"] = _scenario
    if _device_id:
        row["device_id"] = _device_id
    for k, v in fields.items():
        row[k] = v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
    text = json.dumps(row, ensure_ascii=False)
    with _lock:
        if _file is not None:
            _file.write(text + "\n")
            _file.flush()
        if _console:
            print(text)
```

`test-platform/tests/__init__.py`:空文件。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd test-platform && python3 -m unittest tests.test_logging tests.test_environments -v`
Expected: 两个测试类全 PASS

- [ ] **Step 5: 提交**

```bash
git add test-platform/
git commit -m "feat(test-platform): shanka 基础层——environments/report/logging(JSON Lines,零依赖)+ unittest"
```

---

### Task 2: shanka 客户端层(client / cost / cleanup)

**Files:**
- Create: `test-platform/shanka/client.py`
- Create: `test-platform/shanka/cost.py`
- Create: `test-platform/shanka/cleanup.py`
- Create: `test-platform/tests/test_client.py`
- Create: `test-platform/tests/test_cost.py`

**Interfaces:**
- Consumes: Task 1 的 `shanka.logging`(`event`/`set_context`)、`shanka.report`
- Produces:
  - `shanka.client.Response` dataclass:`status: int`、`json: Any`(解析失败为 None)、`headers: dict[str,str]`(小写键)、`request_id: str | None`(X-Request-ID)、`duration_ms: int`
  - `shanka.client.ShankaClient(base_url: str, *, device_id: str | None = None, pace: float = 0.3, timeout: float = 30.0)`:
    - `.request(method: str, path: str, *, body: dict | None = None, idempotent: bool = False, step: str = "") -> Response`
    - `.device_id: str`(随机生成或固定注入;随机 UUID v4)
    - 行为:每次请求前 `time.sleep(pace)`;写操作(idempotent=True)自动生成 `Idempotency-Key` 头;429 时按 Retry-After 等待重试(最多 3 次);`PUT /api-key` 路径**不记录请求事件**(脱敏);其余请求自动经 logging 记录请求事件(request_id 取响应头,失败时记录 error_code)
  - `shanka.cost.THRESHOLD: int` = 3
  - `shanka.cost.aggregate(scenarios: list[Any]) -> int`(求和各场景 `LLM_CALLS` 模块常量)
  - `shanka.cost.requires_confirm(total: int) -> bool`(返回 `total > THRESHOLD`)
  - `shanka.cleanup.DataScope(client: ShankaClient)`:
    - `.create_deck(name: str) -> str`(创建牌组并登记,返回 deck_id)
    - `.cleanup() -> None`(删除全部登记牌组;失败 WARN 不阻断)

- [ ] **Step 1: 写失败测试**

`test-platform/tests/test_cost.py`:

```python
"""shanka.cost 单元测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import cost


class FakeScenario:  # 模拟场景模块:声明 LLM_CALLS
    LLM_CALLS = 2


class CostTest(unittest.TestCase):
    def test_aggregate_sums(self) -> None:
        self.assertEqual(cost.aggregate([FakeScenario, FakeScenario]), 4)

    def test_threshold(self) -> None:
        self.assertFalse(cost.requires_confirm(3))   # 阈值不含
        self.assertTrue(cost.requires_confirm(4))    # 超过需确认


if __name__ == "__main__":
    unittest.main()
```

`test-platform/tests/test_client.py`:

```python
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

    def do_GET(self) -> None:
        global RETRY_COUNT
        HITS[self.path] = HITS.get(self.path, 0) + 1
        if self.path == "/flaky" and RETRY_COUNT == 0:
            RETRY_COUNT += 1
            self._respond(429, {"error": {"code": "RATE_LIMITED"}})
            return
        self._respond(200, {"status": "ok", "path": self.path})

    def do_POST(self) -> None:
        HITS[self.path] = HITS.get(self.path, 0) + 1
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
        self.assertIn("request complete", Path(self.tmp.name).read_text())

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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd test-platform && python3 -m unittest tests.test_client tests.test_cost -v`
Expected: `ModuleNotFoundError: No module named 'shanka.client'`

- [ ] **Step 3: 实现客户端层**

`test-platform/shanka/client.py`:

```python
"""HTTP 抽象:设备头/幂等键/429 重试/请求节奏/脱敏日志/超时。

每次请求后自动经 shanka.logging 记录请求事件(request_id 取后端 X-Request-ID);
PUT /api-key 路径不记录事件(凭据脱敏,红线 4)。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from shanka import logging as shlogging

_PACE_DEFAULT = 0.3  # 契约 1.6:IP 5 req/s,节奏化避免平台自身触发限流
_MAX_RETRY = 3


@dataclass
class Response:
    status: int
    json: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    request_id: str | None = None
    duration_ms: int = 0


class ShankaClient:
    def __init__(
        self,
        base_url: str,
        *,
        device_id: str | None = None,
        pace: float = _PACE_DEFAULT,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id or str(uuid.uuid4())
        self.pace = pace
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idempotent: bool = False,
        step: str = "",
    ) -> Response:
        started = time.monotonic()
        headers = {
            "X-Device-ID": self.device_id,
            "Content-Type": "application/json",
        }
        if idempotent:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        data = json.dumps(body).encode() if body is not None else None

        status, payload, resp_headers = 0, None, {}
        for attempt in range(_MAX_RETRY + 1):
            time.sleep(self.pace)
            req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode()
                    status = resp.status
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    payload = json.loads(raw) if raw else None
                    break
            except urllib.error.HTTPError as e:
                body_raw = e.read().decode()
                status = e.code
                resp_headers = {k.lower(): v for k, v in e.headers.items()}
                payload = json.loads(body_raw) if body_raw else None
                if e.code == 429 and attempt < _MAX_RETRY:
                    wait = int(e.headers.get("Retry-After", "2")) + 1
                    time.sleep(wait)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < _MAX_RETRY:
                    time.sleep(2)
                    continue
                break

        duration_ms = int((time.monotonic() - started) * 1000)
        response = Response(
            status=status,
            json=payload,
            headers=resp_headers,
            request_id=resp_headers.get("x-request-id"),
            duration_ms=duration_ms,
        )
        # 脱敏:PUT /api-key 不记录请求事件(请求体与响应含凭据相关信息)
        if not (method == "PUT" and path == "/api-key"):
            err_code = ""
            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict):
                    err_code = str(err.get("code", ""))
            shlogging.event(
                "WARN" if status >= 400 else "INFO",
                "request complete",
                step=step,
                method=method,
                path=path,
                status=status,
                duration_ms=duration_ms,
                request_id=response.request_id or "",
                error_code=err_code,
            )
        return response
```

`test-platform/shanka/cost.py`:

```python
"""成本护栏:真实 DeepSeek 调用(LLM_CALLS)聚合与阈值闸门。

计数口径:PUT /api-key 校验、POST /samples、POST /tasks 均计 1 次。
"""

from typing import Any

THRESHOLD = 3  # 超过此数(> 3)必须 --confirm-cost


def aggregate(scenarios: list[Any]) -> int:
    return sum(int(getattr(s, "LLM_CALLS", 0)) for s in scenarios)


def requires_confirm(total: int) -> bool:
    return total > THRESHOLD
```

`test-platform/shanka/cleanup.py`:

```python
"""数据策略:场景创建的资源登记与结束清理(默认随机设备,清理不留残留)。"""

from __future__ import annotations

from shanka import logging as shlogging
from shanka.client import ShankaClient


class DataScope:
    def __init__(self, client: ShankaClient) -> None:
        self._client = client
        self._decks: list[str] = []

    def create_deck(self, name: str) -> str:
        r = self._client.request("POST", "/decks", body={"name": name}, idempotent=True, step="deck-create")
        if r.status not in (200, 201):
            raise RuntimeError(f"创建牌组失败: {r.status} {r.json}")
        deck_id = str(r.json["deck_id"])
        self._decks.append(deck_id)
        return deck_id

    def cleanup(self) -> None:
        for deck_id in self._decks:
            r = self._client.request("DELETE", f"/decks/{deck_id}", idempotent=True, step="deck-cleanup")
            if r.status not in (200, 204):
                shlogging.event("WARN", "清理牌组失败", deck_id=deck_id, status=r.status)
        self._decks.clear()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd test-platform && python3 -m unittest tests.test_client tests.test_cost -v`
Expected: 全 PASS(429 重试、设备头、幂等头、日志写入)

- [ ] **Step 5: 提交**

```bash
git add test-platform/
git commit -m "feat(test-platform): shanka 客户端层——client(429重试/脱敏/自动事件)、cost 成本闸门、cleanup 数据策略"
```

---

### Task 3: 迁移 baseline/api_smoke

**Files:**
- Create: `test-platform/scenarios/__init__.py`
- Create: `test-platform/scenarios/baseline/__init__.py`
- Create: `test-platform/scenarios/baseline/api_smoke.py`
- Delete: `main/scripts/smoke_api.py`

**Interfaces:**
- Consumes: `shanka.client.ShankaClient`、`shanka.report`(check/summary)、`shanka.logging`
- Produces: 场景模块约定(供 runner 使用):
  - 模块常量 `NAME: str` = `"api_smoke"`、`SUITE: str` = `"baseline"`、`LLM_CALLS: int` = `0`
  - `def main(argv: list[str] | None = None) -> int`(返回失败步骤数;argv 可空,argparse 解析)

- [ ] **Step 1: 迁移并改写(去 httpx,用 ShankaClient)**

将 `main/scripts/smoke_api.py` 的 8 组检查完整平移,替换 httpx 为 `ShankaClient.request`;保留 `--base-url` / `--device-id` / `--pace` 参数,新增 `--openapi-local`(可选本地 openapi 文件路径;默认运行时 `/openapi.json`);每组检查前给 `step=` 语义名(如 `"healthz"`、`"auth-401"`、`"deck-list"`)。注意:限流检查组(第 8 组)需绕过 client 的节奏——该组保留独立 `urllib.request` 直连连发 6 次(不 sleep),断言至少 1 个 429 且带 Retry-After。要点代码(主函数骨架):

```python
"""API 连通性冒烟场景(运行中服务的 HTTP 实链路验证)。

覆盖:探针、鉴权(X-Device-ID 必须)、牌组列表/创建/详情、幂等重放(C-04)、
错误响应结构、openapi 契约、metrics。不含真实密钥(生成链路见 flow/live_flow)。
运行方式(由 runner 调度或直接):
    python3 scenarios/baseline/api_smoke.py --base-url http://localhost:8000
退出码 = 失败步骤数(0 = 全部通过)。
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request

from shanka.client import ShankaClient
from shanka.report import check, summary

NAME = "api_smoke"
SUITE = "baseline"
LLM_CALLS = 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--device-id", default=None, help="固定 X-Device-ID(默认随机)")
    ap.add_argument("--pace", type=float, default=0.3, help="请求间隔秒(契约 IP 5 req/s)")
    ap.add_argument("--openapi-local", default=None, help="本地 openapi 文件路径(默认运行时 /openapi.json)")
    args = ap.parse_args(argv)

    c = ShankaClient(args.base_url, device_id=args.device_id, pace=args.pace)

    # 1. 探针(豁免鉴权)
    r = c.request("GET", "/healthz", step="healthz")
    check("GET /healthz -> 200", r.status == 200, f"({r.status})")
    check("healthz 响应体", r.json == {"status": "ok"}, str(r.json)[:80])
    r = c.request("GET", "/readyz", step="readyz")
    check("GET /readyz -> 200", r.status == 200, f"({r.status})")

    # 2. 鉴权:无 X-Device-ID 必须 401(不经 client 的设备头,直连)
    try:
        with urllib.request.urlopen(c.base_url + "/decks", timeout=15) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
        e.close()
    check("GET /decks 无设备头 -> 401", status == 401, f"({status})")

    # 3. 业务链路:列表 / 创建 / 详情
    r = c.request("GET", "/decks", step="deck-list")
    check("GET /decks -> 200", r.status == 200, f"({r.status})")
    items = r.json.get("items") if isinstance(r.json, dict) else None
    check("decks 响应含 items 数组", isinstance(items, list))

    deck_name = f"smoke-{time.time_ns() % 10**8}"
    r = c.request("POST", "/decks", body={"name": deck_name}, idempotent=True, step="deck-create")
    check("POST /decks -> 201", r.status == 201, f"({r.status})")
    deck_id = r.json.get("deck_id") if r.status == 201 else None
    check("创建返回 deck_id", isinstance(deck_id, str))
    check("创建返回 name", r.json.get("name") == deck_name)

    # 4. 幂等重放(C-04):同幂等键重放 -> 原结果不 409
    #    (client 每次自动新键会掩盖同键语义,此处手工构造同键两次 POST)
    import uuid as _uuid
    key = str(_uuid.uuid4())
    def _post_with_key(path: str, body: dict, idem_key: str):
        import json as _json
        import urllib.request as _ur
        req = _ur.Request(
            c.base_url + path,
            data=_json.dumps(body).encode(),
            headers={"X-Device-ID": c.device_id, "Content-Type": "application/json",
                     "Idempotency-Key": idem_key},
            method="POST",
        )
        try:
            with _ur.urlopen(req, timeout=15) as resp:
                return resp.status, _json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read().decode() or "{}")

    time.sleep(c.pace)
    st1, body1 = _post_with_key("/decks", {"name": deck_name}, key)
    time.sleep(c.pace)
    st2, body2 = _post_with_key("/decks", {"name": deck_name}, key)
    check("幂等重放非 409", st2 in (200, 201), f"({st2})")
    check("幂等重放同 deck_id", body2.get("deck_id") == body1.get("deck_id"))

    # 5. 详情核对
    if deck_id:
        r = c.request("GET", f"/decks/{deck_id}", step="deck-detail")
        check("GET /decks/{id} -> 200", r.status == 200, f"({r.status})")
        check("详情 deck_id 一致", r.json.get("deck_id") == deck_id)

    # 6. 错误结构:非法 body -> 400 VALIDATION_ERROR
    r = c.request("POST", "/decks", body={}, idempotent=True, step="deck-invalid")
    body = r.json or {}
    check("非法 body -> 400", r.status == 400, f"({r.status})")
    err = body.get("error")
    check("错误结构含 error.code/localization_key",
          isinstance(err, dict) and "code" in err and "localization_key" in err,
          str(body)[:100])

    # 7. 机器契约与观测
    if args.openapi_local:
        import json as _json
        with open(args.openapi_local, encoding="utf-8") as f:
            spec = _json.load(f)
        check("本地 openapi 含 decks 路径", "/decks" in spec.get("paths", {}))
    else:
        r = c.request("GET", "/openapi.json", step="openapi")
        check("GET /openapi.json -> 200", r.status == 200, f"({r.status})")
        paths = r.json.get("paths", {}) if isinstance(r.json, dict) else {}
        check("openapi 含 decks 路径", "/decks" in paths)
    r = c.request("GET", "/metrics", step="metrics")
    check("GET /metrics -> 200", r.status == 200, f"({r.status})")

    # 8. 限流真实生效:连发 6 个(不 sleep)-> 至少 1 个 429 + Retry-After
    codes: list[int] = []
    retry_after: list[str | None] = []
    for _ in range(6):
        try:
            with urllib.request.urlopen(c.base_url + "/openapi.json", timeout=15) as resp:
                codes.append(resp.status)
                retry_after.append(resp.headers.get("Retry-After"))
        except urllib.error.HTTPError as e:
            codes.append(e.code)
            retry_after.append(e.headers.get("Retry-After"))
    check("快速连发触发 429", codes.count(429) >= 1, f"codes={codes}")
    check("429 带 Retry-After 头", any(v is not None for v in retry_after), f"{retry_after}")
    time.sleep(1.2)  # 越过 IP 限流 1s 窗口
    r = c.request("GET", "/healthz", step="healthz-after")
    check("窗口过后恢复 200", r.status == 200, f"({r.status})")

    return summary()


if __name__ == "__main__":
    sys.exit(main())
```

注:第 4 步"幂等重放"用同键手工 POST(客户端的自动幂等键会掩盖同键重放语义,故直连)。第 2/8 步同理直连(绕过自动头/节奏)。

- [ ] **Step 2: 删除旧脚本并更新引用**

```bash
git rm main/scripts/smoke_api.py
```

- [ ] **Step 3: 冒烟验证(后端运行中)**

Run: `cd test-platform && python3 scenarios/baseline/api_smoke.py --base-url http://localhost:8000`
Expected: 全部 PASS,退出码 0

- [ ] **Step 4: 提交**

```bash
git add test-platform/scenarios main/scripts
git commit -m "feat(test-platform): baseline/api_smoke 迁移——httpx 改 ShankaClient,幂等重放/限流组直连,openapi 来源参数化"
```

---

### Task 4: 新建 flow/live_flow

**Files:**
- Create: `test-platform/scenarios/flow/__init__.py`
- Create: `test-platform/scenarios/flow/live_flow.py`

**Interfaces:**
- Consumes: `ShankaClient`、`shanka.logging`、`shanka.report`、`shanka.cleanup.DataScope`;API Key 从 `/home/kbzz1/shanka_backend/.env` 的 `DEEPSEEK_API_KEY` 读取
- Produces: 场景模块约定:`NAME = "live_flow"`、`SUITE = "flow"`、`LLM_CALLS = 3`(api-key 校验 1 + samples 1 + tasks 1)、`def main(argv=None) -> int`

- [ ] **Step 1: 写场景脚本(基于 /tmp/flow_smoke.py 正式化)**

`test-platform/scenarios/flow/live_flow.py` 要点(完整代码,由 /tmp/flow_smoke.py 平移改造):

- 参数:`--base-url`(默认 localhost:8000)、`--device-id`(默认随机)、`--skip-generate`(跳过 POST /tasks 与评级,到样卡为止)、`--keep`(不清理创建的牌组)
- 步骤:
  1. 从 `.env` 读 `DEEPSEEK_API_KEY`(正则解析;缺失则报错退出);`PUT /api-key`(idempotent)→ `GET /api-key/status` 断言 `AVAILABLE`
  - 客户端实例:`ShankaClient(base_url, device_id=..., timeout=60)`(任务生成期事件循环阻塞时轮询请求需长超时,默认 30s 不足)
  2. `GET /pdfs` 选第一个 `PARSED` 的 PDF;无则报错"请先准备已解析 PDF";`GET /pdfs/{id}` 取章节列表,断言非空
  3. 选前 2 章 → `POST /samples`(不幂等)断言 200 且响应数组非空
  4. `--skip-generate` 时到此结束(skip 提示后返回 0);否则:`DataScope.create_deck("联调测试牌组")` → `POST /tasks`(idempotent)断言 201 → 轮询 `GET /tasks/{id}` 至终态(间隔 5s,单次超时 60s,上限 10 分钟,`run_id` 由 runner 注入)
  5. `GET /decks/{deck_id}/cards` 断言非空 → `POST /review-events`(评级 GOOD,client_event_id 新 UUID,device_timezone=Asia/Shanghai)断言 200
  6. `GET /stats/dashboard?timezone=Asia/Shanghai` 断言 200
  7. `--keep` 未设置时 `DataScope.cleanup()`
- 全程经 `shanka.report.check` 记录步骤;`LLM_CALLS = 3` 模块常量
- 429 重试/节奏由 ShankaClient 内置,轮询期间单次请求允许 60s 超时(client timeout 参数)

- [ ] **Step 2: 静态核对**

Run: `cd test-platform && python3 -c "import sys; sys.path.insert(0,'.'); import scenarios.flow.live_flow as m; print(m.NAME, m.LLM_CALLS)"`
Expected: 输出 `live_flow 3`(导入无错)

- [ ] **Step 3: 提交**

```bash
git add test-platform/scenarios/flow
git commit -m "feat(test-platform): flow/live_flow——完整制卡流程场景(Key/PDF/样卡/任务/复习/看板),LLM_CALLS=3"
```

---

### Task 5: runner 调度(suites.py + run.sh)

**Files:**
- Create: `test-platform/runner/__init__.py`
- Create: `test-platform/runner/suites.py`
- Create: `test-platform/runner/run.sh`
- Create: `test-platform/tests/test_suites.py`

**Interfaces:**
- Consumes: Task 1-4 的所有模块与场景(`environments.resolve/is_prod`、`cost.aggregate/requires_confirm`、`logging.init_logger/set_context`、`baseline.api_smoke`、`flow.live_flow`)
- Produces: CLI 入口 `python3 runner/suites.py --environment local|prod --suite quick|full|live [--scenario NAME] [--confirm-cost] [--confirm-prod]`;`run.sh` 为薄壳转发

- [ ] **Step 1: 写失败测试**

`test-platform/tests/test_suites.py`:

```python
"""runner.suites 单元测试:套件构成与闸门逻辑。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import suites


class SuitesTest(unittest.TestCase):
    def test_suite_membership(self) -> None:
        names = {s.NAME: s for s in suites.SUITES["quick"]}
        self.assertIn("api_smoke", names)
        self.assertNotIn("live_flow", names)
        live_names = {s.NAME for s in suites.SUITES["live"]}
        self.assertIn("live_flow", live_names)

    def test_llm_counts(self) -> None:
        self.assertEqual(suites.llm_total("quick"), 0)
        self.assertGreater(suites.llm_total("live"), suites.llm_total("full"))

    def test_prod_gate(self) -> None:
        self.assertFalse(suites.gate_ok(environment="local", confirm_prod=False))
        self.assertTrue(suites.gate_ok(environment="local", confirm_prod=True))
        self.assertFalse(suites.gate_ok(environment="prod", confirm_prod=False))
        self.assertTrue(suites.gate_ok(environment="prod", confirm_prod=True))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd test-platform && python3 -m unittest tests.test_suites -v`
Expected: `ModuleNotFoundError: No module named 'runner'`

- [ ] **Step 3: 实现 suites.py 与 run.sh**

`test-platform/runner/__init__.py`:空文件。

`test-platform/runner/suites.py`:

```python
"""套件定义与调度入口:环境/套件/场景选择、成本闸门(--confirm-cost)、环境安全闸门(--confirm-prod)。

套件:
  quick — 0 次 LLM 调用,纯无 Key 冒烟;
  full  — 非生成场景 + api_key(合计 1 次校验调用;域场景实装后扩展,本期 = quick 成员);
  live  — full + samples/tasks/live_flow,含真实生成(LLM 合计 3,不超阈值;批量扩展后触发闸门)。
"""

from __future__ import annotations

import argparse
import logging as py_logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import cost, environments, logging as shlogging
from scenarios.baseline import api_smoke
from scenarios.flow import live_flow

SUITES: dict[str, list] = {
    "quick": [api_smoke],
    "full": [api_smoke],       # 域场景(identity/pdf/cards/...)实装后按 spec 场景地图扩展
    "live": [api_smoke, live_flow],
}


def llm_total(suite: str) -> int:
    return cost.aggregate(SUITES[suite])


def gate_ok(*, environment: str, confirm_prod: bool) -> bool:
    """prod 环境必须显式 --confirm-prod;local 默认放行。"""
    return not environments.is_prod(environment) or confirm_prod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="测试平台调度入口")
    ap.add_argument("--environment", default="local", choices=list(environments.ENVIRONMENTS))
    ap.add_argument("--suite", default="quick", choices=list(SUITES))
    ap.add_argument("--scenario", default=None, help="只跑指定场景(NAME)")
    ap.add_argument("--confirm-cost", action="store_true", help="确认 LLM 调用成本(合计 > 阈值时必需)")
    ap.add_argument("--confirm-prod", action="store_true", help="确认操作生产环境")
    ap.add_argument("--device-id", default=None, help="固定 X-Device-ID(所有场景共用)")
    args = ap.parse_args(argv)

    if not gate_ok(environment=args.environment, confirm_prod=args.confirm_prod):
        print("拒绝执行:生产环境需要 --confirm-prod", file=sys.stderr)
        return 1

    scenarios = SUITES[args.suite]
    if args.scenario:
        scenarios = [s for s in scenarios if s.NAME == args.scenario]
        if not scenarios:
            print(f"场景不存在: {args.scenario}", file=sys.stderr)
            return 1

    total = cost.aggregate(scenarios)
    if cost.requires_confirm(total) and not args.confirm_cost:
        print(f"拒绝执行:LLM 调用合计 {total} 超过阈值 {cost.THRESHOLD},需 --confirm-cost", file=sys.stderr)
        return 1

    run_id = str(uuid.uuid4())
    base = environments.resolve(args.environment)
    shlogging.init_logger(run_id, Path(__file__).resolve().parents[1] / "logs" / "test-platform.log")

    failed = 0
    for mod in scenarios:
        print(f"\n===== 场景 {mod.NAME} ({mod.SUITE}) =====")
        shlogging.set_context(suite=mod.SUITE, scenario=mod.NAME, device_id=args.device_id or "")
        args_list = ["--base-url", base]
        if args.device_id:
            args_list += ["--device-id", args.device_id]
        failed += mod.main(args_list)
    print(f"\n套件 {args.suite} 完成, 失败步骤 {failed}, run_id={run_id}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

`test-platform/runner/run.sh`:

```bash
#!/usr/bin/env bash
# 测试平台调度入口(薄壳):转发到 suites.py。用法:
#   ./runner/run.sh [--environment local|prod] [--suite quick|full|live] [--scenario NAME] [--confirm-cost] [--confirm-prod]
set -euo pipefail
cd "$(dirname "$0")"
exec python3 suites.py "$@"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd test-platform && chmod +x runner/run.sh && python3 -m unittest tests.test_suites -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add test-platform/runner test-platform/tests/test_suites.py
git commit -m "feat(test-platform): runner 调度——quick/full/live 套件、成本与环境双闸门、run_id 注入、run.sh 薄壳"
```

---

### Task 6: device 真机层(build / install)

**Files:**
- Create: `test-platform/device/build/build_apk.sh`
- Create: `test-platform/device/install/install.sh`

**Interfaces:**
- `build_apk.sh` 参数:`--sdk-dir`(默认 `$HOME/android-sdk`)、`--gradle-dir`(默认 `$HOME/gradle-dist/gradle-9.6.1`)、`--project`(默认 `frontend-app/Front` 相对仓库根);输出:`<project>/app/build/outputs/apk/debug/app-debug.apk`
- `install.sh` 参数:`--adb`(默认自动探测:$ANDROID_HOME/platform-tools/adb → Windows 常见路径)、`--apk`(默认 build_apk 产物路径);无设备时提示跳过,退出 0

- [ ] **Step 1: 写 build_apk.sh**

```bash
#!/usr/bin/env bash
# 本机编译前端 debug APK(测试平台 device 层)。WSL2 内编译,SDK/gradle 路径参数化。
# 用法: ./device/build/build_apk.sh [--sdk-dir DIR] [--gradle-dir DIR] [--project DIR]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SDK_DIR="${SDK_DIR:-$HOME/android-sdk}"
GRADLE_DIR="${GRADLE_DIR:-$HOME/gradle-dist/gradle-9.6.1}"
PROJECT="${PROJECT:-$REPO_ROOT/frontend-app/Front}"

while [ $# -gt 0 ]; do
  case "$1" in
    --sdk-dir) SDK_DIR="$2"; shift 2 ;;
    --gradle-dir) GRADLE_DIR="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[ -d "$SDK_DIR" ] || { echo "Android SDK 不存在: $SDK_DIR(参考 test-platform/AGENTS.md 环境准备)" >&2; exit 1; }
[ -x "$GRADLE_DIR/bin/gradle" ] || { echo "gradle 不存在: $GRADLE_DIR" >&2; exit 1; }
[ -f "$PROJECT/settings.gradle.kts" ] || { echo "前端工程不存在: $PROJECT" >&2; exit 1; }

[ -f "$PROJECT/local.properties" ] || echo "sdk.dir=$SDK_DIR" > "$PROJECT/local.properties"

cd "$PROJECT"
"$GRADLE_DIR/bin/gradle" assembleDebug --no-daemon
echo "APK: $PROJECT/app/build/outputs/apk/debug/app-debug.apk"
```

- [ ] **Step 2: 写 install.sh**

```bash
#!/usr/bin/env bash
# 安装 APK 到已连真机(device 层)。无设备时提示跳过(退出 0)。
# 用法: ./device/install/install.sh [--adb PATH] [--apk PATH]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
APK="${APK:-$REPO_ROOT/frontend-app/Front/app/build/outputs/apk/debug/app-debug.apk}"
ADB=""

while [ $# -gt 0 ]; do
  case "$1" in
    --adb) ADB="$2"; shift 2 ;;
    --apk) APK="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$ADB" ]; then
  for candidate in "$HOME/android-sdk/platform-tools/adb" \
                   "/mnt/c/Users/$(id -un)/AppData/Local/Android/Sdk/platform-tools/adb.exe" \
                   "/mnt/c/Program Files/Android/Android Studio/platform-tools/adb.exe"; do
    if [ -x "$candidate" ]; then ADB="$candidate"; break; fi
  done
fi
[ -n "$ADB" ] || { echo "未找到 adb,请用 --adb 指定(Windows 侧: \$env:LOCALAPPDATA\\Android\\Sdk\\platform-tools\\adb.exe)" >&2; exit 2; }
[ -f "$APK" ] || { echo "APK 不存在: $APK(先跑 device/build/build_apk.sh)" >&2; exit 1; }

DEVICES="$("$ADB" devices | awk 'NR>1 && $2=="device" {print $1}')"
if [ -z "$DEVICES" ]; then
  echo "未检测到已连接设备,跳过安装(可先执行: $ADB devices)"
  exit 0
fi
echo "安装到: $DEVICES"
"$ADB" -s "$(echo "$DEVICES" | head -1)" install -r "$APK"
```

- [ ] **Step 3: 语法校验与 dry-run**

Run: `bash -n test-platform/device/build/build_apk.sh && bash -n test-platform/device/install/install.sh && chmod +x test-platform/device/build/build_apk.sh test-platform/device/install/install.sh`
Expected: 无输出(语法 OK)

- [ ] **Step 4: 提交**

```bash
git add test-platform/device
git commit -m "feat(test-platform): device 真机层——build_apk(WSL2 编译参数化)、install(设备探测与安装,无设备跳过)"
```

---

### Task 7: 文档与登记(AGENTS.md 技能撰写 + 引用同步)

**Files:**
- Create: `test-platform/AGENTS.md`(用 agent-md-maintenance 技能撰写)
- Create: `test-platform/device/AGENTS.md`(同上技能)
- Modify: `main/scripts/AGENTS.md`(更新为冒烟已迁移说明)
- Modify: `docs/frontend/local-dev.md`(增「自动化测试平台」章节)
- Modify: `docs/Progress.md`(登记 test-platform/ 主目录结构变更)
- Modify: `.gitignore`(追加 `test-platform/logs/`;目录由 logging.py init_logger 自动 mkdir,无需 .gitkeep)

- [ ] **Step 1: 调用 agent-md-maintenance 技能撰写两份 AGENTS.md**

技能调用:`Skill("agent-md-maintenance")`,内容要素:平台分层、用法命令、场景地图(6 域 14 场景登记表)、新增场景指引、日志规范(JSON Lines/字段/脱敏/归档约定)、成本与环境闸门说明。

- [ ] **Step 2: 更新 main/scripts/AGENTS.md**

替换原「API 连通性冒烟脚本」描述为:

```markdown
# AGENTS.md

本目录仅保留部署脚本（`run.sh`/`stop.sh`，语义见 docs/Architecture/deployment.md 契约 4.1）与 R1 验收脚本（`live_estimate_smoke.py`，历史验收资产，见 Progress.md R1 记录）。

- 日常 API 冒烟已迁至 `test-platform/scenarios/`（`baseline/api_smoke.py` 无 Key 冒烟、`flow/live_flow.py` 完整制卡流程），由 `test-platform/runner/run.sh` 调度。
- 新增冒烟场景一律落在 `test-platform/`（域分组 + 场景地图见 `test-platform/AGENTS.md`），不再加入本目录。
```

- [ ] **Step 3: local-dev.md 增章节 + Progress.md 登记 + .gitignore**

`docs/frontend/local-dev.md` 追加:

```markdown
## 9. 自动化测试平台

- 位置:`test-platform/`(独立顶层目录,零依赖纯 stdlib,不依赖 main 的 conda 环境)。
- 常用命令:
  - `./test-platform/runner/run.sh --suite quick` — 无 Key 冒烟(后端需运行中)
  - `./test-platform/runner/run.sh --suite live [--confirm-cost]` — 完整制卡流程(真实生成,消耗 DeepSeek 余额)
  - `./test-platform/device/build/build_apk.sh` — WSL2 编译前端 debug APK
  - `./test-platform/device/install/install.sh` — 安装 APK 到已连真机
- 分层/场景地图/日志规范/新增场景指引见 `test-platform/AGENTS.md`;设计见 `docs/superpowers/specs/2026-08-12-test-platform-design.md`。
```

`docs/Progress.md` 在「文档清单」处(或合适位置)登记:`test-platform/` — 测试平台(独立顶层目录,2026-08-12 建立,spec 见 superpowers/specs)。

`.gitignore` 追加:

```gitignore
# 测试平台运行时日志(JSON Lines,可观测性产物)
/test-platform/logs/
```

- [ ] **Step 4: 引用检查与提交**

```bash
grep -rn "smoke_api" docs/ main/scripts/AGENTS.md 2>/dev/null || echo "无残留引用"
git add -A
git commit -m "docs: 测试平台文档与登记——AGENTS.md(技能撰写)、main/scripts 迁移说明、local-dev 章节、Progress 登记、gitignore 日志目录"
```

---

### Task 8: 端到端验收

**Files:** 无新增(验证与修复)

- [ ] **Step 1: 零依赖验证**

Run: `cd test-platform && python3 -m unittest discover -s tests -v`
Expected: 全部 PASS(不依赖任何第三方包)

- [ ] **Step 2: quick 套件实跑(后端运行中)**

Run: `cd test-platform && ./runner/run.sh --suite quick`
Expected: api_smoke 全 PASS,退出码 0;`logs/test-platform.log` 生成 JSON Lines,含 run_id/request_id

- [ ] **Step 3: 闸门行为验证**

Run: `cd test-platform && ./runner/run.sh --environment prod --suite quick`
Expected: 拒绝执行(未带 --confirm-prod),退出码 1
Run: `cd test-platform && python3 -c "from runner.suites import llm_total; assert llm_total('live')==3, llm_total('live'); print('live 合计 3,未超阈值,默认放行')"`
Expected: 打印放行断言(不触发真实生成;实跑放 Task 8 Step 5)

- [ ] **Step 4: 日志可观测性核对**

Run: `grep -c '"run_id"' test-platform/logs/test-platform.log`(> 0);`grep "request complete" test-platform/logs/test-platform.log | tail -1` 含 `request_id`;`grep -c "sk-" test-platform/logs/test-platform.log`(应为 0,无 Key 明文);从该日志取一个 request_id,在 `main/data/logs/app.log` 中 grep 定位同一请求(交叉核对成功)

- [ ] **Step 5: live 套件实跑(可选,消耗真实余额)**

Run: `cd test-platform && ./runner/run.sh --suite live`
Expected: live_flow 全流程 PASS(Key/PDF/样卡/任务/复习/看板),清理生效(跑后 `GET /decks` 无残留「联调测试牌组」)

- [ ] **Step 6: 真机脚本验证**

Run: `./test-platform/device/build/build_apk.sh`(增量编译,应秒级或复用缓存);`./test-platform/device/install/install.sh --adb <Windows-adb>`(设备连接时安装成功;未连接时输出跳过提示)

- [ ] **Step 7: 最终提交**

```bash
git add -A && git commit -m "chore(test-platform): 端到端验收通过——quick 全绿、闸门生效、日志可观测性核对、真机脚本可用"
```

---

## 自审记录

- **Spec 覆盖**:架构目录(Task 1-6)✓、日志可观测性(Task 1 logging、Task 2 client 自动事件、Task 8 grep 与 request_id 交叉核对)✓、成本闸门(Task 2 cost、Task 5 runner、Task 8)✓、环境闸门(Task 5)✓、数据策略(Task 2 cleanup、Task 4 live_flow 清理)✓、迁移(Task 3)✓、live_flow(Task 4)✓、device(Task 6)✓、文档与登记(Task 7)✓、验收(Task 8)✓。
- **类型一致性**:`ShankaClient.request` 签名、`Response` 字段、`check/summary`、`LLM_CALLS`/`NAME`/`SUITE` 场景约定、`suites.llm_total/gate_ok` 跨任务一致。
- **占位符扫描**:无 TBD/TODO;每任务含完整代码或明确步骤。
- **注意点**:api_smoke 第 2/4/8 组检查因语义需要(无设备头/同键重放/超速连发)绕过 ShankaClient 直连——已在 Task 3 代码注释说明;live 套件 LLM 合计 3 不超阈值,闸门验证用 prod 环境用例。
