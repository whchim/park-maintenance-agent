from datetime import datetime

from sqlalchemy.orm import Session

from app.models.ticket import Ticket
from app.models.acceptance import AcceptanceRecord
from app.schemas.acceptance import AcceptanceRecordCreate
from app.schemas.ticket import TicketStatus
from app.services.ticket_service import transition_ticket_with_event


def create_acceptance_record(
    db: Session,
    ticket: Ticket,
    request: AcceptanceRecordCreate,
) -> AcceptanceRecord:
    """
    创建工单验收记录。

    业务规则：
    1. 只有待验收状态的工单才能验收。
    2. 验收意见不能为空。
    3. 保存验收记录。
    4. 将工单状态更新为已完成。
    """

    # 只有维修人员提交维修结果后，企服才能验收
    if ticket.status != TicketStatus.PENDING_REVIEW.value:
        raise ValueError("只有待验收状态的工单才能进行验收")

    # 去掉首尾空格，防止只输入空格
    acceptance_comment = request.acceptance_comment.strip()

    if not acceptance_comment:
        raise ValueError("验收意见不能为空")

    # 验收时间
    now = datetime.now()

    # 创建验收记录
    acceptance_record = AcceptanceRecord(
        ticket_id=ticket.id,
        acceptance_comment=acceptance_comment,
        accepted_by=request.accepted_by,
        accepted=request.accepted,
        accepted_at=now,
    )

    db.add(acceptance_record)

    # 验收通过后，通过统一状态机进入已完成状态
    target_status = TicketStatus.COMPLETED if request.accepted else TicketStatus.PROCESSING
    reason = "企服人员验收通过" if request.accepted else "企服验收未通过，退回工程部处理"
    if not transition_ticket_with_event(
        db,
        ticket,
        target_status,
        actor_type="person",
        actor_id=request.accepted_by,
        reason=reason,
        now=now,
    ):
        raise ValueError("只有待验收状态的工单才能进行验收")

    # 由调用方统一提交事务，但先 flush 以返回可追踪的验收记录编号。
    db.flush()
    return acceptance_record
