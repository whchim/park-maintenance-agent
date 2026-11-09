from datetime import datetime
import json
from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.ticket import Ticket
from app.models.repair import RepairRecord
from app.models.acceptance import AcceptanceRecord
from app.models.ticket_event import TicketEvent
from app.models.repair_personnel import RepairPersonnel
from app.models.ticket_assignment import TicketAssignment
from app.models.agent_run import AgentRun, AgentToolCall
from app.models.renovation_task import RenovationTask
from app.models.workflow_session import WorkflowSession
from app.models.workflow_event import WorkflowEvent
from app.models.customer import Customer
from app.models.follow_up_task import FollowUpTask
from app.models.follow_up_record import FollowUpRecord
from app.models.notification import Notification
from app.schemas.agent import CoordinationRequest, CoordinationResponse
from app.schemas.business_agents import (
    AgentCapabilityResponse,
    EscalationResolutionRequest,
    EscalationResponse,
    InvestmentConfirmRequest,
    InvestmentConfirmResponse,
    InvestmentDraftResponse,
    InvestmentIntakeRequest,
    FollowUpCompleteRequest,
    FollowUpCompleteResponse,
)
from app.schemas.workflow import (
    WorkflowConfirmRequest,
    WorkflowEventResponse,
    FeishuRepairEventRequest,
    WorkflowMessageRequest,
    WorkflowResponse,
    WorkflowStartRequest,
)
from app.schemas.ticket import (
    TicketAcceptRequest,
    TicketCreate,
    TicketResponse,
    TicketStatus,
    TicketStartProcessingRequest,
    TicketStatusUpdate,  # 接收状态更新请求
)
# 接收自然语言报修请求，并返回结构化解析结果
from app.schemas.repair import (
    RepairParseRequest,
    RepairParseResult,
    RepairRecordCreate,
    RepairRecordResponse,
)

from app.schemas.acceptance import (
    AcceptanceRecordCreate,
    AcceptanceRecordResponse,
)
from app.schemas.ticket_event import TicketEventResponse
from app.schemas.repair_personnel import RepairPersonnelResponse
from app.schemas.ticket_assignment import (
    TicketAssignmentCreate,
    TicketAssignmentResponse,
)

# 执行自然语言报修解析
from app.services.repair_service import (
    parse_repair_text,
    create_repair_record,
)

from app.services.ticket_service import (
    accept_ticket,
    create_ticket_from_confirmed,
    start_processing_ticket,
    transition_ticket_with_event,
)
from app.services.sla_service import check_ticket_sla  # 检查 SLA 超时
from app.services.acceptance_service import create_acceptance_record # 创建验收记录
from app.services.repair_personnel_service import list_active_personnel
from app.services.ticket_assignment_service import (
    assign_ticket,
    assignment_response,
)
from app.services.agent_service import coordinate_request
from app.services.workflow_service import (
    confirm_workflow_ticket,
    continue_workflow,
    record_ticket_workflow_event,
    start_workflow,
)
from app.services.feishu_service import parse_message_event, verify_callback
from app.services.business_agent_service import (
    capability_catalog,
    complete_follow_up,
    confirm_investment_intake,
    parse_investment_intake,
)
from app.services.notification_service import create_critical_escalation
from app.api.customer_handoffs import router as customer_handoff_router
from app.services.department_agent_service import review_repair
from app.services.engineering_agent_service import recommend_dispatch
from app.services.llm_service import runtime_status
from app.api.attachments import router as attachment_router
from app.services.attachment_service import bind_images, list_images

app = FastAPI(
title="园区智维 Agent",
description="园区运营协同系统，支持通义千问可配置接入，默认使用规则演示",
version="0.1.0",
)
app.include_router(customer_handoff_router)
app.include_router(attachment_router)


@app.get("/api/v1/agents/runtime")
def get_agent_runtime(request: Request):
    """只检查安全的配置状态，不发起模型调用或泄露密钥。"""
    result = runtime_status()
    result["api_features"] = ["image_attachments", "repair_feedback_read"]
    if environment := getattr(request.app.state, "environment", None):
        result["environment"] = environment
    return result


