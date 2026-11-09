"""持久化演示入口；仅允许由带密码保护的网关访问，不公开后端端口。"""

from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import APIRouter, FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.demo_database import initialize_demo_database


def create_app():
    # 无默认数据路径，防止意外连接开发者现有业务数据库。
    data_dir = Path(os.environ["PARK_DEMO_DATA_DIR"])
    if not data_dir.is_absolute():
        raise RuntimeError("PARK_DEMO_DATA_DIR 必须是独立演示数据目录的绝对路径")
    data_dir = data_dir.resolve()
    try:
        limit = int(os.environ["PARK_MODEL_DAILY_LIMIT"])
        minute_limit = int(os.getenv("PARK_MODEL_PER_MINUTE_LIMIT", "10"))
        if limit < 1 or minute_limit < 1:
            raise ValueError
        budget = Path(os.environ["PARK_MODEL_BUDGET_DB"]).resolve()
        if budget.parent != data_dir or budget.name == "park.db":
            raise ValueError
    except (KeyError, ValueError):
        raise RuntimeError("请配置正整数模型日/分钟额度及数据目录内独立的 PARK_MODEL_BUDGET_DB") from None

    # 注册全部模型和业务路由；不继承本机应用的数据库依赖覆盖。
    from app.main import app as business_app
    from app.services.attachment_service import ImageStore

    @asynccontextmanager
    async def lifespan(app):
        data_dir.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{(data_dir / 'park.db').as_posix()}",
                               connect_args={"check_same_thread": False, "timeout": 10})

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

        factory = sessionmaker(bind=engine, autoflush=False)
        try:
            initialize_demo_database(engine)

            def deployment_db():
                with factory() as db:
                    try:
                        yield db
                    except Exception:
                        db.rollback()
                        raise

            app.dependency_overrides[get_db] = deployment_db
            app.state.image_store = ImageStore(data_dir / "uploads")
            yield
        finally:
            app.dependency_overrides.pop(get_db, None)
            engine.dispose()

    app = FastAPI(title="园区智维 · 求职演示", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.environment = {"name": "portfolio_demo", "synthetic_data": True, "volatile": False}
    router = APIRouter()
    hidden_docs = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
    # 保留 include_router 产生的嵌套路由；新版本FastAPI不一定将其立即展开为APIRoute。
    router.routes.extend(route for route in business_app.router.routes
                         if getattr(route, "path", None) not in hidden_docs)
    app.include_router(router)
    return app
