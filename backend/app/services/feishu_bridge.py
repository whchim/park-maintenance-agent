"""本机飞书单聊桥接：独立收件状态，不持有业务数据库或飞书密钥。"""

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class BridgeError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    open_id: str
    text: str


def parse_single_chat(payload: dict, app_id: str) -> IncomingMessage | None:
    """只接受本应用收到的用户单聊文本；机器人消息、群聊与其他格式不进入业务。"""
    try:
        header = payload['header']
        event = payload['event']
        sender, message = event['sender'], event['message']
        if (header['event_type'] != 'im.message.receive_v1'
                or header.get('app_id') not in (None, app_id)
                or sender.get('sender_type') != 'user' or message.get('chat_type') != 'p2p'
                or message.get('message_type') != 'text'):
            return None
        content = json.loads(message['content'])
        text = content['text'].strip()
        identifiers = (message['message_id'], message['chat_id'], sender['sender_id']['open_id'])
        if not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value) for value in identifiers):
            return None
        if not text or len(text) > 20000:
            return None
        return IncomingMessage(*identifiers, text)
    except (KeyError, TypeError, AttributeError, ValueError):
        return None


class LocalBusinessClient:
    def __init__(self, base_url: str):
        try:
            url = urlsplit(base_url)
            valid = (url.scheme == 'http' and url.hostname == '127.0.0.1'
                     and url.port and 1024 <= url.port <= 65535 and not url.username
                     and not url.password and url.path in ('', '/') and not url.query and not url.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise BridgeError('local_demo_url_required')
        self.base_url = base_url.rstrip('/')

    def call(self, method: str, path: str, payload: dict | None = None) -> dict | list:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        return self._request(method, path, body, 'application/json')

    def upload_repair_image(self, content: bytes, media_type: str) -> dict:
        # 文件名固定生成；飞书image_key只用于官方资源接口，不能拼为用户提供的远程URL。
        extension = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}.get(media_type)
        if not extension or not 0 < len(content) <= 10 * 1024 * 1024:
            raise BridgeError('invalid_repair_image')
        return self._request('POST', f'/api/v1/attachments/images?purpose=repair&filename=feishu-repair.{extension}',
                             content, media_type)

    def _request(self, method: str, path: str, body: bytes | None, media_type: str):
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        request = Request(self.base_url + path, data=body, method=method,
                          headers={'Content-Type': media_type})
        try:
            with build_opener(NoRedirect()).open(request, timeout=90) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise BridgeError('business_response_too_large')
            result = json.loads(raw)
            if not isinstance(result, (dict, list)):
                raise BridgeError('invalid_business_response')
            return result
        except HTTPError as error:
            code = error.code
            error.close()
            raise BridgeError(f'business_http_{code}') from None
        except (URLError, OSError, ValueError):
            raise BridgeError('business_unavailable') from None

    def require_demo(self) -> dict:
        runtime = self.call('GET', '/api/v1/agents/runtime')
        if runtime.get('environment') != {'name': 'isolated_demo', 'synthetic_data': True, 'volatile': True}:
            raise BridgeError('isolated_demo_required')
        return runtime