@app.post("/api/v1/tickets/{ticket_id}/dispatch-suggestion")
def suggest_ticket_dispatch(ticket_id: int, db: Session = Depends(get_db)):
    """独立的只读建议请求，避免在建单写事务内等待远端大模型。"""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.status != TicketStatus.PENDING.value:
        raise HTTPException(status_code=409, detail="只有待分派工单可以生成委派建议")
    return recommend_dispatch(db, ticket, use_ai=True)


@app.get("/api/v1/agents/capabilities", response_model=list[AgentCapabilityResponse])
def get_agent_capabilities():
    """返回 Supervisor 和三个部门 Agent 的当前能力边界。"""
    return capability_catalog()


@app.post("/api/v1/agents/investment/intake", response_model=InvestmentDraftResponse)
def investment_intake(request: InvestmentIntakeRequest):
    """招商 Agent 只生成客户和跟进草稿，不写业务数据。"""
    return parse_investment_intake(request.message, request.source)


@app.post("/api/v1/agents/investment/confirm", response_model=InvestmentConfirmResponse)
def confirm_investment(request: InvestmentConfirmRequest, db: Session = Depends(get_db)):
    """招商人员确认意向草稿后创建客户和跟进任务；签约交接单独办理。"""
    try:
        customer, task, event = confirm_investment_intake(db, request)
        db.commit()
        db.refresh(customer)
        db.refresh(task)
        return {
            "workflow_id": event.workflow_id if event else "",
            "customer": {
                "id": customer.id,
                "customer_no": customer.customer_no,
                "customer_name": customer.customer_name,
                "contact_name": customer.contact_name,
                "contact_phone": customer.contact_phone,
                "stage": customer.stage,
                "intent_level": customer.intent_level,
                "source": customer.source,
                "owner": customer.owner,
            },
            "follow_up_task": {
                "id": task.id,
                "task_no": task.task_no,
                "customer_id": task.customer_id,
                "subject": task.subject,
                "status": task.status,
                "owner": task.owner,
                "due_at": task.due_at,
            },
            "handoff_event_id": None,
        }
    except Exception:
        db.rollback()
        raise


@app.get("/api/v1/agents/investment/customers")
def list_investment_customers(db: Session = Depends(get_db)):
    return db.query(Customer).order_by(Customer.created_at.desc()).all()


@app.get("/api/v1/agents/investment/follow-ups")
def list_investment_follow_ups(db: Session = Depends(get_db)):
    rows = db.query(FollowUpTask, Customer).join(
        Customer, FollowUpTask.customer_id == Customer.id
    ).filter(
        FollowUpTask.status == "pending"
    ).order_by(FollowUpTask.due_at.asc(), FollowUpTask.id.asc()).all()
    return [{
        "id": task.id, "task_no": task.task_no, "customer_id": customer.id,
        "customer_name": customer.customer_name, "intent_level": customer.intent_level,
        "subject": task.subject, "description": task.description, "owner": task.owner,
        "status": task.status, "due_at": task.due_at,
    } for task, customer in rows]


@app.get("/api/v1/agents/investment/customers/{customer_id}/follow-up-records")
def list_customer_follow_up_records(customer_id: int, db: Session = Depends(get_db)):
    if db.query(Customer).filter(Customer.id == customer_id).first() is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    return db.query(FollowUpRecord).filter(
        FollowUpRecord.customer_id == customer_id
    ).order_by(FollowUpRecord.occurred_at.desc(), FollowUpRecord.id.desc()).all()


