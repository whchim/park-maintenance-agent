"""工程卡片通过真实业务API验收；不操作8003，也不向飞书真实发信。"""

from io import BytesIO
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import event

from app.database import engine as business_engine
from app.services.feishu_bridge import BridgeError, BridgeStore
from app.services.feishu_engineering import EngineeringChannel, EngineeringIdentity, load_identities
from scripts.run_demo import create_demo_app
from scripts.run_feishu import SDKMessenger
from test_feishu_bridge import APP, Business, payload


class EngineeringBusiness(Business):
    def upload_repair_image(self, content, media_type):
        response = self.client.post('/api/v1/attachments/images?purpose=repair&filename=feishu-repair.png',
                                    content=content, headers={'content-type': media_type})
        if response.status_code != 201:
            raise BridgeError(f'business_http_{response.status_code}')
        return response.json()


class Messenger:
    def __init__(self):
        self.sent, self.fail, self.downloads, self.patches = [], False, [], []
        output = BytesIO()
        Image.new('RGB', (12, 12), 'blue').save(output, format='PNG')
        self.image = output.getvalue()

    def send(self, owner, msg_type, content, key):
        if self.fail: raise BridgeError('feishu_send_network_error')
        # 飞书UUID去重的最小模拟；同key重试得到相同message_id。
        existing = next((item for item in self.sent if item['key'] == key), None)
        if existing: return existing['message_id'], 'oc_' + owner
        message_id = 'om_out_' + str(len(self.sent) + 1)
        self.sent.append({'owner': owner, 'type': msg_type, 'content': content, 'key': key, 'message_id': message_id})
        return message_id, 'oc_' + owner

    def patch(self, message_id, content):
        if self.fail: raise BridgeError('feishu_patch_network_error')
        message = next(item for item in self.sent if item['message_id'] == message_id)
        message['content'] = content
        self.patches.append((message_id, content))

    def download_image(self, message_id, image_key):
        self.downloads.append((message_id, image_key))
        return self.image


@pytest.fixture
def setup(tmp_path):
    def blocked(*args): pytest.fail('工程卡片测试不得连接现有业务数据库')
    event.listen(business_engine, 'connect', blocked)
    store = BridgeStore(tmp_path / 'engineering.sqlite3', APP)
    identities = {
        'ou_manager': EngineeringIdentity(frozenset({'dispatcher'}), 'MANAGER-DEMO'),
        'ou_worker': EngineeringIdentity(frozenset({'technician'}), 'WORKER-DEMO', 'ENG-DEMO-001'),
        'ou_other': EngineeringIdentity(frozenset({'technician'}), 'OTHER-DEMO', 'ENG-DEMO-002')}
    try:
        with TestClient(create_demo_app()) as client:
            business, messenger = EngineeringBusiness(client), Messenger()
            channel = EngineeringChannel(store, business, messenger, APP, identities)
            ticket = client.post('/api/v1/tickets', json={'location': 'A栋302', 'issue_type': '空调漏水',
                'description': '空调排水管漏水', 'priority': 'normal'}).json()
            yield client, channel, store, business, messenger, ticket
    finally:
        store.close()
        event.remove(business_engine, 'connect', blocked)


def drain(channel):
    for _ in range(60):
        if not channel.process_one(): return
    pytest.fail('工作队列未收敛')


def send(channel, text, number, owner='ou_manager'):
    assert channel.receive(payload(text, 'om_in_' + str(number), owner, 'oc_' + owner)) == 'queued'
    drain(channel)


def card(messenger, owner):
    return next(item for item in reversed(messenger.sent) if item['owner'] == owner and item['type'] == 'interactive')


def callback(card, nonce=None):
    if nonce is None:
        nonce = next(button['value']['nonce'] for element in card['content']['elements'] for button in element.get('actions', []))
    return {'header': {'event_type': 'card.action.trigger', 'app_id': APP},
            'event': {'operator': {'open_id': card['owner']}, 'context': {'open_message_id': card['message_id'],
                      'open_chat_id': 'oc_' + card['owner']}, 'action': {'value': {'nonce': nonce}}}}


def click(channel, value):
    assert channel.receive_action(value) == 'queued'
    drain(channel)


def prepare_processing(setup):
    client, channel, store, business, messenger, ticket = setup
    send(channel, '派工 1', 1)
    click(channel, callback(card(messenger, 'ou_manager')))
    click(channel, callback(card(messenger, 'ou_worker')))
    click(channel, callback(card(messenger, 'ou_worker')))
    assert client.get('/api/v1/tickets/1').json()['status'] == 'processing'


