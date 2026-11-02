import hashlib
import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.customer_handoff import CustomerHandoff
from app.models.follow_up_task import FollowUpTask
from app.models.workflow_event import WorkflowEvent
from app.models.workflow_session import WorkflowSession
from app.schemas.customer_handoff import HandoffDocument
from app.services.workflow_service import append_event


class HandoffNotFound(ValueError):
    pass


FIELD_LABELS = {
    "contract_no": "合同编号", "contract_version": "合同版本",
    "contact_sheet_ref": "最终版客户联系单引用", "contact_sheet_version": "联系单版本",
    "location": "入驻位置", "contact_name": "联系人", "contact_phone": "联系方式",
    "service_requirements": "服务需求（无需求请明确填无）", "agreements": "约定事项（无约定请明确填无）",
}


def check_document(document: dict) -> dict:
    """企服/招商受理能力的规则回退：检查缺项，不批准合同或承诺服务。"""
    missing = [name for name in FIELD_LABELS if not document.get(name, "").strip()]
    return {"mode": "rule_fallback_demo", "template_status": "demo_fields_pending_official_template",
            "missing_fields": missing, "missing_labels": [FIELD_LABELS[name] for name in missing],
            "next_action": "supplement" if missing else "human_confirm"}


def get_handoff(db: Session, handoff_id: int) -> CustomerHandoff:
    handoff = db.get(CustomerHandoff, handoff_id)
    if handoff is None:
        raise HandoffNotFound("客户交接单不存在")
    return handoff


def _session(db: Session, handoff: CustomerHandoff) -> WorkflowSession:
    return db.query(WorkflowSession).filter_by(workflow_id=handoff.workflow_id).one()


def handoff_response(db: Session, handoff: CustomerHandoff) -> dict:
    customer = db.get(Customer, handoff.customer_id)
    events = db.query(WorkflowEvent).filter_by(workflow_id=handoff.workflow_id).order_by(WorkflowEvent.id).all()
    document = json.loads(handoff.document_json)
    return {
        "id": handoff.id, "customer_id": handoff.customer_id, "customer_name": customer.customer_name,
        "workflow_id": handoff.workflow_id, "status": handoff.status, "revision": handoff.revision,
        "document": document, "check": check_document(document),
        "submitted_by": handoff.submitted_by, "submitted_at": handoff.submitted_at,
        "decided_by": handoff.decided_by, "decided_at": handoff.decided_at,
        "decision_comment": handoff.decision_comment,
        "events": [{"event_id": e.event_id, "event_type": e.event_type,
                    "source_agent": e.source_agent, "target_agent": e.target_agent,
                    "payload": e.payload, "occurred_at": e.occurred_at} for e in events],
    }


def create_handoff(db: Session, customer_id: int) -> CustomerHandoff:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HandoffNotFound("客户不存在")
    existing = db.query(CustomerHandoff).filter_by(customer_id=customer_id).first()
    if existing:
        return existing
    document = HandoffDocument(contact_name=customer.contact_name or "", contact_phone=customer.contact_phone or "").model_dump()
    workflow = WorkflowSession(workflow_id=f"wf_{uuid4().hex[:24]}", channel="internal",
                               status="waiting_input", current_step="customer_handoff_draft",
                               original_message=f"{customer.customer_name}签约客户交接",
                               draft_json=json.dumps(document, ensure_ascii=False),
                               missing_fields_json=json.dumps(check_document(document)["missing_fields"]))
    db.add(workflow)
    db.flush()
    handoff = CustomerHandoff(customer_id=customer_id, workflow_id=workflow.workflow_id,
                              status="draft", revision=1, document_json=workflow.draft_json)
    db.add(handoff)
    db.flush()
    return handoff


def _claim(db: Session, handoff: CustomerHandoff, revision: int, allowed: tuple[str, ...], **values) -> None:
    changed = db.query(CustomerHandoff).filter(
        CustomerHandoff.id == handoff.id, CustomerHandoff.revision == revision,
        CustomerHandoff.status.in_(allowed),
    ).update({**values, "revision": revision + 1, "updated_at": datetime.now()}, synchronize_session=False)
    if changed != 1:
        raise ValueError("交接状态或版本已变化，请刷新后重试")
    db.refresh(handoff)