@app.post(
    "/api/v1/agents/investment/follow-ups/{task_id}/complete",
    response_model=FollowUpCompleteResponse,
)
def complete_investment_follow_up(
    task_id: int,
    request: FollowUpCompleteRequest,
    db: Session = Depends(get_db),
):
    try:
        record, next_task = complete_follow_up(
            db,
            task_id=task_id,
            actor=request.actor,
            outcome=request.outcome,
            idempotency_key=request.idempotency_key,
        )
        db.commit()
        db.refresh(record)
        if next_task:
            db.refresh(next_task)
        return {
            "record": {
                "id": record.id,
                "task_id": record.task_id,
                "customer_id": record.customer_id,
                "actor": record.actor,
                "outcome": record.outcome,
                "occurred_at": record.occurred_at,
            },
            "next_task": ({
                "id": next_task.id,
                "task_no": next_task.task_no,
                "subject": next_task.subject,
                "status": next_task.status,
                "due_at": next_task.due_at,
            } if next_task else None),
        }
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@app.post("/api/v1/tickets/{ticket_id}/escalation/scan", response_model=EscalationResponse | None)
def scan_ticket_escalation(ticket_id: int, db: Session = Depends(get_db)):
    """由调度器或管理人员触发 SLA 扫描；重复扫描不会重复建待办。"""
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    try:
        notification = create_critical_escalation(db, ticket)
        if notification is None:
            db.commit()
            return None
        record_ticket_workflow_event(
            db,
            ticket=ticket,
            event_type="repair.escalation.required",
            source_agent="sla_service",
            target_agent="工程_agent",
            payload={"notification_no": notification.notification_no, "reason": notification.reason},
            status="waiting_engineering",
            current_step="critical_phone_escalation",
            requires_human=True,
        )
        db.commit()
        db.refresh(notification)
        return notification
    except Exception:
        db.rollback()
        raise


@app.post("/api/v1/escalations/scan", response_model=list[EscalationResponse])
def scan_critical_escalations(db: Session = Depends(get_db)):
    """供定时调度器调用，批量生成已超过 5 分钟且仍未响应的特急电话待办。"""
    now = datetime.now()
    tickets = db.query(Ticket).filter(
        Ticket.priority == "critical",
        Ticket.accepted_at.is_(None),
    ).all()
    created = []
    try:
        for ticket in tickets:
            existing = db.query(Notification).filter(
                Notification.idempotency_key == f"ticket:{ticket.id}:critical-no-response"
            ).first()
            if existing is not None:
                continue
            notification = create_critical_escalation(db, ticket, now=now)
            if notification is None:
                continue
            record_ticket_workflow_event(
                db,
                ticket=ticket,
                event_type="repair.escalation.required",
                source_agent="sla_service",
                target_agent="工程_agent",
                payload={"notification_no": notification.notification_no, "reason": notification.reason},
                status="waiting_engineering",
                current_step="critical_phone_escalation",
                requires_human=True,
            )
            created.append(notification)
        db.commit()
        for notification in created:
            db.refresh(notification)
        return created
    except Exception:
        db.rollback()
        raise


@app.get("/api/v1/notifications/pending", response_model=list[EscalationResponse])
def list_pending_notifications(db: Session = Depends(get_db)):
    return db.query(Notification).filter(Notification.status == "pending").order_by(Notification.created_at.asc()).all()


@app.post("/api/v1/notifications/{notification_id}/resolve", response_model=EscalationResponse)
def resolve_notification(
    notification_id: int,
    request: EscalationResolutionRequest,
    db: Session = Depends(get_db),
):
    notification = db.query(Notification).filter(Notification.id == notification_id).first()
    if notification is None:
        raise HTTPException(status_code=404, detail="电话对接待办不存在")
    if notification.status != "pending":
        raise HTTPException(status_code=409, detail="待办已经处理")
    notification.status = "completed"
    notification.contacted_by = request.contacted_by
    notification.contact_result = request.contact_result
    notification.completed_at = datetime.now()
    if notification.ticket_id:
        ticket = db.query(Ticket).filter(Ticket.id == notification.ticket_id).first()
        if ticket is not None:
            record_ticket_workflow_event(
                db,
                ticket=ticket,
                event_type="repair.escalation.contact_recorded",
                source_agent="工程负责人",
                target_agent="supervisor",
                payload={
                    "notification_no": notification.notification_no,
                    "contacted_by": request.contacted_by,
                    "contact_result": request.contact_result,
                },
                status="waiting_engineering",
                current_step="engineering_follow_up",
                requires_human=False,
            )
    db.commit()
    db.refresh(notification)
    return notification


