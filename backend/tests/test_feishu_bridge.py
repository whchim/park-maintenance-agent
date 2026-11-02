"""飞书桥接的真实业务API集成测试；只使用独立内存库和模拟发信器。"""

import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.database import engine as business_engine
from app.services.feishu_bridge import (
    BridgeError, BridgeStore, FeishuBridge, IncomingMessage, LocalBusinessClient,
    parse_single_chat, single_instance, format_workflow,
)
from scripts.run_demo import create_demo_app
from scripts.run_feishu import SafeSDKLog


APP = 'cli_test1234'


def test_progress_does_not_reuse_intake_model_label():
    message = format_workflow({'draft': {'mode': 'llm', 'ai': {'model': 'qwen-old-review'}},
        'ticket': {'ticket_no': 'WX-DEMO', 'location': 'A栋302', 'status': 'processing'}})
    assert '工单进度' in message and '未调用AI' in message
    assert 'qwen-old-review' not in message and '报修受理' not in message


def payload(text, message_id='om_1', owner='ou_demo', chat='oc_demo'):
    return {'header': {'event_type': 'im.message.receive_v1', 'app_id': APP},
            'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': owner}},
                      'message': {'message_id': message_id, 'chat_id': chat, 'chat_type': 'p2p',
                                  'message_type': 'text', 'content': json.dumps({'text': text})}}}


class Business(LocalBusinessClient):
    def __init__(self, client):
        self.client, self.calls = client, []

    def call(self, method, path, payload=None):
        self.calls.append((method, path))
        response = self.client.request(method, path, json=payload)
        if response.status_code >= 400:
            raise BridgeError(f'business_http_{response.status_code}')
        return response.json()


class Messenger:
    def __init__(self):
        self.sent, self.fail = [], False

    def reply(self, message_id, text, key):
        if self.fail:
            raise BridgeError('feishu_reply_network_error')
        self.sent.append((message_id, text, key))


@pytest.fixture
def setup(tmp_path):
    def blocked(*args):
        pytest.fail('飞书桥接测试不得连接现有业务数据库')
    event.listen(business_engine, 'connect', blocked)
    store = BridgeStore(tmp_path / 'bridge.sqlite3', APP)
    try:
        with TestClient(create_demo_app()) as client:
            business, messenger = Business(client), Messenger()
            bridge = FeishuBridge(store, business, messenger, APP, {'ou_demo', 'ou_other'})
            yield client, bridge, store, business, messenger
    finally:
        store.close()
        event.remove(business_engine, 'connect', blocked)


def send(bridge, text, number, owner='ou_demo', chat='oc_demo'):
    assert bridge.receive(payload(text, f'om_{number}', owner, chat)) == 'queued'
    assert bridge.process_one()


def test_real_api_followup_explicit_confirmation_and_progress(setup):
    client, bridge, store, business, messenger = setup
    send(bridge, '报修 漏水', 1)
    assert '尚未创建工单' in messenger.sent[-1][1]
    send(bridge, '确认 普通', 2)
    assert '仍有缺项' in messenger.sent[-1][1]
    assert client.get('/api/v1/tickets').json() == []
    send(bridge, '位置在A栋302', 3)
    assert '请核对后发送' in messenger.sent[-1][1]
    send(bridge, '确认', 4)
    assert '没有执行建单' in messenger.sent[-1][1]
    assert client.get('/api/v1/tickets').json() == []
    send(bridge, '确认 加急', 5)
    tickets = client.get('/api/v1/tickets').json()
    assert len(tickets) == 1 and tickets[0]['priority'] == 'urgent'
    assert tickets[0]['assignee'] is None
    send(bridge, '确认 特急', 6)
    send(bridge, '进度', 7)
    assert tickets[0]['ticket_no'] in messenger.sent[-1][1]
    assert client.get('/api/v1/tickets').json() == tickets
    assert store.summary()['inbox_counts'] == {'done': 7}


