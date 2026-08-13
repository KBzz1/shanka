"""R1 live 执行驱动（task-3 brief：driver 单元流程 5 步）。

正式链路逐单元执行：上传真实 PDF → 解析（V3A 三重校验）→ 抽样块章节注入（DB 直插）→
建牌组 → 保存 Key → POST /tasks → 显式 scan_once → 单元验证（COMPLETED/卡 Schema 合法/
入库计数=计划数/幂等重放）→ 记录 usage/model/fingerprint/价格 → 成本累计检查 → 报告。

红线 4（Key 安全）：
- .env 只运行时读取（--env-file，默认仓库根 .env；手写解析，无新依赖）；任何文件/日志/
  输出不得出现明文 Key（日志/报告只用 masked() 脱敏展示）；
- 源码无真实 Key 字面量（dry-run 种子 Key 为测试假值，与 tests 一致）。
LOCAL-DONE 前不触网：默认 dry-run（httpx.MockTransport 注入，全流程零网络）；
--live 标志才允许真实 DeepSeek 调用（T4 主 Agent 使用）。

成本边界（默认 --max-cost-yuan 5 --max-total-yuan 10）：每单元后累计检查（含第 1 单元）；
超限立即停止并保留真实失败（报告 stop_reason）。价格用 estimate_cost_by_kind
（effective_date = 当日 UTC）。

canary 语义（F2）：第 1 单元（index==1）= canary，失败 → 立即停止，stop_reason =
canary_failed；其余单元失败 → 记录 FAILED 后继续。单次运行保护（F3）：报告文件
（--report，默认 /tmp/r1-live-report.json）已存在 → 拒绝运行，须 --allow-rerun 显式授权。

用法：cd main && conda run -n shanka-backend python -m tests.live.driver \
  --frame /tmp/r1-frame.json --db /tmp/r1-live.db --storage /tmp/r1-storage \
  [--limit N] [--max-cost-yuan 5] [--max-total-yuan 10] [--report PATH] [--allow-rerun] \
  [--dry-run|--live]
"""

import argparse
import json
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey, Batch, Card, Chapter, KnowledgePoint
from infra.db.session import create_db_engine, create_session_factory, format_utc
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.api_key.service import masked
from services.generation.cost import estimate_cost_by_kind
from services.generation.schema_validator import load_card_schema, validate_card
from services.pdf.scanner import scan_once as scan_pdfs
from services.tasks.executor import ClientFactory
from services.tasks.executor import scan_once as scan_tasks

# ---------------------------------------------------------------------------
# 常量（无 Key 字面量；dry-run 种子为测试假值）
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]  # main/tests/live/driver.py → 仓库根
_DEFAULT_ENV_FILE = _REPO_ROOT / ".env"
_ALEMBIC_INI = _REPO_ROOT / "main" / "alembic.ini"

_DIFFICULTY_MAP = {"easy": "COMPACT", "medium": "BALANCED", "hard": "EXTENSIVE"}
_DIFFICULTY_RATIO = {"basic": 0.4, "understanding": 0.4, "application": 0.2}
_DRY_RUN_KEY = "sk-dry-run-fake-0000"  # 测试假值（与 tests/unit/test_deepseek_adapter.py 同款约定）
_DRY_RUN_FINGERPRINT = "fp_dry_run_0001"

_TOKEN_KEYS = ("prompt", "cache_hit", "cache_miss", "output")


# ---------------------------------------------------------------------------
# .env（仅运行时读取；不落盘不落日志）
# ---------------------------------------------------------------------------


def load_env_file(path: Path) -> dict[str, str]:
    """手写 .env 解析（KEY=VALUE，忽略注释/空行，去首尾空白与引号）。

    红线 4：返回值只在本进程内使用，任何文件/日志/输出不得包含明文 Key。
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


# ---------------------------------------------------------------------------
# 客户端（RecordingClient：按单元捕获 chat 结果——usage/model/fingerprint/耗时）
# ---------------------------------------------------------------------------


class RecordingClient:
    """包装 DeepSeekClient 记录每次 chat 结果（driver 按单元聚合，不进 DB）。"""

    def __init__(self, inner: DeepSeekClient) -> None:
        self._inner = inner
        self.results: list[dict[str, Any]] = []

    def chat(self, prompt: str, api_key: str = "") -> dict[str, Any]:
        result = self._inner.chat(prompt, api_key)
        self.results.append(result)
        return result

    def close(self) -> None:
        self._inner.close()


def _make_dry_run_handler(batch_size: int) -> Callable[[httpx.Request], httpx.Response]:
    """dry-run mock transport 工厂：每批返回 batch_size 张合法卡 + usage + fingerprint。

    M6：卡数从 Settings.batch_size 取（dry-run 入库计数校验与配置解耦，不再依赖模块常量）。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        cards = [
            {"type": "QUESTION", "question": f"dryrun-q{i}", "answer": f"dryrun-a{i}"}
            for i in range(batch_size)
        ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"cards": cards}, ensure_ascii=False)}}
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_cache_hit_tokens": 2,
                    "prompt_cache_miss_tokens": 8,
                },
                "model": "deepseek-v4-flash",
                "system_fingerprint": _DRY_RUN_FINGERPRINT,
            },
        )

    return handler