def _workflow_response(db: Session, session: WorkflowSession) -> dict:
    ticket = db.query(Ticket).filter(Ticket.id == session.ticket_id).first() if session.ticket_id else None
    events = db.query(WorkflowEvent).filter(
        WorkflowEvent.workflow_id == session.workflow_id
    ).order_by(WorkflowEvent.id.asc()).all()
    return {
        "workflow_id": session.workflow_id,
        "channel": session.channel,
        "requester_id": session.requester_id,
        "status": session.status,
        "current_step": session.current_step,
        "original_message": session.original_message,
        "draft": session.draft,
        "missing_fields": session.missing_fields,
        "ticket_id": session.ticket_id,
        "ticket": ticket,
        "attachments": list_images(db, workflow_id=session.workflow_id),
        "events": [
            {
                "event_id": event.event_id,
                "workflow_id": event.workflow_id,
                "event_type": event.event_type,
                "source_agent": event.source_agent,
                "target_agent": event.target_agent,
                "business_id": event.business_id,
                "ticket_id": event.ticket_id,
                "payload": event.payload,
                "workflow_status": event.workflow_status,
                "requires_human": event.requires_human,
                "idempotency_key": event.idempotency_key,
                "status": event.status,
                "error": event.error,
                "occurred_at": event.occurred_at,
            }
            for event in events
        ],
    }


@app.post("/api/v1/agent/runs", response_model=WorkflowResponse)
def start_agent_workflow(request: WorkflowStartRequest, db: Session = Depends(get_db)):
    try:
        session = start_workflow(
            db,
            message=request.message,
            channel=request.channel,
            requester_id=request.requester_id,
            idempotency_key=request.idempotency_key,
            attachment_ids=request.attachment_ids,
        )
        db.commit()
        db.refresh(session)
        return _workflow_response(db, session)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@app.post("/api/v1/integrations/feishu/repair-events", response_model=WorkflowResponse)
def receive_feishu_repair_event(
    request: FeishuRepairEventRequest,
    db: Session = Depends(get_db),
):
    """将飞书办公消息转换为统一 Supervisor 流程输入。"""
    try:
        session = start_workflow(
            db,
            message=request.text,
            channel="feishu",
            requester_id=request.open_id,
            idempotency_key=request.event_id,
        )
        db.commit()
        db.refresh(session)
        return _workflow_response(db, session)
    except Exception:
        db.rollback()
        raise


@app.post("/api/v1/integrations/feishu/events")
async def receive_feishu_event(request: Request, db: Session = Depends(get_db)):
    """接收飞书开放平台事件，并将文本消息交给 Supervisor。"""
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="飞书事件不是有效 JSON") from exc

    headers = {key.lower(): value for key, value in request.headers.items()}
    if not verify_callback(payload, raw_body, headers):
        raise HTTPException(status_code=403, detail="飞书事件校验失败")

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    event = parse_message_event(payload)
    if event is None:
        return {"status": "ignored"}

    try:
        session = start_workflow(
            db,
            message=event["text"],
            channel="feishu",
            requester_id=event["open_id"],
            idempotency_key=event["event_id"],
        )
        db.commit()
        db.refresh(session)
        return _workflow_response(db, session)
    except Exception:
        db.rollback()
        raise


@app.post("/api/v1/agent/runs/{workflow_id}/messages", response_model=WorkflowResponse)
def continue_agent_workflow(
    workflow_id: str,
    request: WorkflowMessageRequest,
    db: Session = Depends(get_db),
):
    session = db.query(WorkflowSession).filter(WorkflowSession.workflow_id == workflow_id).first()
    if session is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    try:
        continue_workflow(db, session=session, message=request.message, idempotency_key=request.idempotency_key,
                          attachment_ids=request.attachment_ids)
        db.commit()
        db.refresh(session)
        return _workflow_response(db, session)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/agent/runs/{workflow_id}/confirm-ticket", response_model=WorkflowResponse)
