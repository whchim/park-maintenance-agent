from datetime import datetime, timezone
import sqlite3

import pytest

from app.services import model_budget
from app.services.model_budget import ModelBudgetExceeded, model_call_slot


def setup_budget(monkeypatch, tmp_path, limit=2):
    path = tmp_path / "model-budget.sqlite3"
    monkeypatch.setenv("PARK_MODEL_DAILY_LIMIT", str(limit))
    monkeypatch.setenv("PARK_MODEL_BUDGET_DB", str(path))
    return path


def test_daily_limit_is_persistent_and_failure_still_counts(monkeypatch, tmp_path):
    path = setup_budget(monkeypatch, tmp_path)
    with model_call_slot():
        pass
    with pytest.raises(RuntimeError):
        with model_call_slot():
            raise RuntimeError("模拟网络故障")
    with pytest.raises(ModelBudgetExceeded, match="daily_budget_exceeded"):
        with model_call_slot():
            pytest.fail("额度耗尽后不能发送模型请求")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT attempts FROM daily_requests").fetchall() == [(2,)]
    monkeypatch.setenv("PARK_MODEL_DAILY_LIMIT", "3")
    with model_call_slot():
        pass  # 故障路径已释放并发槽位。


def test_busy_does_not_consume_budget(monkeypatch, tmp_path):
    path = setup_budget(monkeypatch, tmp_path, limit=10)
    with model_call_slot(), model_call_slot():
        with pytest.raises(ModelBudgetExceeded, match="model_busy"):
            with model_call_slot():
                pytest.fail("不应允许第三个并发调用")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT attempts FROM daily_requests").fetchall() == [(2,)]


@pytest.mark.parametrize("value", ["", "0", "-1", "abc"])
def test_invalid_quota_fails_closed(monkeypatch, tmp_path, value):
    setup_budget(monkeypatch, tmp_path)
    monkeypatch.setenv("PARK_MODEL_DAILY_LIMIT", value)
    with pytest.raises(ModelBudgetExceeded, match="budget_unavailable"):
        with model_call_slot():
            pytest.fail("错误配置不能绕过配额")


def test_unwritable_ledger_fails_closed(monkeypatch, tmp_path):
    setup_budget(monkeypatch, tmp_path)
    monkeypatch.setenv("PARK_MODEL_BUDGET_DB", str(tmp_path))
    with pytest.raises(ModelBudgetExceeded, match="budget_unavailable"):
        with model_call_slot():
            pytest.fail("账本故障不能绕过配额")


def test_previous_day_does_not_use_today_quota(monkeypatch, tmp_path):
    path = setup_budget(monkeypatch, tmp_path, limit=1)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE daily_requests (day TEXT PRIMARY KEY, attempts INTEGER NOT NULL)")
        connection.execute("INSERT INTO daily_requests VALUES ('2000-01-01', 100)")
    with model_call_slot():
        pass
    with sqlite3.connect(path) as connection:
        today = datetime.now(timezone.utc).date().isoformat()
        assert connection.execute("SELECT attempts FROM daily_requests WHERE day=?", (today,)).fetchone() == (1,)


def test_unconfigured_local_calls_are_unchanged(monkeypatch):
    monkeypatch.delenv("PARK_MODEL_DAILY_LIMIT", raising=False)
    with model_call_slot():
        pass


def test_minute_limit_is_persistent_and_window_renews(monkeypatch, tmp_path):
    path = setup_budget(monkeypatch, tmp_path, limit=10)
    monkeypatch.setenv("PARK_MODEL_PER_MINUTE_LIMIT", "1")
    monkeypatch.setattr(model_budget.time, "time", lambda: 12000)
    with model_call_slot():
        pass
    with pytest.raises(ModelBudgetExceeded, match="model_rate_limited"):
        with model_call_slot():
            pytest.fail("分钟额度用完后不能发送请求")
    monkeypatch.setattr(model_budget.time, "time", lambda: 12060)
    with model_call_slot():
        pass
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT attempts FROM daily_requests").fetchone() == (2,)


def test_real_transport_boundary_and_structured_fallback(monkeypatch, tmp_path):
    from pydantic import BaseModel
    from app.services import llm_service

    class Output(BaseModel):
        value: str

    setup_budget(monkeypatch, tmp_path, limit=1)
    monkeypatch.setenv("PARK_AI_MODE", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-test-not-a-real-key")
    calls = []

    def fake_transport(settings, body):
        calls.append(body)
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"value":"ok"}'}}]}

    monkeypatch.setattr(llm_service, "_send_completion", fake_transport)
    first, metadata = llm_service.run_structured("test", "test", {}, Output)
    assert first.value == "ok" and metadata["mode"] == "llm"
    second, metadata = llm_service.run_structured("test", "test", {}, Output)
    assert second is None and metadata["fallback_reason"] == "daily_budget_exceeded"
    result, metadata = llm_service.run_tool_turn([], [])
    assert result is None and metadata["fallback_reason"] == "daily_budget_exceeded"
    assert metadata["request_attempted"] is False
    assert len(calls) == 1
