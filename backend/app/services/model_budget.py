"""部署演示的模型请求账本：先计数再发请求，重启后不重置每日额度。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from threading import BoundedSemaphore
import time


slots = BoundedSemaphore(2)


class ModelBudgetExceeded(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@contextmanager
def model_call_slot():
    setting = os.getenv("PARK_MODEL_DAILY_LIMIT")
    if setting is None:
        # 保持未配置部署配额的本地入口行为不变。
        yield
        return
    if not slots.acquire(blocking=False):
        raise ModelBudgetExceeded("model_busy")
    try:
        try:
            limit = int(setting)
            minute_limit = int(os.getenv("PARK_MODEL_PER_MINUTE_LIMIT", "10"))
            if limit < 1 or minute_limit < 1:
                raise ValueError
            path = Path(os.environ["PARK_MODEL_BUDGET_DB"])
            path.parent.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).date().isoformat()
            minute = int(time.time()) // 60
            connection = sqlite3.connect(path, timeout=5)
            try:
                connection.execute("CREATE TABLE IF NOT EXISTS daily_requests (day TEXT PRIMARY KEY, attempts INTEGER NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS minute_requests (minute INTEGER PRIMARY KEY, attempts INTEGER NOT NULL)")
                connection.execute("BEGIN IMMEDIATE")
                used = connection.execute("SELECT attempts FROM daily_requests WHERE day=?", (day,)).fetchone()
                if used and used[0] >= limit:
                    connection.rollback()
                    raise ModelBudgetExceeded("daily_budget_exceeded")
                recent = connection.execute("SELECT attempts FROM minute_requests WHERE minute=?", (minute,)).fetchone()
                if recent and recent[0] >= minute_limit:
                    connection.rollback()
                    raise ModelBudgetExceeded("model_rate_limited")
                connection.execute("INSERT INTO daily_requests VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET attempts=attempts+1", (day,))
                connection.execute("INSERT INTO minute_requests VALUES (?, 1) ON CONFLICT(minute) DO UPDATE SET attempts=attempts+1", (minute,))
                connection.execute("DELETE FROM minute_requests WHERE minute < ?", (minute - 2,))
                connection.commit()
            finally:
                connection.close()
        except (KeyError, ValueError, OSError, sqlite3.Error):
            raise ModelBudgetExceeded("budget_unavailable") from None
        yield
    finally:
        slots.release()