def confirm_agent_workflow_ticket(
    workflow_id: str,
    request: WorkflowConfirmRequest,
    db: Session = Depends(get_db),
):
    session = db.query(WorkflowSession).filter(WorkflowSession.workflow_id == workflow_id).first()
    if session is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    try:
        confirm_workflow_ticket(
            db,
            session=session,
            location=request.location,
            issue_type=request.issue_type,
            description=request.description,
            priority=request.priority,
            idempotency_key=request.idempotency_key,
        )
        db.commit()
        db.refresh(session)
        return _workflow_response(db, session)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

# 首页接口
@app.get("/")
def read_root():
    return {
    "message": "园区智维 Agent 已启动",
    "version": "0.1.0",
    }

# 健康检查接口
@app.get("/health")
def health_check():
    return {
    "status": "ok",
    "service": "park-maintenance-agent",
    }


# 查询当前可参与任务分配的工程人员目录
@app.get(
    "/api/v1/repair-personnel",
    response_model=list[RepairPersonnelResponse],
)
def list_repair_personnel(db: Session = Depends(get_db)):
    return list_active_personnel(db)

# 自然语言报修解析接口
@app.post(
    "/api/v1/repairs/parse",
    response_model=RepairParseResult,
)
def parse_repair(request: RepairParseRequest):
    """
    接收用户的自然语言报修描述，
    返回位置、故障类型和优先级建议。
    """

    # 调用报修解析服务，获取结构化结果
    result = review_repair(request.text)

    return result


# Supervisor 协调 Agent：编排专业 Agent 并返回真实工具调用轨迹
@app.post(
    "/api/v1/agent/coordinate",
    response_model=CoordinationResponse,
)
def coordinate_agent_request(
    request: CoordinationRequest,
    db: Session = Depends(get_db),
):
    try:
        result = coordinate_request(db, request.message)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# 查询已保存的 Agent 执行过程
@app.get("/api/v1/agent/runs/{run_id}")
def get_agent_run(run_id: str, db: Session = Depends(get_db)):
    workflow = db.query(WorkflowSession).filter(
        WorkflowSession.workflow_id == run_id
    ).first()
    if workflow is not None:
        return _workflow_response(db, workflow)

    runs = (
        db.query(AgentRun)
        .filter(AgentRun.run_id == run_id)
        .order_by(AgentRun.id.asc())
        .all()
    )
    if not runs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent 执行记录不存在")

    results = []
    for run in runs:
        calls = (
            db.query(AgentToolCall)
            .filter(AgentToolCall.agent_run_id == run.id)
            .order_by(AgentToolCall.id.asc())
            .all()
        )
        results.append({
            "run_id": run.run_id,
            "agent_name": run.agent_name,
            "status": run.status,
            "input_text": run.input_text,
            "output": json.loads(run.output_json) if run.output_json else None,
            "error": run.error,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "tool_calls": [{
                "tool_name": call.tool_name,
                "status": call.status,
                "input": json.loads(call.input_json),
                "output": json.loads(call.output_json) if call.output_json else None,
                "error": call.error,
                "started_at": call.started_at,
                "completed_at": call.completed_at,
            } for call in calls],
        })
    return results

# 工单创建接口
@app.post(
    "/api/v1/tickets",
    response_model=TicketResponse,
    status_code=status.HTTP_201_CREATED,
)

def create_ticket(
    ticket: TicketCreate,
    db: Session = Depends(get_db),
):
    """保存人工确认的报修信息，与 Web/飞书协同确认共用建单规则。"""
    try:
        # 公共服务生成编号及创建事件；附件绑定失败时整个事务一起回滚。
        new_ticket = create_ticket_from_confirmed(db, ticket)
        bind_images(db, ticket.attachment_ids, purpose="intake", ticket_id=new_ticket.id)
        db.commit()
        db.refresh(new_ticket)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise

    return new_ticket


