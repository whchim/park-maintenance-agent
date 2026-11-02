from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.schemas.attachment import AttachmentId, AttachmentResponse

from app.schemas.ticket import Priority, TicketResponse


class WorkflowStartRequest(BaseModel):
    """飞书或其他入口提交一次新的企服报修流程。"""

    message: str = Field(default="", max_length=20000, description="员工自然语言报修内容；仅附图时仍需补齐文字")
    attachment_ids: list[AttachmentId] = Field(default_factory=list, max_length=5)
    channel: str = Field(default="web", min_length=1, description="入口渠道，例如 feishu")
    requester_id: str | None = Field(default=None, description="入口侧稳定用户标识")
    idempotency_key: str | None = Field(default=None, description="入口重试使用的幂等键")

    @model_validator(mode="after")
    def require_content(self):
        if not self.message.strip() and not self.attachment_ids:
            raise ValueError("请提供报修文字或图片")
        return self


class FeishuRepairEventRequest(BaseModel):
    """飞书事件适配层的最小输入；真实 webhook 验签后再映射到此模型。"""

    event_id: str = Field(min_length=1, description="飞书事件唯一标识")
    open_id: str = Field(min_length=1, description="飞书用户稳定标识")
    text: str = Field(min_length=1, description="飞书消息文本")


class WorkflowMessageRequest(BaseModel):
    """补充 Supervisor 追问所需的信息。"""

    message: str = Field(default="", max_length=20000, description="对缺失信息的补充")
    attachment_ids: list[AttachmentId] = Field(default_factory=list, max_length=5)
    idempotency_key: str | None = Field(default=None, description="消息重试使用的幂等键")

    @model_validator(mode="after")
    def require_content(self):
        if not self.message.strip() and not self.attachment_ids:
            raise ValueError("请提供补充文字或图片")
        return self


class WorkflowConfirmRequest(BaseModel):
    """提交人确认结构化报修后，由授权人员确定优先级并建单。"""

    location: str = Field(min_length=1)
    issue_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    priority: Priority
    idempotency_key: str = Field(min_length=1, description="确认动作的幂等键")


class WorkflowEventResponse(BaseModel):
    event_id: str
    workflow_id: str
    event_type: str
    source_agent: str
    target_agent: str
    business_id: str | None
    ticket_id: int | None
    payload: dict
    workflow_status: str
    requires_human: bool
    idempotency_key: str
    status: str
    error: str | None
    occurred_at: datetime


class WorkflowResponse(BaseModel):
    workflow_id: str
    channel: str
    requester_id: str | None
    status: str
    current_step: str
    original_message: str
    draft: dict
    missing_fields: list[str]
    ticket_id: int | None
    ticket: TicketResponse | None = None
    events: list[WorkflowEventResponse]
    attachments: list[AttachmentResponse] = Field(default_factory=list)
