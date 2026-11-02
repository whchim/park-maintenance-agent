from app.schemas.ticket import TicketStatus
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.repair_personnel import RepairPersonnel
from app.models.ticket import Ticket
from app.models.ticket_assignment import TicketAssignment
from app.models.ticket_event import TicketEvent
from app.schemas.ticket import TicketAcceptRequest, TicketStartProcessingRequest
from app.schemas.ticket import TicketCreate
from app.services.ticket_event_service import record_ticket_event

# 定义每种状态允许流转到哪些下一状态
ALLOWED_TRANSITIONS = {
    TicketStatus.PENDING: [TicketStatus.ACCEPTED],
    TicketStatus.ACCEPTED: [TicketStatus.PROCESSING],
    TicketStatus.PROCESSING: [TicketStatus.PENDING_REVIEW],
    TicketStatus.PENDING_REVIEW: [TicketStatus.COMPLETED, TicketStatus.PROCESSING],
    TicketStatus.COMPLETED: [],
}


def create_ticket_from_confirmed(
    db: Session,
    ticket_data: TicketCreate,
    *,
    actor_type: str = "system",
    reason: str = "创建维修工单",
) -> Ticket:
    """由已确认的业务数据创建工单，不重新推断或覆盖优先级。"""
    now = datetime.now()
    today_prefix = f"WX{now:%Y%m%d}"
    today_count = db.query(Ticket).filter(
        Ticket.ticket_no.like(f"{today_prefix}%")
    ).count()
    ticket = Ticket(
        ticket_no=f"{today_prefix}{today_count + 1:04d}",
        location=ticket_data.location,
        issue_type=ticket_data.issue_type,
        description=ticket_data.description,
        priority=ticket_data.priority.value,
        status=TicketStatus.PENDING.value,
    )
    db.add(ticket)
    db.flush()
    record_ticket_event(
        db,
        ticket,
        from_status=None,
        to_status=TicketStatus.PENDING,
        actor_type=actor_type,
        reason=reason,
    )
    return ticket


def can_transition(
    current_status: TicketStatus,
    target_status: TicketStatus
) -> bool:
    """
    判断工单能否从当前状态流转到目标状态。
    """
    allowed_statuses = ALLOWED_TRANSITIONS.get(current_status, [])

    # 目标状态在允许列表中，才允许流转
    return target_status in allowed_statuses

def transition_ticket(
    ticket,
    target_status: TicketStatus,
    now: datetime | None = None,
) -> bool:
    """
    执行工单状态流转，并在关键节点记录时间。

    接单时：记录 accepted_at
    提交维修结果时：记录 submitted_at

    返回 True 表示流转成功，False 表示不允许流转。
    """

    # 获取工单当前状态
    current_status = TicketStatus(ticket.status)

    # 先检查状态流转是否合法
    if not can_transition(current_status, target_status):
        return False

    # 允许调用方传入同一个时间点，保证关联记录的时间一致
    if now is None:
        now = datetime.now()

    # 进入 accepted 状态，说明维修人员已接单
    if target_status == TicketStatus.ACCEPTED:
        ticket.accepted_at = now

    # 进入 pending_review 状态，说明维修人员已提交处理结果
    elif target_status == TicketStatus.PENDING_REVIEW:
        ticket.submitted_at = now
    elif target_status == TicketStatus.PROCESSING:
        # 企服验收退回后，工程部需要重新提交维修结果。
        ticket.submitted_at = None

    # 更新工单状态
    ticket.status = target_status.value

    return True


def transition_ticket_with_event(
    db: Session,
    ticket,
    target_status: TicketStatus,
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> bool:
    """执行状态流转并把审计事件加入同一个事务。"""
    current_status = TicketStatus(ticket.status)
    event_time = now or datetime.now()
    if not transition_ticket(ticket, target_status, now=event_time):
        return False

    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            from_status=current_status.value,
            to_status=target_status.value,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            occurred_at=event_time,
            is_inferred=False,
        )
    )
    return True


def accept_ticket(
    db: Session,
    ticket: Ticket,
    request: TicketAcceptRequest,
    now: datetime | None = None,
) -> RepairPersonnel:
    """校验当前分派人员身份，并完成工程人员接单。"""

    # 接单必须从待接单状态开始，不能重复接单或跳过状态流转
    if ticket.status != TicketStatus.PENDING.value:
        raise ValueError("只有待接单状态的工单才能接单")

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

    # 分派记录是责任归属依据，只有当前分派人员可以确认接单
    current_assignment = (
        db.query(TicketAssignment)
        .filter(
            TicketAssignment.ticket_id == ticket.id,
            TicketAssignment.personnel_id == personnel.id,
            TicketAssignment.is_current.is_(True),
        )
        .first()
    )
    if current_assignment is None:
        raise ValueError("该工程人员不是当前分派人员，不能接单")

    accepted = transition_ticket_with_event(
        db,
        ticket,
        TicketStatus.ACCEPTED,
        actor_type="person",
        actor_id=personnel.personnel_no,
        reason="工程人员接单",
        now=now,
    )
    if not accepted:
        # 保留状态机的最终判断，避免服务层校验和状态机规则分叉
        raise ValueError("只有待接单状态的工单才能接单")

    return personnel


def start_processing_ticket(
    db: Session,
    ticket: Ticket,
    request: TicketStartProcessingRequest,
    now: datetime | None = None,
) -> RepairPersonnel:
    """校验已接单工程人员身份，并将工单置为处理中。"""

    # 只有已经接单的工单才能开始维修
    if ticket.status != TicketStatus.ACCEPTED.value:
        raise ValueError("只有已接单状态的工单才能开始维修")

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

    # 接单后仍以当前分派记录作为责任归属依据
    current_assignment = (
        db.query(TicketAssignment)
        .filter(
            TicketAssignment.ticket_id == ticket.id,
            TicketAssignment.personnel_id == personnel.id,
            TicketAssignment.is_current.is_(True),
        )
        .first()
    )
    if current_assignment is None:
        raise ValueError("该工程人员不是当前分派人员，不能开始维修")

    started = transition_ticket_with_event(
        db,
        ticket,
        TicketStatus.PROCESSING,
        actor_type="person",
        actor_id=personnel.personnel_no,
        reason="工程人员开始维修",
        now=now,
    )
    if not started:
        raise ValueError("只有已接单状态的工单才能开始维修")

    return personnel