# 查询全部工单
@app.get(
    "/api/v1/tickets",
    response_model=list[TicketResponse],
)
def list_tickets(db: Session = Depends(get_db)):
    # 按创建时间倒序，最新工单排在前面
    return (
        db.query(Ticket)
        .order_by(Ticket.created_at.desc())
        .all()
    )

# 查询所有超时工单
@app.get("/api/v1/tickets/overdue")
def list_overdue_tickets(db: Session = Depends(get_db)):
    """
    查询当前所有接单超时或处理超时的工单。
    SLA 超时状态通过实时计算得到，不单独存入数据库。
    """

    # 获取所有工单
    tickets = db.query(Ticket).all()

    # 存放超时工单
    overdue_tickets = []

    # 获取当前时间，确保本次查询使用同一个时间基准
    now = datetime.now()

    for ticket in tickets:
        # 计算当前工单的 SLA 状态
        sla_result = check_ticket_sla(ticket, now)

        # 接单超时
        if sla_result["accept_timeout"]:
            overdue_tickets.append({
                "ticket_id": ticket.id,
                "ticket_no": ticket.ticket_no,
                "priority": ticket.priority,
                "status": ticket.status,
                "timeout_type": "accept_timeout",
                "deadline": sla_result["accept_deadline"],
            })

        # 处理超时
        if sla_result["process_timeout"]:
            overdue_tickets.append({
                "ticket_id": ticket.id,
                "ticket_no": ticket.ticket_no,
                "priority": ticket.priority,
                "status": ticket.status,
                "timeout_type": "process_timeout",
                "deadline": sla_result["process_deadline"],
            })

    return overdue_tickets

# 根据 ID 查询单条工单
@app.get(
    "/api/v1/tickets/{ticket_id}",
    response_model=TicketResponse,
)
def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
):
    # 根据主键查询工单
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()

    # 如果找不到工单，返回 HTTP 404
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在",
        )

    return ticket


# 查询工单时间线，只读返回状态和关键业务事件
@app.get(
    "/api/v1/tickets/{ticket_id}/events",
    response_model=list[TicketEventResponse],
)
def list_ticket_events(
    ticket_id: int,
    db: Session = Depends(get_db),
):
    ticket = (
        db.query(Ticket)
        .filter(Ticket.id == ticket_id)
        .first()
    )

    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在",
        )

    return (
        db.query(TicketEvent)
        .filter(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.occurred_at.asc(), TicketEvent.id.asc())
        .all()
    )


# 将待接单工单分派给工程人员，分派不会直接改变工单状态
@app.post(
    "/api/v1/tickets/{ticket_id}/assignments",
    response_model=TicketAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_ticket_assignment(
    ticket_id: int,
    request: TicketAssignmentCreate,
    db: Session = Depends(get_db),
):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在",
        )

    try:
        assignment = assign_ticket(db, ticket, request)
        db.flush()
        personnel = (
            db.query(RepairPersonnel)
            .filter(RepairPersonnel.id == assignment.personnel_id)
            .first()
        )
        record_ticket_workflow_event(
            db,
            ticket=ticket,
            event_type="repair.assignment.confirmed",
            source_agent="工程负责人",
            target_agent="工程_agent",
            payload={
                "ticket_id": ticket.id,
                "personnel_no": personnel.personnel_no,
                "personnel_name": personnel.display_name,
                "assigned_by": request.assigned_by,
            },
            status="waiting_engineering",
            current_step="engineering_dispatch",
            requires_human=False,
        )
        db.commit()
        db.refresh(assignment)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception:
        db.rollback()
        raise

    return assignment_response(assignment, personnel)


# 查询工单的全部分派历史，当前分派记录通过 is_current 区分
@app.get(
    "/api/v1/tickets/{ticket_id}/assignments",
    response_model=list[TicketAssignmentResponse],
)
def list_ticket_assignments(
    ticket_id: int,
    db: Session = Depends(get_db),
):
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在",
        )

    rows = (
        db.query(TicketAssignment, RepairPersonnel)
        .join(RepairPersonnel, TicketAssignment.personnel_id == RepairPersonnel.id)
        .filter(TicketAssignment.ticket_id == ticket_id)
        .order_by(TicketAssignment.assigned_at.asc(), TicketAssignment.id.asc())
        .all()
    )
    return [assignment_response(assignment, personnel) for assignment, personnel in rows]


