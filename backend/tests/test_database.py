from pathlib import Path

from app.database import DEFAULT_DATABASE_PATH, engine


def test_database_path_is_fixed_to_backend_directory():
    """默认数据库路径不依赖启动命令的当前工作目录。"""
    assert Path(engine.url.database).resolve() == DEFAULT_DATABASE_PATH.resolve()


def test_sqlite_foreign_keys_are_enabled():
    """应用建立的 SQLite 连接必须启用外键约束。"""
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
