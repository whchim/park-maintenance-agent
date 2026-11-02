from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class FollowUpTask(Base):
    """招商 Agent 生成的客户跟进任务。"""

    __tablename__ = "follow_up_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_no = Column(String(40), unique=True, nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    subject = Column(String(160), nullable=False)
    description = Column(Text, nullable=False)
    owner = Column(String(80), nullable=True)
    status = Column(String(30), nullable=False, default="pending")
    idempotency_key = Column(String(120), unique=True, nullable=False, index=True)
    due_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
