from app.schemas.ticket import Priority
from app.services.priority_service import classify_priority


def test_classify_critical_priority():
    # 电梯困人应该识别为紧急
    result = classify_priority("A栋电梯困人，请立即处理")

    assert result == Priority.CRITICAL


def test_classify_urgent_priority():
    # 漏水应该识别为加急
    result = classify_priority("A栋3楼卫生间漏水")

    assert result == Priority.URGENT


def test_classify_normal_priority():
    # 普通照明问题默认普通
    result = classify_priority("走廊灯不亮")

    assert result == Priority.NORMAL


def test_critical_has_higher_priority():
    # 同时出现加急和紧急关键词，应该优先判定为紧急
    result = classify_priority("卫生间漏水，同时发现严重漏电")

    assert result == Priority.CRITICAL