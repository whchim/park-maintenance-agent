from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class TicketEvent(Base):
    """工单状态和关键业务动作的审计记录。"""

    __tablename__ = "ticket_events"

    # 事件主键
    id = Column(Integer, primary_key=True, index=True)

    # 关联工单
    ticket_id = Column(
        Integer,
        ForeignKey("tickets.id"),
        nullable=False,
        index=True,
    )

    # 状态变化前后的值；创建事件没有 from_status
    from_status = Column(String(30), nullable=True)
    to_status = Column(String(30), nullable=False)

    # 操作来源和操作人，当前 Demo 可使用 system 或 agent
    actor_type = Column(String(30), nullable=False, default="system")
    actor_id = Column(String(100), nullable=True)

    # 变更原因或业务说明
    reason = Column(Text, nullable=True)

    # 事件发生时间
    occurred_at = Column(DateTime, default=datetime.now, nullable=False, index=True)

    # 历史回填无法确认精确时间时标记为推断事件
    is_inferred = Column(Boolean, nullable=False, default=False)
