"""飞书企业自建应用长连接；仅连接本机隔离演示API，不重启或迁移业务库。"""

import argparse
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import time
from threading import Event, Thread


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.feishu_bridge import BridgeError, BridgeStore, FeishuBridge, LocalBusinessClient, single_instance
from app.services.feishu_engineering import EngineeringChannel, EngineeringIdentity, load_identities
from app.services.feishu_reminders import ReminderChannel


class SafeSDKLog(logging.Handler):
    """SDK日志可能携带连接URL和消息正文，只保留连接状态，不转发原始日志。"""
    def __init__(self, store: BridgeStore | None = None):
        super().__init__()
        self.store = store
        self.last_state = None

    def emit(self, record):
        message = record.getMessage()
        state = None
        if 'disconnected to ' in message:
            state = 'disconnected'
        elif 'connected to ' in message:
            state = 'connected'
        elif 'connect failed' in message:
            state = 'connection_failed'
        elif 'trying to reconnect' in message:
            state = 'reconnecting'
        if state and self.store and state != self.last_state:
            self.last_state = state
            self.store.set_meta('connection', state)
            print(json.dumps({'feishu_connection': state}), flush=True)


def configure_sdk_log(store=None):
    logger = logging.getLogger('Lark')
    logger.handlers.clear()
    logger.addHandler(SafeSDKLog(store))
    logger.propagate = False


class SDKMessenger:
    def __init__(self, client):
        self.client = client

    def reply(self, message_id: str, text: str, key: str):
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        body = ReplyMessageRequestBody.builder().msg_type('text').content(
            json.dumps({'text': text}, ensure_ascii=False)).uuid(key).build()
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        try:
            response = self.client.im.v1.message.reply(request)
        except Exception:
            raise BridgeError('feishu_reply_network_error') from None
        if not response.success():
            code = response.code if type(response.code) is int else 0
            raise BridgeError(f'feishu_api_{code}')

    def send(self, owner: str, msg_type: str, content: dict, key: str):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        body = CreateMessageRequestBody.builder().receive_id(owner).msg_type(msg_type).content(
            json.dumps(content, ensure_ascii=False)).uuid(key).build()
        request = CreateMessageRequest.builder().receive_id_type('open_id').request_body(body).build()
        try:
            response = self.client.im.v1.message.create(request)
        except Exception:
            raise BridgeError('feishu_send_network_error') from None
        if not response.success():
            code = response.code if type(response.code) is int else 0
            raise BridgeError(f'feishu_api_{code}')
        return response.data.message_id, response.data.chat_id

    def patch(self, message_id: str, card: dict):
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
        body = PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build()
        request = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        try:
            response = self.client.im.v1.message.patch(request)
        except Exception:
            raise BridgeError('feishu_patch_network_error') from None
        if not response.success():
            code = response.code if type(response.code) is int else 0
            raise BridgeError(f'feishu_api_{code}')

    def download_image(self, message_id: str, image_key: str) -> bytes:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        request = GetMessageResourceRequest.builder().message_id(message_id).file_key(image_key).type('image').build()
        try:
            response = self.client.im.v1.message_resource.get(request)
        except Exception:
            raise BridgeError('feishu_image_network_error') from None
        if not response.success():
            code = response.code if type(response.code) is int else 0
            raise BridgeError(f'feishu_api_{code}')
        try:
            return response.file.read(10 * 1024 * 1024 + 1)
        finally:
            response.file.close()


