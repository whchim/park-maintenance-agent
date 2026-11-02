"""统一提醒的时间规则；只读业务快照，不调用模型、不发送消息、不改业务状态。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.sla_service import check_ticket_sla


PARK_TIMEZONE = timezone(timedelta(hours=8))
PRIORITIES = {'normal': '普通', 'urgent': '加急', 'critical': '特急'}


def park_now():
    return datetime.now(PARK_TIMEZONE).replace(tzinfo=None)


def parse_time(value):
    if value is None:
        return None
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return result.astimezone(PARK_TIMEZONE).replace(tzinfo=None) if result.tzinfo else result


def ticket_deadlines(ticket):
    """复用业务SLA，避免飞书另写一套普通/加急时限。"""
    if ticket['status'] not in ('pending', 'accepted', 'processing'):
        return {'accept_deadline': None, 'process_deadline': None}
    snapshot = SimpleNamespace(**{key: parse_time(ticket.get(key)) for key in ('created_at', 'accepted_at', 'submitted_at')},
                               priority=ticket['priority'])
    return check_ticket_sla(snapshot, now=park_now())


def deadline_text(ticket):
    times = ticket_deadlines(ticket)
    label = PRIORITIES[ticket['priority']]
    if ticket['status'] == 'pending' and ticket['priority'] == 'critical':
        return f'优先级：{label}\n特急响应：工程负责人通知发送成功后5分钟未接单，提醒电话对接。'
    deadline = times.get('accept_deadline') or times.get('process_deadline')
    if deadline:
        phase = '接单' if not ticket.get('accepted_at') else '处理'
        return f'优先级：{label}\n{phase}截止：{deadline:%Y-%m-%d %H:%M:%S}（北京时间，Demo规则）'
    return f'优先级：{label}'


def ticket_reminder_kinds(ticket, now, pushed_at=None):
    """特急无发送成功证据时绝不以建单时间代替推送时间。"""
    now = parse_time(now)
    if ticket['status'] == 'pending' and not ticket.get('accepted_at'):
        if ticket['priority'] == 'critical':
            pushed = parse_time(pushed_at)
            return ['critical_no_response'] if pushed and now >= pushed + timedelta(minutes=5) else []
        deadline = ticket_deadlines(ticket)['accept_deadline']
        return ['accept_overdue'] if deadline and now >= deadline else []
    if ticket['status'] in ('accepted', 'processing') and not ticket.get('submitted_at'):
        deadline = ticket_deadlines(ticket)['process_deadline']
        return ['process_overdue'] if deadline and now >= deadline else []
    return []


def follow_up_due(task, now):
    due = parse_time(task.get('due_at'))
    return task.get('status') == 'pending' and due is not None and parse_time(now) >= due
