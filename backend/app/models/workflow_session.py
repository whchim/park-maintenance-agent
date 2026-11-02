import json
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class WorkflowSession(Base):
    """保存 Supervisor 可暂停、追问和恢复的业务流程上下文。"""

    __tablename__ = "workflow_sessions"

    id = Column(Integer, primary_key=True, index=True)
    workflow_id = Column(String(40), unique=True, nullable=False, index=True)
    channel = Column(String(30), nullable=False, default="web")
    requester_id = Column(String(100), nullable=True)
    status = Column(String(30), nullable=False, default="waiting_input")
    current_step = Column(String(50), nullable=False, default="repair_intake")
    original_message = Column(Text, nullable=False)
    draft_json = Column(Text, nullable=False, default="{}")
    missing_fields_json = Column(Text, nullable=False, default="[]")
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=True, index=True)
    confirmed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    @property
    def draft(self) -> dict:
        return json.loads(self.draft_json or "{}")

    @property
    def missing_fields(self) -> list[str]:
        return json.loads(self.missing_fields_json or "[]")
