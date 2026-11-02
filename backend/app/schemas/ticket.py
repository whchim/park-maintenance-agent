from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field
from app.schemas.attachment import AttachmentId

# 使用 Enum 限制工单优先级，避免出现非法值
class Priority(str, Enum):
    NORMAL = "normal"       # 普通
    URGENT = "urgent"       # 加急
    CRITICAL = "critical"   # 紧急


class TicketStatus(str, Enum):
    PENDING = "pending"               # 待处理
    ACCEPTED = "accepted"             # 已接受
    PROCESSING = "processing"         # 处理中
    PENDING_REVIEW = "pending_review" # 待审核
    COMPLETED = "completed"           # 已完成

# 定义创建工单时需要接收的数据
class TicketCreate(BaseModel):
    # 报修地点，至少需要提供一个字符
    location: str = Field(min_length=1, description="报修地点")

    # 故障类型，例如漏水、空调故障
    issue_type: str = Field(min_length=1, description="故障类型")

    # 用户对故障情况的具体描述
    description: str = Field(min_length=1, description="故障描述")

    # 如果用户没有指定优先级，默认按普通工单处理
    priority: Priority = Priority.NORMAL
    attachment_ids: list[AttachmentId] = Field(default_factory=list, max_length=5)


class TicketResponse(BaseModel):
    # 允许 Pydantic 从 SQLAlchemy 对象的属性读取数据
    model_config = {"from_attributes": True}

    id: int                         
    ticket_no: str              # 工单编号                          
    location: str               # 报修地点
    issue_type: str             # 故障类型
    description: str            # 故障描述
    priority: Priority          # 工单优先级
    status: TicketStatus        # 工单状态
    assignee: str | None = None # 责任人  
    created_at: datetime        # 创建时间

    # 维修人员接单时间，尚未接单时为 None
    accepted_at: datetime | None = None

    # 维修人员提交处理结果的时间，尚未提交时为 None
    submitted_at: datetime | None = None

class TicketStatusUpdate(BaseModel):
    # 用户希望将工单更新为哪个状态
    status: TicketStatus


class TicketAcceptRequest(BaseModel):
    """工程人员确认接单时提交的身份。"""

    # 使用工程人员稳定编号，避免用显示名称作为身份凭证
    personnel_no: str = Field(min_length=1, description="接单工程人员编号")


class TicketStartProcessingRequest(BaseModel):
    """工程人员开始维修时提交的身份。"""

    # 开始维修必须由已经接单的工程人员本人触发
    personnel_no: str = Field(min_length=1, description="维修工程人员编号")
