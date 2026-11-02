from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class RenovationTask(Base):
    """跨部门协同场景中的工程改造任务。"""

    __tablename__ = "renovation_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_no = Column(String(40), unique=True, nullable=False, index=True)
    location = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    status = Column(String(30), nullable=False, default="pending")
    source_run_id = Column(String(40), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
