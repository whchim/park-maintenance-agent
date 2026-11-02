from datetime import datetime

from pydantic import BaseModel


class TicketEventResponse(BaseModel):
    """工单时间线中的单条事件。"""

    model_config = {"from_attributes": True}

    id: int
    ticket_id: int
    from_status: str | None
    to_status: str
    actor_type: str
    actor_id: str | None
    reason: str | None
    occurred_at: datetime
    is_inferred: bool
