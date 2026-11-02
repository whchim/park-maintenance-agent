import re
import json
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.follow_up_task import FollowUpTask
from app.models.follow_up_record import FollowUpRecord
from app.models.workflow_event import WorkflowEvent
from app.models.workflow_session import WorkflowSession
from app.services.workflow_service import _new_id
from app.services.department_agent_service import draft_investment
from app.services.llm_service import runtime_status


def capability_catalog() -> list[dict]:
    catalog = [
        {
            "agent_name": "supervisor_agent",
            "department": "平台协同层",
            "status": "implemented_rule_fallback",
            "capabilities": ["应用层流程续办（非原生图检查点）", "人工门禁", "结构化事件交接", "LangGraph 企服/工程局部编排；部门路由仍为确定性规则"],
            "write_guards": ["不直接写业务表", "关键动作必须调用业务 Service"],
        },
        {
            "agent_name": "service_agent",
            "department": "企服部",
            "status": "implemented_rule_fallback",
            "capabilities": ["报修信息完整性初审", "缺项追问", "企服验收/退回", "签约客户联系单完整性检查与人工接收/退回"],
            "write_guards": ["不判断报修通过/拒绝", "不绕过人工验收"],
        },
        {
            "agent_name": "engineering_agent",
            "department": "工程部",
            "status": "implemented_rule_fallback",
            "capabilities": ["LangGraph 只读工具决策循环（需启用千问）", "人员技能/区域推荐", "当前未结束分派数查询", "维修交接", "处理结果凭证检查"],
            "write_guards": ["不自动正式分派", "不未经企服验收关闭工单"],
        },
        {
            "agent_name": "investment_agent",
            "department": "招商部",
            "status": "implemented_rule_fallback_mvp",
            "capabilities": ["客户意向结构化", "跟进任务草稿", "签约联系单版本提交与企服交接"],
            "write_guards": ["不编造客户事实", "客户建档和任务创建需要人工确认"],
        },
    ]
    if runtime_status()["ready"]:
        for entry in catalog:
            if entry["agent_name"] != "supervisor_agent":
                entry["status"] = "llm_configured_not_verified"
    return catalog


def _parse_investment_rules(message: str, source: str | None = None) -> dict:
    text = message.strip()
    phone = re.search(r"1[3-9]\d{9}", text)
    name = None
    for marker in ("客户", "公司", "企业"):
        if marker in text:
            tail = text.split(marker, 1)[1].strip("：:，,。 ")
            for separator in (
                "明确意向", "考虑中", "意向不明确", "确定租", "马上签",
                "计划", "打算", "有意", "希望", "想", "需要", "联系", "咨询", "，", ",", "。",
            ):
                tail = tail.split(separator, 1)[0]
            name = tail.strip("：:，,。 ") or None
            break
    missing = []
    if not name:
        missing.append("customer_name")
    intent_level = None
    if any(token in text for token in ("明确意向", "确定租", "马上签", "近期签")):
        intent_level = "A"
    elif any(token in text for token in ("考虑中", "有诉求", "比较一下")):
        intent_level = "B"
    elif any(token in text for token in ("不急", "了解一下", "还没确定")):
        intent_level = "C"
    interval_days = {"A": 1, "B": 3, "C": 7}.get(intent_level)
    return {
        "mode": "rule_fallback_demo",
        "customer_name": name,
        "contact_name": None,
        "contact_phone": phone.group(0) if phone else None,
        "source": source,
        "intent_level": intent_level,
        "follow_up_interval_days": interval_days,
        "missing_fields": missing,
        "follow_up_subject": "跟进客户意向并补充正式客户资料",
        "follow_up_description": text,
    }


def parse_investment_intake(message: str, source: str | None = None) -> dict:
    """招商模型只起草；无配置或调用失败时明确返回规则回退结果。"""
    return draft_investment(message, source, _parse_investment_rules(message, source))


