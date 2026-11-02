import json
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class Notification(Base):
    """系统提醒/电话对接待办；外部发送由后续渠道适配层负责。"""

    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    notification_no = Column(String(40), unique=True, nullable=False, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=True, index=True)
    kind = Column(String(50), nullable=False)
    status = Column(String(30), nullable=False, default="pending")
    recipient = Column(String(100), nullable=True)
    reason = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    contact_result = Column(Text, nullable=True)
    contacted_by = Column(String(100), nullable=True)
    idempotency_key = Column(String(120), unique=True, nullable=False, index=True)
    due_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json or "{}")
