from app.database import SessionLocal, engine
from app.models.acceptance import AcceptanceRecord
from app.models.ticket import Ticket
from app.models.ticket_event import TicketEvent


def _add_event_if_missing(db, **values):
    existing = (
        db.query(TicketEvent)
        .filter(
            TicketEvent.ticket_id == values["ticket_id"],
            TicketEvent.to_status == values["to_status"],
            TicketEvent.occurred_at == values["occurred_at"],
        )
        .first()
    )
    if existing is None:
        db.add(TicketEvent(**values))


def migrate():
    """创建工单事件表并从现有可观察字段幂等回填历史事件。"""
    TicketEvent.__table__.create(
        bind=engine,
        checkfirst=True,
    )

    db = SessionLocal()
    try:
        tickets = db.query(Ticket).order_by(Ticket.id).all()

        for ticket in tickets:
            _add_event_if_missing(
                db,
                ticket_id=ticket.id,
                from_status=None,
                to_status="pending",
                actor_type="migration",
                reason="历史工单创建事件回填",
                occurred_at=ticket.created_at,
                is_inferred=False,
            )

            status = ticket.status
            accepted_at = ticket.accepted_at or ticket.updated_at
            inferred_acceptance = ticket.accepted_at is None

            if status in {"accepted", "processing", "pending_review", "completed"}:
                _add_event_if_missing(
                    db,
                    ticket_id=ticket.id,
                    from_status="pending",
                    to_status="accepted",
                    actor_type="migration",
                    reason="历史工单接单事件回填",
                    occurred_at=accepted_at,
                    is_inferred=inferred_acceptance,
                )

            if status in {"processing", "pending_review", "completed"}:
                _add_event_if_missing(
                    db,
                    ticket_id=ticket.id,
                    from_status="accepted",
                    to_status="processing",
                    actor_type="migration",
                    reason="历史工单处理中事件回填",
                    occurred_at=accepted_at,
                    is_inferred=True,
                )

            if status in {"pending_review", "completed"}:
                submitted_at = ticket.submitted_at or ticket.updated_at
                _add_event_if_missing(
                    db,
                    ticket_id=ticket.id,
                    from_status="processing",
                    to_status="pending_review",
                    actor_type="migration",
                    reason="历史工单待验收事件回填",
                    occurred_at=submitted_at,
                    is_inferred=ticket.submitted_at is None,
                )

            if status == "completed":
                acceptance = (
                    db.query(AcceptanceRecord)
                    .filter(AcceptanceRecord.ticket_id == ticket.id)
                    .order_by(AcceptanceRecord.accepted_at.desc())
                    .first()
                )
                completed_at = acceptance.accepted_at if acceptance else ticket.updated_at
                _add_event_if_missing(
                    db,
                    ticket_id=ticket.id,
                    from_status="pending_review",
                    to_status="completed",
                    actor_type="migration",
                    actor_id=acceptance.accepted_by if acceptance else None,
                    reason="历史工单完成事件回填",
                    occurred_at=completed_at,
                    is_inferred=acceptance is None,
                )

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
