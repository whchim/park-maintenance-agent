"""所有数据库都位于 pytest 临时目录，绝不使用本机业务库。"""

import pytest
from sqlalchemy import create_engine, inspect, text

from app.main import app  # 注册全部业务模型，不启动应用。
from app.database import Base
from app.demo_database import initialize_demo_database


def test_fresh_database_and_restart_preserve_records(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'demo.sqlite3').as_posix()}")
    try:
        initialize_demo_database(engine)
        assert set(Base.metadata.tables).issubset(inspect(engine).get_table_names())
        with engine.begin() as connection:
            assert connection.execute(text("SELECT count(*) FROM repair_personnel")).scalar() == 3
            connection.execute(text("UPDATE repair_personnel SET display_name='已修改的模拟员' WHERE personnel_no='ENG-DEMO-001'"))
        initialize_demo_database(engine)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM repair_personnel")).scalar() == 3
            assert connection.execute(text("SELECT display_name FROM repair_personnel WHERE personnel_no='ENG-DEMO-001'")).scalar() == "已修改的模拟员"
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()


def test_old_incompatible_database_is_unchanged(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'old.sqlite3').as_posix()}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE tickets (id INTEGER PRIMARY KEY, description TEXT)"))
            connection.execute(text("INSERT INTO tickets VALUES (1, '保留原数据')"))
        with pytest.raises(RuntimeError, match="结构不兼容"):
            initialize_demo_database(engine)
        assert inspect(engine).get_table_names() == ["tickets"]
        with engine.connect() as connection:
            assert connection.execute(text("SELECT * FROM tickets")).all() == [(1, "保留原数据")]
    finally:
        engine.dispose()
