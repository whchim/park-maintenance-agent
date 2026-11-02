"""提醒通过真实隔离业务API测试；只推进测试时钟，不改8003或真实任务到期日。"""

from datetime import timedelta
from threading import Thread

import pytest

from app.services.feishu_bridge import BridgeError
from app.services.feishu_reminders import ReminderChannel
from app.services.reminder_policy import deadline_text, follow_up_due, parse_time, ticket_reminder_kinds
from test_feishu_engineering import setup, prepare_processing


@pytest.fixture
def reminder(setup):
    client, engineering, store, business, messenger, ticket = setup
    clock = [parse_time(ticket['created_at'])]
    channel = ReminderChannel(store, business, messenger, engineering.identities,
                              {'招商演示员': 'ou_investment'}, now=lambda: clock[0])
    return (*setup, channel, clock)


def drain(channel):
    for _ in range(30):
        if not channel.send_one():
            return
    pytest.fail('提醒发送未收敛')


def rows(channel, kind):
    return channel.store.db.execute('SELECT * FROM reminder_deliveries WHERE kind=?', (kind,)).fetchall()


def lead(client):
    result = client.post('/api/v1/agents/investment/confirm', json={
        'customer_name': '提醒测试企业', 'intent_level': 'A', 'owner': '招商演示员',
        'follow_up_subject': '确认需求', 'follow_up_description': '仅用于隔离测试', 'idempotency_key': 'reminder-lead'})
    assert result.status_code == 200
    return result.json()['follow_up_task']


def test_new_ticket_push_is_read_only_and_restart_deduplicates(reminder):
    client, engineering, store, business, messenger, ticket, channel, clock = reminder
    before = client.get('/api/v1/tickets').json()
    business.calls.clear()
    assert channel.scan()
    drain(channel)
    assert len(messenger.sent) == 1 and messenger.sent[0]['owner'] == 'ou_manager'
    assert '接单截止' in messenger.sent[0]['content']['text']
    assert rows(channel, 'ticket_new')[0]['sent_at'] == clock[0].isoformat()
    reopened = ReminderChannel(store, business, messenger, engineering.identities, now=lambda: clock[0])
    assert reopened.scan()
    drain(reopened)
    assert len(messenger.sent) == 1
    assert all(method == 'GET' for method, path in business.calls)
    assert client.get('/api/v1/tickets').json() == before


def test_critical_five_minutes_start_only_after_successful_push(reminder, monkeypatch):
    client, engineering, store, business, messenger, ticket, channel, clock = reminder
    critical = client.post('/api/v1/tickets', json={
        'location': 'A栋模拟点', 'issue_type': '模拟特急故障', 'description': '仅测试提醒', 'priority': 'critical'}).json()
    clock[0] = parse_time(critical['created_at']) + timedelta(days=1)
    assert ticket_reminder_kinds(critical, clock[0]) == []
    messenger.fail = True
    channel.scan()
    drain(channel)
    channel.scan()
    assert not rows(channel, 'critical_no_response')
    assert all(row['sent_at'] is None for row in rows(channel, 'ticket_new'))
    messenger.fail = False
    monkeypatch.setattr('app.services.feishu_reminders.time.time', lambda: 10**12)
    drain(channel)
    receipt = next(row for row in rows(channel, 'ticket_new') if row['entity_id'] == critical['id'])
    assert receipt['sent_at'] == clock[0].isoformat()
    clock[0] += timedelta(minutes=4, seconds=59)
    channel.scan()
    assert not rows(channel, 'critical_no_response')
    clock[0] += timedelta(seconds=1)
    channel.scan()
    drain(channel)
    assert len(rows(channel, 'critical_no_response')) == 1
    assert rows(channel, 'critical_no_response')[0]['status'] == 'sent'
    channel.scan()
    drain(channel)
    assert len(rows(channel, 'critical_no_response')) == 1
    assert client.get(f"/api/v1/tickets/{critical['id']}").json()['status'] == 'pending'


def test_failed_reminder_retries_same_uuid_and_stops_after_three(reminder, monkeypatch):
    client, engineering, store, business, messenger, ticket, channel, clock = reminder
    attempts = []
    def failed(owner, kind, content, key):
        attempts.append(key)
        raise BridgeError('feishu_send_network_error')
    monkeypatch.setattr(messenger, 'send', failed)
    channel.scan()
    channel.send_one()
    monkeypatch.setattr('app.services.feishu_reminders.time.time', lambda: 10**12)
    channel.send_one()
    monkeypatch.setattr('app.services.feishu_reminders.time.time', lambda: 10**13)
    channel.send_one()
    channel.scan()
    assert not channel.send_one()
    assert len(attempts) == 3 and len(set(attempts)) == 1
    row = rows(channel, 'ticket_new')[0]
    assert row['status'] == 'failed' and row['sent_at'] is None


def test_due_followup_pushes_once_to_explicit_owner(reminder):
    client, engineering, store, business, messenger, ticket, channel, clock = reminder
    task = lead(client)
    channel.scan()
    drain(channel)
    assert not rows(channel, 'follow_up_due')
    clock[0] = parse_time(task['due_at'])
    channel.scan()
    drain(channel)
    row = rows(channel, 'follow_up_due')[0]
    assert row['owner'] == 'ou_investment' and row['status'] == 'sent'
    channel.scan()
    drain(channel)
    assert len(rows(channel, 'follow_up_due')) == 1
    assert '提醒测试企业' in next(item['content']['text'] for item in messenger.sent if item['owner'] == 'ou_investment')


