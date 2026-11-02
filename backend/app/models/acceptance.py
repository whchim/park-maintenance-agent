from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class AcceptanceRecord(Base):
    """
    企服验收记录模型。

    一条记录对应一次工单验收。
    保存验收意见、验收人员和验收时间。
    """

    __tablename__ = "acceptance_records"

    # 验收记录主键
    id = Column(Integer, primary_key=True, index=True)

    # 关联工单 ID
    ticket_id = Column(
        Integer,
        ForeignKey("tickets.id"),
        nullable=False,
        index=True,
    )

    # 企服填写的验收意见
    acceptance_comment = Column(Text, nullable=False)

    # 验收人员：当前 Demo 没有登录系统，因此允许为空
    accepted_by = Column(String(100), nullable=True)

    # False 表示验收未通过并退回工程部处理
    accepted = Column(Boolean, nullable=False, default=True)

    # 验收时间
    accepted_at = Column(DateTime, default=datetime.now, nullable=False)
