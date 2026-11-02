import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

# 默认数据库固定在 backend 目录；环境变量可用于测试或显式切换数据库。
DEFAULT_DATABASE_PATH = Path(__file__).resolve().parents[1] / "park_maintenance.db"
DATABASE_URL = os.getenv(
    "PARK_MAINTENANCE_DATABASE_URL",
    f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}",
)

# 创建数据库引擎，负责与 SQLite 建立连接
# check_same_thread=False 是 SQLite 配合 FastAPI 多线程请求时常用的设置
engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    **engine_kwargs,
)


if engine.url.get_backend_name() == "sqlite":
    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        """SQLite 默认按连接关闭外键约束，应用连接必须显式开启。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

# 创建数据库会话工厂
# 每次操作数据库时，通过它创建一个 Session
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

# 所有数据库模型都继承这个 Base
Base = declarative_base()


def get_db():
    """为每个请求提供数据库会话，并在请求结束后关闭。"""
    db = SessionLocal()

    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