def save_document(db: Session, handoff: CustomerHandoff, request) -> None:
    document_json = request.document.model_dump_json()
    _claim(db, handoff, request.expected_revision, ("draft", "returned"), document_json=document_json)
    workflow = _session(db, handoff)
    check = check_document(request.document.model_dump())
    workflow.draft_json = document_json
    workflow.missing_fields_json = json.dumps(check["missing_fields"])
    workflow.status = "waiting_input" if check["missing_fields"] else "waiting_human"
    workflow.current_step = "customer_handoff_draft"


def _replayed(db: Session, handoff: CustomerHandoff, request, operation: str) -> bool:
    # 绑定动作、流程、原请求内容；不能将其他交接单的键当成自己的成功结果。
    digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    key = "handoff:" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()
    event = db.query(WorkflowEvent).filter_by(idempotency_key=key).first()
    if event:
        if event.workflow_id != handoff.workflow_id or event.payload.get("request_digest") != digest or event.payload.get("operation") != operation:
            raise ValueError("幂等键已用于其他交接动作或不同内容")
        return True
    return False


def _event(db: Session, handoff: CustomerHandoff, request, event_type: str, operation: str, payload: dict) -> None:
    append_event(db, session=_session(db, handoff), event_type=event_type,
                 source_agent="investment_agent" if operation == "submit" else "service_agent",
                 target_agent="service_agent" if operation == "submit" else ("supervisor" if request.accepted else "investment_agent"),
                 payload={**payload, "handoff_id": handoff.id, "customer_id": handoff.customer_id,
                          "actor": request.actor, "revision": handoff.revision, "operation": operation,
                          "request_digest": hashlib.sha256(request.model_dump_json().encode()).hexdigest()},
                 idempotency_key="handoff:" + hashlib.sha256(request.idempotency_key.encode()).hexdigest(),
                 business_id=str(handoff.customer_id), requires_human=operation == "submit")


def submit_handoff(db: Session, handoff: CustomerHandoff, request) -> None:
    if _replayed(db, handoff, request, "submit"):
        return
    document = json.loads(handoff.document_json)
    if check_document(document)["missing_fields"]:
        raise ValueError("联系单仍有缺失字段，请补充后提交")
    if not request.contract_signed:
        raise ValueError("正式交接需招商人员明确确认客户已签约")
    _claim(db, handoff, request.expected_revision, ("draft", "returned"), status="submitted",
           submitted_by=request.actor, submitted_at=datetime.now())
    workflow = _session(db, handoff)
    workflow.status, workflow.current_step = "waiting_human", "service_customer_receipt"
    customer = db.get(Customer, handoff.customer_id)
    customer.stage = "signed"
    # 签约后停止意向客户回访链，保留取消任务和历史结果，不删除记录。
    cancelled = db.query(FollowUpTask).filter_by(customer_id=customer.id, status="pending").update(
        {"status": "cancelled"}, synchronize_session=False)
    _event(db, handoff, request, "customer.handoff.submitted", "submit",
           {"document": document, "contract_signed_confirmed": True, "cancelled_follow_ups": cancelled})


def decide_handoff(db: Session, handoff: CustomerHandoff, request) -> None:
    if _replayed(db, handoff, request, "decide"):
        return
    _claim(db, handoff, request.expected_revision, ("submitted",),
           status="accepted" if request.accepted else "returned", decided_by=request.actor,
           decided_at=datetime.now(), decision_comment=request.comment)
    workflow = _session(db, handoff)
    workflow.status = "completed" if request.accepted else "waiting_input"
    workflow.current_step = "customer_handoff_received" if request.accepted else "customer_handoff_correction"
    _event(db, handoff, request, "customer.handoff.accepted" if request.accepted else "customer.handoff.returned",
           "decide", {"accepted": request.accepted, "comment": request.comment,
                      "document": json.loads(handoff.document_json)})