@pytest.mark.parametrize('start', ['新报修', '报修', '新报修：'])
def test_empty_repair_start_replaces_old_session_without_changing_old_ticket(setup, start):
    client, bridge, store, business, messenger = setup
    send(bridge, '报修 B栋501灯不亮', 1)
    send(bridge, '确认普通', 2)
    old_session = store.session('ou_demo', 'oc_demo')
    old_ticket = client.get('/api/v1/tickets').json()[0]
    send(bridge, start, 3)
    new_session = store.session('ou_demo', 'oc_demo')
    assert new_session != old_session
    assert '已开始新的报修草稿' in messenger.sent[-1][1]
    workflow = client.get(f'/api/v1/agent/runs/{new_session}').json()
    assert workflow['status'] == 'waiting_input' and workflow['missing_fields']
    assert workflow['ticket_id'] is None
    send(bridge, '确认普通', 4)
    assert '仍有缺项' in messenger.sent[-1][1]
    assert client.get('/api/v1/tickets').json() == [old_ticket]
    # 隔离测试强制规则模式，使用已有“漏水”关键词；真实千问可理解口语“滴水”。
    send(bridge, '位置在A栋302，空调排水管持续漏水。', 5)
    assert '请核对后发送' in messenger.sent[-1][1]
    send(bridge, '确认普通', 6)
    tickets = client.get('/api/v1/tickets').json()
    assert len(tickets) == 2
    assert next(item for item in tickets if item['id'] == old_ticket['id']) == old_ticket
    created = next(item for item in tickets if item['id'] != old_ticket['id'])
    assert 'A栋302' in created['location']
    assert f"工单ID：{created['id']}" in messenger.sent[-1][1]
    send(bridge, '确认普通', 7)
    assert client.get('/api/v1/tickets').json() == tickets


@pytest.mark.parametrize(('text', 'expected_priority'), [
    ('确认普通', 'normal'), ('确认加急', 'urgent'), ('确认特急', 'critical'),
    ('确认 普通', 'normal'), ('确认：加急', 'urgent'), ('确认:特急', 'critical'),
    ('确认报修普通', 'normal'), ('确认\u3000普通', 'normal'), ('确认普通。', 'normal'),
])
def test_explicit_confirmation_accepts_natural_chinese_formats(setup, text, expected_priority):
    client, bridge, store, business, messenger = setup
    send(bridge, '报修 A栋302漏水', 1)
    workflow_id = store.session('ou_demo', 'oc_demo')
    draft = client.get(f'/api/v1/agent/runs/{workflow_id}').json()['draft']
    send(bridge, text, 2)
    tickets = client.get('/api/v1/tickets').json()
    assert len(tickets) == 1 and tickets[0]['priority'] == expected_priority
    assert tickets[0]['description'] == draft['description']
    assert tickets[0]['ticket_no'] in messenger.sent[-1][1]
    send(bridge, text, 3)
    assert client.get('/api/v1/tickets').json() == tickets


@pytest.mark.parametrize('priority', ['普通', '加急', '特急'])
def test_priority_selection_does_not_pollute_draft_or_create_ticket(setup, priority):
    client, bridge, store, business, messenger = setup
    send(bridge, '报修 A栋302漏水', 1)
    workflow_id = store.session('ou_demo', 'oc_demo')
    url = f'/api/v1/agent/runs/{workflow_id}'
    before = client.get(url).json()
    business.calls.clear()
    send(bridge, priority, 2)
    assert f'确认{priority}' in messenger.sent[-1][1]
    assert client.get(url).json() == before
    assert all(method == 'GET' for method, _ in business.calls)
    assert client.get('/api/v1/tickets').json() == []
    # 复现截图：单独回复优先级后，再发送无空格确认即可建单，描述保持原样。
    send(bridge, '确认' + priority, 3)
    tickets = client.get('/api/v1/tickets').json()
    assert len(tickets) == 1 and tickets[0]['description'] == before['draft']['description']


@pytest.mark.parametrize('text', ['普通', '确认普通'])
def test_priority_and_compact_confirmation_preserve_missing_information_gate(setup, text):
    client, bridge, store, business, messenger = setup
    send(bridge, '报修 漏水', 1)
    business.calls.clear()
    send(bridge, text, 2)
    assert '仍有缺项' in messenger.sent[-1][1]
    assert all(method == 'GET' for method, _ in business.calls)
    assert client.get('/api/v1/tickets').json() == []


