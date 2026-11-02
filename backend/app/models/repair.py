from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class RepairRecord(Base):
    """
    维修记录表。

    一张工单可以对应多条维修记录，
    用于保存每次维修提交的说明和照片。
    """

    __tablename__ = "repair_records"

    # 维修记录主键
    id = Column(Integer, primary_key=True, index=True)

    # 关联工单表的主键
    ticket_id = Column(
        Integer,
        ForeignKey("tickets.id"),
        nullable=False,
        index=True,
    )

    # 维修处理说明
    repair_description = Column(Text, nullable=False)

    # 照片路径，暂时使用字符串保存
    # 多张照片之间用 JSON 格式存储
    photo_urls = Column(Text, nullable=True)

    # 维修结果提交时间
    submitted_at = Column(
        DateTime,
        default=datetime.now,
        nullable=False,
    )