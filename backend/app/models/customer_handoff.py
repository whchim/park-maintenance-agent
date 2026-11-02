from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class CustomerHandoff(Base):
    """独立的签约客户联系单；每次提交的版本快照保存在流程事件中。"""

    __tablename__ = "customer_handoffs"

    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), unique=True, nullable=False)
    workflow_id = Column(String(40), ForeignKey("workflow_sessions.workflow_id"), unique=True, nullable=False)
    status = Column(String(30), nullable=False, default="draft")
    # 乐观锁版本号与正式联系单版本是不同概念，前者防止覆盖他人已修改的内容。
    revision = Column(Integer, nullable=False, default=1)
    document_json = Column(Text, nullable=False, default="{}")
    submitted_by = Column(String(100), nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    decided_by = Column(String(100), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decision_comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
