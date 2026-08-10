"""应用装配（project-structure 3）：唯一创建入口 create_app；模块级 app 供 uvicorn 启动。"""

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.api import api_key, cards, decks, metrics, pdfs, probes, review, stats
from app.config import Settings
from app.middleware.body_capture import BodyCaptureMiddleware
from app.middleware.device_id import DeviceIDMiddleware
from app.middleware.error_handler import register_exception_handlers
from app.middleware.logging import LoggingMiddleware
from app.middleware.metrics_middleware import MetricsMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIDMiddleware
from infra.db.session import create_db_engine, create_session_factory
from infra.logging import setup_logging
from infra.storage.local import LocalStorage
from services.pdf.scanner import scan_once

logger = logging.getLogger(__name__)


def _pdf_scanner_loop(
    session_factory: sessionmaker[Session],
    storage: LocalStorage,
    stop_event: threading.Event,
    interval: float,
) -> None:
    """扫描器后台循环（Task 4）：逐间隔 scan_once；单轮失败不中断循环。

    wait-first：首个间隔为启动宽限期（DB/表就绪后再扫描，避免与启动期 DDL 竞争
    BEGIN IMMEDIATE 写锁——scan_once 走 engine 级 begin 事件，读也走写事务）。
    """
    while not stop_event.is_set():
        stop_event.wait(interval)
        if stop_event.is_set():
            return
        try:
            scan_once(session_factory, storage=storage)
        except Exception:  # 扫描失败不中断循环（scan_once 内部已记录解析失败）
            logger.warning("pdf scanner loop iteration failed", exc_info=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level)
    engine = create_db_engine(settings.database_url)
    storage = LocalStorage(settings.storage_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # PDF 扫描器后台循环：daemon 线程 + stop_event 优雅退出（测试不进入 lifespan，
        # 显式调 scan_once；间隔可配 Settings.pdf_scan_interval_seconds）
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_pdf_scanner_loop,
            args=(
                app.state.session_factory,
                storage,
                stop_event,
                settings.pdf_scan_interval_seconds,
            ),
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=5)
            engine.dispose()

    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    register_exception_handlers(app)
    # 中间件运行序（外层→内层）：Metrics → RequestID → RateLimit → DeviceID → Logging →
    # BodyCapture → 路由。Starlette add_middleware 为 insert(0) 语义（后加者在外层），
    # 故按目标运行序倒序添加；历史沿革：Task 6 在 Logging 之后插入 DeviceID，Task 9 在
    # DeviceID 与 RequestID 之间插入 RateLimit（键用原始头，运行于 DeviceID 外层），
    # Task 10 追加 Metrics（最外层），Task 4/V1 在添加序最前加入 BodyCapture（运行序最内、
    # 路由前，位于 Logging 内层——幂等 body 捕获须先于路由 handler 完成）。
    app.add_middleware(BodyCaptureMiddleware)  # 添加序最前 → 运行序最内（路由前）
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(DeviceIDMiddleware)
    app.add_middleware(RateLimitMiddleware, settings=settings)  # 在 DeviceID 与 RequestID 之间
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(MetricsMiddleware)  # 添加序最后 → 运行序最外层（统计所有响应含 401/429）
    app.include_router(probes.router)
    app.include_router(metrics.router)
    app.include_router(decks.router)
    app.include_router(cards.router)
    app.include_router(review.router)
    app.include_router(stats.router)
    app.include_router(pdfs.router)
    app.include_router(api_key.router)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.storage = storage
    return app


app = create_app()
