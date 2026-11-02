from datetime import datetime

from sqlalchemy.orm import Session

from app.models.repair_personnel import RepairPersonnel
from app.models.ticket import Ticket
from app.models.ticket_assignment import TicketAssignment
from app.schemas.ticket import TicketStatus
from app.schemas.ticket_assignment import TicketAssignmentCreate


def assign_ticket(
    db: Session,
    ticket: Ticket,
    request: TicketAssignmentCreate,
) -> TicketAssignment:
    """把待接单工单分派给一名活跃工程人员。"""
    if ticket.status != TicketStatus.PENDING.value:
        raise ValueError("只有待接单状态的工单才能分派")

    personnel = (
        db.query(RepairPersonnel)
        .filter(
            RepairPersonnel.personnel_no == request.personnel_no,
            RepairPersonnel.is_active.is_(True),
        )
        .first()
    )
    if personnel is None:
        raise ValueError("工程人员不存在或已停用")

    # 保留旧分派记录，便于审计和后续支持重新分派
    db.query(TicketAssignment).filter(
        TicketAssignment.ticket_id == ticket.id,
        TicketAssignment.is_current.is_(True),
    ).update({"is_current": False}, synchronize_session=False)

    assignment = TicketAssignment(
        ticket_id=ticket.id,
        personnel_id=personnel.id,
        assigned_by=request.assigned_by,
        assigned_at=datetime.now(),
        is_current=True,
    )
    db.add(assignment)

    # 兼容现有工单返回结构，保存稳定人员编号而不是显示名称
    ticket.assignee = personnel.personnel_no

    return assignment


def assignment_response(
    assignment: TicketAssignment,
    personnel: RepairPersonnel,
) -> dict:
    return {
        "id": assignment.id,
        "ticket_id": assignment.ticket_id,
        "personnel_id": assignment.personnel_id,
        "personnel_no": personnel.personnel_no,
        "personnel_name": personnel.display_name,
        "assigned_by": assignment.assigned_by,
        "assigned_at": assignment.assigned_at,
        "is_current": assignment.is_current,
    }
