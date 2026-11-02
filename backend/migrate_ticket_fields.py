from sqlalchemy import inspect, text

from app.database import engine


def migrate():
    # 检查数据库中 tickets 表当前有哪些字段
    inspector = inspect(engine)

    columns = {
        column["name"]
        for column in inspector.get_columns("tickets")
    }

    # 需要新增的字段
    new_columns = {
        "accepted_at": "DATETIME",
        "submitted_at": "DATETIME",
    }

    # 逐个检查，避免重复添加
    with engine.begin() as connection:
        for column_name, column_type in new_columns.items():

            if column_name not in columns:
                sql = (
                    f"ALTER TABLE tickets "
                    f"ADD COLUMN {column_name} {column_type}"
                )

                connection.execute(text(sql))

                print(f"已新增字段：{column_name}")
            else:
                print(f"字段已存在，跳过：{column_name}")

    print("数据库迁移完成！")


if __name__ == "__main__":
    migrate()