def send_image(channel, number=10, owner='ou_worker', key='img_demo'):
    value = payload('ignored', 'om_image_' + str(number), owner, 'oc_' + owner)
    value['event']['message'].update(message_type='image', content=json.dumps({'image_key': key}))
    result = channel.receive(value)
    if result == 'queued': drain(channel)
    return result


def test_end_to_end_dispatch_accept_start_photos_preview_and_submit(setup):
    client, channel, store, business, messenger, ticket = setup
    send(channel, '派工 1', 1)
    assert client.get('/api/v1/tickets/1/assignments').json() == []
    dispatch = callback(card(messenger, 'ou_manager'))
    assert channel.receive_action(dispatch) == 'queued'
    assert channel.receive_action(dispatch) == 'duplicate'
    drain(channel)
    assignments = client.get('/api/v1/tickets/1/assignments').json()
    assert len(assignments) == 1 and assignments[0]['assigned_by'] == 'MANAGER-DEMO'
    assert client.get('/api/v1/tickets/1').json()['status'] == 'pending'
    worker = callback(card(messenger, 'ou_worker'))
    click(channel, worker)
    assert client.get('/api/v1/tickets/1').json()['status'] == 'accepted'
    assert channel.receive_action(worker) == 'duplicate'
    click(channel, callback(card(messenger, 'ou_worker')))
    assert client.get('/api/v1/tickets/1').json()['status'] == 'processing'
    send(channel, '维修反馈 1 更换排水管，试机未见漏水', 5, 'ou_worker')
    assert client.get('/api/v1/tickets/1/repair-records').json() == []
    assert '还缺成功保存的现场照片' in messenger.sent[-1]['content']['text']
    assert send_image(channel) == 'queued'
    assert messenger.downloads == [('om_image_10', 'img_demo')]
    assert send_image(channel) == 'duplicate'
    send(channel, '维修反馈 1 更换排水管，试机未见漏水', 6, 'ou_worker')
    preview = callback(card(messenger, 'ou_worker'))
    assert client.get('/api/v1/tickets/1/repair-records').json() == []
    click(channel, preview)
    assert channel.receive_action(preview) == 'duplicate'
    ticket = client.get('/api/v1/tickets/1').json()
    assert ticket['status'] == 'pending_review'
    records = client.get('/api/v1/tickets/1/repair-records').json()
    assert len(records) == 1 and records[0]['repair_description'] == '更换排水管，试机未见漏水'
    assert len(records[0]['photo_urls']) == 1
    images = client.get('/api/v1/tickets/1/attachments').json()
    assert len(images) == 1 and images[0]['repair_record_id'] == records[0]['id']
    assert client.get(images[0]['url']).status_code == 200
    assert client.get('/api/v1/tickets/1/assignments').json() == assignments


def test_same_person_demo_roles_and_reporter_notification(setup):
    client, channel, store, business, messenger, ticket = setup
    owner = 'ou_manager'
    channel.identities = {owner: EngineeringIdentity(frozenset({'dispatcher', 'technician'}), 'DEMO-MANAGER', 'ENG-DEMO-001')}
    store.set_meta('paired_open_id', owner)
    workflow = client.post('/api/v1/integrations/feishu/repair-events', json={
        'event_id': 'demo-feishu-entry', 'open_id': owner, 'text': 'A栋302漏水'}).json()
    workflow_id = workflow['workflow_id']
    draft = workflow['draft']
    confirmed = client.post(f'/api/v1/agent/runs/{workflow_id}/confirm-ticket', json={
        **{key: draft[key] for key in ('location', 'issue_type', 'description')},
        'priority': 'normal', 'idempotency_key': 'demo-feishu-confirm'}).json()
    ticket_id = confirmed['ticket_id']
    store.set_session(owner, 'oc_' + owner, workflow_id)
    send(channel, f'派工 {ticket_id}', 1)
    for _ in range(3): click(channel, callback(card(messenger, owner)))
    assert client.get(f'/api/v1/tickets/{ticket_id}').json()['status'] == 'processing'
    send_image(channel, owner=owner)
    send(channel, f'维修反馈 {ticket_id} 更换排水管，试机未见漏水', 20, owner)
    click(channel, callback(card(messenger, owner)))
    assert client.get(f'/api/v1/tickets/{ticket_id}').json()['status'] == 'pending_review'
    assert any('待企服验收' in item['content'].get('text', '') for item in messenger.sent)
    channel.clear_context(owner, 'oc_' + owner)
    downloads = list(messenger.downloads)
    send_image(channel, 21, owner)
    assert messenger.downloads == downloads