def test_completed_followup_cancels_queued_notification(reminder):
    client, engineering, store, business, messenger, ticket, channel, clock = reminder
    task = lead(client)
    clock[0] = parse_time(task['due_at'])
    channel.scan()
    result = client.post(f"/api/v1/agents/investment/follow-ups/{task['id']}/complete", json={
        'actor': '招商演示员', 'outcome': '已完成模拟回访', 'idempotency_key': 'reminder-complete'})
    assert result.status_code == 200
    drain(channel)
    assert rows(channel, 'follow_up_due')[0]['status'] == 'cancelled'
    assert not any(item['owner'] == 'ou_investment' for item in messenger.sent)


def test_unmapped_owner_gets_no_customer_information(reminder):
    client, engineering, store, business, messenger, ticket, channel, clock = reminder
    task = lead(client)
    channel.investment = {'其他负责人': 'ou_investment'}
    clock[0] = parse_time(task['due_at'])
    channel.scan()
    drain(channel)
    assert channel.summary()['unmapped_count'] == 1
    assert not rows(channel, 'follow_up_due')
    assert not any(item['owner'] == 'ou_investment' for item in messenger.sent)


def test_revoked_recipient_cannot_receive_already_queued_customer_reminder(reminder):
    client, engineering, store, business, messenger, ticket, channel, clock = reminder
    task = lead(client)
    clock[0] = parse_time(task['due_at'])
    channel.scan()
    assert rows(channel, 'follow_up_due')
    channel.investment.clear()
    drain(channel)
    assert rows(channel, 'follow_up_due')[0]['status'] == 'cancelled'
    assert not any(item['owner'] == 'ou_investment' for item in messenger.sent)


def test_one_managers_receipt_does_not_start_failed_managers_timer(reminder):
    from app.services.feishu_engineering import EngineeringIdentity
    client, engineering, store, business, messenger, ticket, channel, clock = reminder
    channel.engineering['ou_second'] = EngineeringIdentity(frozenset({'dispatcher'}), 'SECOND')
    critical = client.post('/api/v1/tickets', json={
        'location': 'A栋', 'issue_type': '模拟故障', 'description': '仅测试回执归属', 'priority': 'critical'}).json()
    channel.scan()
    with store.lock, store.db:
        store.db.execute("UPDATE reminder_deliveries SET status='failed' WHERE owner='ou_second'")
    drain(channel)
    clock[0] += timedelta(minutes=6)
    channel.scan()
    assert [row['owner'] for row in rows(channel, 'critical_no_response')] == ['ou_manager']


def test_assignment_acceptance_cancels_old_unanswered_notice(reminder):
    client, engineering, store, business, messenger, ticket, channel, clock = reminder
    clock[0] += timedelta(days=2)
    channel.scan()
    assert rows(channel, 'accept_overdue')
    client.post('/api/v1/tickets/1/assignments', json={'personnel_no': 'ENG-DEMO-001', 'assigned_by': 'TEST'})
    client.post('/api/v1/tickets/1/accept', json={'personnel_no': 'ENG-DEMO-001'})
    drain(channel)
    assert all(row['status'] == 'cancelled' for row in rows(channel, 'ticket_new') + rows(channel, 'accept_overdue'))


def test_process_overdue_stops_after_repair_submitted(reminder):
    client, engineering, store, business, messenger, ticket, channel, clock = reminder
    prepare_processing((client, engineering, store, business, messenger, ticket))
    clock[0] = parse_time(client.get('/api/v1/tickets/1').json()['accepted_at']) + timedelta(hours=49)
    channel.scan()
    assert len(rows(channel, 'process_overdue')) == 2
    client.post('/api/v1/tickets/1/repair-records', json={'personnel_no': 'ENG-DEMO-001', 'repair_description': '测试提交'})
    drain(channel)
    assert all(row['status'] == 'cancelled' for row in rows(channel, 'process_overdue'))


def test_concurrent_scans_and_sends_keep_one_delivery(reminder):
    client, engineering, store, business, messenger, ticket, channel, clock = reminder
    scans = [Thread(target=channel.scan) for _ in range(3)]
    for thread in scans: thread.start()
    for thread in scans: thread.join()
    sends = [Thread(target=channel.send_one) for _ in range(3)]
    for thread in sends: thread.start()
    for thread in sends: thread.join()
    assert len(rows(channel, 'ticket_new')) == 1 and len(messenger.sent) == 1


def test_timezone_and_completed_ticket_deadlines():
    ticket = {'id': 1, 'ticket_no': 'WX-DEMO', 'created_at': '2026-11-01T01:00:00Z',
              'accepted_at': None, 'submitted_at': None, 'status': 'pending', 'priority': 'urgent'}
    assert '2026-11-01 13:00:00' in deadline_text(ticket)
    assert ticket_reminder_kinds(ticket, parse_time('2026-11-01T13:00:00')) == ['accept_overdue']
    ticket['status'] = 'completed'
    assert ticket_reminder_kinds(ticket, parse_time('2026-12-01T13:00:00')) == []
    assert '截止' not in deadline_text(ticket)
    assert not follow_up_due({'status': 'pending', 'due_at': None}, parse_time('2026-11-01T13:00:00'))
