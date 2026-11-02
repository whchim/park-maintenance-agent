from datetime import datetime

from pydantic import BaseModel, Field


class CoordinationRequest(BaseModel):
    """Supervisor 接收的跨部门自然语言任务。"""

    message: str = Field(min_length=1, description="跨部门业务描述")


class AgentToolCallResponse(BaseModel):
    tool_name: str
    status: str
    input: dict
    output: object | None
    error: str | None
    started_at: datetime
    completed_at: datetime | None


class AgentStepResponse(BaseModel):
    run_id: str
    agent_name: str
    status: str
    input_text: str
    output: object | None
    error: str | None
    started_at: datetime
    completed_at: datetime | None
    tool_calls: list[AgentToolCallResponse]


class CoordinationResponse(BaseModel):
    run_id: str
    status: str
    mode: str
    summary: str
    intent: dict
    created_ticket: dict | None
    created_renovation_task: dict | None
    dispatch_recommendation: dict | None
    steps: list[AgentStepResponse]