# 当前分派的工程人员确认接单，并记录真实人员编号
@app.post(
    "/api/v1/tickets/{ticket_id}/accept",
    response_model=TicketResponse,
)
def accept_ticket_action(
    ticket_id: int,
    request: TicketAcceptRequest,
    db: Session = Depends(get_db),
):
    """工程人员确认接单；分派和接单是两个独立业务动作。"""

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在",
        )

    try:
        accept_ticket(db, ticket, request)
        record_ticket_workflow_event(
            db,
            ticket=ticket,
            event_type="repair.assignment.accepted",
            source_agent="工程人员",
            target_agent="工程_agent",
            payload={"ticket_id": ticket.id, "personnel_no": request.personnel_no},
            status="waiting_engineering",
            current_step="repair_execution",
        )
        db.commit()
        db.refresh(ticket)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception:
        db.rollback()
        raise

    return ticket


# 已接单工程人员确认开始维修，并记录真实人员编号
@app.post(
    "/api/v1/tickets/{ticket_id}/start-processing",
    response_model=TicketResponse,
)
def start_processing_action(
    ticket_id: int,
    request: TicketStartProcessingRequest,
    db: Session = Depends(get_db),
):
    """工程人员开始维修；开始处理必须发生在接单之后。"""

    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在",
        )

    try:
        start_processing_ticket(db, ticket, request)
        record_ticket_workflow_event(
            db,
            ticket=ticket,
            event_type="repair.processing.started",
            source_agent="工程人员",
            target_agent="工程_agent",
            payload={"ticket_id": ticket.id, "personnel_no": request.personnel_no},
            status="waiting_engineering",
            current_step="repair_execution",
        )
        db.commit()
        db.refresh(ticket)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception:
        db.rollback()
        raise

    return ticket

@app.get("/api/v1/tickets/{ticket_id}/repair-records", response_model=list[RepairRecordResponse])
def ticket_repair_records(ticket_id: int, db: Session = Depends(get_db)):
    """让企服同时核对维修说明与留图；读取不会改变验收状态。"""
    if db.get(Ticket, ticket_id) is None:
        raise HTTPException(404, "工单不存在")
    records = db.query(RepairRecord).filter(RepairRecord.ticket_id == ticket_id).order_by(RepairRecord.id).all()
    return [{"id": record.id, "ticket_id": record.ticket_id, "repair_description": record.repair_description,
             "photo_urls": json.loads(record.photo_urls or "[]"), "submitted_at": record.submitted_at} for record in records]


# 提交维修结果
@app.post(
    "/api/v1/tickets/{ticket_id}/repair-records",
    response_model=RepairRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_repair_record(
    ticket_id: int,
    request: RepairRecordCreate,
    db: Session = Depends(get_db),
):
    """
    维修人员提交维修结果。

    提交成功后：
    1. 保存维修说明和照片路径。
    2. 将工单状态更新为 pending_review（待验收）。
    """

    # 1. 根据工单 ID 查询工单
    ticket = (
        db.query(Ticket)
        .filter(Ticket.id == ticket_id)
        .first()
    )

    # 2. 工单不存在时返回 404
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在",
        )

    # 3. 调用维修记录服务
    try:
        record = create_repair_record(
            db=db,
            ticket=ticket,
            request=request,
        )
        record_ticket_workflow_event(
            db,
            ticket=ticket,
            event_type="repair.completed.submitted",
            source_agent="工程人员",
            target_agent="企服_agent",
            payload={
                "ticket_id": ticket.id,
                "personnel_no": request.personnel_no,
                "repair_record_id": record.id,
                "photo_count": len(json.loads(record.photo_urls or "[]")),
            },
            status="waiting_acceptance",
            current_step="service_acceptance",
            requires_human=True,
        )
        db.commit()
        db.refresh(record)
    except ValueError as exc:
        # 只有处理中状态的工单才能提交维修结果
        # 维修说明不能为空，且提交人必须是当前分派人员
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # 4. 将数据库中的 JSON 字符串转换为 Python 列表
    photo_urls = json.loads(record.photo_urls or "[]")

    # 5. 返回符合 RepairRecordResponse 的数据
    return RepairRecordResponse(
        id=record.id,
        ticket_id=record.ticket_id,
        repair_description=record.repair_description,
        photo_urls=photo_urls,
        submitted_at=record.submitted_at,
    )