def auth_check(client, app_id, app_secret):
    from lark_oapi.api.auth.v3 import InternalTenantAccessTokenRequest, InternalTenantAccessTokenRequestBody
    body = InternalTenantAccessTokenRequestBody.builder().app_id(app_id).app_secret(app_secret).build()
    request = InternalTenantAccessTokenRequest.builder().request_body(body).build()
    try:
        response = client.auth.v3.tenant_access_token.internal(request)
    except Exception:
        raise BridgeError('feishu_auth_network_error') from None
    if not response.success():
        code = response.code if type(response.code) is int else 0
        raise BridgeError(f'feishu_auth_{code}')
    # 不打印、不保存tenant_access_token，SDK后续会按需获取并在内存中缓存。


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-url', default='http://127.0.0.1:8003', help='已运行的隔离演示后端，仅允许127.0.0.1')
    parser.add_argument('--check', action='store_true', help='只检查本机配置、后端环境和飞书真实鉴权，不监听或发消息')
    parser.add_argument('--status', action='store_true', help='显示本机桥接状态，不输出凭证、身份标识或消息正文')
    parser.add_argument('--demo-engineering', action='store_true', help='显式允许已配对本人演示工程负责人和维修员；仅隔离演示')
    parser.add_argument('--demo-personnel', help='本人模拟维修员对应的活跃目录编号；须配合--demo-engineering')
    parser.add_argument('--engineering-identities', type=Path, help='可选本机角色映射JSON文件；不与本人角色演示参数混用')
    parser.add_argument('--reminders', action='store_true', help='启用独立线程自动扫描和发送业务提醒')
    parser.add_argument('--reminder-interval', type=int, default=30, help='提醒扫描秒数，范围5至300，默认30')
    parser.add_argument('--demo-investment-owner', help='明确绑定本人接收提醒的模拟招商负责人名称；须启用reminders')
    parser.add_argument('--reminder-self-test', action='store_true', help='仅向已配对本人发一条标明模拟测试的通知，不登记业务回执')
    args = parser.parse_args()
    app_id = os.getenv('FEISHU_APP_ID', '').strip()
    app_secret = os.getenv('FEISHU_APP_SECRET', '').strip()
    if not re.fullmatch(r'cli_[A-Za-z0-9]{4,100}', app_id) or not app_secret:
        raise BridgeError('feishu_credentials_missing_or_invalid')
    fingerprint = sha256(app_id.encode()).hexdigest()[:16]
    state_path = BACKEND_DIR / '.local' / f'feishu-{fingerprint}.sqlite3'
    if args.status:
        if not state_path.exists():
            print(json.dumps({'connection': 'not_started', 'paired': False}))
            return
        store = BridgeStore(state_path, app_id)
        try:
            summary = store.summary()
            with store.lock:
                if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='eng_jobs'").fetchone():
                    summary['engineering'] = {
                        'jobs': dict(store.db.execute('SELECT status,COUNT(*) FROM eng_jobs GROUP BY status').fetchall()),
                        'outbox': dict(store.db.execute('SELECT status,COUNT(*) FROM eng_outbox GROUP BY status').fetchall()),
                        'last_error_code': store.meta('engineering_error'),
                        'last_send_error_code': store.meta('engineering_send_error')}
                if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reminder_deliveries'").fetchone():
                    summary['reminders'] = {
                        'deliveries': dict(store.db.execute('SELECT status,COUNT(*) FROM reminder_deliveries GROUP BY status').fetchall()),
                        'last_scan_at': store.meta('reminder_last_scan_at'),
                        'unmapped_count': int(store.meta('reminder_unmapped_count') or 0),
                        'scan_error': store.meta('reminder_scan_error') or None}
            print(json.dumps(summary, ensure_ascii=False))
        finally:
            store.close()
        return
    business = LocalBusinessClient(args.backend_url)
    runtime = business.require_demo()
    import lark_oapi as lark
    configure_sdk_log()
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).timeout(15).log_level(lark.LogLevel.INFO).build()
    auth_check(client, app_id, app_secret)
    if args.check:
        print(json.dumps({'credentials_present': True, 'tenant_auth_verified': True,
                          'backend_environment': runtime['environment'], 'message_delivery_verified': False}))
        return
    with single_instance(state_path.with_suffix('.lock')):
        store = BridgeStore(state_path, app_id)
        configure_sdk_log(store)
        allowed = {item.strip() for item in os.getenv('FEISHU_ALLOWED_OPEN_IDS', '').split(',') if item.strip()}
        if any(not re.fullmatch(r'ou_[A-Za-z0-9_-]{1,180}', item) for item in allowed):
            store.close()
            raise BridgeError('invalid_allowed_open_ids')
        code = None if allowed or store.meta('paired_open_id') else f'{secrets.randbelow(1_000_000):06d}'
        bridge = FeishuBridge(store, business, SDKMessenger(client), app_id, allowed, code)
        identities = {}
        if args.engineering_identities:
            if args.demo_engineering or args.demo_personnel:
                raise BridgeError('engineering_identity_modes_conflict')
            identities = load_identities(args.engineering_identities)
        elif args.demo_engineering:
            owner = store.meta('paired_open_id')
            if not owner or not args.demo_personnel:
                raise BridgeError('paired_owner_and_demo_personnel_required')
            directory = business.call('GET', '/api/v1/repair-personnel')
            if not any(item['personnel_no'] == args.demo_personnel for item in directory):
                raise BridgeError('demo_personnel_not_active')
            identities[owner] = EngineeringIdentity(frozenset({'dispatcher', 'technician'}),
                                                    'FEISHU-DEMO-MANAGER', args.demo_personnel)
        elif args.demo_personnel:
            raise BridgeError('demo_engineering_flag_required')
        engineering = EngineeringChannel(store, business, SDKMessenger(client), app_id, identities)
        if not 5 <= args.reminder_interval <= 300:
            raise BridgeError('invalid_reminder_interval')
        investment = {}
        if args.demo_investment_owner:
            if not args.reminders or not store.meta('paired_open_id') or not 1 <= len(args.demo_investment_owner.strip()) <= 80:
                raise BridgeError('invalid_demo_investment_binding')
            investment[args.demo_investment_owner.strip()] = store.meta('paired_open_id')
        if args.reminder_self_test:
            owner = store.meta('paired_open_id')
            if not owner:
                raise BridgeError('paired_owner_required')
            SDKMessenger(client).send(owner, 'text', {'text': '自动提醒联通测试 · 模拟测试\n这条消息仅验证机器人主动发送能力，不表示任何客户已到期或工单已超时，不启动特急5分钟计时。业务资料与任务期限未修改。'}, secrets.token_hex(16))
            print(json.dumps({'reminder_self_test_sent': True, 'business_receipt_created': False}), flush=True)
            store.close()
            return
        reminders = ReminderChannel(store, business, SDKMessenger(client), identities, investment,
                                    engineering_channel=engineering) if args.reminders else None
        stopped = Event()

        def receive(data):
            try:
                payload = json.loads(lark.JSON.marshal(data))
                outcome = engineering.receive(payload)
                if outcome == 'not_engineering':
                    from app.services.feishu_bridge import parse_single_chat
                    text_message = parse_single_chat(payload, app_id)
                    if text_message and re.match(r'^(?:报修|新报修)', text_message.text):
                        engineering.clear_context(text_message.open_id, text_message.chat_id)
                    outcome = bridge.receive(payload)
                if outcome == 'paired':
                    print('本人单聊配对完成；未授予工程分派或企服验收权限。', flush=True)
                if outcome in ('paired', 'queued'):
                    store.set_meta('last_receive_outcome', outcome)
            except Exception:
                # 入队失败不能当作已接收，让SDK按事件处理失败重试。
                raise RuntimeError('channel_inbox_unavailable') from None

        def worker():
            while not stopped.is_set():
                ordinary = bridge.process_one()
                operational = engineering.process_one()
                if not ordinary and not operational:
                    stopped.wait(0.25)

        def reminder_worker():
            # 独立于大模型处理线程；模型请求不能阻塞本轮提醒调度。
            next_scan = 0.0
            while not stopped.is_set():
                if time.monotonic() >= next_scan:
                    reminders.scan()
                    next_scan = time.monotonic() + args.reminder_interval
                if not reminders.send_one():
                    stopped.wait(0.25)

        def card_action(data):
            from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
            try:
                result = engineering.receive_action(json.loads(lark.JSON.marshal(data)))
                accepted = result in ('queued', 'duplicate')
                return P2CardActionTriggerResponse({'toast': {'type': 'info' if accepted else 'error',
                    'content': ('已收到，请等待业务结果；重复点击不会重复提交。' if accepted
                                else '卡片已失效或当前账号无权操作，请刷新任务卡。')}})
            except Exception:
                raise RuntimeError('channel_card_inbox_unavailable') from None

        handler = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(receive).register_p2_card_action_trigger(card_action).build()
        websocket = lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.INFO)
        thread = Thread(target=worker, name='feishu-business-worker', daemon=True)
        thread.start()
        reminder_thread = Thread(target=reminder_worker, name='feishu-reminder-worker', daemon=True) if reminders else None
        if reminder_thread:
            reminder_thread.start()
        store.set_meta('connection', 'connecting')
        print('飞书鉴权通过，正在建立长连接；只使用模拟业务资料。', flush=True)
        if code:
            print(f'连接成功后，在你与机器人单聊中发送：绑定 {code}（10分钟内有效，仅绑定一个本人账号）', flush=True)
        else:
            print('使用已配对账号或本机允许名单接收单聊消息。', flush=True)
        if identities:
            print('工程模拟角色已显式启用；使用“工程帮助”“派工 工单ID”“维修 工单ID”。', flush=True)
        if reminders:
            print(f'自动提醒已启用，每{args.reminder_interval}秒扫描；仅向明确绑定的模拟负责人发送。', flush=True)
        try:
            websocket.start()
        finally:
            stopped.set()
            thread.join(timeout=2)
            if reminder_thread:
                reminder_thread.join(timeout=2)
            store.set_meta('connection', 'stopped')
            if not thread.is_alive() and (not reminder_thread or not reminder_thread.is_alive()):
                store.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('飞书桥接已停止，业务后端及资料保留。')
    except BridgeError as error:
        print(json.dumps({'status': 'failed', 'reason': error.code}), flush=True)
        sys.exit(1)
    except Exception:
        # 原始SDK异常可能含URL或凭证，不将其输出到终端或聊天。
        print(json.dumps({'status': 'failed', 'reason': 'feishu_startup_error'}), flush=True)
        sys.exit(1)
