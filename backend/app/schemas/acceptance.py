from datetime import datetime

from pydantic import BaseModel, Field


class AcceptanceRecordCreate(BaseModel):
    """企服提交验收时需要提供的数据。"""

    acceptance_comment: str = Field(
        min_length=1,
        description="企服验收意见",
    )

    accepted_by: str | None = Field(
        default=None,
        description="验收人员",
    )

    accepted: bool = Field(default=True, description="是否验收通过；不通过将工单退回处理中")


class AcceptanceRecordResponse(BaseModel):
    """验收成功后返回的数据。"""

    id: int
    ticket_id: int
    acceptance_comment: str
    accepted_by: str | None
    accepted: bool = True
    accepted_at: datetime