def confirm_investment_intake(db: Session, request) -> tuple[Customer, FollowUpTask, WorkflowEvent | None]:
    existing = db.query(FollowUpTask).filter(
        FollowUpTask.idempotency_key == request.idempotency_key
    ).first()
    if existing:
        customer = db.query(Customer).filter(Customer.id == existing.customer_id).one()
        event = db.query(WorkflowEvent).filter(
            WorkflowEvent.business_id == str(customer.id),
            WorkflowEvent.event_type == "investment.lead.confirmed",
        ).order_by(WorkflowEvent.id.desc()).first()
        return customer, existing, event

    now = datetime.now()
    customer = Customer(
        customer_no=f"KH{now:%Y%m%d}{db.query(Customer).count() + 1:04d}",
        customer_name=request.customer_name,
        contact_name=request.contact_name,
        contact_phone=request.contact_phone,
        source=request.source,
        owner=request.owner,
        stage="lead",
        intent_level=request.intent_level,
        notes="招商 Agent 结构化草稿经人工确认创建",
    )
    db.add(customer)
    db.flush()
    task = FollowUpTask(
        task_no=f"GJ{now:%Y%m%d}{db.query(FollowUpTask).count() + 1:04d}",
        customer_id=customer.id,
        subject=request.follow_up_subject,
        description=request.follow_up_description,
        owner=request.owner,
        idempotency_key=request.idempotency_key,
        status="pending",
        due_at=now + timedelta(days={"A": 1, "B": 3, "C": 7}[request.intent_level]),
    )
    db.add(task)
    db.flush()
    workflow = WorkflowSession(
        workflow_id=_new_id("wf"),
        channel="internal",
        requester_id=request.owner,
        status="completed",
        current_step="investment_follow_up_created",
        original_message=request.follow_up_description,
        draft_json=json.dumps({
            "customer_id": customer.id,
            "customer_name": customer.customer_name,
            "intent_level": customer.intent_level,
            "follow_up_due_at": task.due_at.isoformat(),
            "follow_up_task_id": task.id,
        }, ensure_ascii=False),
        missing_fields_json="[]",
    )
    db.add(workflow)
    db.flush()
    event = WorkflowEvent(
        event_id=_new_id("evt"),
        workflow_id=workflow.workflow_id,
        event_type="investment.lead.confirmed",
        source_agent="investment_agent",
        target_agent="supervisor",
        business_id=str(customer.id),
        payload_json=json.dumps({
            "customer_id": customer.id,
            "customer_no": customer.customer_no,
            "customer_name": customer.customer_name,
            "contact_name": customer.contact_name,
            "contact_phone": customer.contact_phone,
            "source": customer.source,
            "follow_up_task_id": task.id,
            "customer_stage": "lead",
            "mode": "rule_fallback_demo",
        }, ensure_ascii=False),
        workflow_status="completed",
        requires_human=False,
        idempotency_key=f"{request.idempotency_key}:lead-confirmed",
        status="completed",
    )
    db.add(event)
    db.flush()
    return customer, task, event


def complete_follow_up(
    db: Session,
    *,
    task_id: int,
    actor: str,
    outcome: str,
    idempotency_key: str,
) -> tuple[FollowUpRecord, FollowUpTask | None]:
    actor, outcome = actor.strip(), outcome.strip()
    if not actor or not outcome:
        raise ValueError("回访人员和回访结果不能为空")
    existing = db.query(FollowUpRecord).filter(
        FollowUpRecord.idempotency_key == idempotency_key
    ).first()
    if existing:
        if existing.task_id != task_id or existing.actor != actor or existing.outcome != outcome:
            raise ValueError("幂等键已用于其他任务或不同回访内容")
        next_task = (
            db.query(FollowUpTask).filter(FollowUpTask.id == existing.next_task_id).first()
            if existing.next_task_id else None
        )
        return existing, next_task

    task = db.query(FollowUpTask).filter(FollowUpTask.id == task_id).first()
    if task is None:
        raise ValueError("跟进任务不存在")
    if task.status != "pending":
        raise ValueError("跟进任务已经完成")

    customer = db.query(Customer).filter(Customer.id == task.customer_id).one()
    # 原子占用待完成任务，避免两个提交各自生成下一次回访。
    claimed = db.query(FollowUpTask).filter(
        FollowUpTask.id == task_id, FollowUpTask.status == "pending"
    ).update({"status": "completed"}, synchronize_session="fetch")
    if claimed != 1:
        raise ValueError("跟进任务已经完成")
    record = FollowUpRecord(
        task_id=task.id,
        customer_id=customer.id,
        actor=actor,
        outcome=outcome.strip(),
        idempotency_key=idempotency_key,
    )
    db.add(record)

    interval_days = {"A": 1, "B": 3, "C": 7}.get(customer.intent_level)
    next_task = None
    if interval_days and customer.stage == "lead":
        now = datetime.now()
        next_task = FollowUpTask(
            task_no=f"GJ{now:%Y%m%d}{db.query(FollowUpTask).count() + 1:04d}",
            customer_id=customer.id,
            subject=task.subject,
            description=f"上次回访：{outcome.strip()}",
            owner=task.owner,
            status="pending",
            idempotency_key=f"{idempotency_key}:next",
            due_at=now + timedelta(days=interval_days),
        )
        db.add(next_task)
        db.flush()
        record.next_task_id = next_task.id
    db.flush()
    return record, next_task