@pytest.mark.parametrize('change', ['owner', 'chat', 'message', 'app', 'nonce', 'extra', 'expired', 'revoked'])
def test_forged_forwarded_expired_or_revoked_cards_cannot_dispatch(setup, change, monkeypatch):
    client, channel, store, business, messenger, ticket = setup
    send(channel, '派工 1', 1)
    value = callback(card(messenger, 'ou_manager'))
    if change == 'owner': value['event']['operator']['open_id'] = 'ou_worker'
    if change == 'chat': value['event']['context']['open_chat_id'] = 'oc_group'
    if change == 'message': value['event']['context']['open_message_id'] = 'om_forwarded'
    if change == 'app': value['header']['app_id'] = 'cli_other'
    if change == 'nonce': value['event']['action']['value']['nonce'] = 'a' * 32
    if change == 'extra': value['event']['action']['value']['personnel_no'] = 'ENG-DEMO-002'
    if change == 'expired': monkeypatch.setattr('app.services.feishu_engineering.time.time', lambda: 10**12)
    if change == 'revoked': channel.identities.pop('ou_manager')
    assert channel.receive_action(value) == 'rejected'
    drain(channel)
    assert client.get('/api/v1/tickets/1/assignments').json() == []


def test_roleless_and_wrong_technician_never_receive_other_tasks(setup):
    client, channel, store, business, messenger, ticket = setup
    assert channel.receive(payload('派工 1', owner='ou_unbound')) == 'unauthorized'
    send(channel, '派工 1', 30, 'ou_worker')
    assert store.meta('engineering_error') == 'engineering_role_required'
    prepare_processing(setup)
    send(channel, '维修 1', 20, 'ou_other')
    assert store.meta('engineering_error') == 'current_technician_required'
    assert all(item['type'] == 'text' for item in messenger.sent if item['owner'] == 'ou_other')
    assert client.get('/api/v1/tickets/1/repair-records').json() == []


def test_changed_assignment_invalidates_old_card(setup):
    client, channel, store, business, messenger, ticket = setup
    send(channel, '派工 1', 1)
    value = callback(card(messenger, 'ou_manager'))
    client.post('/api/v1/tickets/1/assignments', json={'personnel_no': 'ENG-DEMO-002', 'assigned_by': 'WEB-DEMO'})
    click(channel, value)
    assert store.meta('engineering_error') == 'stale_engineering_card'
    rows = client.get('/api/v1/tickets/1/assignments').json()
    assert len(rows) == 1 and rows[0]['personnel_no'] == 'ENG-DEMO-002'


def test_successful_commit_with_lost_response_is_not_replayed(setup, monkeypatch):
    client, channel, store, business, messenger, ticket = setup
    send(channel, '派工 1', 1)
    value = callback(card(messenger, 'ou_manager'))
    original = business.call
    def timeout(method, path, payload=None):
        result = original(method, path, payload)
        if method == 'POST' and path.endswith('/assignments'): raise BridgeError('business_unavailable')
        return result
    monkeypatch.setattr(business, 'call', timeout)
    click(channel, value)
    assert store.meta('engineering_error') == 'engineering_submission_uncertain'
    assert len(client.get('/api/v1/tickets/1/assignments').json()) == 1
    assert channel.receive_action(value) == 'duplicate'
    drain(channel)
    assert len(client.get('/api/v1/tickets/1/assignments').json()) == 1


def test_notification_send_retry_does_not_repeat_assignment(setup, monkeypatch):
    client, channel, store, business, messenger, ticket = setup
    send(channel, '派工 1', 1)
    value = callback(card(messenger, 'ou_manager'))
    messenger.fail = True
    click(channel, value)
    assert len(client.get('/api/v1/tickets/1/assignments').json()) == 1
    calls = list(business.calls)
    messenger.fail = False
    monkeypatch.setattr('app.services.feishu_engineering.time.time', lambda: 10**12)
    drain(channel)
    assert business.calls == calls
    assert card(messenger, 'ou_worker')
    assert set(channel.summary()['outbox']) == {'sent'}
    assert messenger.patches


