from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.sla_service import check_ticket_sla


def test_normal_ticket_not_accept_timeout():
    """普通工单创建 23 小时，还没有超过 24 小时接单时限。"""

    created_at = datetime(2026, 10, 31, 8, 0, 0)

    ticket = SimpleNamespace(
        priority="normal",
        created_at=created_at,
        accepted_at=None,
        submitted_at=None,
    )

    # 模拟创建 23 小时后的时间
    now = created_at + timedelta(hours=23)

    result = check_ticket_sla(ticket, now)

    assert result["accept_timeout"] is False
    assert result["process_timeout"] is False


def test_normal_ticket_accept_timeout():
    """普通工单创建 25 小时，超过 24 小时接单时限。"""

    created_at = datetime(2026, 10, 31, 8, 0, 0)

    ticket = SimpleNamespace(
        priority="normal",
        created_at=created_at,
        accepted_at=None,
        submitted_at=None,
    )

    # 模拟创建 25 小时后的时间
    now = created_at + timedelta(hours=25)

    result = check_ticket_sla(ticket, now)

    assert result["accept_timeout"] is True


def test_critical_ticket_process_timeout():
    """紧急工单接单 31 分钟，超过 30 分钟处理时限。"""

    accepted_at = datetime(2026, 10, 31, 8, 0, 0)

    ticket = SimpleNamespace(
        priority="critical",
        created_at=accepted_at - timedelta(minutes=1),
        accepted_at=accepted_at,
        submitted_at=None,
    )

    # 模拟接单 31 分钟后的时间
    now = accepted_at + timedelta(minutes=31)

    result = check_ticket_sla(ticket, now)

    assert result["process_timeout"] is True

def test_accepted_ticket_should_not_accept_timeout():
    """已经接单的工单，不再计算接单超时。"""

    created_at = datetime(2026, 10, 31, 8, 0, 0)
    accepted_at = created_at + timedelta(minutes=10)

    ticket = SimpleNamespace(
        priority="normal",
        created_at=created_at,
        accepted_at=accepted_at,
        submitted_at=None,
    )

    # 即使已经超过创建后的 24 小时，也不应该再判定接单超时
    now = created_at + timedelta(hours=30)

    result = check_ticket_sla(ticket, now)

    assert result["accept_timeout"] is False
    assert result["accept_deadline"] is None


def test_submitted_ticket_should_not_process_timeout():
    """已经提交维修结果的工单，不再计算处理超时。"""

    created_at = datetime(2026, 10, 31, 8, 0, 0)
    accepted_at = created_at + timedelta(minutes=10)
    submitted_at = accepted_at + timedelta(minutes=20)

    ticket = SimpleNamespace(
        priority="critical",
        created_at=created_at,
        accepted_at=accepted_at,
        submitted_at=submitted_at,
    )

    # 即使提交后已经过去很久，也不应该再判定处理超时
    now = submitted_at + timedelta(hours=2)

    result = check_ticket_sla(ticket, now)

    assert result["process_timeout"] is False
    assert result["process_deadline"] is None    