@app.get("/api/v1/tickets/{ticket_id}/acceptance-records", response_model=list[AcceptanceRecordResponse])
def ticket_acceptance_records(ticket_id: int, db: Session = Depends(get_db)):
    """按验收时间返回全部通过及退回记录，读取不会改变工单状态。"""
    if db.get(Ticket, ticket_id) is None:
        raise HTTPException(404, "工单不存在")
    records = db.query(AcceptanceRecord).filter(
        AcceptanceRecord.ticket_id == ticket_id
    ).order_by(AcceptanceRecord.accepted_at, AcceptanceRecord.id).all()
    return [AcceptanceRecordResponse(
        id=record.id, ticket_id=record.ticket_id,
        acceptance_comment=record.acceptance_comment, accepted_by=record.accepted_by,
        accepted=record.accepted, accepted_at=record.accepted_at,
    ) for record in records]


# 提交验收意见
@app.post(
    "/api/v1/tickets/{ticket_id}/acceptance",
    response_model=AcceptanceRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_acceptance_record(
    ticket_id: int,
    request: AcceptanceRecordCreate,
    db: Session = Depends(get_db),
):
    """
    企服提交工单验收意见。

    验收成功后：
    1. 保存验收记录。
    2. 将工单状态更新为 completed。
    """

    # 1. 根据工单 ID 查询工单
    ticket = (
        db.query(Ticket)
        .filter(Ticket.id == ticket_id)
        .first()
    )

    # 2. 工单不存在，返回 404
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在",
        )

    # 3. 创建验收记录并更新工单状态
    try:
        record = create_acceptance_record(
            db=db,
            ticket=ticket,
            request=request,
        )
        record_ticket_workflow_event(
            db,
            ticket=ticket,
            event_type="repair.acceptance.decided",
            source_agent="企服人员",
            target_agent="ticket_service",
            payload={
                "ticket_id": ticket.id,
                "accepted": request.accepted,
                "accepted_by": request.accepted_by,
                "acceptance_comment": request.acceptance_comment.strip(),
            },
            status="completed" if request.accepted else "waiting_engineering",
            current_step="completed" if request.accepted else "repair_rework",
            requires_human=False,
        )
        db.commit()
        db.refresh(record)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # 4. 返回验收记录
    return AcceptanceRecordResponse(
        id=record.id,
        ticket_id=record.ticket_id,
        acceptance_comment=record.acceptance_comment,
        accepted_by=record.accepted_by,
        accepted=record.accepted,
        accepted_at=record.accepted_at,
    )

# 更新工单状态
@app.patch(
    "/api/v1/tickets/{ticket_id}/status",
    response_model=TicketResponse,
)
def update_ticket_status(
    ticket_id: int,
    request: TicketStatusUpdate,
    db: Session = Depends(get_db),
):
    # 1. 根据工单 ID 查询数据库
    ticket = (
        db.query(Ticket)
        .filter(Ticket.id == ticket_id)
        .first()
    )

    # 2. 如果工单不存在，返回 404
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在",
        )

    # 3. 获取工单当前状态
    current_status = TicketStatus(ticket.status)

    try:
        # 执行状态流转，并记录接单时间或提交时间
        success = transition_ticket_with_event(
            db,
            ticket,
            request.status,
            reason="工单状态更新",
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"不允许从 {current_status.value} "
                    f"流转到 {request.status.value}"
                ),
            )

        db.commit()
        db.refresh(ticket)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return ticket