def make_unit_client(settings: Settings, api_key: str, *, live: bool) -> RecordingClient:
    """构造单元客户端：live 用生产 transport，dry-run 注入 MockTransport。"""
    if live:
        inner = DeepSeekClient(settings, api_key=api_key)
    else:
        inner = DeepSeekClient(
            settings,
            transport=httpx.MockTransport(_make_dry_run_handler(settings.batch_size)),
            api_key=api_key,
        )
    return RecordingClient(inner)


def _aggregate_usage(results: list[dict[str, Any]]) -> dict[str, int]:
    """按单元聚合 chat 结果的 token 计数（4 键，缺失按 0）。"""
    summed = {"prompt": 0, "cache_hit": 0, "cache_miss": 0, "output": 0}
    for result in results:
        usage = result.get("usage") or {}
        summed["prompt"] += int(usage.get("prompt_tokens") or 0)
        summed["cache_hit"] += int(usage.get("prompt_cache_hit_tokens") or 0)
        summed["cache_miss"] += int(usage.get("prompt_cache_miss_tokens") or 0)
        summed["output"] += int(usage.get("completion_tokens") or 0)
    return summed


# ---------------------------------------------------------------------------
# DB 基础设施
# ---------------------------------------------------------------------------


def migrate_db(db_path: Path) -> sessionmaker[Session]:
    """alembic upgrade head（正式链路 schema）；返回 session_factory。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    return create_session_factory(engine)


# ---------------------------------------------------------------------------
# 单元验证（validate_card 直读 DB）
# ---------------------------------------------------------------------------


def _card_orm_to_internal(card: Card) -> dict[str, Any]:
    """Card ORM → 内部卡 dict（schema 校验口径与入库一致）。

    只带非 None 字段：schema 的 property type 约束对显式 None 违约（生产路径的
    _to_internal_card 也不产出缺失键），缺失键省略而非置 None。
    """
    internal: dict[str, Any] = {"type": card.card_type, "front": card.front, "back": card.back}
    for key, value in (
        ("question", card.question),
        ("answer", card.answer),
        ("statement", card.statement),
        ("explanation", card.explanation),
    ):
        if value is not None:
            internal[key] = value
    if card.answer_boolean is not None:
        internal["answer_boolean"] = bool(card.answer_boolean)
    return internal


def _verify_task_cards(
    session: Session, *, deck_id: str, after_position: int, schema: dict[str, Any]
) -> tuple[int, list[str]]:
    """卡 Schema 合法（validate_card 直读 DB）：本单元入库的卡 = 牌组 position > after_position。

    generation_item_id 是含 task_id 的稳定 UUID（非明文前缀），卡归属用牌组 position 尾段
    （单元严格串行，position 单调递增；单元内 0 卡时下一单元的 before 标记前移，无歧义）。
    返回 (卡数, schema 违约列表)。
    """
    cards = session.scalars(
        select(Card)
        .where(Card.deck_id == deck_id, Card.position > after_position)
        .order_by(Card.position)
    ).all()
    violations: list[str] = []
    for card in cards:
        bad = validate_card(_card_orm_to_internal(card), schema)
        if bad:
            violations.append(f"card {card.card_id}: {'; '.join(bad)}")
    return len(cards), violations


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _unit_client_factory(
    settings: Settings, *, live: bool, recorders: list[RecordingClient]
) -> Callable[[str], RecordingClient]:
    """构造单元 client_factory：executor 每次调用构造客户端并登记到 recorders（逐单元聚合）。

    参数化 recorders（B023：闭包不绑定循环变量——循环内只调用本函数，不定义闭包）。
    """

    def factory(decrypted_key: str) -> RecordingClient:
        recorder = make_unit_client(settings, decrypted_key, live=live)
        recorders.append(recorder)
        return recorder

    return factory


def _frame_blocks(frame: dict[str, Any]) -> list[dict[str, Any]]:
    """frame JSON → 块列表（校验必填键/难度值/数量）。"""
    blocks = frame.get("blocks")
    if not isinstance(blocks, list) or len(blocks) < 1:
        raise ValueError("frame JSON 缺少 blocks 列表")
    for block in blocks:
        for key in ("index", "chapter_name", "start_page", "end_page", "difficulty"):
            if key not in block:
                raise ValueError(f"frame 块缺少字段 {key!r}: {block}")
        if block["difficulty"] not in _DIFFICULTY_MAP:
            raise ValueError(f"frame 块难度非法: {block['difficulty']!r}")
    return blocks


def run_driver(args: argparse.Namespace) -> dict[str, Any]:
    """驱动主流程。返回报告 dict（含逐单元记录与汇总；无明文 Key）。"""
    env = load_env_file(Path(args.env_file))
    # 加密密钥：优先 .env 提供；缺省时运行时生成临时密钥（内存内，不落盘不落日志——
    # 本机 .env 目前只有 DEEPSEEK_API_KEY；PUT /api-key 与 DB 直插都需要 32 字节 hex）
    encryption_key_raw = env.get("API_KEY_ENCRYPTION_KEY")
    if encryption_key_raw:
        encryption_key_source = "env"
    else:
        encryption_key_source = "ephemeral-random"
        encryption_key_raw = secrets.token_hex(32)
        print(
            f"警告: {args.env_file} 无 API_KEY_ENCRYPTION_KEY，已生成运行时临时密钥（仅本次进程内有效）"
        )
    live = bool(args.live)
    env_key = env.get("DEEPSEEK_API_KEY")
    if live:
        if not env_key:
            raise SystemExit(f"{args.env_file} 缺少 DEEPSEEK_API_KEY（live 模式必须提供真实 Key）")
    else:
        env_key = _DRY_RUN_KEY
    api_key = env_key  # 两分支均已收窄为 str（红线 4：仅进程内使用，不落盘不落日志）

    db_path = Path(args.db)
    storage_path = Path(args.storage)
    report_path = Path(args.report)
    frame = json.loads(Path(args.frame).read_text(encoding="utf-8"))
    blocks = _frame_blocks(frame)
    limit = args.limit if args.limit and args.limit > 0 else len(blocks)
    limit = min(limit, len(blocks))

    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=storage_path,
        api_key_encryption_key=encryption_key_raw,
        task_scan_interval_seconds=3600.0,  # 禁用后台自动循环：driver 显式 scan_once
        pdf_scan_interval_seconds=3600.0,
        rate_limit_ip_per_second=10000,
        rate_limit_write_per_minute=10000,
        rate_limit_api_key_per_hour=1000,
        rate_limit_samples_per_hour=1000,
        rate_limit_pdf_per_hour=1000,
    )
    session_factory = migrate_db(db_path)
    device_id = args.device_id or str(uuid.uuid4())
    headers: dict[str, str] = {}  # P4-4：X-Device-ID 已退出，仅 Bearer（下方注册后注入）

    report: dict[str, Any] = {
        "driver": "r1-live-driver",
        "mode": "live" if live else "dry-run",
        "device_id": device_id,
        "frame": {"path": str(args.frame), "seed": frame.get("seed"), "block_count": len(blocks)},
        "budgets": {"max_cost_yuan": args.max_cost_yuan, "max_total_yuan": args.max_total_yuan},
        "effective_date": datetime.now(UTC).date().isoformat(),
        "units": [],
        "summary": {},
    }

    app = create_app(settings)
    with TestClient(app) as client:
        # P4-4 起仅 Bearer：注册/登录 live 驱动账号（测试假凭据，非敏感信息）；
        # report 不记录 token/密码。
        reg = client.post(
            "/auth/register", json={"username": "live-driver", "password": "live-driver-pass-1"}
        )
        if reg.status_code == 409:
            reg = client.post(
                "/auth/login", json={"username": "live-driver", "password": "live-driver-pass-1"}
            )
        headers["Authorization"] = f"Bearer {reg.json()['access_token']}"
        # ---- 1. 上传真实 PDF → 解析（V3A 三重校验）→ 抽样块章节注入（DB 直插）----
        pdf_path = Path(args.pdf)
        with pdf_path.open("rb") as f:
            resp = client.post(
                "/pdfs",
                files={"file": (pdf_path.name, f, "application/pdf")},
                headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            )
        if resp.status_code != 201:
            raise SystemExit(f"POST /pdfs 失败: {resp.status_code} {resp.text}")
        file_id = resp.json()["file_id"]
        scan_pdfs(app.state.session_factory, storage=app.state.storage)
        pdf_view = client.get(f"/pdfs/{file_id}", headers=headers).json()
        if pdf_view["status"] != "PARSED":
            raise SystemExit(
                f"PDF 解析未完成: status={pdf_view['status']} error={pdf_view['error_code']}"
            )
        report["pdf"] = {"file_id": file_id, "filename": pdf_view["filename"], "status": "PARSED"}

        # 章节注入：清掉解析产物，直插 60 块（抽样框由主 Agent 审阅固定后 driver 消费）
        with session_factory() as session:
            for old in session.scalars(select(Chapter).where(Chapter.file_id == file_id)).all():
                session.delete(old)
            inserted: list[Chapter] = []
            for block in blocks:
                ch = Chapter(
                    chapter_id=str(uuid.uuid4()),
                    file_id=file_id,
                    name=f"{block['chapter_name']}-块{block['index']:02d}",
                    start_page=int(block["start_page"]),
                    end_page=int(block["end_page"]),
                )
                session.add(ch)
                inserted.append(ch)
            session.flush()
            chapter_ids = [ch.chapter_id for ch in inserted]
            session.commit()

        # ---- 2. 建牌组 ----
        resp = client.post(
            "/decks",
            json={"name": "R1 live 牌组"},
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        if resp.status_code != 201:
            raise SystemExit(f"POST /decks 失败: {resp.status_code} {resp.text}")
        deck_id = resp.json()["deck_id"]
        report["deck_id"] = deck_id

        # ---- 3. Key 保存（live：PUT /api-key 正式链路；dry-run：DB 直插避免触网）----
        if live:
            resp = client.put(
                "/api-key",
                json={"api_key": api_key},
                headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            )
            # M5：先查 HTTP 状态码（非 200/201 → 明确错误退出，含 masked Key 说明）
            if resp.status_code not in (200, 201):
                raise SystemExit(
                    f"PUT /api-key 失败: HTTP {resp.status_code}（Key 未保存，仅以掩码 "
                    f"{masked(api_key)} 呈现，明文未落盘未记录；响应: {resp.text[:300]}）"
                )
            body = resp.json()
            report["api_key"] = {
                "saved_via": "http",
                "status": body.get("status"),
                "masked_key": masked(api_key),
                "encryption_key_source": encryption_key_source,
            }
            if body.get("status") != "AVAILABLE":
                raise SystemExit(f"PUT /api-key 未通过校验: status={body.get('status')}")
        else:
            enc_key = key_from_settings(settings)
            if enc_key is None:
                raise SystemExit("API_KEY_ENCRYPTION_KEY 不是合法 32 字节 hex")
            with session_factory() as session:
                session.add(
                    ApiKey(
                        device_id=device_id,
                        encrypted_key=encrypt_key(api_key, enc_key),
                        status="AVAILABLE",
                        masked_key=masked(api_key),
                        updated_at=format_utc(datetime.now(UTC)),
                    )
                )
                session.commit()
            report["api_key"] = {
                "saved_via": "db-seed",
                "status": "AVAILABLE",
                "masked_key": masked(api_key),
                "encryption_key_source": encryption_key_source,
            }

        # ---- 4. 逐单元正式链路 ----
        schema = load_card_schema()
        total_cost = 0.0
        stop_reason: str | None = None
        for block in blocks[:limit]:
            unit_start = time.monotonic()
            unit: dict[str, Any] = {
                "index": int(block["index"]),
                "chapter_name": block["chapter_name"],
                "pages": [int(block["start_page"]), int(block["end_page"])],
                "difficulty": block["difficulty"],
                "quantity_tendency": _DIFFICULTY_MAP[block["difficulty"]],
                "failures": [],
            }
            idem_key = str(uuid.uuid4())
            body = {
                "file_id": file_id,
                "deck_id": deck_id,
                "chapter_ids": [chapter_ids[unit["index"] - 1]],
                "generation_config": {
                    "quantity_tendency": unit["quantity_tendency"],
                    "difficulty_ratio": _DIFFICULTY_RATIO,
                },
            }
            recorders: list[RecordingClient] = []
            factory = _unit_client_factory(settings, live=live, recorders=recorders)

            resp = client.post(
                "/tasks", json=body, headers={**headers, "Idempotency-Key": idem_key}
            )
            if resp.status_code != 201:
                unit["failures"].append(f"POST /tasks {resp.status_code}: {resp.text}")
            else:
                task_view = resp.json()
                task_id = task_view["task_id"]
                unit["task_id"] = task_id
                unit["idempotency_key"] = idem_key
                with session_factory() as session:
                    before_max_position = (
                        session.scalar(
                            select(func.max(Card.position)).where(Card.deck_id == deck_id)
                        )
                        or 0
                    )
                scan_tasks(
                    app.state.session_factory,
                    settings=settings,
                    client_factory=cast(ClientFactory, factory),
                )
                detail = client.get(f"/tasks/{task_id}", headers=headers).json()
                if detail["status"] != "COMPLETED":
                    unit["failures"].append(
                        f"任务未 COMPLETED: status={detail['status']} "
                        f"stage={detail['stage']} error={detail['error_code']}"
                    )
                with session_factory() as session:
                    kps = session.scalars(
                        select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)
                    ).all()
                    batches = session.scalars(
                        select(Batch).where(Batch.task_id == task_id).order_by(Batch.batch_index)
                    ).all()
                    inserted_cards, schema_violations = _verify_task_cards(
                        session, deck_id=deck_id, after_position=before_max_position, schema=schema
                    )
                    planned = len(kps)
                    if schema_violations:
                        unit["failures"].append(f"卡 Schema 违约: {schema_violations}")
                    if inserted_cards != planned:
                        unit["failures"].append(f"入库计数 {inserted_cards} != 计划数 {planned}")
                unit["planned_cards"] = planned
                unit["inserted_cards"] = inserted_cards
                unit["batches"] = {
                    "total": detail["total_batch_count"],
                    "completed": detail["completed_batch_count"],
                }
                unit["started_at"] = detail["started_at"]
                unit["ended_at"] = detail["ended_at"]

                # 幂等重放：同幂等键重发 POST /tasks → 同响应 + 不重复执行
                replay = client.post(
                    "/tasks", json=body, headers={**headers, "Idempotency-Key": idem_key}
                )
                replay_ok = (
                    replay.status_code == resp.status_code
                    and replay.json().get("task_id") == task_id
                )
                with session_factory() as session:
                    after_cards = (
                        session.scalar(
                            select(func.count(Card.card_id)).where(
                                Card.deck_id == deck_id, Card.position > before_max_position
                            )
                        )
                        or 0
                    )
                    after_batches = (
                        session.scalar(
                            select(func.count(Batch.batch_id)).where(Batch.task_id == task_id)
                        )
                        or 0
                    )
                replay_ok = (
                    replay_ok and after_cards == inserted_cards and after_batches == len(batches)
                )
                unit["replay_ok"] = replay_ok
                if not replay_ok:
                    unit["failures"].append("幂等重放失败（响应不一致或重复执行）")

                # 记录 usage/model/fingerprint/价格/耗时
                results = [r for rec in recorders for r in rec.results]
                tokens = _aggregate_usage(results)
                unit["tokens"] = {k: tokens[k] for k in _TOKEN_KEYS}
                unit["cost_yuan"] = estimate_cost_by_kind(
                    tokens["cache_hit"],
                    tokens["cache_miss"],
                    tokens["output"],
                    effective_date=report["effective_date"],
                )
                unit["model"] = results[-1].get("model") if results else None
                unit["fingerprint"] = results[-1].get("system_fingerprint") if results else None
                unit["fingerprints"] = sorted(
                    {r.get("system_fingerprint") for r in results if r.get("system_fingerprint")}
                )
                unit["duration_ms"] = sum(int(r.get("duration_ms") or 0) for r in results)
                unit["wall_ms"] = round((time.monotonic() - unit_start) * 1000)

            unit["status"] = "OK" if not unit["failures"] else "FAILED"
            report["units"].append(unit)
            total_cost += float(unit.get("cost_yuan", {}).get("total", 0.0))

            # canary 语义（F2）：第 1 单元（index==1）= canary，失败即停（计划「canary 失败即停」）；
            # 其余单元失败只记录 FAILED 后继续（真实失败保留在报告里）
            if unit["status"] == "FAILED" and unit["index"] == 1:
                stop_reason = "canary_failed"
                break

            # 成本累计检查（canary 后从第 1 单元即检查；超限立即停止，保留真实失败）
            unit_cost = float(unit.get("cost_yuan", {}).get("total", 0.0))
            if unit_cost > args.max_cost_yuan:
                stop_reason = "max_cost_yuan_exceeded"
                break
            if total_cost > args.max_total_yuan:
                stop_reason = "max_total_yuan_exceeded"
                break

    # ---- 5. 汇总 + 输出 ----
    ok = sum(1 for u in report["units"] if u["status"] == "OK")
    failed = sum(1 for u in report["units"] if u["status"] == "FAILED")
    report["summary"] = {
        "units_attempted": len(report["units"]),
        "units_succeeded": ok,
        "units_failed": failed,
        "total_cost_yuan": round(total_cost, 6),
        "stop_reason": stop_reason,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(report)
    return report


def _print_summary(report: dict[str, Any]) -> None:
    """人类可读摘要（无明文 Key：api_key 只输出 masked_key）。"""
    print(f"R1 live driver [{report['mode']}]")
    print(f"  pdf={report['pdf']['file_id']} deck={report['deck_id']}")
    api_key = report.get("api_key", {})
    print(
        f"  api_key: saved_via={api_key.get('saved_via')} status={api_key.get('status')} "
        f"masked={api_key.get('masked_key')}"
    )
    for unit in report["units"]:
        cost = unit.get("cost_yuan", {}).get("total", "-")
        print(
            f"  [{unit['index']:02d}] {unit['status']:<6} task={unit.get('task_id', '-')[:8]} "
            f"tend={unit['quantity_tendency']:<8} cards={unit.get('inserted_cards', '-')}/{unit.get('planned_cards', '-')} "
            f"tokens={unit.get('tokens', {})} cost={cost} "
            f"fp={unit.get('fingerprint')} wall={unit.get('wall_ms')}ms"
        )
    summary = report["summary"]
    print(
        f"  汇总: 成功 {summary['units_succeeded']}/{summary['units_attempted']} "
        f"失败 {summary['units_failed']} 总成本 {summary['total_cost_yuan']} 元 "
        f"停止原因 {summary['stop_reason']}"
    )
    print("  报告: 已写入（JSON）")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="R1 live 执行驱动（默认 dry-run；--live 才允许真实调用）"
    )
    parser.add_argument(
        "--frame", type=Path, required=True, help="抽样框 JSON（sample_frame.py 输出）"
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=Path("/home/kbzz1/shanka_backend/res/AI-Agents-in-Depth-zh-CN.pdf"),
        help="样书 PDF（只读引用）",
    )
    parser.add_argument(
        "--db", type=Path, required=True, help="SQLite DB 路径（alembic upgrade head）"
    )
    parser.add_argument("--storage", type=Path, required=True, help="PDF 存储目录")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("/tmp/r1-live-report.json"),
        help="报告 JSON 路径（默认 /tmp/r1-live-report.json）",
    )
    parser.add_argument(
        "--allow-rerun",
        action="store_true",
        help="允许覆盖已存在的报告文件（正式样本只运行 1 次；仅实质修复后显式授权才重跑）",
    )
    parser.add_argument("--max-cost-yuan", type=float, default=5.0, help="单单元成本上限（元）")
    parser.add_argument("--max-total-yuan", type=float, default=10.0, help="总成本上限（元）")
    parser.add_argument("--limit", type=int, default=0, help="只执行前 N 个单元（0 = 全部）")
    parser.add_argument("--device-id", type=str, default="", help="固定设备 ID（默认随机 UUID）")
    parser.add_argument(
        "--env-file", type=str, default=str(_DEFAULT_ENV_FILE), help=".env 路径（仅运行时读取）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="dry-run（默认；mock transport 全流程零网络）"
    )
    parser.add_argument(
        "--live", action="store_true", help="允许真实 DeepSeek 调用（T4 主 Agent 使用）"
    )
    args = parser.parse_args()
    if args.live and args.dry_run:
        raise SystemExit("--live 与 --dry-run 互斥")
    # F3：单次运行保护——报告文件已存在 → 拒绝覆盖（正式样本只运行 1 次）
    if args.report.exists() and not args.allow_rerun:
        raise SystemExit(
            f"报告已存在: {args.report}（正式样本只运行 1 次，拒绝覆盖；"
            f"实质修复后可加 --allow-rerun 显式授权重跑）"
        )
    run_driver(args)


if __name__ == "__main__":
    main()