def test_restart_blocks_inflight_mutation_and_keeps_pending_notifications(setup):
    client, channel, store, business, messenger, ticket = setup
    send(channel, '派工 1', 1)
    value = callback(card(messenger, 'ou_manager'))
    assert channel.receive_action(value) == 'queued'
    nonce = value['event']['action']['value']['nonce']
    with store.lock, store.db:
        store.db.execute("UPDATE eng_jobs SET status='working' WHERE kind='action'")
        store.db.execute("UPDATE eng_cards SET status='executing' WHERE nonce=?", (nonce,))
    reopened = EngineeringChannel(store, business, messenger, APP, channel.identities)
    assert not reopened.process_one()
    assert reopened.summary()['jobs'] == {'blocked': 1, 'done': 1}
    assert reopened.receive_action(value) == 'duplicate'
    assert client.get('/api/v1/tickets/1/assignments').json() == []


@pytest.mark.parametrize('condition', ['no_context', 'invalid', 'too_large', 'sixth'])
def test_image_requires_context_valid_content_and_count_limit(setup, condition):
    client, channel, store, business, messenger, ticket = setup
    if condition == 'no_context':
        assert send_image(channel) == 'queued'
        assert messenger.downloads == []
        return
    prepare_processing(setup)
    if condition == 'invalid': messenger.image = b'not an image'
    if condition == 'too_large': messenger.image = b'x' * (10 * 1024 * 1024 + 1)
    if condition == 'sixth':
        for number in range(5): assert send_image(channel, 30 + number) == 'queued'
    assert send_image(channel, 40) == 'queued'
    if condition == 'sixth': assert len(messenger.downloads) == 5
    else: assert store.meta('engineering_error') == 'invalid_repair_image'
    assert client.get('/api/v1/tickets/1/repair-records').json() == []


def test_preview_is_invalid_after_web_repair_submission(setup):
    client, channel, store, business, messenger, ticket = setup
    prepare_processing(setup)
    send_image(channel)
    send(channel, '维修反馈 1 已更换排水管', 40, 'ou_worker')
    value = callback(card(messenger, 'ou_worker'))
    client.post('/api/v1/tickets/1/repair-records', json={'personnel_no': 'ENG-DEMO-001', 'repair_description': 'Web已提交'})
    click(channel, value)
    assert store.meta('engineering_error') == 'stale_engineering_card'
    assert len(client.get('/api/v1/tickets/1/repair-records').json()) == 1


@pytest.mark.parametrize('data', [
    {'ou_a': {'roles': ['admin'], 'actor_id': 'A'}},
    {'ou_a': {'roles': ['technician'], 'actor_id': 'A'}},
    {'ou_a': {'roles': ['dispatcher'], 'actor_id': 'A', 'personnel_no': 'ENG-1'}},
    {'ou_a': {'roles': ['technician'], 'actor_id': 'A', 'personnel_no': 'ENG-1'},
     'ou_b': {'roles': ['technician'], 'actor_id': 'B', 'personnel_no': 'ENG-1'}},
])
def test_identity_file_rejects_ambiguous_or_excessive_roles(tmp_path, data):
    path = tmp_path / 'roles.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(BridgeError, match='invalid_engineering_identities'): load_identities(path)


def test_sdk_send_and_download_use_official_resource_models():
    """对实际SDK构建请求，mock最末端传输，不使用本机凭证或网络。"""
    sent, downloaded, patched = [], [], []
    def create(request):
        sent.append(request)
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id='om_test', chat_id='oc_test'))
    def get(request):
        downloaded.append(request)
        return SimpleNamespace(success=lambda: True, file=BytesIO(b'image'))
    def patch(request):
        patched.append(request)
        return SimpleNamespace(success=lambda: True)
    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        message=SimpleNamespace(create=create, patch=patch), message_resource=SimpleNamespace(get=get))))
    messenger = SDKMessenger(client)
    assert messenger.send('ou_demo', 'interactive', {'elements': []}, 'test-uuid') == ('om_test', 'oc_test')
    assert sent[0].receive_id_type == 'open_id'
    assert sent[0].request_body.msg_type == 'interactive'
    assert messenger.download_image('om_image', 'img_key') == b'image'
    assert downloaded[0].type == 'image' and downloaded[0].message_id == 'om_image'
    messenger.patch('om_test', {'elements': []})
    assert patched[0].message_id == 'om_test'
    assert json.loads(patched[0].request_body.content) == {'elements': []}


