from datetime import datetime

from pydantic import BaseModel, Field


class TicketAssignmentCreate(BaseModel):
    """分派工单时提交的工程人员编号和分派人。"""

    personnel_no: str = Field(min_length=1, description="工程人员编号")
    assigned_by: str = Field(min_length=1, description="分派人编号")


class TicketAssignmentResponse(BaseModel):
    """工单分派记录接口返回的数据。"""

    id: int
    ticket_id: int
    personnel_id: int
    personnel_no: str
    personnel_name: str
    assigned_by: str
    assigned_at: datetime
    is_current: bool
