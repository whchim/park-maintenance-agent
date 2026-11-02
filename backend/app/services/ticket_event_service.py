from datetime import datetime

from sqlalchemy.orm import Session

from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent
from app.schemas.ticket import TicketStatus


def record_ticket_event(
    db: Session,
    ticket: Ticket,
    *,
    from_status: TicketStatus | str | None,
    to_status: TicketStatus | str,
    actor_type: str = "system",
    actor_id: str | None = None,
    reason: str | None = None,
    occurred_at: datetime | None = None,
    is_inferred: bool = False,
) -> TicketEvent:
    """将一次工单动作加入当前事务，提交由调用方统一完成。"""
    event = TicketEvent(
        ticket_id=ticket.id,
        from_status=(
            from_status.value
            if isinstance(from_status, TicketStatus)
            else from_status
        ),
        to_status=(
            to_status.value
            if isinstance(to_status, TicketStatus)
            else to_status
        ),
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason,
        occurred_at=occurred_at or datetime.now(),
        is_inferred=is_inferred,
    )
    db.add(event)
    return event
