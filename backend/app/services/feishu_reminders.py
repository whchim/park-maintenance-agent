"""单实例飞书主动提醒：纯业务规则 + 明确身份映射 + 可恢复投递账本。"""

from hashlib import sha256
import json
import re
from threading import Lock
import time

from app.services.feishu_bridge import BridgeError
from app.services.reminder_policy import deadline_text, follow_up_due, park_now, parse_time, ticket_reminder_kinds


class ReminderChannel:
    def __init__(self, store, business, messenger, engineering_identities, investment_owners=None, now=park_now, engineering_channel=None):
        self.store, self.business, self.messenger = store, business, messenger
        self.engineering, self.investment, self.now = engineering_identities, investment_owners or {}, now
        self.scan_lock = Lock()
        self.engineering_channel = engineering_channel
        for owner in [*engineering_identities, *self.investment.values()]:
            if not re.fullmatch(r'ou_[A-Za-z0-9_-]{1,180}', owner):
                raise BridgeError('invalid_reminder_recipient')
        with store.lock, store.db:
            store.db.execute('''CREATE TABLE IF NOT EXISTS reminder_deliveries (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, entity_id INTEGER NOT NULL, owner TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                retry_after REAL NOT NULL DEFAULT 0, sent_at TEXT, message_id TEXT, chat_id TEXT, error_code TEXT)''')
            # 同一UUID恢复发送；不重放任何业务API写入。
            store.db.execute("UPDATE reminder_deliveries SET status='queued' WHERE status='sending'")

    def _candidate(self, kind, entity, owner, text):
        cycle = (entity.get('accepted_at') or '') + ':' + (entity.get('assignee') or '') if kind == 'process_overdue' else ''
        token = entity.get('task_no') or entity.get('ticket_no')
        revision = entity.get('due_at') if kind == 'follow_up_due' else entity.get('created_at')
        key = sha256(json.dumps([self.store.meta('app_fingerprint'), kind, entity['id'], token, revision, cycle, owner],
                                ensure_ascii=False).encode()).hexdigest()[:32]
        return {'id': key, 'kind': kind, 'entity_id': entity['id'], 'owner': owner, 'text': text}

    def _pushed_at(self, ticket_id, owner, ticket):
        key = self._candidate('ticket_new', ticket, owner, '')['id']
        with self.store.lock:
            row = self.store.db.execute("SELECT sent_at FROM reminder_deliveries WHERE id=? AND status='sent'", (key,)).fetchone()
        return row['sent_at'] if row else None

    def _ticket_candidates(self, ticket):
        now, candidates = self.now(), []
        managers = [owner for owner, identity in self.engineering.items() if 'dispatcher' in identity.roles]
        technicians = [owner for owner, identity in self.engineering.items()
                       if 'technician' in identity.roles and identity.personnel_no == ticket.get('assignee')]
        if ticket['status'] == 'pending' and not ticket.get('accepted_at'):
            for owner in managers:
                candidates.append(self._candidate('ticket_new', ticket, owner,
                    f"工程新工单 · 模拟业务\n工单：{ticket['ticket_no']}（ID {ticket['id']}）\n位置：{ticket['location']}\n故障：{ticket['issue_type']}\n{deadline_text(ticket)}\n可单独发送以下指令查看建议或当前状态：\n派工 {ticket['id']}\n" +
                    ('工单已有分派，请核对当前维修员及进度。' if ticket.get('assignee') else '由工程负责人核对建议后确认委派。')))
        # 特急升级分别依据该负责人实际收到的本机制通知回执。
        for owner in dict.fromkeys(managers + technicians):
            receipt = self._pushed_at(ticket['id'], owner, ticket)
            for kind in ticket_reminder_kinds(ticket, now, receipt):
                reason = {'critical_no_response': '特急通知发送成功已满5分钟，仍未接单，请工程负责人电话对接并记录结果。',
                          'accept_overdue': '已超过接单截止时间，请负责人核对并跟进。',
                          'process_overdue': '已超过处理截止时间，请维修员反馈进展，负责人协调处理。'}[kind]
                if kind == 'critical_no_response' and owner not in managers:
                    continue
                candidates.append(self._candidate(kind, ticket, owner,
                    f"工程时限提醒 · 模拟业务\n工单：{ticket['ticket_no']}（ID {ticket['id']}）\n位置：{ticket['location']}\n{deadline_text(ticket)}\n{reason}\n本提醒未执行电话联系或更改工单状态。"))
        return candidates

    def _follow_up_candidates(self, tasks):
        result = []
        for task in tasks:
            owner = self.investment.get(task.get('owner'))
            if owner and follow_up_due(task, self.now()):
                result.append(self._candidate('follow_up_due', task, owner,
                    f"招商回访到期 · 模拟业务\n任务：{task['task_no']}\n客户：{task['customer_name']}\n负责人：{task['owner']}\n到期：{parse_time(task['due_at']):%Y-%m-%d %H:%M:%S}（北京时间）\n事项：{task['subject']}\n请在Web招商客户工作区记录实际回访结果；提醒不会自动标记已联系或完成。"))
        return result

    def scan(self):
        if not self.scan_lock.acquire(blocking=False):
            return False
        try:
            self.business.require_demo()
            tickets = self.business.call('GET', '/api/v1/tickets')
            tasks = self.business.call('GET', '/api/v1/agents/investment/follow-ups') if self.investment else []
            candidates = [item for ticket in tickets for item in self._ticket_candidates(ticket)] + self._follow_up_candidates(tasks)
            with self.store.lock, self.store.db:
                for item in candidates:
                    self.store.db.execute('INSERT OR IGNORE INTO reminder_deliveries (id,kind,entity_id,owner,payload) VALUES (?,?,?,?,?)',
                        (item['id'], item['kind'], item['entity_id'], item['owner'], json.dumps(item, ensure_ascii=False)))
            self.store.set_meta('reminder_last_scan_at', self.now().isoformat())
            managers = any('dispatcher' in identity.roles for identity in self.engineering.values())
            unmapped = sum(1 for ticket in tickets if ticket['status'] == 'pending' and not managers)
            unmapped += sum(1 for task in tasks if follow_up_due(task, self.now()) and task.get('owner') not in self.investment)
            self.store.set_meta('reminder_unmapped_count', str(unmapped))
            if self.engineering_channel:
                self.engineering_channel.queue_sync(tickets)
            self.store.set_meta('reminder_scan_error', '')
            return True
        except Exception as error:
            self.store.set_meta('reminder_scan_error', self._error(error))
            return False
        finally:
            self.scan_lock.release()

    @staticmethod
    def _error(error):
        code = error.code if isinstance(error, BridgeError) else 'reminder_operation_error'
        return code if re.fullmatch(r'[a-z0-9_]{1,80}', code) else 'reminder_operation_error'

    def _current(self, delivery):
        self.business.require_demo()
        if delivery['kind'] == 'follow_up_due':
            tasks = self.business.call('GET', '/api/v1/agents/investment/follow-ups')
            candidates = self._follow_up_candidates(tasks)
        else:
            try:
                ticket = self.business.call('GET', f"/api/v1/tickets/{delivery['entity_id']}")
            except BridgeError as error:
                if error.code == 'business_http_404':
                    return None
                raise
            candidates = self._ticket_candidates(ticket)
        return next((item for item in candidates if item['id'] == delivery['id']), None)

    def send_one(self):
        with self.store.lock, self.store.db:
            row = self.store.db.execute("SELECT * FROM reminder_deliveries WHERE status='queued' AND retry_after<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return False
            delivery = dict(row)
            self.store.db.execute("UPDATE reminder_deliveries SET status='sending' WHERE id=?", (delivery['id'],))
        try:
            current = self._current(delivery)
            if not current:
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE reminder_deliveries SET status='cancelled' WHERE id=?", (delivery['id'],))
                return True
            message_id, chat_id = self.messenger.send(delivery['owner'], 'text', {'text': current['text']}, delivery['id'])
            if not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value) for value in (message_id, chat_id)):
                raise BridgeError('invalid_send_response')
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE reminder_deliveries SET status='sent',sent_at=?,message_id=?,chat_id=?,error_code=NULL,payload=? WHERE id=?",
                    (self.now().isoformat(), message_id, chat_id, json.dumps(current, ensure_ascii=False), delivery['id']))
        except Exception as error:
            attempts, code = delivery['attempts'] + 1, self._error(error)
            with self.store.lock, self.store.db:
                self.store.db.execute('UPDATE reminder_deliveries SET status=?,attempts=?,retry_after=?,error_code=? WHERE id=?',
                    ('failed' if attempts >= 3 else 'queued', attempts, time.time() + 2 ** attempts, code, delivery['id']))
        return True

    def summary(self):
        with self.store.lock:
            return {'deliveries': dict(self.store.db.execute('SELECT status,COUNT(*) FROM reminder_deliveries GROUP BY status').fetchall()),
                    'last_scan_at': self.store.meta('reminder_last_scan_at'),
                    'unmapped_count': int(self.store.meta('reminder_unmapped_count') or 0),
                    'scan_error': self.store.meta('reminder_scan_error') or None}
