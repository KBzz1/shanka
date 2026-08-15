"""应用装配（project-structure 3）：唯一创建入口 create_app；模块级 app 供 uvicorn 启动。"""

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.api import (
    api_key,
    auth,
    cards,
    decks,
    metrics,
    observability,
    pdfs,
    preferences,
    probes,
    projects,
    review,
    stats,
    study,
    tasks,
)
from app.config import Settings
from app.middleware.auth import BearerAuthMiddleware
from app.middleware.body_capture import BodyCaptureMiddleware
from app.middleware.error_handler import register_exception_handlers
from app.middleware.ip_limit import IpRateLimitMiddleware
from app.middleware.logging import LoggingMiddleware
from app.middleware.metrics_middleware import MetricsMiddleware
from app.middleware.rate_limit import RateLimiter, RateLimitMiddleware
from app.middleware.request_id import RequestIDMiddleware
from infra.db.session import create_db_engine, create_session_factory
from infra.logging import setup_logging
from infra.storage.local import LocalStorage
from services.pdf.scanner import scan_once as scan_pdfs
from services.tasks.executor import scan_once as scan_tasks

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
            scan_pdfs(session_factory, storage=storage)
        except Exception:  # 扫描失败不中断循环（scan_once 内部已记录解析失败）
            logger.warning("pdf scanner loop iteration failed", exc_info=True)


def _task_executor_loop(
    session_factory: sessionmaker[Session],
    stop_event: threading.Event,
    interval: float,
) -> None:
    """任务执行器后台循环（Task 4）：逐间隔 scan_once；单轮失败不中断循环。

    wait-first：首个间隔为启动宽限期（与 PDF 扫描器同款，避免与启动期 DDL 竞争
    BEGIN IMMEDIATE 写锁——executor 走 engine 级 begin 事件，读也走写事务）。
    """
    while not stop_event.is_set():
        stop_event.wait(interval)
        if stop_event.is_set():
            return
        try:
            scan_tasks(session_factory)
        except Exception:  # 扫描失败不中断循环（executor 内部已记录任务失败）
            logger.warning("task executor loop iteration failed", exc_info=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level, settings.log_dir)
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
        task_stop_event = threading.Event()
        task_thread = threading.Thread(
            target=_task_executor_loop,
            args=(app.state.session_factory, task_stop_event, settings.task_scan_interval_seconds),
            daemon=True,
        )
        task_thread.start()
        try:
            yield
        finally:
            stop_event.set()
            task_stop_event.set()
            thread.join(timeout=5)
            task_thread.join(timeout=5)
            engine.dispose()

    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    register_exception_handlers(app)
    # 中间件运行序（外层→内层）：Metrics → RequestID → IpRateLimit → Auth → RateLimit →
    # Logging → BodyCapture → 路由。Starlette add_middleware 为 insert(0) 语义
    # （后加者在外层），故按目标运行序倒序添加；历史沿革：Task 6 在 Logging 之后插入
    # DeviceID，Task 9 在 DeviceID 与 RequestID 之间插入 RateLimit（键用原始头，运行于
    # DeviceID 外层），Task 10 追加 Metrics（最外层），Task 4/V1 在添加序最前加入
    # BodyCapture（运行序最内、路由前，位于 Logging 内层——幂等 body 捕获须先于路由
    # handler 完成），P4-3 将 Auth 移出 RateLimit 外层（限流业务维度键改读
    # principal.user_id），P4-3 fix round 1 在 Auth 外层补 IpRateLimit（IP 5 req/s
    # 总闸门——覆盖未认证 401 流量，契约 1.6「全部接口」；业务维度限流位于 Auth 内层
    # 管不到该流量），P4-4 删除 DeviceID（X-Device-ID 退出——Auth 紧邻 RateLimit 内层）。
    app.add_middleware(BodyCaptureMiddleware)  # 添加序最前 → 运行序最内（路由前）
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RateLimitMiddleware, settings=settings)  # 运行序位于 Auth 内层
    app.add_middleware(BearerAuthMiddleware)  # P4-3：运行序位于 RateLimit 外层（先认证再限流）
    app.add_middleware(
        IpRateLimitMiddleware, settings=settings
    )  # fix round 1：运行序位于 Auth 外层（IP 总闸门覆盖未认证流量）
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(MetricsMiddleware)  # 添加序最后 → 运行序最外层（统计所有响应含 401/429）
    app.include_router(probes.router)
    app.include_router(auth.router)
    app.include_router(metrics.router)
    app.include_router(decks.router)
    app.include_router(cards.router)
    app.include_router(
        cards.router_rewrite
    )  # V2.5 两阶段重写（/cards/{card_id}/rewrite-previews 三端点）
    app.include_router(cards.router_batches)  # V2.5 删除批次（/card-deletion-batches/*）
    app.include_router(review.router)
    app.include_router(stats.router)
    app.include_router(pdfs.router)
    app.include_router(projects.router)
    app.include_router(preferences.router)
    app.include_router(study.router)  # V2.5 今日学习计划（/study/today）
    app.include_router(api_key.router)
    app.include_router(tasks.router)
    app.include_router(observability.router)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.storage = storage
    # login email 桶（P4-3→V2.4 桶键改 email）：service 层限流共享实例（body 于
    # BodyCapture 内层，middleware 不可读——裁决：限流器在 auth handler 取用）
    app.state.login_email_limiter = RateLimiter(
        limit=settings.rate_limit_login_email_per_hour, window_seconds=3600
    )
    return app


app = create_app()
