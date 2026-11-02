from datetime import datetime
from types import SimpleNamespace

import pytest

from app.schemas.ticket import TicketStatus
from app.services.ticket_service import can_transition, transition_ticket


def test_pending_can_be_accepted():
    # 待接单 -> 已接单：应该允许
    assert can_transition(
        TicketStatus.PENDING,
        TicketStatus.ACCEPTED
    )


def test_pending_cannot_be_completed():
    # 待接单 -> 已完成：应该拒绝
    assert not can_transition(
        TicketStatus.PENDING,
        TicketStatus.COMPLETED
    )


def test_completed_cannot_continue():
    # 已完成是终态，不能继续流转
    assert not can_transition(
        TicketStatus.COMPLETED,
        TicketStatus.ACCEPTED
    )


def test_transition_uses_supplied_timestamp():
    """状态机应使用调用方提供的时间，便于关联记录保持一致。"""
    submitted_at = datetime(2026, 10, 31, 20, 0, 0)
    ticket = SimpleNamespace(status=TicketStatus.PROCESSING.value)

    assert transition_ticket(
        ticket,
        TicketStatus.PENDING_REVIEW,
        now=submitted_at,
    )
    assert ticket.status == TicketStatus.PENDING_REVIEW.value
    assert ticket.submitted_at == submitted_at
