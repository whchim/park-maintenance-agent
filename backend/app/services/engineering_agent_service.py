"""工程建议入口：读取人员与当前分派快照，按调用场景选择规则或只读 Agent 图。"""

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.ticket import Ticket
from app.models.ticket_assignment import TicketAssignment
from app.models.repair_personnel import RepairPersonnel
from app.services.repair_personnel_service import list_active_personnel
from app.agents.engineering import run_engineering


def recommend_dispatch(db: Session, ticket: Ticket, *, use_ai: bool = False) -> dict:
    """按技能和负责区域生成候选排序，不创建分派记录。"""
    personnel = list_active_personnel(db)
    rules = _rule_recommendation(personnel, ticket)
    if not use_ai:
        return rules
    # 在图运行前一次性读取快照；图本身没有数据库权限。
    counts = db.query(RepairPersonnel.personnel_no, func.count(TicketAssignment.id)).join(
        TicketAssignment, TicketAssignment.personnel_id == RepairPersonnel.id,
    ).join(Ticket, Ticket.id == TicketAssignment.ticket_id).filter(
        TicketAssignment.is_current.is_(True), Ticket.status != "completed",
    ).group_by(RepairPersonnel.personnel_no).all()
    snapshot = {person["personnel_no"]: 0 for person in personnel}
    snapshot.update({number: count for number, count in counts if number in snapshot})
    return run_engineering(
        {"ticket_id": ticket.id, "location": ticket.location, "issue_type": ticket.issue_type,
         "description": ticket.description, "priority": ticket.priority}, personnel, snapshot, rules,
    )


def _rule_recommendation(personnel: list[dict], ticket: Ticket) -> dict:
    if not personnel:
        return {
            "mode": "rule_fallback_demo",
            "recommended_personnel": None,
            "reason": "暂无活跃工程人员目录",
            "candidates": [],
            "requires_human_confirmation": True,
        }

    issue = f"{ticket.issue_type or ''}{ticket.description}"
    scored = []
    for person in personnel:
        skills = " ".join(person.get("skills") or [])
        searchable = f"{skills}{person['display_name']}"
        score = 1
        if any(word in searchable for word in ("水电", "漏水", "照明")) and any(
            word in issue for word in ("漏水", "照明", "停电")
        ):
            score += 2
        if any(word in searchable for word in ("空调", "设备")) and any(
            word in issue for word in ("空调", "设备")
        ):
            score += 2
        if person.get("responsible_area") and person["responsible_area"] in ticket.location:
            score += 1
        scored.append((score, person))

    scored.sort(key=lambda item: (-item[0], item[1]["personnel_no"]))
    recommended = scored[0][1]
    return {
        "mode": "rule_fallback_demo",
        "recommended_personnel": recommended["personnel_no"],
        "recommended_name": recommended["display_name"],
        "reason": "根据故障类型、技能和负责区域排序；工程负责人仍需确认委派",
        "candidates": [
            {
                "personnel_no": person["personnel_no"],
                "display_name": person["display_name"],
                "score": score,
            }
            for score, person in scored
        ],
        "requires_human_confirmation": True,
    }
