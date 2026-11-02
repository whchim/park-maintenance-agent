import json
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class WorkflowEvent(Base):
    """部门 Agent 与业务 Service 之间的结构化交接事件。"""

    __tablename__ = "workflow_events"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String(40), unique=True, nullable=False, index=True)
    workflow_id = Column(String(40), nullable=False, index=True)
    event_type = Column(String(80), nullable=False, index=True)
    source_agent = Column(String(80), nullable=False)
    target_agent = Column(String(80), nullable=False)
    business_id = Column(String(100), nullable=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=True, index=True)
    payload_json = Column(Text, nullable=False, default="{}")
    workflow_status = Column(String(30), nullable=False)
    requires_human = Column(Boolean, nullable=False, default=False)
    idempotency_key = Column(String(120), unique=True, nullable=False, index=True)
    status = Column(String(20), nullable=False, default="completed")
    error = Column(Text, nullable=True)
    occurred_at = Column(DateTime, default=datetime.now, nullable=False, index=True)

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json or "{}")