@pytest.mark.parametrize('text', ['确认', '确认普通吗', '确认普通或加急', '确认加急但先不提交'])
def test_ambiguous_confirmation_does_not_create_ticket_or_update_draft(setup, text):
    client, bridge, store, business, messenger = setup
    send(bridge, '报修 A栋302漏水', 1)
    business.calls.clear()
    send(bridge, text, 2)
    assert '没有执行建单' in messenger.sent[-1][1]
    assert all(method == 'GET' for method, _ in business.calls)
    assert client.get('/api/v1/tickets').json() == []


def test_duplicate_and_reply_failure_resume_without_repeating_business(setup, monkeypatch):
    client, bridge, store, business, messenger = setup
    messenger.fail = True
    message = payload('报修 A栋302漏水')
    assert bridge.receive(message) == 'queued'
    assert bridge.receive(message) == 'duplicate'
    bridge.process_one()
    calls = list(business.calls)
    assert store.summary()['inbox_counts'] == {'queued': 1}
    messenger.fail = False
    monkeypatch.setattr('app.services.feishu_bridge.time.time', lambda: 10**12)
    bridge.process_one()
    assert business.calls == calls
    assert len(messenger.sent) == 1
    assert bridge.receive(message) == 'duplicate'
    assert not bridge.process_one()


def test_conversation_isolation_and_owner_check(setup):
    client, bridge, store, business, messenger = setup
    send(bridge, '报修 A栋302漏水', 1)
    first = store.session('ou_demo', 'oc_demo')
    send(bridge, '报修 B栋102门禁损坏', 2, 'ou_other', 'oc_other')
    second = store.session('ou_other', 'oc_other')
    assert first != second
    send(bridge, '进度', 3)
    assert 'A栋' in messenger.sent[-1][1] and 'B栋' not in messenger.sent[-1][1]
    store.set_session('ou_demo', 'oc_demo', second)
    send(bridge, '确认 普通', 4)
    assert store.summary()['last_error_code'] == 'workflow_owner_mismatch'
    assert client.get('/api/v1/tickets').json() == []


def test_revoked_permission_does_not_process_queued_work(setup):
    client, bridge, store, business, messenger = setup
    bridge.receive(payload('报修 A栋302漏水'))
    bridge.allowed_ids = {'ou_other'}
    bridge.process_one()
    assert business.calls == [] and messenger.sent == []


def test_pairing_expiry_owner_and_code_not_persisted(setup, monkeypatch):
    client, old, store, business, messenger = setup
    bridge = FeishuBridge(store, business, messenger, APP, pairing_code='123456')
    assert bridge.receive(payload('报修 A栋302漏水')) == 'unauthorized'
    assert bridge.receive(payload('绑定 错误')) == 'unauthorized'
    expired_now = bridge.pairing_expires + 1
    monkeypatch.setattr('app.services.feishu_bridge.time.monotonic', lambda: expired_now)
    assert bridge.receive(payload('绑定 123456')) == 'unauthorized'
    bridge.pairing_expires += 1000
    assert bridge.receive(payload('绑定 123456')) == 'paired'
    assert store.next_job()['text'] == ''
    bridge.process_one()
    assert '配对完成' in messenger.sent[-1][1] and '123456' not in messenger.sent[-1][1]
    assert bridge.receive(payload('绑定 123456', 'om_2', 'ou_other')) == 'unauthorized'
    assert bridge.receive(payload('进度', 'om_3', chat='oc_other')) == 'unauthorized'
    assert business.calls == []


@pytest.mark.parametrize('change', ['group', 'bot', 'image', 'app', 'content', 'id'])
def test_unsupported_events_are_ignored(change, setup):
    client, bridge, store, business, messenger = setup
    value = payload('报修 A栋302漏水')
    if change == 'group': value['event']['message']['chat_type'] = 'group'
    if change == 'bot': value['event']['sender']['sender_type'] = 'app'
    if change == 'image': value['event']['message']['message_type'] = 'image'
    if change == 'app': value['header']['app_id'] = 'cli_other'
    if change == 'content': value['event']['message']['content'] = 'invalid JSON'
    if change == 'id': value['event']['message']['message_id'] = '../bad'
    assert parse_single_chat(value, APP) is None
    assert bridge.receive(value) == 'ignored'
    assert not bridge.process_one() and business.calls == []


