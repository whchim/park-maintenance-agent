import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.ticket import Ticket
from app.models.workflow_event import WorkflowEvent
from app.models.workflow_session import WorkflowSession
from app.schemas.repair import RepairParseResult
from app.schemas.ticket import Priority, TicketCreate
from app.agents.repair_intake import run_intake
from app.services.engineering_agent_service import recommend_dispatch
from app.services.ticket_service import create_ticket_from_confirmed
from app.services.attachment_service import bind_images, link_workflow_ticket


WAITING_INPUT = "waiting_input"
WAITING_HUMAN = "waiting_human"
READY_FOR_SERVICE = "ready_for_service"
COMPLETED = "completed"
FAILED = "failed"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


def _serialize_parse(result: RepairParseResult) -> dict:
    return result.model_dump()


def _set_draft(session: WorkflowSession, parsed: RepairParseResult) -> None:
    session.draft_json = json.dumps(_serialize_parse(parsed), ensure_ascii=False)
    session.missing_fields_json = json.dumps(parsed.missing_fields, ensure_ascii=False)
    session.status = WAITING_INPUT if parsed.missing_fields else WAITING_HUMAN
    session.current_step = "repair_intake_review"
    session.updated_at = datetime.now()


def append_event(
    db: Session,
    *,
    session: WorkflowSession,
    event_type: str,
    source_agent: str,
    target_agent: str,
    payload: dict,
    idempotency_key: str,
    requires_human: bool = False,
    business_id: str | None = None,
    ticket_id: int | None = None,
) -> WorkflowEvent:
    existing = (
        db.query(WorkflowEvent)
        .filter(WorkflowEvent.idempotency_key == idempotency_key)
        .first()
    )
    if existing is not None:
        return existing

    event = WorkflowEvent(
        event_id=_new_id("evt"),
        workflow_id=session.workflow_id,
        event_type=event_type,
        source_agent=source_agent,
        target_agent=target_agent,
        business_id=business_id,
        ticket_id=ticket_id,
        payload_json=json.dumps(payload, ensure_ascii=False),
        workflow_status=session.status,
        requires_human=requires_human,
        idempotency_key=idempotency_key,
        status="completed",
    )
    db.add(event)
    return event


def record_ticket_workflow_event(
    db: Session,
    *,
    ticket: Ticket,
    event_type: str,
    source_agent: str,
    target_agent: str,
    payload: dict,
    status: str,
    current_step: str,
    requires_human: bool = False,
) -> WorkflowEvent | None:
    """将工单服务动作追加到所属 Supervisor 流程的同一事务。"""
    session = db.query(WorkflowSession).filter(
        WorkflowSession.ticket_id == ticket.id
    ).first()
    if session is None:
        return None

    occurred_at = datetime.now()
    session.status = status
    session.current_step = current_step
    session.updated_at = occurred_at
    return append_event(
        db,
        session=session,
        event_type=event_type,
        source_agent=source_agent,
        target_agent=target_agent,
        payload=payload,
        idempotency_key=f"ticket:{ticket.id}:{event_type}:{occurred_at.isoformat()}",
        requires_human=requires_human,
        business_id=str(ticket.id),
        ticket_id=ticket.id,
    )


def start_workflow(
    db: Session,
    *,
    message: str,
    channel: str = "web",
    requester_id: str | None = None,
    idempotency_key: str | None = None,
    attachment_ids: list[str] | None = None,
) -> WorkflowSession:
    if idempotency_key:
        existing_event = (
            db.query(WorkflowEvent)
            .filter(WorkflowEvent.idempotency_key == idempotency_key)
            .first()
        )
        if existing_event:
            return db.query(WorkflowSession).filter(
                WorkflowSession.workflow_id == existing_event.workflow_id
            ).one()

    if message.strip():
        parsed = run_intake(message)
    else:
        # 附图不能被当作已理解的文字，纯图片输入明确要求补项且不发起模型调用。
        parsed = RepairParseResult(description="已上传现场图片（尚未进行AI识图）",
                                   missing_fields=["location", "issue_type"],
                                   follow_up_question="已收到图片，请补充报修位置和故障现象；当前AI仅处理文字。")
    session = WorkflowSession(
        workflow_id=_new_id("wf"),
        channel=channel,
        requester_id=requester_id,
        original_message=message.strip(),
        status=WAITING_INPUT if parsed.missing_fields else WAITING_HUMAN,
        current_step="repair_intake_review",
        draft_json=json.dumps(_serialize_parse(parsed), ensure_ascii=False),
        missing_fields_json=json.dumps(parsed.missing_fields, ensure_ascii=False),
    )
    db.add(session)
    db.flush()
    bind_images(db, attachment_ids or [], purpose="intake", workflow_id=session.workflow_id)
    append_event(
        db,
        session=session,
        event_type="repair.intake.received",
        source_agent="feishu" if channel == "feishu" else "channel_adapter",
        target_agent="supervisor",
        payload={"message": message.strip(), "missing_fields": parsed.missing_fields, "attachment_ids": attachment_ids or []},
        idempotency_key=idempotency_key or f"{session.workflow_id}:received",
        requires_human=False,
    )
    append_event(
        db,
        session=session,
        event_type="repair.intake.reviewed",
        source_agent="service_agent",
        target_agent="企服_agent",
        payload={"draft": _serialize_parse(parsed), "mode": parsed.mode, "ai": parsed.ai},
        idempotency_key=f"{session.workflow_id}:reviewed:0",
        requires_human=not bool(parsed.missing_fields),
    )
    return session


