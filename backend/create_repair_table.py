from app.database import Base, engine
from app.models.ticket import Ticket
from app.models.repair import RepairRecord


def create_repair_table():
    """
    只创建维修记录表，不修改已有表结构。
    """

    RepairRecord.__table__.create(
        bind=engine,
        checkfirst=True,
    )

    print("维修记录表创建完成。")


if __name__ == "__main__":
    create_repair_table()