from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.ticket import Ticket
from app.models.acceptance import AcceptanceRecord
from app.schemas.acceptance import AcceptanceRecordCreate
from app.schemas.ticket import TicketStatus
from app.services.acceptance_service import create_acceptance_record


@pytest.fixture
def db_session():
    """创建独立的内存数据库供测试使用。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    TestingSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
    )

    Base.metadata.create_all(bind=engine)

    db = TestingSessionLocal()

    yield db

    db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def pending_review_ticket(db_session):
    """创建一条待验收工单。"""

    ticket = Ticket(
        ticket_no="WX-TEST-001",
        location="A栋3楼卫生间",
        issue_type="漏水",
        description="水管漏水",
        priority="urgent",
        status=TicketStatus.PENDING_REVIEW.value,
    )

    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)

    return ticket


def test_create_acceptance_record_success(db_session, pending_review_ticket):
    """待验收工单可以成功验收。"""

    request = AcceptanceRecordCreate(
        acceptance_comment="维修完成，现场确认正常",
        accepted_by="企服人员A",
    )

    record = create_acceptance_record(
        db=db_session,
        ticket=pending_review_ticket,
        request=request,
    )

    # 验收记录应保存成功
    assert record.id is not None
    assert record.ticket_id == pending_review_ticket.id
    assert record.acceptance_comment == "维修完成，现场确认正常"
    assert record.accepted_by == "企服人员A"
    assert isinstance(record.accepted_at, datetime)

    # 工单应更新为已完成
    db_session.refresh(pending_review_ticket)
    assert pending_review_ticket.status == TicketStatus.COMPLETED.value


def test_create_acceptance_record_rejects_wrong_status(
    db_session,
    pending_review_ticket,
):
    """非待验收工单不能进行验收。"""

    pending_review_ticket.status = TicketStatus.PROCESSING.value
    db_session.commit()

    request = AcceptanceRecordCreate(
        acceptance_comment="维修完成，现场确认正常",
    )

    with pytest.raises(ValueError, match="只有待验收状态的工单才能进行验收"):
        create_acceptance_record(
            db=db_session,
            ticket=pending_review_ticket,
            request=request,
        )

    # 工单状态不应被修改
    db_session.refresh(pending_review_ticket)
    assert pending_review_ticket.status == TicketStatus.PROCESSING.value


def test_create_acceptance_record_rejects_empty_comment(
    db_session,
    pending_review_ticket,
):
    """只包含空格的验收意见不能提交。"""

    request = AcceptanceRecordCreate(
        acceptance_comment="   ",
    )

    with pytest.raises(ValueError, match="验收意见不能为空"):
        create_acceptance_record(
            db=db_session,
            ticket=pending_review_ticket,
            request=request,
        )

    # 验收失败后，工单仍然处于待验收状态
    db_session.refresh(pending_review_ticket)
    assert pending_review_ticket.status == TicketStatus.PENDING_REVIEW.value


def test_rejected_acceptance_returns_ticket_to_processing(db_session, pending_review_ticket):
    request = AcceptanceRecordCreate(
        acceptance_comment="现场仍有渗漏，请重新处理",
        accepted_by="企服人员A",
        accepted=False,
    )

    record = create_acceptance_record(db_session, pending_review_ticket, request)

    assert record.accepted is False
    db_session.refresh(pending_review_ticket)
    assert pending_review_ticket.status == TicketStatus.PROCESSING.value
    assert pending_review_ticket.submitted_at is None
