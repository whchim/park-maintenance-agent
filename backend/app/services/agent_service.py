import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.models.agent_run import AgentRun, AgentToolCall
from app.models.renovation_task import RenovationTask
from app.models.ticket import Ticket
from app.schemas.ticket import Priority, TicketStatus
from app.services.priority_service import classify_priority
from app.services.repair_personnel_service import list_active_personnel
from app.services.engineering_agent_service import recommend_dispatch
from app.services.repair_service import parse_repair_text
from app.services.sla_service import SLA_RULES
from app.services.ticket_event_service import record_ticket_event


def _json(value: Any) -> str:
    """将 Agent 轨迹中的结果转换为可读 JSON，避免暴露 ORM 对象地址。"""
    if isinstance(value, Ticket):
        value = {
            "id": value.id,
            "ticket_no": value.ticket_no,
            "location": value.location,
            "issue_type": value.issue_type,
            "priority": value.priority,
            "status": value.status,
        }
    elif isinstance(value, RenovationTask):
        value = {
            "id": value.id,
            "task_no": value.task_no,
            "location": value.location,
            "status": value.status,
        }
    return json.dumps(value, ensure_ascii=False, default=str)


class AgentRecorder:
    """为每个 Agent 记录输入、输出和工具调用。"""

    def __init__(self, db: Session, agent_name: str, input_text: str, run_id: str):
        self.db = db
        self.run = AgentRun(
            run_id=run_id,
            agent_name=agent_name,
            status="running",
            input_text=input_text,
            started_at=datetime.now(),
        )
        db.add(self.run)
        db.flush()

    def tool(self, name: str, payload: dict, operation: Callable[[], Any]) -> Any:
        started_at = datetime.now()
        call = AgentToolCall(
            agent_run_id=self.run.id,
            tool_name=name,
            status="running",
            input_json=_json(payload),
            started_at=started_at,
        )
        self.db.add(call)
        self.db.flush()
        try:
            result = operation()
            call.status = "success"
            call.output_json = _json(result)
            call.completed_at = datetime.now()
            return result
        except Exception as exc:
            call.status = "failed"
            call.error = str(exc)
            call.completed_at = datetime.now()
            raise

    def finish(self, output: Any, status: str = "completed", error: str | None = None):
        self.run.status = status
        self.run.output_json = _json(output)
        self.run.error = error
        self.run.completed_at = datetime.now()


def _extract_location(text: str) -> str:
    room_match = re.search(r"([A-ZＡ-Ｚ]\s*栋\s*\d+\s*(?:室|号)?)", text)
    if room_match:
        return room_match.group(1).replace(" ", "")
    named_area_match = re.search(
        r"([A-ZＡ-Ｚ]\s*栋(?:\s*[一二三四五六七八九十0-9]+\s*楼)?[^，。；;]*?(?:卫生间|办公室|机房|走廊))",
        text,
    )
    if named_area_match:
        return named_area_match.group(1).replace(" ", "")
    return "待确认位置"


def _detect_intent(db: Session, message: str) -> dict:
    repair = parse_repair_text(message)
    has_repair = repair.issue_type is not None or any(word in message for word in ("漏水", "空调", "照明", "停电", "门禁"))
    has_renovation = any(word in message for word in ("入驻", "改造", "装修", "施工", "交付"))
    return {
        "repair": has_repair,
        "renovation": has_renovation,
        "location": _extract_location(message) if _extract_location(message) != "待确认位置" else (repair.location or "待确认位置"),
        "repair_parse": repair.model_dump(),
    }


def _create_ticket(db: Session, message: str, run_id: str, repair: dict) -> Ticket:
    now = datetime.now()
    prefix = f"WX{now:%Y%m%d}"
    count = db.query(Ticket).filter(Ticket.ticket_no.like(f"{prefix}%")).count()
    description = repair["description"] or message
    inferred_priority = classify_priority(description)
    ticket = Ticket(
        ticket_no=f"{prefix}{count + 1:04d}",
        location=repair["location"] or "待确认位置",
        issue_type=repair["issue_type"] or "综合工程问题",
        description=description,
        priority=inferred_priority.value,
        status=TicketStatus.PENDING.value,
    )
    db.add(ticket)
    db.flush()
    record_ticket_event(
        db,
        ticket,
        from_status=None,
        to_status=TicketStatus.PENDING,
        actor_type="agent",
        actor_id="repair_intake_agent",
        reason=f"Supervisor 编排创建维修工单，run_id={run_id}",
    )
    return ticket


def _create_renovation_task(db: Session, message: str, location: str, run_id: str) -> RenovationTask:
    now = datetime.now()
    prefix = f"GZ{now:%Y%m%d}"
    count = db.query(RenovationTask).filter(RenovationTask.task_no.like(f"{prefix}%")).count()
    task = RenovationTask(
        task_no=f"{prefix}{count + 1:04d}",
        location=location,
        description=message,
        status="pending",
        source_run_id=run_id,
    )
    db.add(task)
    db.flush()
    return task