def test_restart_recovers_cached_reply_and_sessions(tmp_path):
    path = tmp_path / 'state.sqlite3'
    store = BridgeStore(path, APP)
    message = IncomingMessage('om_1', 'oc_demo', 'ou_demo', '报修 模拟')
    store.pair(message)
    store.set_session(message.open_id, message.chat_id, 'wf_' + 'a' * 24)
    store.save_reply(message.message_id, '已保存的回复')
    store.close()
    reopened = BridgeStore(path, APP)
    try:
        class NoBusiness:
            def require_demo(self): pytest.fail('恢复已生成回复不得重复执行业务')
        messenger = Messenger()
        bridge = FeishuBridge(reopened, NoBusiness(), messenger, APP)
        assert bridge.process_one()
        assert messenger.sent[0][1] == '已保存的回复'
        assert reopened.session('ou_demo', 'oc_demo') == 'wf_' + 'a' * 24
        with pytest.raises(BridgeError, match='state_app_mismatch'):
            BridgeStore(path, 'cli_other')
    finally:
        reopened.close()


def test_retry_keeps_conversation_order_and_stops_after_three_failures(setup, monkeypatch):
    client, bridge, store, business, messenger = setup
    messenger.fail = True
    bridge.receive(payload('帮助', 'om_1'))
    bridge.receive(payload('报修 A栋302漏水', 'om_2'))
    bridge.process_one()
    assert not bridge.process_one()
    assert business.calls == []
    store.failed('om_1', 'feishu_reply_network_error')
    store.failed('om_1', 'feishu_reply_network_error')
    assert store.summary()['inbox_counts'] == {'failed': 1, 'queued': 1}
    messenger.fail = False
    bridge.process_one()
    assert messenger.sent[0][0] == 'om_2'


@pytest.mark.parametrize('url', ['https://example.com', 'http://localhost:8003', 'http://127.0.0.1:8003@evil.com',
                                 'http://127.0.0.1:8003/path', 'http://127.0.0.1:80', 'http://127.0.0.1:8003?x=1'])
def test_nonlocal_or_ambiguous_backend_is_rejected(url):
    with pytest.raises(BridgeError, match='local_demo_url_required'):
        LocalBusinessClient(url)


def test_production_backend_rejected_before_business_action(setup, monkeypatch):
    client, bridge, store, business, messenger = setup
    original = business.call
    def call(method, path, payload=None):
        if path == '/api/v1/agents/runtime': return {'environment': {'name': 'production'}}
        return original(method, path, payload)
    monkeypatch.setattr(business, 'call', call)
    send(bridge, '报修 A栋302漏水', 1)
    assert store.summary()['last_error_code'] == 'isolated_demo_required'
    assert business.calls == [] and messenger.sent == []


def test_stale_session_and_single_instance(setup, tmp_path):
    client, bridge, store, business, messenger = setup
    store.set_session('ou_demo', 'oc_demo', 'wf_' + 'b' * 24)
    send(bridge, '进度', 1)
    assert '原演示流程已不可用' in messenger.sent[-1][1]
    with single_instance(tmp_path / 'bridge.lock'):
        with pytest.raises(BridgeError, match='bridge_already_running'):
            with single_instance(tmp_path / 'bridge.lock'):
                pytest.fail('不应同时持有两把锁')


def test_sdk_log_drops_raw_connection_url_and_tokens(setup, capsys):
    client, bridge, store, business, messenger = setup
    logger = SafeSDKLog(store)
    logger.emit(logging.LogRecord('Lark', logging.INFO, '', 1, 'connected to wss://secret/?token=PRIVATE', (), None))
    captured = capsys.readouterr().out
    assert 'connected' in captured and 'PRIVATE' not in captured and 'wss://' not in captured
    assert store.summary()['connection'] == 'connected'
