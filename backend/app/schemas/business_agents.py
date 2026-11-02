from datetime import datetime

from pydantic import BaseModel, Field


class AgentCapabilityResponse(BaseModel):
    agent_name: str
    department: str
    status: str
    capabilities: list[str]
    write_guards: list[str]


class InvestmentIntakeRequest(BaseModel):
    message: str = Field(min_length=1, description="客户/渠道自然语言记录")
    source: str | None = None
    owner: str | None = None


class InvestmentDraftResponse(BaseModel):
    mode: str
    customer_name: str | None
    contact_name: str | None
    contact_phone: str | None
    source: str | None
    intent_level: str | None
    follow_up_interval_days: int | None
    missing_fields: list[str]
    follow_up_subject: str
    follow_up_description: str
    ai: dict = Field(default_factory=dict)
    evidence: dict[str, str] = Field(default_factory=dict)
    intent_reason: str | None = None


class InvestmentConfirmRequest(BaseModel):
    customer_name: str = Field(min_length=1)
    contact_name: str | None = None
    contact_phone: str | None = None
    source: str | None = None
    owner: str | None = None
    intent_level: str = Field(pattern="^[ABC]$")
    follow_up_subject: str = Field(min_length=1)
    follow_up_description: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class InvestmentConfirmResponse(BaseModel):
    workflow_id: str
    customer: dict
    follow_up_task: dict
    handoff_event_id: str | None


class FollowUpCompleteRequest(BaseModel):
    actor: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class FollowUpCompleteResponse(BaseModel):
    record: dict
    next_task: dict | None


class EscalationResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    notification_no: str
    ticket_id: int
    kind: str
    status: str
    recipient: str | None
    reason: str
    contacted_by: str | None
    contact_result: str | None
    due_at: datetime | None
    created_at: datetime


class EscalationResolutionRequest(BaseModel):
    contacted_by: str = Field(min_length=1)
    contact_result: str = Field(min_length=1)
