"""Web与飞书共用隔离业务API，渠道同步不能改派、冒用身份或重放业务。"""

import json

import pytest

from app.services.feishu_engineering import EngineeringIdentity
from app.services.feishu_reminders import ReminderChannel
from test_feishu_engineering import setup, send, drain, card, callback


def assign(client, number='ENG-DEMO-001'):
    response = client.post('/api/v1/tickets/1/assignments', json={'personnel_no': number, 'assigned_by': 'WEB-DEMO'})
    assert response.status_code == 201


def start(client, number='ENG-DEMO-001'):
    assert client.post('/api/v1/tickets/1/accept', json={'personnel_no': number}).status_code == 200
    assert client.post('/api/v1/tickets/1/start-processing', json={'personnel_no': number}).status_code == 200


def sync(client, channel):
    channel.queue_sync(client.get('/api/v1/tickets').json())
    drain(channel)


def no_buttons(message):
    return not any(element.get('actions') for element in message['content']['elements'])


def test_dispatch_query_after_web_start_returns_actual_unbound_worker_without_model_or_writes(setup):
    client, channel, store, business, messenger, ticket = setup
    channel.identities.pop('ou_other')
    assign(client, 'ENG-DEMO-002')
    start(client, 'ENG-DEMO-002')
    before = client.get('/api/v1/tickets/1/assignments').json()
    business.calls.clear()
    send(channel, '派工 1', 10)
    message = card(messenger, 'ou_manager')
    text = json.dumps(message, ensure_ascii=False)
    assert '水电维修模拟员' in text and 'ENG-DEMO-002' in text and '维修中' in text
    assert '尚未绑定飞书' in text and '未调用AI' in text and no_buttons(message)
    assert all(method == 'GET' for method, path in business.calls)
    assert client.get('/api/v1/tickets/1/assignments').json() == before
    assert channel.context('ou_manager', 'oc_ou_manager') is None


@pytest.mark.parametrize('text', ['派工 1”查看工程Agent建议，并由负责人确认委派', '派工1，查看当前进度'])
def test_copied_instruction_suffix_is_only_a_status_query(setup, text):
    client, channel, store, business, messenger, ticket = setup
    assign(client, 'ENG-DEMO-002')
    start(client, 'ENG-DEMO-002')
    business.calls.clear()
    send(channel, text, 10)
    assert '维修中' in json.dumps(card(messenger, 'ou_manager'), ensure_ascii=False)
    assert all(method == 'GET' for method, path in business.calls)


def test_periodic_scan_pushes_web_assignment_to_bound_worker_once(setup):
    client, channel, store, business, messenger, ticket = setup
    assign(client)
    reminders = ReminderChannel(store, business, messenger, channel.identities, engineering_channel=channel)
    business.calls.clear()
    assert reminders.scan()
    drain(channel)
    task = card(messenger, 'ou_worker')
    assert not no_buttons(task) and '确认接单' in json.dumps(task, ensure_ascii=False)
    assert '空调维修模拟员' in json.dumps(card(messenger, 'ou_manager'), ensure_ascii=False)
    before = (len(messenger.sent), len(messenger.patches))
    reminders.scan()
    drain(channel)
    assert (len(messenger.sent), len(messenger.patches)) == before
    assert all(method == 'GET' for method, path in business.calls)
    assert client.get('/api/v1/tickets/1').json()['status'] == 'pending'


def test_web_reassignment_retires_old_worker_card_without_granting_unbound_identity(setup):
    client, channel, store, business, messenger, ticket = setup
    channel.identities.pop('ou_other')
    assign(client)
    sync(client, channel)
    old = callback(card(messenger, 'ou_worker'))
    old_id = card(messenger, 'ou_worker')['message_id']
    assign(client, 'ENG-DEMO-002')
    sync(client, channel)
    assert card(messenger, 'ou_worker')['message_id'] == old_id
    assert no_buttons(card(messenger, 'ou_worker'))
    assert channel.receive_action(old) == 'rejected'
    assert '尚未绑定飞书' in json.dumps(card(messenger, 'ou_manager'), ensure_ascii=False)
    assert not any(identity.personnel_no == 'ENG-DEMO-002' for identity in channel.identities.values())
    assert client.get('/api/v1/tickets/1').json()['assignee'] == 'ENG-DEMO-002'
    assert client.get('/api/v1/tickets/1').json()['status'] == 'pending'


def test_web_progress_sync_does_not_silently_select_material_context(setup):
    client, channel, store, business, messenger, ticket = setup
    assign(client)
    sync(client, channel)
    message_id = card(messenger, 'ou_worker')['message_id']
    start(client)
    sync(client, channel)
    assert card(messenger, 'ou_worker')['message_id'] == message_id
    assert '维修中' in json.dumps(card(messenger, 'ou_worker'), ensure_ascii=False)
    assert channel.context('ou_worker', 'oc_ou_worker') is None
    send(channel, '维修 1', 10, 'ou_worker')
    assert channel.context('ou_worker', 'oc_ou_worker')['ticket_id'] == 1


def test_stale_scan_never_pushes_old_accept_button_after_web_start(setup):
    client, channel, store, business, messenger, ticket = setup
    assign(client)
    channel.queue_sync(client.get('/api/v1/tickets').json())
    start(client)
    drain(channel)
    assert messenger.sent == []
    sync(client, channel)
    assert no_buttons(card(messenger, 'ou_worker'))
    assert '维修中' in json.dumps(card(messenger, 'ou_worker'), ensure_ascii=False)


def test_web_acceptance_updates_existing_card_and_clears_material_context(setup):
    client, channel, store, business, messenger, ticket = setup
    assign(client)
    start(client)
    send(channel, '维修 1', 10, 'ou_worker')
    old_id = card(messenger, 'ou_worker')['message_id']
    assert client.post('/api/v1/tickets/1/repair-records', json={
        'personnel_no': 'ENG-DEMO-001', 'repair_description': 'Web模拟维修完成'}).status_code == 201
    assert client.post('/api/v1/tickets/1/acceptance', json={
        'accepted': True, 'accepted_by': 'WEB-DEMO', 'acceptance_comment': 'Web模拟验收通过'}).status_code == 201
    sync(client, channel)
    assert card(messenger, 'ou_worker')['message_id'] == old_id
    assert '企服已验收并关闭' in json.dumps(card(messenger, 'ou_worker'), ensure_ascii=False)
    assert no_buttons(card(messenger, 'ou_worker'))
    assert channel.context('ou_worker', 'oc_ou_worker') is None


def test_owner_who_is_current_worker_can_continue_from_dispatch_query(setup):
    client, channel, store, business, messenger, ticket = setup
    channel.identities['ou_manager'] = EngineeringIdentity(frozenset({'dispatcher', 'technician'}), 'DEMO', 'ENG-DEMO-001')
    assign(client)
    start(client)
    send(channel, '派工 1', 10)
    assert '维修中' in json.dumps(card(messenger, 'ou_manager'), ensure_ascii=False)
    assert channel.context('ou_manager', 'oc_ou_manager')['ticket_id'] == 1


def test_malformed_dispatch_and_unknown_ticket_have_specific_guidance(setup):
    client, channel, store, business, messenger, ticket = setup
    send(channel, '派工 随便一单', 10)
    assert '只输入一条指令' in messenger.sent[-1]['content']['text']
    send(channel, '派工 999', 11)
    assert '未找到这张工单' in messenger.sent[-1]['content']['text']
    assert client.get('/api/v1/tickets/1/assignments').json() == []
