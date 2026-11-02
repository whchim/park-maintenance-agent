"""验收入口必须默认离线，且失败不能伪装成在线验收通过。"""

import pytest
from sqlalchemy import Engine, event

from scripts.verify_agents import run_checks
from app.services import llm_service


@pytest.fixture
def forbid_database_connect():
    def blocked(*args):
        pytest.fail("独立 Agent 验收不得连接数据库")
    event.listen(Engine, "connect", blocked)
    try:
        yield
    finally:
        event.remove(Engine, "connect", blocked)


def test_verification_defaults_offline_even_when_parent_enabled_qwen(monkeypatch, forbid_database_connect):
    monkeypatch.setenv("PARK_AI_MODE", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-never-send")
    monkeypatch.setattr(llm_service, "_post_completion", lambda *_: pytest.fail("离线验收不得联网"))
    report, code = run_checks()
    assert code == 0
    assert report["status"] == "offline_checks_passed"
    assert len(report["cases"]) == 3
    assert report["cases"][-1]["result"]["orchestration"]["model_calls"] == 0
    assert llm_service.get_model_settings().mode == "qwen"  # 函数退出恢复调用方的进程配置。


def test_online_verification_requires_ready_configuration(monkeypatch, forbid_database_connect):
    monkeypatch.setenv("PARK_AI_MODE", "qwen")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(llm_service, "_post_completion", lambda *_: pytest.fail("缺少密钥不得联网"))
    report, code = run_checks(online=True)
    assert code == 2
    assert report["status"] == "configuration_not_ready"
    assert report["cases"] == []


def test_online_verification_fallback_is_failure_and_stops_further_requests(monkeypatch, forbid_database_connect):
    monkeypatch.setenv("PARK_AI_MODE", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-never-send")
    monkeypatch.setenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("QWEN_MODEL", "qwen-plus")
    monkeypatch.setenv("QWEN_TIMEOUT_SECONDS", "15")
    calls = []

    def timeout(*args):
        calls.append(True)
        raise TimeoutError("test-key-never-send")
    monkeypatch.setattr(llm_service, "_post_completion", timeout)
    report, code = run_checks(online=True)
    assert code == 1
    assert report["status"] == "checks_failed"
    assert len(report["cases"]) == len(calls) == 1
    assert report["cases"][0]["checks"]["expected_execution_mode"] is False
    assert report["cases"][0]["result"]["ai"]["fallback_reason"] == "timeout"
    assert "test-key-never-send" not in str(report)