def test_failed_photo_natural_feedback_retry_preview_and_manual_submit(setup, monkeypatch):
    """复现真实截图；权限恢复前只存说明，不把失败照片当成已上传。"""
    client, channel, store, business, messenger, ticket = setup
    prepare_processing(setup)
    original = messenger.download_image
    def denied(*args): raise BridgeError('feishu_api_99991672')
    monkeypatch.setattr(messenger, 'download_image', denied)
    send_image(channel)
    assert 'im:message:readonly' in messenger.sent[-1]['content']['text']
    send(channel, '漏水已维修完毕', 41, 'ou_worker')
    draft = channel.draft('ou_worker', 'oc_ou_worker', 1, channel.snapshot(1)[3])
    assert draft['description'] == '漏水已维修完毕'
    assert '还缺成功保存的现场照片' in messenger.sent[-1]['content']['text']
    assert client.get('/api/v1/tickets/1/repair-records').json() == []
    monkeypatch.setattr(messenger, 'download_image', original)
    send(channel, '重试照片', 42, 'ou_worker')
    assert messenger.downloads == [('om_image_10', 'img_demo')]
    preview = callback(card(messenger, 'ou_worker'))
    assert client.get('/api/v1/tickets/1').json()['status'] == 'processing'
    assert '未调用AI' in json.dumps(card(messenger, 'ou_worker'), ensure_ascii=False)
    send(channel, '重试照片', 43, 'ou_worker')
    assert messenger.downloads == [('om_image_10', 'img_demo')]
    assert len(channel.evidence('ou_worker', 'oc_ou_worker', 1, channel.snapshot(1)[3])) == 1
    click(channel, preview)
    assert client.get('/api/v1/tickets/1').json()['status'] == 'pending_review'
    records = client.get('/api/v1/tickets/1/repair-records').json()
    assert len(records) == 1 and records[0]['repair_description'] == '漏水已维修完毕'
    assert len(records[0]['photo_urls']) == 1
    send(channel, '进度', 44, 'ou_worker')
    assert '待企服验收' in json.dumps(card(messenger, 'ou_worker'), ensure_ascii=False)


def test_legacy_failed_photo_can_be_explicitly_retried_in_current_task(setup, monkeypatch):
    client, channel, store, business, messenger, ticket = setup
    prepare_processing(setup)
    with store.lock, store.db:
        store.db.execute('INSERT INTO eng_jobs VALUES (?,?,?,?,?,?)',
            ('message:om_legacy', 'ou_worker', 'oc_ou_worker', 'image', json.dumps({'text': 'img_old'}), 'blocked'))
    send(channel, '漏水已维修完毕', 41, 'ou_worker')
    send(channel, '重试照片', 42, 'ou_worker')
    assert messenger.downloads == [('om_legacy', 'img_old')]
    assert client.get('/api/v1/tickets/1/repair-records').json() == []
    assert '确认提交维修结果' in json.dumps(card(messenger, 'ou_worker'), ensure_ascii=False)


@pytest.mark.parametrize('kind', ['text', 'image'])
def test_queued_material_keeps_original_ticket_after_context_switch(setup, kind):
    client, channel, store, business, messenger, ticket = setup
    prepare_processing(setup)
    value = payload('漏水已维修完毕', 'om_pending', 'ou_worker', 'oc_ou_worker')
    if kind == 'image':
        value['event']['message'].update(message_type='image', content=json.dumps({'image_key': 'img_pending'}))
    assert channel.receive(value) == 'queued'
    with store.lock, store.db:
        store.db.execute('UPDATE eng_context SET ticket_id=999,snapshot=?', ('another-task',))
    drain(channel)
    stamp = channel.snapshot(1)[3]
    if kind == 'text': assert channel.draft('ou_worker', 'oc_ou_worker', 1, stamp)['description'] == '漏水已维修完毕'
    else: assert len(channel.evidence('ou_worker', 'oc_ou_worker', 1, stamp)) == 1
    assert not store.db.execute('SELECT 1 FROM eng_evidence WHERE ticket_id=999').fetchone()
    assert client.get('/api/v1/tickets/1/repair-records').json() == []


def test_failed_photo_from_other_context_is_not_reassigned(setup):
    client, channel, store, business, messenger, ticket = setup
    prepare_processing(setup)
    with store.lock, store.db:
        store.db.execute('INSERT INTO eng_jobs VALUES (?,?,?,?,?,?)',
            ('message:om_other', 'ou_worker', 'oc_ou_worker', 'image',
             json.dumps({'text': 'img_other', 'context': {'ticket_id': 999, 'snapshot': 'old'}}), 'blocked'))
    send(channel, '重试照片', 41, 'ou_worker')
    assert store.meta('engineering_error') == 'stale_engineering_card'
    assert messenger.downloads == []


