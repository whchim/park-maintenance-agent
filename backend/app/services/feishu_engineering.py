"""飞书工程协作渠道；业务事实由已有API提供，卡片不是部门权限凭证。"""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
import time

from PIL import Image

from app.services.feishu_bridge import BridgeError, IncomingMessage, parse_single_chat
from app.agents.repair_feedback import review_feedback
from app.services.reminder_policy import deadline_text


STATUS_LABELS = {'pending': '待接单', 'accepted': '已接单', 'processing': '维修中',
                 'pending_review': '待企服验收', 'completed': '已完成'}


@dataclass(frozen=True)
class EngineeringIdentity:
    roles: frozenset[str]
    actor_id: str
    personnel_no: str | None = None


def load_identities(path: Path) -> dict[str, EngineeringIdentity]:
    """本机管理员配置；不从消息正文、按钮value或模型输出接受角色。"""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or len(data) > 100:
            raise ValueError()
        result, personnel = {}, set()
        for owner, item in data.items():
            if not re.fullmatch(r'ou_[A-Za-z0-9_-]{1,180}', owner) or not isinstance(item, dict):
                raise ValueError()
            if set(item) - {'roles', 'actor_id', 'personnel_no'}:
                raise ValueError()
            roles = item['roles']
            if not isinstance(roles, list) or not roles or set(roles) - {'dispatcher', 'technician'}:
                raise ValueError()
            actor = item['actor_id']
            number = item.get('personnel_no')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', actor):
                raise ValueError()
            if 'technician' in roles:
                if not isinstance(number, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', number) or number in personnel:
                    raise ValueError()
                personnel.add(number)
            elif number is not None:
                raise ValueError()
            result[owner] = EngineeringIdentity(frozenset(roles), actor, number)
        return result
    except (OSError, ValueError, TypeError, KeyError):
        raise BridgeError('invalid_engineering_identities') from None


def plain(text: str) -> dict:
    return {'tag': 'div', 'text': {'tag': 'plain_text', 'content': text[:6000]}}


class EngineeringChannel:
    """仅在isolated_demo运行；与文字报修共享锁，独立记录工程卡片及出站通知。"""
    def __init__(self, store, business, messenger, app_id: str, identities: dict[str, EngineeringIdentity]):
        self.store, self.business, self.messenger = store, business, messenger
        self.app_id, self.identities = app_id, identities
        with store.lock, store.db:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS eng_jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, chat TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued');
                CREATE TABLE IF NOT EXISTS eng_cards (
                    nonce TEXT PRIMARY KEY, owner TEXT NOT NULL, chat TEXT NOT NULL, ticket_id INTEGER NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL, snapshot TEXT NOT NULL, expires REAL NOT NULL,
                    message_id TEXT, status TEXT NOT NULL DEFAULT 'ready');
                CREATE TABLE IF NOT EXISTS eng_outbox (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, chat TEXT NOT NULL, msg_type TEXT NOT NULL,
                    content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                    retry_after REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS eng_context (
                    owner TEXT NOT NULL, chat TEXT NOT NULL, ticket_id INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    PRIMARY KEY(owner,chat));
                CREATE TABLE IF NOT EXISTS eng_evidence (
                    message_id TEXT PRIMARY KEY, owner TEXT NOT NULL, chat TEXT NOT NULL, ticket_id INTEGER NOT NULL,
                    snapshot TEXT NOT NULL, attachment_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS eng_feedback_drafts (
                    owner TEXT NOT NULL, chat TEXT NOT NULL, ticket_id INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    messages TEXT NOT NULL, description TEXT, ai TEXT NOT NULL, PRIMARY KEY(owner,chat,ticket_id));
                CREATE TABLE IF NOT EXISTS eng_card_views (
                    owner TEXT NOT NULL, chat TEXT NOT NULL, ticket_id INTEGER NOT NULL, kind TEXT NOT NULL,
                    message_id TEXT NOT NULL, PRIMARY KEY(owner,chat,ticket_id,kind));
                CREATE TABLE IF NOT EXISTS eng_sync_observed (
                    ticket_id INTEGER PRIMARY KEY, stamp TEXT NOT NULL, identities TEXT NOT NULL);
            ''')
            # 上一次进程若在HTTP提交期间中断，不能盲目重放旧API写请求。
            store.db.execute("UPDATE eng_jobs SET status='blocked' WHERE status='working'")
            store.db.execute("UPDATE eng_cards SET status='blocked' WHERE status='executing'")
            store.db.execute("UPDATE eng_outbox SET status='queued' WHERE status='sending'")

    def identity(self, owner, role=None):
        identity = self.identities.get(owner)
        if identity is None or (role and role not in identity.roles):
            raise BridgeError('engineering_role_required')
        return identity

    def clear_context(self, owner, chat):
        # 开始另一条报修时退出旧维修图片上下文，避免把新报修图归给旧维修单。
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM eng_context WHERE owner=? AND chat=?', (owner, chat))

    @staticmethod
    def command(text):
        return text == '重试照片' or bool(re.match(r'^(?:工程帮助|派工|维修反馈|维修)(?:\s|[0-9]|$)', text))

    def context(self, owner, chat):
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM eng_context WHERE owner=? AND chat=?', (owner, chat)).fetchone()
        return dict(row) if row else None

    def receive(self, payload):
        """SDK事件回调只校验和入队；图片资源读取留给工作线程。"""
        try:
            raw = payload['event']['message']
            image = raw.get('message_type') == 'image'
            if image:
                key = json.loads(raw['content'])['image_key']
                if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,240}', key):
                    return 'ignored'
                facade = json.loads(json.dumps(payload))
                facade['event']['message'].update(message_type='text', content=json.dumps({'text': key}))
                message = parse_single_chat(facade, self.app_id)
            else:
                message = parse_single_chat(payload, self.app_id)
            if not message:
                return 'not_engineering'
            context = self.context(message.open_id, message.chat_id)
            explicit = self.command(message.text)
            reserved = (message.text in ('帮助', 'help', '开始') or bool(re.match(r'^(?:报修|新报修|绑定)', message.text)))
            contextual = (not image and not explicit and not reserved and context is not None
                          and message.open_id in self.identities
                          and 'technician' in self.identities[message.open_id].roles)
            if not image and not explicit and not contextual:
                return 'not_engineering'
            self.identity(message.open_id, 'technician' if image else None)
            if image:
                # 没有明确维修上下文的图片不擅自归到最近报修单。
                if not context:
                    return self._enqueue(message, 'command', {'text': '图片缺少工单上下文'})
            data = {'text': message.text}
            if context:
                # 消息到达时就绑定任务，不能被后来“维修 另一工单”改变归属。
                data['context'] = context
            return self._enqueue(message, 'image' if image else 'feedback' if contextual else 'command', data)
        except (KeyError, TypeError, ValueError, AttributeError):
            return 'ignored'
        except BridgeError:
            return 'unauthorized'

    def _enqueue(self, message, kind, payload):
        with self.store.lock, self.store.db:
            inserted = self.store.db.execute('INSERT OR IGNORE INTO eng_jobs (id,owner,chat,kind,payload) VALUES (?,?,?,?,?)',
                ('message:' + message.message_id, message.open_id, message.chat_id, kind,
                 json.dumps(payload, ensure_ascii=False))).rowcount
        return 'queued' if inserted else 'duplicate'

    def receive_action(self, payload):
        """nonce是指向服务端动作的引用；转发、改value或换操作者不能扩大权限。"""
        try:
            if payload['header']['event_type'] != 'card.action.trigger' or payload['header'].get('app_id') not in (None, self.app_id):
                return 'rejected'
            event = payload['event']
            owner = event['operator']['open_id']
            context, value = event['context'], event['action']['value']
            if not isinstance(value, dict) or set(value) != {'nonce'} or not re.fullmatch(r'[a-f0-9]{32}', value['nonce']):
                return 'rejected'
            with self.store.lock, self.store.db:
                row = self.store.db.execute('SELECT * FROM eng_cards WHERE nonce=?', (value['nonce'],)).fetchone()
                if (not row or row['owner'] != owner or row['chat'] != context['open_chat_id']
                        or not row['message_id'] or row['message_id'] != context['open_message_id']
                        or row['expires'] < time.time()):
                    return 'rejected'
                self.identity(owner, 'dispatcher' if row['action'] == 'assign' else 'technician')
                if row['status'] == 'superseded':
                    return 'rejected'
                if row['status'] != 'ready':
                    return 'duplicate'
                self.store.db.execute("UPDATE eng_cards SET status='queued' WHERE nonce=?", (row['nonce'],))
                self.store.db.execute('INSERT INTO eng_jobs (id,owner,chat,kind,payload) VALUES (?,?,?,?,?)',
                    ('action:' + row['nonce'], owner, row['chat'], 'action', json.dumps({'nonce': row['nonce']})))
            return 'queued'
        except (KeyError, ValueError, TypeError, AttributeError, BridgeError):
            return 'rejected'

    def snapshot(self, ticket_id):
        ticket = self.business.call('GET', f'/api/v1/tickets/{ticket_id}')
        assignments = self.business.call('GET', f'/api/v1/tickets/{ticket_id}/assignments')
        repairs = self.business.call('GET', f'/api/v1/tickets/{ticket_id}/repair-records')
        if not isinstance(ticket, dict) or not isinstance(assignments, list) or not isinstance(repairs, list):
            raise BridgeError('invalid_engineering_snapshot')
        current = next((item for item in assignments if item['is_current']), None)
        stamp = sha256(json.dumps([ticket, assignments, repairs], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return ticket, current, repairs, stamp

    def technician_snapshot(self, owner, ticket_id):
        identity = self.identity(owner, 'technician')
        state = self.snapshot(ticket_id)
        if not state[1] or state[1]['personnel_no'] != identity.personnel_no:
            raise BridgeError('current_technician_required')
        return state

    def _queue_output(self, job_id, owner, chat, content, msg_type='text', suffix='reply'):
        if msg_type == 'text':
            content = {'text': content}
        key = sha256(f'{self.app_id}:{job_id}:{suffix}'.encode()).hexdigest()[:32]
        with self.store.lock, self.store.db:
            view = self.view_key(content) if msg_type == 'interactive' else None
            if view:
                previous = self.store.db.execute('SELECT message_id FROM eng_card_views WHERE owner=? AND chat=? AND ticket_id=? AND kind=?',
                                                 (owner, chat, *view)).fetchone()
                if previous:
                    self.store.db.execute("UPDATE eng_cards SET status='superseded' WHERE message_id=? AND status IN ('ready','queued')", (previous['message_id'],))
                    # 较早的失败更新不能在重试时覆盖后来已确认展示的新内容。
                    for pending in self.store.db.execute("SELECT id,content FROM eng_outbox WHERE msg_type='patch' AND status='queued'").fetchall():
                        if json.loads(pending['content'])['message_id'] == previous['message_id']:
                            self.store.db.execute("UPDATE eng_outbox SET status='superseded' WHERE id=?", (pending['id'],))
                    content = {'message_id': previous['message_id'], 'card': content}
                    msg_type = 'patch'
            self.store.db.execute('INSERT OR IGNORE INTO eng_outbox (id,owner,chat,msg_type,content) VALUES (?,?,?,?,?)',
                (key, owner, chat, msg_type, json.dumps(content, ensure_ascii=False)))

    @staticmethod
    def view_key(content):
        title = content.get('header', {}).get('title', {}).get('content', '')
        first = (content.get('elements') or [{}])[0].get('text', {}).get('content', '')
        match = re.match(r'^工单：[^\n]+（ID ([0-9]+)）', first)
        return (int(match.group(1)), title) if match else None

    def retire_cards(self, job_id, ticket_id, status):
        """保留历史记录；有message_id的旧按钮卡变为只读，当前任务卡由patch更新。"""
        with self.store.lock, self.store.db:
            rows = self.store.db.execute('SELECT DISTINCT owner,chat,message_id FROM eng_cards WHERE ticket_id=? AND message_id IS NOT NULL', (ticket_id,)).fetchall()
            self.store.db.execute("UPDATE eng_cards SET status='superseded' WHERE ticket_id=? AND status IN ('ready','queued') AND message_id IS NOT NULL", (ticket_id,))
            task_views = {row[0] for row in self.store.db.execute("SELECT message_id FROM eng_card_views WHERE ticket_id=? AND kind=?",
                         (ticket_id, '维修人员任务 · 模拟业务'))}
            for row in rows:
                if row['message_id'] in task_views:
                    continue
                content = {'message_id': row['message_id'], 'card': self.card('历史操作已结束', [plain(
                    f'此卡片的操作入口已停用。当前工单状态：{STATUS_LABELS.get(status, "请刷新查询")}。\n请使用最新任务卡查看和办理。')])}
                key = sha256(f"{self.app_id}:{job_id}:retire:{row['message_id']}".encode()).hexdigest()[:32]
                self.store.db.execute('INSERT OR IGNORE INTO eng_outbox (id,owner,chat,msg_type,content) VALUES (?,?,?,?,?)',
                    (key, row['owner'], row['chat'], 'patch', json.dumps(content, ensure_ascii=False)))

    def _button(self, owner, chat, ticket_id, action, stamp, title, payload=None):
        token = secrets.token_hex(16)
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO eng_cards (nonce,owner,chat,ticket_id,action,payload,snapshot,expires) VALUES (?,?,?,?,?,?,?,?)',
                (token, owner, chat, ticket_id, action, json.dumps(payload or {}, ensure_ascii=False), stamp, time.time() + 900))
        return {'tag': 'button', 'text': {'tag': 'plain_text', 'content': title}, 'type': 'primary',
                'value': {'nonce': token}, 'confirm': {'title': {'tag': 'plain_text', 'content': '确认业务操作'},
                'text': {'tag': 'plain_text', 'content': title + '？本次仅操作模拟业务数据。'}}}

    @staticmethod
    def card(title, elements):
        return {'config': {'wide_screen_mode': True}, 'header': {'template': 'blue',
                'title': {'tag': 'plain_text', 'content': title + ' · 模拟业务'}}, 'elements': elements}

    def dispatch_card(self, owner, chat, ticket_id):
        self.identity(owner, 'dispatcher')
        ticket, current, _, stamp = self.snapshot(ticket_id)
        if current or ticket['status'] != 'pending':
            identity = self.identities[owner]
            if current and 'technician' in identity.roles and identity.personnel_no == current['personnel_no']:
                return self.task_card(owner, chat, ticket_id)
            return self.status_card(owner, ticket, current)
        suggestion = self.business.call('POST', f'/api/v1/tickets/{ticket_id}/dispatch-suggestion')
        mode = ('通义千问 ' + str(suggestion.get('ai', {}).get('model') or '')
                if suggestion.get('mode') == 'llm' else '规则回退')
        elements = [plain(f"工单：{ticket['ticket_no']}（ID {ticket_id}）\n位置：{ticket['location']}\n故障：{ticket['issue_type']}\n{deadline_text(ticket)}"),
                    plain(f"工程Agent：{mode}\n{suggestion.get('reason') or '无推荐理由'}\n建议不等于正式派工；请负责人核对并选择。")]
        bound = {item.personnel_no for item in self.identities.values() if 'technician' in item.roles}
        for person in suggestion.get('candidates', [])[:20]:
            number = person['personnel_no']
            elements.append(plain(f"{person['display_name']}（{number}）" + ('，已绑定飞书维修角色' if number in bound else '，飞书未绑定，可在Web办理')))
            if number in bound and (not current or current['personnel_no'] != number):
                button = self._button(owner, chat, ticket_id, 'assign', stamp,
                                      '确认委派 ' + person['display_name'], {'personnel_no': number})
                elements.append({'tag': 'action', 'actions': [button]})
        elements.append(plain('卡片15分钟有效；状态或分派变化后需重新发送“派工 工单ID”。'))
        return self.card('工程负责人派工确认', elements)

    def status_card(self, owner, ticket, current):
        """负责人查看实际进度；查看权限不能变成其他维修员的操作权限。"""
        self.identity(owner, 'dispatcher')
        worker = next((key for key, identity in self.identities.items()
                       if current and 'technician' in identity.roles and identity.personnel_no == current['personnel_no']), None)
        assigned = f"{current['personnel_name']}（{current['personnel_no']}）" if current else '尚未分派'
        if current:
            guidance = ('当前维修员已绑定飞书，任务会同步到其账号。' if worker else
                        '当前维修员尚未绑定飞书账号，请继续在Web办理；飞书账号映射需管理员明确配置。')
            guidance += '\n当前账号以工程负责人身份查看进度，维修操作由当前分派人员办理。'
        else:
            guidance = '当前状态不提供派工操作，请在Web核对分派与业务记录。'
        return self.card('工程工单当前状态', [plain(
            f"工单：{ticket['ticket_no']}（ID {ticket['id']}）\n位置：{ticket['location']}\n状态：{STATUS_LABELS.get(ticket['status'], '请刷新查询')}\n当前维修员：{assigned}\n{deadline_text(ticket)}"),
            plain(guidance), plain(f"查询进度可单独发送：派工 {ticket['id']}\n本次读取实际状态，未调用AI或重复派工。")])

    def task_card(self, owner, chat, ticket_id, activate_context=True):
        ticket, current, _, stamp = self.technician_snapshot(owner, ticket_id)
        elements = [plain(f"工单：{ticket['ticket_no']}（ID {ticket_id}）\n位置：{ticket['location']}\n故障：{ticket['issue_type']}\n当前维修员：{current['personnel_name']}\n状态：{STATUS_LABELS.get(ticket['status'], '请刷新查询')}\n{deadline_text(ticket)}")]
        if ticket['status'] in ('pending', 'accepted'):
            action, title = ('accept', '确认接单') if ticket['status'] == 'pending' else ('start', '开始维修')
            elements.append({'tag': 'action', 'actions': [self._button(owner, chat, ticket_id, action, stamp, title)]})
        elif ticket['status'] == 'processing':
            if activate_context:
                with self.store.lock, self.store.db:
                    self.store.db.execute('INSERT INTO eng_context VALUES (?,?,?,?) ON CONFLICT(owner,chat) DO UPDATE SET ticket_id=excluded.ticket_id,snapshot=excluded.snapshot',
                                          (owner, chat, ticket_id, stamp))
                elements.append(plain('当前正在办理这张工单。可以直接发送处理说明，例如“漏水已维修完毕”，并发送现场照片。\n说明和照片会分别保留，材料齐全后展示预览；只有你确认后才提交企服验收。'))
            else:
                elements.append(plain(f'任务状态已同步。要在飞书提交说明和照片，请先单独发送“维修 {ticket_id}”选择材料所属工单。'))
        elif ticket['status'] == 'completed':
            elements.append(plain('企服已验收并关闭这张工单，当前无需维修操作。'))
        else:
            elements.append(plain('当前无维修动作；待企服在Web验收。工程人员不能直接关闭工单。'))
        return self.card('维修人员任务', elements)

    def _sync_identity_stamp(self):
        return sha256(json.dumps([(owner, sorted(identity.roles), identity.personnel_no)
                                 for owner, identity in sorted(self.identities.items())]).encode()).hexdigest()

    def queue_sync(self, tickets):
        """扫描只记录观察到的变化；卡片生成与普通工程操作在原工作线程串行执行。"""
        identities = self._sync_identity_stamp()
        for ticket in tickets:
            with self.store.lock:
                historical = self.store.db.execute('SELECT 1 FROM eng_card_views WHERE ticket_id=?', (ticket['id'],)).fetchone()
            if not ticket.get('assignee') or (ticket['status'] == 'completed' and not historical):
                continue
            state = self.snapshot(ticket['id'])
            managers = [owner for owner, identity in self.identities.items() if 'dispatcher' in identity.roles]
            workers = [owner for owner, identity in self.identities.items()
                       if state[1] and 'technician' in identity.roles and identity.personnel_no == state[1]['personnel_no']]
            if not managers and not workers:
                continue
            key = 'sync:' + sha256(f"{ticket['id']}:{state[3]}:{identities}".encode()).hexdigest()
            with self.store.lock, self.store.db:
                previous = self.store.db.execute('SELECT stamp,identities FROM eng_sync_observed WHERE ticket_id=?', (ticket['id'],)).fetchone()
                if previous and previous['stamp'] == state[3] and previous['identities'] == identities:
                    continue
                self.store.db.execute('INSERT INTO eng_sync_observed VALUES (?,?,?) ON CONFLICT(ticket_id) DO UPDATE SET stamp=excluded.stamp,identities=excluded.identities',
                                      (ticket['id'], state[3], identities))
                for queued in self.store.db.execute("SELECT id,payload FROM eng_jobs WHERE kind='sync' AND status='queued'").fetchall():
                    if json.loads(queued['payload'])['ticket_id'] == ticket['id']:
                        self.store.db.execute("UPDATE eng_jobs SET status='superseded' WHERE id=?", (queued['id'],))
                self.store.db.execute('INSERT OR IGNORE INTO eng_jobs (id,owner,chat,kind,payload) VALUES (?,?,?,?,?)',
                    (key, (managers or workers)[0], '', 'sync', json.dumps({'ticket_id': ticket['id'], 'stamp': state[3]})))

    def handle_sync(self, job, data):
        ticket, current, _, stamp = self.snapshot(data['ticket_id'])
        if stamp != data['stamp']:
            return  # 在途状态已改变，下轮扫描展示最新快照，不重放旧阶段。
        ticket_id = ticket['id']
        managers = [owner for owner, identity in self.identities.items() if 'dispatcher' in identity.roles]
        workers = [owner for owner, identity in self.identities.items()
                   if current and 'technician' in identity.roles and identity.personnel_no == current['personnel_no']]
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM eng_context WHERE ticket_id=? AND snapshot!=?', (ticket_id, stamp))
            views = self.store.db.execute('SELECT * FROM eng_card_views WHERE ticket_id=?', (ticket_id,)).fetchall()
        self.retire_cards(job['id'], ticket_id, ticket['status'])
        for view in views:
            if view['kind'] == '维修人员任务 · 模拟业务' and view['owner'] not in workers:
                readonly = self.card('维修人员任务', [plain(f"工单：{ticket['ticket_no']}（ID {ticket_id}）\n分派已更新，原维修操作入口已停用。请使用当前负责人提供的任务信息。")])
                self._queue_output(job['id'], view['owner'], view['chat'], readonly, 'interactive', 'former-' + view['owner'])
        for owner in dict.fromkeys(managers + workers):
            view = next((view for view in views if view['owner'] == owner), None)
            if ticket['status'] == 'completed' and not view:
                continue
            chat = view['chat'] if view else (self.store.meta('paired_chat_id') if owner == self.store.meta('paired_open_id') else '')
            card = self.task_card(owner, chat, ticket_id, activate_context=False) if owner in workers else self.status_card(owner, ticket, current)
            self._queue_output(job['id'], owner, chat, card, 'interactive', 'current-' + owner)

    def evidence(self, owner, chat, ticket_id, stamp):
        with self.store.lock:
            rows = self.store.db.execute('SELECT attachment_id FROM eng_evidence WHERE owner=? AND chat=? AND ticket_id=? AND snapshot=? ORDER BY rowid',
                                         (owner, chat, ticket_id, stamp)).fetchall()
        return [row['attachment_id'] for row in rows]

    def feedback_card(self, owner, chat, ticket_id, description, ai=None, model_called=True):
        ticket, _, _, stamp = self.technician_snapshot(owner, ticket_id)
        if ticket['status'] != 'processing' or not description.strip() or len(description) > 4000:
            raise BridgeError('processing_and_description_required')
        photos = self.evidence(owner, chat, ticket_id, stamp)
        if not photos:
            raise BridgeError('repair_photo_required')
        metadata = ai or {}
        mode = ('本次说明整理：通义千问 ' + str(metadata.get('model') or '') if metadata.get('mode') == 'llm'
                else '本次说明按原文暂存（规则回退）')
        if not model_called:
            mode = '本次仅更新已保存材料，未调用AI；维修说明沿用此前草稿。'
        elements = [plain(f"工单：{ticket['ticket_no']}（ID {ticket_id}）\n维修说明：{description}\n已保存照片：{len(photos)}张\n{mode}\n尚未提交。材料检查不代表已验证照片真实性或维修效果。"),
                    {'tag': 'action', 'actions': [self._button(owner, chat, ticket_id, 'submit', stamp,
                        '确认提交维修结果', {'repair_description': description, 'attachment_ids': photos})]},
                    plain('提交成功后待企服验收，不会直接关闭工单。')]
        return self.card('维修凭证提交预览', elements)

    def draft(self, owner, chat, ticket_id, stamp):
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM eng_feedback_drafts WHERE owner=? AND chat=? AND ticket_id=? AND snapshot=?',
                                         (owner, chat, ticket_id, stamp)).fetchone()
        return dict(row) if row else None

    def present_feedback(self, job, ticket_id, stamp, model_called):
        draft = self.draft(job['owner'], job['chat'], ticket_id, stamp)
        if not draft or not draft['description']:
            return False
        photos = self.evidence(job['owner'], job['chat'], ticket_id, stamp)
        ai = json.loads(draft['ai'])
        if not photos:
            source = ('本次说明由通义千问整理。' if ai.get('mode') == 'llm' else '本次按原文暂存（规则回退）。') if model_called else '本次仅核对已保存材料，未调用AI。'
            self._queue_output(job['id'], job['owner'], job['chat'],
                f"已保留维修说明：{draft['description']}\n{source}\n还缺成功保存的现场照片，工单尚未提交。请发送照片；若上一张因权限失败，可在修复权限后发送“重试照片”。")
        else:
            card = self.feedback_card(job['owner'], job['chat'], ticket_id, draft['description'], ai, model_called)
            self._queue_output(job['id'], job['owner'], job['chat'], card, 'interactive')
        return True

    def handle_feedback(self, job, data):
        context = data.get('context')
        if not context:
            raise BridgeError('repair_context_required')
        ticket_id, owner, chat = context['ticket_id'], job['owner'], job['chat']
        ticket, _, _, stamp = self.technician_snapshot(owner, ticket_id)
        if data['text'] in ('进度', '状态', '维修进度'):
            self._queue_output(job['id'], owner, chat, self.task_card(owner, chat, ticket_id), 'interactive')
            return
        if stamp != context['snapshot'] or ticket['status'] != 'processing':
            raise BridgeError('stale_engineering_card')
        previous = self.draft(owner, chat, ticket_id, stamp)
        if len(data['text']) > 4000:
            raise BridgeError('feedback_too_long')
        messages = (json.loads(previous['messages']) if previous else []) + [data['text']]
        messages = messages[-8:]
        if sum(map(len, messages)) > 8000:
            raise BridgeError('feedback_too_long')
        reviewed = review_feedback(ticket, messages, len(self.evidence(owner, chat, ticket_id, stamp)))
        if reviewed['intent'] == 'progress_query':
            self._queue_output(job['id'], owner, chat, self.task_card(owner, chat, ticket_id), 'interactive')
            return
        if reviewed['intent'] != 'repair_feedback' or not reviewed['description']:
            self._queue_output(job['id'], owner, chat, '当前正在办理这张维修工单。请说明实际处理内容或结果，我会整理反馈草稿；查询进度可直接发送“进度”，另报故障请发送“新报修 描述”。尚未提交维修结果。')
            return
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO eng_feedback_drafts VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner,chat,ticket_id) DO UPDATE SET snapshot=excluded.snapshot,messages=excluded.messages,description=excluded.description,ai=excluded.ai',
                (owner, chat, ticket_id, stamp, json.dumps(messages, ensure_ascii=False), reviewed['description'], json.dumps(reviewed['ai'], ensure_ascii=False)))
        self.present_feedback(job, ticket_id, stamp, True)

    def handle_command(self, job, data):
        text, owner, chat = data['text'], job['owner'], job['chat']
        if text == '工程帮助':
            self._queue_output(job['id'], owner, chat, '工程负责人：派工 工单ID\n维修员：维修 工单ID，按卡片接单和开始维修；处理中可以直接发送处理说明和现场照片，顺序不限。材料齐全后核对预览，再点击确认提交。照片失败时可发送“重试照片”；另报故障请发送“新报修 描述”。\n所有角色与资料均为模拟；企服仍在Web验收。')
            return
        if text == '重试照片':
            self.retry_photo(job, data)
            return
        # 派工入口只读查询，容忍复制通知时带入的引号/说明；写动作仍由卡片确认。
        dispatch = re.match(r'^派工\s*([1-9][0-9]{0,8})(?=$|[\s”"，,。；;：:])', text)
        task = re.fullmatch(r'维修\s*([1-9][0-9]{0,8})', text)
        feedback = re.fullmatch(r'维修反馈\s*([1-9][0-9]{0,8})\s+(.+)', text, re.DOTALL)
        if dispatch:
            card = self.dispatch_card(owner, chat, int(dispatch.group(1)))
        elif task:
            card = self.task_card(owner, chat, int(task.group(1)))
            self.retire_cards(job['id'], int(task.group(1)), self.snapshot(int(task.group(1)))[0]['status'])
        elif feedback:
            ticket_id = int(feedback.group(1))
            state = self.technician_snapshot(owner, ticket_id)
            self.handle_feedback(job, {'text': feedback.group(2).strip(), 'context': {'ticket_id': ticket_id, 'snapshot': state[3]}})
            return
        else:
            raise BridgeError('engineering_command_format')
        self._queue_output(job['id'], owner, chat, card, 'interactive')

    def retry_photo(self, job, data):
        context = data.get('context') or self.context(job['owner'], job['chat'])
        if not context:
            raise BridgeError('repair_context_required')
        with self.store.lock:
            previous = self.store.db.execute("SELECT * FROM eng_jobs WHERE owner=? AND chat=? AND kind='image' AND status='blocked' ORDER BY rowid DESC LIMIT 1",
                                              (job['owner'], job['chat'])).fetchone()
        if not previous:
            raise BridgeError('no_failed_photo')
        photo = json.loads(previous['payload'])
        original_context = photo.get('context')
        if original_context and (original_context['ticket_id'] != context['ticket_id'] or original_context['snapshot'] != context['snapshot']):
            raise BridgeError('stale_engineering_card')
        photo['context'] = context
        photo['source_message_id'] = previous['id'].removeprefix('message:')
        self.handle_image(job, photo)

    def handle_image(self, job, data):
        owner, chat = job['owner'], job['chat']
        row = data.get('context')
        if not row:
            raise BridgeError('repair_context_required')
        ticket_id = row['ticket_id']
        ticket, _, _, stamp = self.technician_snapshot(owner, ticket_id)
        if ticket['status'] != 'processing' or stamp != row['snapshot']:
            raise BridgeError('stale_engineering_card')
        if len(self.evidence(owner, chat, ticket_id, stamp)) >= 5:
            raise BridgeError('repair_photo_limit')
        source_message_id = data.get('source_message_id') or job['id'].removeprefix('message:')
        evidence_key = 'message:' + source_message_id
        with self.store.lock:
            exists = self.store.db.execute('SELECT * FROM eng_evidence WHERE message_id=?', (evidence_key,)).fetchone()
        if exists:
            if exists['ticket_id'] != ticket_id or exists['snapshot'] != stamp:
                raise BridgeError('stale_engineering_card')
            self._queue_output(job['id'], owner, chat, '这张照片已经保存，无需重复上传。')
            return
        content = self.messenger.download_image(source_message_id, data['text'])
        if not content or len(content) > 10 * 1024 * 1024:
            raise BridgeError('invalid_repair_image')
        try:
            with Image.open(BytesIO(content)) as image:
                media_type = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp'}[image.format]
        except Exception:
            raise BridgeError('invalid_repair_image') from None
        uploaded = self.business.upload_repair_image(content, media_type)
        attachment_id = uploaded.get('id')
        if not isinstance(attachment_id, str) or not re.fullmatch(r'[a-f0-9]{32}', attachment_id):
            raise BridgeError('invalid_attachment_response')
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO eng_evidence VALUES (?,?,?,?,?,?)',
                                  (evidence_key, owner, chat, ticket_id, stamp, attachment_id))
        if not self.present_feedback(job, ticket_id, stamp, False):
            self._queue_output(job['id'], owner, chat,
                f"已保存工单{ticket['ticket_no']}的现场照片，共{len(self.evidence(owner, chat, ticket_id, stamp))}张。\n请直接说明实际处理内容或结果，例如“漏水已维修完毕”，我会整理预览供你确认。尚未提交维修结果。")

    def handle_action(self, job, data):
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM eng_cards WHERE nonce=?', (data['nonce'],)).fetchone()
        if not row or row['status'] != 'queued' or row['expires'] < time.time():
            raise BridgeError('stale_engineering_card')
        action, owner, ticket_id = row['action'], job['owner'], row['ticket_id']
        identity = self.identity(owner, 'dispatcher' if action == 'assign' else 'technician')
        ticket, current, _, stamp = self.snapshot(ticket_id)
        if stamp != row['snapshot']:
            raise BridgeError('stale_engineering_card')
        body = json.loads(row['payload'])
        if action == 'assign':
            if ticket['status'] != 'pending': raise BridgeError('pending_ticket_required')
            recipient = next((key for key, item in self.identities.items() if 'technician' in item.roles and item.personnel_no == body['personnel_no']), None)
            if not recipient: raise BridgeError('technician_binding_required')
            path, payload = 'assignments', {'personnel_no': body['personnel_no'], 'assigned_by': identity.actor_id}
        else:
            if not current or current['personnel_no'] != identity.personnel_no:
                raise BridgeError('current_technician_required')
            expected = {'accept': 'pending', 'start': 'accepted', 'submit': 'processing'}
            if action not in expected or ticket['status'] != expected[action]:
                raise BridgeError('stale_engineering_card')
            path = {'accept': 'accept', 'start': 'start-processing', 'submit': 'repair-records'}[action]
            payload = {'personnel_no': identity.personnel_no}
            if action == 'submit':
                # 照片来自本操作者、本会话、本次维修快照，不接受卡片传入的任意附件ID。
                valid_photos = self.evidence(owner, job['chat'], ticket_id, stamp)
                if not body.get('repair_description', '').strip() or not body.get('attachment_ids') or any(photo not in valid_photos for photo in body['attachment_ids']):
                    raise BridgeError('repair_photo_required')
                payload.update(body)
        # 旧业务API没有通用幂等键：此标记后无论超时或崩溃，均不得自动重试写请求。
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE eng_cards SET status='executing' WHERE nonce=?", (row['nonce'],))
        try:
            self.business.call('POST', f'/api/v1/tickets/{ticket_id}/{path}', payload)
        except Exception:
            raise BridgeError('engineering_submission_uncertain') from None
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE eng_cards SET status='done' WHERE nonce=?", (row['nonce'],))
        if action == 'assign':
            # 通知与正式分派分开，消息发送失败不等于分派失败，不再调用分派API。
            recipient_chat = job['chat'] if recipient == owner else ''
            card = self.task_card(recipient, recipient_chat, ticket_id)
            self._queue_output(job['id'], recipient, recipient_chat, card, 'interactive', 'worker-task')
            self._queue_output(job['id'], owner, job['chat'], '已记录分派，等待维修人员接单。')
        else:
            card = self.task_card(owner, job['chat'], ticket_id)
            self._queue_output(job['id'], owner, job['chat'], card, 'interactive')
            if action == 'submit':
                self.notify_reporter(job['id'], ticket_id, ticket['ticket_no'])
        self.retire_cards(job['id'], ticket_id, self.snapshot(ticket_id)[0]['status'])

    def notify_reporter(self, job_id, ticket_id, ticket_no):
        # 仅通知已验证的原飞书流程发起人；不接受卡片传入的任意接收者。
        with self.store.lock:
            sessions = self.store.db.execute('SELECT * FROM bridge_sessions').fetchall()
        for session in sessions:
            try:
                workflow = self.business.call('GET', '/api/v1/agent/runs/' + session['workflow_id'])
            except BridgeError:
                continue
            if (workflow.get('ticket_id') == ticket_id and workflow.get('channel') == 'feishu'
                    and workflow.get('requester_id') == session['open_id']):
                self._queue_output(job_id, session['open_id'], session['chat_id'],
                    f'工单{ticket_no}已提交维修结果和照片，当前待企服验收。请在Web工作台核对凭证并决定通过或退回；尚未关闭工单。',
                    suffix='reporter-' + session['workflow_id'])

    def process_one(self):
        # 先完成上一任务的已准备消息，再处理同步任务，避免未绑定message_id的卡片重复创建。
        if self.send_one():
            return True
        with self.store.lock, self.store.db:
            row = self.store.db.execute("SELECT * FROM eng_jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            job = dict(row) if row else None
            if job:
                self.store.db.execute("UPDATE eng_jobs SET status='working' WHERE id=?", (job['id'],))
        # 网络调用不能持有回调入队使用的锁，否则飞书快速ACK会被发送请求阻塞。
        if job is None:
            return self.send_one()
        try:
            self.identity(job['owner'])
            self.business.require_demo()
            handler = {'command': self.handle_command, 'image': self.handle_image, 'action': self.handle_action,
                       'feedback': self.handle_feedback, 'sync': self.handle_sync}[job['kind']]
            handler(job, json.loads(job['payload']))
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE eng_jobs SET status='done' WHERE id=?", (job['id'],))
        except Exception as error:
            code = error.code if isinstance(error, BridgeError) else 'engineering_channel_error'
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE eng_jobs SET status='blocked' WHERE id=?", (job['id'],))
                if job['kind'] == 'action':
                    self.store.db.execute("UPDATE eng_cards SET status='blocked' WHERE nonce=? AND status!='done'", (json.loads(job['payload'])['nonce'],))
                self.store.db.execute('INSERT INTO bridge_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('engineering_error', code))
            if job['owner'] in self.identities:
                messages = {'repair_photo_required': '还缺成功保存的现场照片，请发送图片，说明会保留。',
                            'feishu_api_99991672': '这张照片尚未保存，维修结果尚未提交。飞书应用缺少读取消息图片所需权限；请管理员增加im:message:readonly并发布生效后，在当前单聊发送“重试照片”。处理说明可以先发，我会保留。',
                            'feishu_image_network_error': '这张照片暂未读取成功，维修结果尚未提交。请稍后发送“重试照片”，或重新发送图片；已保存的说明会保留。',
                            'repair_context_required': '请先发送“维修 工单ID”选择当前任务，再发送照片或处理说明。',
                            'no_failed_photo': '当前没有可重试的失败照片，请直接发送现场图片。',
                            'invalid_repair_image': '照片未保存。请使用10MiB以内的静态JPEG、PNG或WebP图片重新发送。',
                            'repair_photo_limit': '已达到本次最多5张照片，请核对说明并确认提交。',
                            'feedback_too_long': '说明过长，本次没有更新草稿或提交。请将处理说明精简至4000字以内；多轮说明总计不超过8000字。',
                            'current_technician_required': '你不是当前分派的维修人员，请核对工单或联系工程负责人。',
                            'engineering_role_required': '当前模拟角色没有此操作权限。',
                            'engineering_command_format': '请只输入一条指令，例如“派工 3”查看建议或实际进度，或“维修 3”打开本人当前维修任务。请勿把整段说明作为提交指令。',
                            'pending_ticket_required': '这张工单已进入后续处理阶段，请发送“派工 工单ID”查询实际负责人和进度。',
                            'business_http_404': '未找到这张工单，请核对工单ID后重新查询。',
                            'stale_engineering_card': '卡片或维修上下文已失效，请重新发送“派工 工单ID”或“维修 工单ID”刷新。',
                            'engineering_submission_uncertain': '本次提交结果无法确认，系统不会自动重放。请先查询工单并核对历史记录，再刷新任务卡。'}
                self._queue_output(job['id'], job['owner'], job['chat'], messages.get(code,
                    '本次操作未能完成，请查询当前工单并刷新任务卡；系统已保留错误记录供排查，未自动重新提交。'))
        return True

    def send_one(self):
        with self.store.lock, self.store.db:
            row = self.store.db.execute("SELECT * FROM eng_outbox WHERE status='queued' AND retry_after<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
            if not row: return False
            message = dict(row)
            self.store.db.execute("UPDATE eng_outbox SET status='sending' WHERE id=?", (message['id'],))
        try:
            # 原报修人可以收到结果通知，但不会因此取得工程卡片操作权限。
            if message['msg_type'] != 'text' or message['owner'] != self.store.meta('paired_open_id'):
                self.identity(message['owner'])
            content = json.loads(message['content'])
            is_card = message['msg_type'] in ('interactive', 'patch')
            if message['msg_type'] == 'patch':
                message_id, chat_id = content['message_id'], message['chat']
                content = content['card']
                self.messenger.patch(message_id, content)
            else:
                message_id, chat_id = self.messenger.send(message['owner'], message['msg_type'], content, message['id'])
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', message_id) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', chat_id):
                raise BridgeError('invalid_send_response')
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE eng_outbox SET status='sent' WHERE id=?", (message['id'],))
                # 每张卡片只更新其中实际出现的nonce，不把其他历史卡片误关联到这次消息。
                if is_card:
                    for element in content['elements']:
                        for button in element.get('actions', []):
                            self.store.db.execute('UPDATE eng_cards SET message_id=?,chat=? WHERE nonce=?',
                                (message_id, chat_id, button['value']['nonce']))
                    view = self.view_key(content)
                    if view:
                        self.store.db.execute('INSERT INTO eng_card_views VALUES (?,?,?,?,?) ON CONFLICT(owner,chat,ticket_id,kind) DO UPDATE SET message_id=excluded.message_id',
                            (message['owner'], chat_id, *view, message_id))
        except Exception as error:
            attempts = message['attempts'] + 1
            code = error.code if isinstance(error, BridgeError) else 'engineering_send_error'
            if not re.fullmatch(r'[a-z0-9_]{1,80}', code): code = 'engineering_send_error'
            with self.store.lock, self.store.db:
                self.store.db.execute('UPDATE eng_outbox SET status=?,attempts=?,retry_after=? WHERE id=?',
                    ('failed' if attempts >= 3 else 'queued', attempts, time.time() + 2 ** attempts, message['id']))
                self.store.db.execute('INSERT INTO bridge_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                      ('engineering_send_error', code))
        return True

    def summary(self):
        with self.store.lock:
            return {'jobs': dict(self.store.db.execute('SELECT status,COUNT(*) FROM eng_jobs GROUP BY status').fetchall()),
                    'outbox': dict(self.store.db.execute('SELECT status,COUNT(*) FROM eng_outbox GROUP BY status').fetchall()),
                    'last_error_code': self.store.meta('engineering_error'),
                    'last_send_error_code': self.store.meta('engineering_send_error')}
