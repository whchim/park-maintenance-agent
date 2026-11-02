"""只替换模型传输，验证工程反馈图的原文约束及材料检查，不调用业务写入。"""

import pytest

from app.agents.repair_feedback import review_feedback
from app.services import llm_service
from test_department_ai import completion, fact


TICKET = {'ticket_no': 'WX-DEMO', 'location': 'A栋302', 'issue_type': '漏水', 'status': 'processing'}


@pytest.fixture
def qwen(monkeypatch, offline_model_by_default):
    monkeypatch.setenv('PARK_AI_MODE', 'qwen')
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test-key-never-send')
    monkeypatch.setenv('QWEN_MODEL', 'qwen-plus')


def test_model_extracts_latest_correction_with_actual_photo_count(qwen, monkeypatch):
    calls = []
    def respond(settings, body):
        calls.append(body)
        return completion({'intent': 'repair_feedback', 'description': fact('尚未更换，仅清理排水管')})
    monkeypatch.setattr(llm_service, '_post_completion', respond)
    result = review_feedback(TICKET, ['已更换排水管', '更正：尚未更换，仅清理排水管'], 0)
    assert result['description'] == '尚未更换，仅清理排水管'
    assert result['missing_fields'] == ['photos']
    assert result['ai']['mode'] == 'llm' and len(calls) == 1
    assert result['steps'][-1] == '等待补充材料'


@pytest.mark.parametrize('description', ['已更换排水管', '已试机一小时正常'])
def test_model_cannot_copy_outdated_or_invented_result(qwen, monkeypatch, description):
    monkeypatch.setattr(llm_service, '_post_completion', lambda *_: completion({
        'intent': 'repair_feedback', 'description': fact(description)}))
    result = review_feedback(TICKET, ['已更换排水管', '更正：尚未更换，仅清理排水管'], 1)
    assert result['description'] == '更正：尚未更换，仅清理排水管'
    assert result['ai']['mode'] == 'rule_fallback_demo'
    assert result['ai']['fallback_reason'] == 'invalid_output'
    assert result['steps'][-1] == '等待人工核对提交'


@pytest.mark.parametrize(('text', 'intent'), [('你好', 'other'), ('现在什么状态', 'progress_query')])
def test_non_feedback_never_reuses_prior_description(qwen, monkeypatch, text, intent):
    monkeypatch.setattr(llm_service, '_post_completion', lambda *_: completion({'intent': intent, 'description': None}))
    result = review_feedback(TICKET, ['已更换排水管', text], 1)
    assert result['intent'] == intent and result['description'] is None
    assert result['missing_fields'] == ['description']


def test_offline_fallback_keeps_exact_negative_statement():
    result = review_feedback(TICKET, ['排水尚未恢复，维修仍未完成'], 1)
    assert result['description'] == '排水尚未恢复，维修仍未完成'
    assert result['ai']['mode'] == 'rule_fallback_demo'
    assert result['ai']['fallback_reason'] == 'disabled'


def test_offline_fallback_never_truncates_late_negative_correction():
    text = '已处理' + '排水管检查；' * 60 + '更正：尚未恢复排水，仍需维修'
    result = review_feedback(TICKET, [text], 1)
    assert len(text) > 300 and result['description'] == text
