"""应用装配（project-structure 3）：唯一创建入口 create_app；模块级 app 供 uvicorn 启动。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import probes
from app.config import Settings
from app.middleware.error_handler import register_exception_handlers
from app.middleware.logging import LoggingMiddleware
from app.middleware.request_id import RequestIDMiddleware
from infra.db.session import create_db_engine, create_session_factory
from infra.logging import setup_logging
from infra.storage.local import LocalStorage


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level)
    engine = create_db_engine(settings.database_url)
    storage = LocalStorage(settings.storage_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    register_exception_handlers(app)
    # 中间件运行序（外层→内层）：RequestID → Logging → 路由。
    # Starlette add_middleware 为 insert(0) 语义（后加者在外层），故按目标运行序
    # 倒序添加；最终约定（外层→内层）：Metrics → RequestID → DeviceID →
    # RateLimit → Logging → 路由（Task 6/9/10 按目标序倒序追加）。
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(probes.router)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.storage = storage
    return app


app = create_app()
