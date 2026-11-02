from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base


class Customer(Base):
    """招商 Agent 管理的客户意向/客户基础档案。"""

    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    customer_no = Column(String(40), unique=True, nullable=False, index=True)
    customer_name = Column(String(120), nullable=False)
    contact_name = Column(String(80), nullable=True)
    contact_phone = Column(String(40), nullable=True)
    stage = Column(String(30), nullable=False, default="lead")
    # 招商负责人确认的意向等级：A 每日、B 3 日、C 7 日回访
    intent_level = Column(String(1), nullable=True)
    source = Column(String(80), nullable=True)
    owner = Column(String(80), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