def test_progress_and_unrelated_text_do_not_become_repair_description(setup, monkeypatch):
    client, channel, store, business, messenger, ticket = setup
    prepare_processing(setup)
    def forbidden(*args): pytest.fail('明确进度查询不应调用模型')
    with monkeypatch.context() as patcher:
        patcher.setattr('app.services.feishu_engineering.review_feedback', forbidden)
        send(channel, '进度', 41, 'ou_worker')
    send(channel, '你好', 42, 'ou_worker')
    assert channel.draft('ou_worker', 'oc_ou_worker', 1, channel.snapshot(1)[3]) is None
    assert '尚未提交维修结果' in messenger.sent[-1]['content']['text']
    channel.clear_context('ou_worker', 'oc_ou_worker')
    assert channel.receive(payload('漏水已维修完毕', 'om_no_context', 'ou_worker', 'oc_ou_worker')) == 'not_engineering'
    assert client.get('/api/v1/tickets/1/repair-records').json() == []


def test_task_and_corrected_preview_patch_existing_messages_and_reject_old_buttons(setup):
    client, channel, store, business, messenger, ticket = setup
    send(channel, '派工 1', 1)
    dispatch = callback(card(messenger, 'ou_manager'))
    click(channel, dispatch)
    task_id = card(messenger, 'ou_worker')['message_id']
    accept = callback(card(messenger, 'ou_worker'))
    click(channel, accept)
    assert card(messenger, 'ou_worker')['message_id'] == task_id
    click(channel, callback(card(messenger, 'ou_worker')))
    assert card(messenger, 'ou_worker')['message_id'] == task_id
    assert '历史操作已结束' in json.dumps(card(messenger, 'ou_manager'), ensure_ascii=False)
    send_image(channel)
    send(channel, '已更换排水管', 41, 'ou_worker')
    old = callback(card(messenger, 'ou_worker'))
    preview_id = card(messenger, 'ou_worker')['message_id']
    send(channel, '更正：仅清理排水管，尚未更换', 42, 'ou_worker')
    assert card(messenger, 'ou_worker')['message_id'] == preview_id
    assert channel.receive_action(old) != 'queued'
    assert client.get('/api/v1/tickets/1/repair-records').json() == []
    current = callback(card(messenger, 'ou_worker'))
    click(channel, current)
    assert client.get('/api/v1/tickets/1/repair-records').json()[0]['repair_description'] == '更正：仅清理排水管，尚未更换'
    assert all('actions' not in element for item in messenger.sent if item['type'] == 'interactive' for element in item['content']['elements'])


def test_patch_send_retry_never_replays_repair_submission(setup, monkeypatch):
    client, channel, store, business, messenger, ticket = setup
    prepare_processing(setup)
    send_image(channel)
    send(channel, '漏水已维修完毕', 41, 'ou_worker')
    value = callback(card(messenger, 'ou_worker'))
    messenger.fail = True
    click(channel, value)
    assert client.get('/api/v1/tickets/1').json()['status'] == 'pending_review'
    calls = list(business.calls)
    messenger.fail = False
    monkeypatch.setattr('app.services.feishu_engineering.time.time', lambda: 10**12)
    drain(channel)
    assert business.calls == calls
    assert len(client.get('/api/v1/tickets/1/repair-records').json()) == 1
    assert set(channel.summary()['outbox']) == {'sent'}


def test_official_callback_dispatch_and_marshal_match_channel_payload(setup):
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    client, channel, store, business, messenger, ticket = setup
    send(channel, '派工 1', 1)
    value = callback(card(messenger, 'ou_manager'))
    value['schema'] = '2.0'
    results = []
    def receive(data):
        results.append(channel.receive_action(json.loads(lark.JSON.marshal(data))))
        return P2CardActionTriggerResponse({'toast': {'type': 'info', 'content': '已收到'}})
    handler = lark.EventDispatcherHandler.builder('', '').register_p2_card_action_trigger(receive).build()
    result = handler._do_without_validation(json.dumps(value).encode())
    assert results == ['queued']
    assert json.loads(lark.JSON.marshal(result))['toast']['content'] == '已收到'
    assert client.get('/api/v1/tickets/1/assignments').json() == []
    drain(channel)
    assert len(client.get('/api/v1/tickets/1/assignments').json()) == 1
