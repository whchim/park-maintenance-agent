from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String

from app.database import Base


class TicketAssignment(Base):
    """工单责任人分派记录。"""

    __tablename__ = "ticket_assignments"

    # 分派记录主键
    id = Column(Integer, primary_key=True, index=True)

    # 关联工单和工程人员
    ticket_id = Column(
        Integer,
        ForeignKey("tickets.id"),
        nullable=False,
        index=True,
    )
    personnel_id = Column(
        Integer,
        ForeignKey("repair_personnel.id"),
        nullable=False,
        index=True,
    )

    # 分派操作人使用稳定人员编号；后续可替换为登录用户外键
    assigned_by = Column(String(30), nullable=False)
    assigned_at = Column(DateTime, default=datetime.now, nullable=False)

    # 重新分派时保留历史记录，只保留一条当前记录
    is_current = Column(Boolean, nullable=False, default=True)
