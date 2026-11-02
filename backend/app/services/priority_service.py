from app.schemas.ticket import Priority


# 定义紧急关键词
CRITICAL_KEYWORDS = [
    "电梯困人",
    "人员被困",
    "火灾",
    "起火",
    "触电",
    "严重漏电",
    "燃气泄漏",
]

# 定义加急关键词
URGENT_KEYWORDS = [
    "漏水",
    "停电",
    "空调故障",
    "设备故障",
    "门禁故障",
]


def classify_priority(description: str) -> Priority:
    """
    根据报修描述判断工单优先级。

    特急关键词优先于加急关键词。
    """

    # 统一转换为小写，方便后续扩展英文关键词
    text = description.lower()

    # 先检查紧急关键词
    for keyword in CRITICAL_KEYWORDS:
        if keyword in text:
            return Priority.CRITICAL

    # 再检查加急关键词
    for keyword in URGENT_KEYWORDS:
        if keyword in text:
            return Priority.URGENT

    # 没有匹配到关键词，默认普通
    return Priority.NORMAL