@contextmanager
def single_instance(path: Path):
    """OS文件锁随进程退出释放；不删除锁文件或其他进程的数据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if __import__('os').name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BridgeError('bridge_already_running') from None
        try:
            yield
        finally:
            if __import__('os').name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class BridgeStore:
    """独立渠道收件箱：先落盘再ACK；模型处理和飞书回复失败均可恢复。"""
    def __init__(self, path: Path, app_id: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS bridge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bridge_sessions (
                    open_id TEXT NOT NULL, chat_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
                    PRIMARY KEY(open_id, chat_id));
                CREATE TABLE IF NOT EXISTS bridge_inbox (
                    message_id TEXT PRIMARY KEY, open_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    text TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    outgoing_text TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    retry_after REAL NOT NULL DEFAULT 0, error_code TEXT, created_at REAL NOT NULL);
            ''')
        fingerprint = sha256(app_id.encode()).hexdigest()
        previous = self.meta('app_fingerprint')
        if previous and previous != fingerprint:
            self.db.close()
            raise BridgeError('state_app_mismatch')
        self.set_meta('app_fingerprint', fingerprint)

    def meta(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute('SELECT value FROM bridge_meta WHERE key=?', (key,)).fetchone()
            return row['value'] if row else None

    def set_meta(self, key: str, value: str):
        with self.lock, self.db:
            self.db.execute('INSERT INTO bridge_meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def enqueue(self, message: IncomingMessage, kind: str = 'business') -> bool:
        with self.lock, self.db:
            result = self.db.execute('''INSERT OR IGNORE INTO bridge_inbox
                (message_id, open_id, chat_id, text, kind, created_at) VALUES (?, ?, ?, ?, ?, ?)''',
                (message.message_id, message.open_id, message.chat_id,
                 '' if kind == 'paired' else message.text, kind, time.time()))
            return result.rowcount == 1

    def pair(self, message: IncomingMessage) -> bool:
        with self.lock, self.db:
            owner = self.meta('paired_open_id')
            if owner and (owner != message.open_id or self.meta('paired_chat_id') != message.chat_id):
                return False
            self.db.executemany('INSERT INTO bridge_meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                [('paired_open_id', message.open_id), ('paired_chat_id', message.chat_id)])
            # 配对码不写入收件箱；成功回复也经过同一个可恢复outbox。
            self.db.execute('''INSERT OR IGNORE INTO bridge_inbox
                (message_id, open_id, chat_id, text, kind, created_at) VALUES (?, ?, ?, '', 'paired', ?)''',
                (message.message_id, message.open_id, message.chat_id, time.time()))
            return True

    def next_job(self) -> dict | None:
        with self.lock:
            # 同一会话的前一条消息重试时，后续补充不能抢先执行；其他会话仍可继续。
            row = self.db.execute('''SELECT current.* FROM bridge_inbox AS current
                WHERE current.status='queued' AND current.retry_after<=? AND NOT EXISTS (
                    SELECT 1 FROM bridge_inbox AS prior WHERE prior.status='queued'
                    AND prior.open_id=current.open_id AND prior.chat_id=current.chat_id
                    AND prior.rowid<current.rowid)
                ORDER BY current.rowid LIMIT 1''', (time.time(),)).fetchone()
            return dict(row) if row else None

    def save_reply(self, message_id: str, text: str):
        with self.lock, self.db:
            self.db.execute('UPDATE bridge_inbox SET outgoing_text=?, error_code=NULL WHERE message_id=?', (text, message_id))

    def done(self, message_id: str):
        with self.lock, self.db:
            self.db.execute("UPDATE bridge_inbox SET status='done', error_code=NULL WHERE message_id=?", (message_id,))
            self.db.execute('INSERT INTO bridge_meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            ('last_reply_at', str(time.time())))

    def failed(self, message_id: str, code: str):
        safe_code = code if re.fullmatch(r'[a-z0-9_]{1,80}', code) else 'controlled_error'
        with self.lock, self.db:
            row = self.db.execute('SELECT attempts FROM bridge_inbox WHERE message_id=?', (message_id,)).fetchone()
            attempts = row['attempts'] + 1
            self.db.execute('UPDATE bridge_inbox SET attempts=?, status=?, retry_after=?, error_code=? WHERE message_id=?',
                            (attempts, 'failed' if attempts >= 3 else 'queued', time.time() + 2 ** attempts, safe_code, message_id))
            self.db.execute('INSERT INTO bridge_meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            ('last_error_code', safe_code))

    def session(self, open_id: str, chat_id: str) -> str | None:
        with self.lock:
            row = self.db.execute('SELECT workflow_id FROM bridge_sessions WHERE open_id=? AND chat_id=?', (open_id, chat_id)).fetchone()
            return row['workflow_id'] if row else None

    def set_session(self, open_id: str, chat_id: str, workflow_id: str):
        if not re.fullmatch(r'wf_[a-f0-9]{24}', workflow_id):
            raise BridgeError('invalid_workflow_id')
        with self.lock, self.db:
            self.db.execute('''INSERT INTO bridge_sessions VALUES (?, ?, ?)
                ON CONFLICT(open_id, chat_id) DO UPDATE SET workflow_id=excluded.workflow_id''', (open_id, chat_id, workflow_id))

    def summary(self) -> dict:
        with self.lock:
            counts = {row['status']: row['count'] for row in self.db.execute('SELECT status, COUNT(*) AS count FROM bridge_inbox GROUP BY status')}
            return {'paired': bool(self.meta('paired_open_id')), 'connection': self.meta('connection') or 'not_started',
                    'inbox_counts': counts, 'last_error_code': self.meta('last_error_code'),
                    'session_count': self.db.execute('SELECT COUNT(*) FROM bridge_sessions').fetchone()[0]}

    def close(self):
        with self.lock:
            self.db.close()


HELP = ('园区协同机器人 · 本机模拟业务测试\n'
        '1. 发送“报修 A栋302空调漏水”开始受理。\n'
        '2. 根据追问直接补充文字。\n'
        '3. 核对草稿后发送“确认普通 / 确认加急 / 确认特急”之一建单（中间可加空格）。\n'
        '4. 发送“进度”查询本次报修；“新报修 描述”开始另一单。\n'
        '报修受理仅处理单聊文字；已授权工程角色可用“工程帮助”查看任务卡和维修传图指引。\n'
        '不自动分派或验收，群聊尚未接入。请只输入模拟资料。')


def format_workflow(workflow: dict) -> str:
    draft = workflow.get('draft') or {}
    ai = draft.get('ai') or {}
    mode = (f"AI：通义千问 {ai.get('model') or ''}" if draft.get('mode') == 'llm'
            else 'AI：本次使用规则回退，请核对结果')
    if workflow.get('ticket'):
        # 查询读取的是业务状态，不能把建单前的初审模型冒充本次调用。
        lines = ['工单进度 · 模拟业务', '本次读取系统状态，未调用AI。']
        ticket = workflow['ticket']
        from app.services.reminder_policy import deadline_text
        statuses = {'pending': '待接单', 'accepted': '已接单', 'processing': '处理中', 'pending_review': '待企服验收', 'completed': '已完成'}
        lines += [f"工单：{ticket['ticket_no']}", *([f"工单ID：{ticket['id']}"] if ticket.get('id') else []), f"位置：{ticket['location']}",
                  f"状态：{statuses.get(ticket['status'], ticket['status'])}",
                  deadline_text(ticket) if ticket.get('created_at') and ticket.get('priority') else '',
                  '已授权工程人员可在飞书任务卡或Web办理分派及维修反馈；企服验收在Web办理。']
    else:
        lines = ['报修受理 · 模拟业务', mode]
        lines += [f"位置：{draft.get('location') or '待补充'}", f"故障：{draft.get('issue_type') or '待补充'}"]
        if workflow.get('missing_fields'):
            lines += [draft.get('follow_up_question') or '请补充具体位置和故障现象。', '尚未创建工单，请直接回复补充内容。']
        else:
            lines += [f"描述：{draft.get('description') or ''}",
                      '尚未创建工单。请核对后发送：确认普通 / 确认加急 / 确认特急（中间可加空格）。']
    return '\n'.join(lines)[:9000]


class FeishuBridge:
    def __init__(self, store: BridgeStore, business, messenger, app_id: str,
                 allowed_ids: set[str] | None = None, pairing_code: str | None = None):
        self.store, self.business, self.messenger = store, business, messenger
        self.app_id = app_id
        self.allowed_ids = allowed_ids or set()
        self.pairing_code = pairing_code
        self.pairing_expires = time.monotonic() + 600

    def authorized(self, message: IncomingMessage) -> bool:
        if self.allowed_ids:
            return message.open_id in self.allowed_ids
        return self.store.meta('paired_open_id') == message.open_id and self.store.meta('paired_chat_id') == message.chat_id

    def receive(self, payload: dict) -> str:
        message = parse_single_chat(payload, self.app_id)
        if message is None:
            return 'ignored'
        if not self.authorized(message):
            if (not self.allowed_ids and not self.store.meta('paired_open_id') and self.pairing_code
                    and time.monotonic() < self.pairing_expires
                    and hmac.compare_digest(message.text.encode('utf-8'), ('绑定 ' + self.pairing_code).encode('utf-8'))
                    and self.store.pair(message)):
                self.pairing_code = None
                return 'paired'
            return 'unauthorized'
        # 已授权用户发送重复配对指令时仅返回帮助，不把配对内容交给模型。
        kind = 'paired' if message.text.startswith('绑定 ') else 'business'
        return 'queued' if self.store.enqueue(message, kind) else 'duplicate'

    def _workflow(self, message: IncomingMessage) -> dict | None:
        workflow_id = self.store.session(message.open_id, message.chat_id)
        if not workflow_id:
            return None
        workflow = self.business.call('GET', f'/api/v1/agent/runs/{workflow_id}')
        if workflow.get('requester_id') != message.open_id or workflow.get('channel') != 'feishu':
            raise BridgeError('workflow_owner_mismatch')
        return workflow

    def handle(self, message: IncomingMessage) -> str:
        if message.text in ('帮助', 'help', '开始'):
            return HELP
        self.business.require_demo()
        key = f'feishu:{self.app_id}:{message.message_id}'
        new_repair = re.fullmatch(r'(?:报修|新报修)[\s：:]*(.*)', message.text, re.DOTALL)
        if new_repair:
            # 单独“新报修”也是明确的新受理意图；先建立缺项草稿，后续补充不能落回旧单。
            # 使用用户原始消息，不编造位置或故障，仍由现有完整性流程追问。
            text = new_repair.group(1).strip() or message.text
            workflow = self.business.call('POST', '/api/v1/integrations/feishu/repair-events',
                                          {'event_id': key, 'open_id': message.open_id, 'text': text})
            if workflow.get('requester_id') != message.open_id or workflow.get('channel') != 'feishu':
                raise BridgeError('workflow_owner_mismatch')
            self.store.set_session(message.open_id, message.chat_id, workflow['workflow_id'])
            return ('已开始新的报修草稿，尚未创建工单。\n' if not new_repair.group(1).strip() else '') + format_workflow(workflow)
        try:
            workflow = self._workflow(message)
        except BridgeError as error:
            if error.code == 'business_http_404':
                return '原演示流程已不可用，可能因后端重启清空。请发送“新报修 位置和故障描述”重新开始；不会关联其他人的记录。'
            raise
        if not workflow:
            return HELP
        if message.text in ('进度', '状态'):
            return format_workflow(workflow)
        # 确认指令不依赖中文输入中不自然的空格，仍须整条消息明确确认并指定优先级。
        confirmation = re.fullmatch(r'确认\s*(?:报修)?[\s：:]*(普通|加急|特急)[。！!]?', message.text)
        if workflow.get('ticket_id'):
            return ('' if confirmation else '当前会话已有工单，这条文字未作为新报修录入。另报故障请先发送“新报修”。\n') + format_workflow(workflow)
        priority_only = re.fullmatch(r'(普通|加急|特急)[。！!]?', message.text)
        if priority_only:
            # 单独选择优先级不等于确认建单，也不能作为故障补充发给模型污染草稿。
            if workflow['status'] != 'waiting_human' or workflow.get('missing_fields'):
                return '报修信息仍有缺项，尚未建单。\n' + format_workflow(workflow)
            return (f'已识别优先级“{priority_only.group(1)}”，尚未创建工单。\n'
                    f'请核对草稿后发送“确认{priority_only.group(1)}”（中间可加空格）。')
        if confirmation:
            if workflow['status'] != 'waiting_human' or workflow.get('missing_fields'):
                return '报修信息仍有缺项，尚未建单。\n' + format_workflow(workflow)
            draft = workflow['draft']
            priority = {'普通': 'normal', '加急': 'urgent', '特急': 'critical'}[confirmation.group(1)]
            workflow = self.business.call('POST', f"/api/v1/agent/runs/{workflow['workflow_id']}/confirm-ticket",
                {key: draft[key] for key in ('location', 'issue_type', 'description')} | {'priority': priority, 'idempotency_key': key})
            return format_workflow(workflow)
        if message.text.startswith('确认'):
            return '请明确选择优先级：确认普通 / 确认加急 / 确认特急（中间可加空格）。当前没有执行建单。'
        if workflow['status'] in ('waiting_input', 'waiting_human'):
            workflow = self.business.call('POST', f"/api/v1/agent/runs/{workflow['workflow_id']}/messages",
                                          {'message': message.text, 'idempotency_key': key})
            return format_workflow(workflow)
        return format_workflow(workflow)

    def process_one(self) -> bool:
        job = self.store.next_job()
        if not job:
            return False
        message = IncomingMessage(job['message_id'], job['chat_id'], job['open_id'], job['text'])
        try:
            if not self.authorized(message):
                # 本机允许名单收回后，既不执行缓存业务，也不向已撤权账号发回复。
                self.store.done(message.message_id)
                return True
            reply = job['outgoing_text']
            if reply is None:
                reply = ('本人单聊配对完成，仅授予隔离演示的报修提交权限。\n\n' + HELP
                         if job['kind'] == 'paired' else self.handle(message))
                self.store.save_reply(message.message_id, reply)
            reply_key = sha256(f'{self.app_id}:{message.message_id}:reply'.encode()).hexdigest()[:32]
            self.messenger.reply(message.message_id, reply, reply_key)
            self.store.done(message.message_id)
        except BridgeError as error:
            self.store.failed(message.message_id, error.code)
        except Exception:
            # 外部SDK异常可能包含认证头、请求正文或远端URL，仅记录受控类别。
            self.store.failed(message.message_id, 'unexpected_channel_error')
        return True
