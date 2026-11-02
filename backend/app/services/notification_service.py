import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.models.ticket import Ticket


def create_critical_escalation(db: Session, ticket: Ticket, now: datetime | None = None) -> Notification | None:
    now = now or datetime.now()
    if ticket.priority != "critical" or ticket.accepted_at is not None or ticket.status != "pending":
        return None
    due_at = ticket.created_at + timedelta(minutes=5)
    if now < due_at:
        return None
    key = f"ticket:{ticket.id}:critical-no-response"
    existing = db.query(Notification).filter(Notification.idempotency_key == key).first()
    if existing:
        return existing
    notification = Notification(
        notification_no=f"NT{now:%Y%m%d%H%M%S}{ticket.id:04d}",
        ticket_id=ticket.id,
        kind="critical_phone_escalation",
        status="pending",
        recipient="工程负责人",
        reason="特急工单推送后 5 分钟未响应，需要授权人员电话对接",
        payload_json=json.dumps({
            "ticket_no": ticket.ticket_no,
            "location": ticket.location,
            "issue_type": ticket.issue_type,
            "priority": ticket.priority,
            "rule": "critical_no_response_5_minutes",
        }, ensure_ascii=False),
        idempotency_key=key,
        due_at=due_at,
        created_at=now,
    )
    db.add(notification)
    db.flush()
    return notification
