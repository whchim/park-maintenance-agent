
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.database import Base


class Ticket(Base):
    # 指定数据库表名称
    __tablename__ = "tickets"

    # 主键，自动递增
    id = Column(Integer, primary_key=True, index=True)

    # 工单编号，不能重复
    ticket_no = Column(String(32), unique=True, index=True, nullable=False)

    # 报修地点
    location = Column(String(200), nullable=False)

    # 故障类型
    issue_type = Column(String(100), nullable=False)

    # 故障描述
    description = Column(String(1000), nullable=False)

    # 工单优先级
    priority = Column(String(20), nullable=False, default="normal")

    # 工单状态
    status = Column(String(30), nullable=False, default="pending")

    # 责任人，可以暂时为空
    assignee = Column(String(100), nullable=True)

    # 工单创建时间
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    # 工单更新时间
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )

    # 维修人员接单时间
    # 工单从 pending 变为 accepted 时记录
    accepted_at = Column(DateTime, nullable=True)

    # 维修人员提交处理结果的时间
    # 工单从 processing 变为 pending_review 时记录
    submitted_at = Column(DateTime, nullable=True)