"""为验收记录增加通过/退回标记，只新增字段，不修改既有记录。"""

from sqlalchemy import inspect, text

from app.database import engine


def migrate() -> None:
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("acceptance_records")
    }
    if "accepted" in columns:
        print("字段已存在，跳过：accepted")
        return

    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE acceptance_records "
                "ADD COLUMN accepted BOOLEAN NOT NULL DEFAULT 1"
            )
        )
    print("已新增字段：accepted")


if __name__ == "__main__":
    migrate()
