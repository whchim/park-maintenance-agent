from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class FollowUpRecord(Base):
    """一次客户回访结果及其后续计划。"""

    __tablename__ = "follow_up_records"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("follow_up_tasks.id"), nullable=False, unique=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    actor = Column(String(80), nullable=False)
    outcome = Column(Text, nullable=False)
    next_task_id = Column(Integer, ForeignKey("follow_up_tasks.id"), nullable=True)
    idempotency_key = Column(String(120), unique=True, nullable=False, index=True)
    occurred_at = Column(DateTime, default=datetime.now, nullable=False)