def continue_workflow(
    db: Session,
    *,
    session: WorkflowSession,
    message: str,
    idempotency_key: str | None = None,
    attachment_ids: list[str] | None = None,
) -> WorkflowSession:
    if session.ticket_id:
        raise ValueError("工单已经创建，不能继续补充报修信息")
    if session.current_step != "repair_intake_review":
        raise ValueError("该流程不是报修受理流程")
    if idempotency_key:
        existing = db.query(WorkflowEvent).filter(
            WorkflowEvent.idempotency_key == idempotency_key
        ).first()
        if existing:
            if existing.workflow_id != session.workflow_id:
                raise ValueError("幂等键已用于其他流程")
            return session

    # 从已持久化消息恢复全部上下文；不能只拼接最初消息和最后一条而丢失中间补充。
    previous_messages = db.query(WorkflowEvent).filter(
        WorkflowEvent.workflow_id == session.workflow_id,
        WorkflowEvent.event_type == "repair.intake.supplemented",
    ).order_by(WorkflowEvent.id).all()
    bind_images(db, attachment_ids or [], purpose="intake", workflow_id=session.workflow_id)
    combined = "；".join(part for part in [session.original_message] + [event.payload["message"] for event in previous_messages] + [message.strip()] if part)
    # 只有补充了新文字才重新初审；单纯附图不改变已经核对的文字结论。
    parsed = run_intake(combined, message.strip()) if message.strip() else RepairParseResult(**session.draft)
    _set_draft(session, parsed)
    append_event(
        db,
        session=session,
        event_type="repair.intake.supplemented",
        source_agent="feishu" if session.channel == "feishu" else "channel_adapter",
        target_agent="supervisor",
        payload={"message": message.strip(), "missing_fields": parsed.missing_fields, "attachment_ids": attachment_ids or []},
        idempotency_key=idempotency_key or f"{session.workflow_id}:supplement:{session.updated_at.isoformat()}",
    )
    append_event(
        db,
        session=session,
        event_type="repair.intake.reviewed",
        source_agent="service_agent",
        target_agent="企服_agent",
        payload={"draft": _serialize_parse(parsed), "mode": parsed.mode, "ai": parsed.ai},
        idempotency_key=f"{session.workflow_id}:reviewed:{session.updated_at.isoformat()}",
        requires_human=not bool(parsed.missing_fields),
    )
    return session


def confirm_workflow_ticket(
    db: Session,
    *,
    session: WorkflowSession,
    location: str,
    issue_type: str,
    description: str,
    priority: Priority,
    idempotency_key: str,
) -> Ticket:
    existing_event = db.query(WorkflowEvent).filter(
        WorkflowEvent.idempotency_key == idempotency_key
    ).first()
    if existing_event and existing_event.workflow_id != session.workflow_id:
        raise ValueError("幂等键已用于其他流程")
    if existing_event and existing_event.ticket_id:
        return db.query(Ticket).filter(Ticket.id == existing_event.ticket_id).one()
    if session.ticket_id:
        return db.query(Ticket).filter(Ticket.id == session.ticket_id).one()
    if session.current_step != "repair_intake_review":
        raise ValueError("该流程不是报修受理流程")
    if session.status != WAITING_HUMAN or session.missing_fields:
        raise ValueError("报修信息尚未完整，不能确认建单")

    ticket = create_ticket_from_confirmed(
        db,
        TicketCreate(
            location=location,
            issue_type=issue_type,
            description=description,
            priority=priority,
        ),
        actor_type="企服_agent",
        reason="提交人确认结构化报修后创建工单",
    )
    db.flush()
    dispatch_recommendation = recommend_dispatch(db, ticket)
    session.ticket_id = ticket.id
    link_workflow_ticket(db, session.workflow_id, ticket.id)
    session.status = READY_FOR_SERVICE
    session.current_step = "engineering_handoff"
    session.confirmed_at = datetime.now()
    session.updated_at = datetime.now()
    append_event(
        db,
        session=session,
        event_type="repair.ticket.confirmed",
        source_agent="企服_agent",
        target_agent="ticket_service",
        payload={
            "ticket_id": ticket.id,
            "location": location,
            "issue_type": issue_type,
            "description": description,
            "priority": priority.value,
        },
        idempotency_key=idempotency_key,
        requires_human=True,
        business_id=str(ticket.id),
        ticket_id=ticket.id,
    )
    append_event(
        db,
        session=session,
        event_type="repair.assignment.requested",
        source_agent="supervisor",
        target_agent="工程_agent",
        payload={
            "ticket_id": ticket.id,
            "location": ticket.location,
            "issue_type": ticket.issue_type,
            "priority": ticket.priority,
            "recommendation": dispatch_recommendation,
        },
        idempotency_key=f"{session.workflow_id}:engineering-handoff",
        requires_human=True,
        business_id=str(ticket.id),
        ticket_id=ticket.id,
    )
    return ticket