def _step_response(db: Session, run: AgentRun) -> dict:
    calls = db.query(AgentToolCall).filter(AgentToolCall.agent_run_id == run.id).order_by(AgentToolCall.id.asc()).all()
    return {
        "run_id": run.run_id,
        "agent_name": run.agent_name,
        "status": run.status,
        "input_text": run.input_text,
        "output": json.loads(run.output_json) if run.output_json else None,
        "error": run.error,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "tool_calls": [
            {
                "tool_name": call.tool_name,
                "status": call.status,
                "input": json.loads(call.input_json),
                "output": json.loads(call.output_json) if call.output_json else None,
                "error": call.error,
                "started_at": call.started_at,
                "completed_at": call.completed_at,
            }
            for call in calls
        ],
    }


def coordinate_request(db: Session, message: str) -> dict:
    """运行一个可追踪的跨部门 Supervisor 编排流程。"""
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    supervisor = AgentRecorder(db, "supervisor_agent", message, run_id)
    try:
        intent = supervisor.tool("detect_business_intent", {"message": message}, lambda: _detect_intent(db, message))

        repair_ticket = None
        renovation_task = None
        dispatch = None
        steps = [supervisor.run]

        if intent["repair"]:
            intake = AgentRecorder(db, "repair_intake_agent", message, run_id)
            def parse_intake() -> dict:
                parsed_result = dict(intent["repair_parse"])
                # Supervisor 已经完成房间级位置识别，后续业务工具沿用这个结果。
                if intent["location"] != "待确认位置":
                    parsed_result["location"] = intent["location"]
                return parsed_result

            parsed = intake.tool("parse_repair_text", {"text": message}, parse_intake)
            intake.finish(parsed)
            steps.append(intake.run)

            risk = AgentRecorder(db, "risk_assessment_agent", message, run_id)
            risk_output = risk.tool(
                "classify_priority_and_sla",
                {"description": parsed.get("description", message)},
                lambda: {
                    "priority": classify_priority(parsed.get("description", message)).value,
                    "sla": SLA_RULES[classify_priority(parsed.get("description", message)).value],
                    "reason": "基于现有确定性优先级规则，结果需人工核对",
                },
            )
            risk.finish(risk_output)
            steps.append(risk.run)

            repair_ticket = supervisor.tool(
                "create_repair_ticket",
                {"location": parsed.get("location"), "issue_type": parsed.get("issue_type")},
                lambda: _create_ticket(db, message, supervisor.run.run_id, parsed),
            )
            dispatch_agent = AgentRecorder(db, "dispatch_agent", message, run_id)
            dispatch = dispatch_agent.tool(
                "recommend_repair_personnel",
                {"ticket_id": repair_ticket.id},
                lambda: recommend_dispatch(db, repair_ticket),
            )
            dispatch_agent.finish(dispatch)
            steps.append(dispatch_agent.run)

        if intent["renovation"]:
            renovation_agent = AgentRecorder(db, "renovation_agent", message, run_id)
            renovation_task = renovation_agent.tool(
                "create_renovation_task",
                {"location": intent["location"]},
                lambda: _create_renovation_task(db, message, intent["location"], supervisor.run.run_id),
            )
            renovation_agent.finish({"task_no": renovation_task.task_no, "status": renovation_task.status})
            steps.append(renovation_agent.run)

        summary_parts = []
        if repair_ticket:
            summary_parts.append(f"已创建维修工单 {repair_ticket.ticket_no}")
        if renovation_task:
            summary_parts.append(f"已创建工程改造任务 {renovation_task.task_no}")
        if dispatch:
            summary_parts.append(f"推荐工程人员 {dispatch.get('recommended_name') or '待补充'}，等待人工确认")
        if not summary_parts:
            summary_parts.append("暂未识别出可执行的园区业务任务")

        supervisor.finish({"intent": intent, "summary": "；".join(summary_parts)})

        db.flush()
        return {
            "run_id": supervisor.run.run_id,
            "status": "completed",
            "mode": "rule_fallback_demo",
            "summary": "；".join(summary_parts),
            "intent": intent,
            "created_ticket": {
                "id": repair_ticket.id,
                "ticket_no": repair_ticket.ticket_no,
                "status": repair_ticket.status,
            } if repair_ticket else None,
            "created_renovation_task": {
                "id": renovation_task.id,
                "task_no": renovation_task.task_no,
                "status": renovation_task.status,
            } if renovation_task else None,
            "dispatch_recommendation": dispatch,
            "steps": [_step_response(db, run) for run in steps],
        }
    except Exception as exc:
        supervisor.finish({}, status="failed", error=str(exc))
        raise
