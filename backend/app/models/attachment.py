"""渠道无关的现场证据元数据；图片本体由附件存储服务管理。"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.database import Base


class Attachment(Base):
    __tablename__ = "attachments"

    id = Column(String(32), primary_key=True)
    original_name = Column(String(180), nullable=False)
    media_type = Column(String(40), nullable=False)
    storage_key = Column(String(40), nullable=False, unique=True)
    size_bytes = Column(Integer, nullable=False)
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    purpose = Column(String(20), nullable=False)
    source_channel = Column(String(30), nullable=False, default="web")
    # 预留消息追溯字段，不能据此宣称已下载过飞书图片。
    source_reference = Column(String(200), nullable=True)
    workflow_id = Column(String(40), ForeignKey("workflow_sessions.workflow_id"), nullable=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=True, index=True)
    repair_record_id = Column(Integer, ForeignKey("repair_records.id"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
