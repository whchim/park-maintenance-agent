# SLA 时间规则
# 单位：分钟
# 注意：这是本项目 Demo 使用的模拟规则

SLA_RULES = {
    "normal": {
        "accept_minutes": 24 * 60,   # 24 小时接单
        "process_minutes": 48 * 60,  # 48 小时处理
    },
    "urgent": {
        "accept_minutes": 4 * 60,    # 4 小时接单
        "process_minutes": 8 * 60,   # 8 小时处理
    },
    "critical": {
        "accept_minutes": 5,         # 5 分钟接单
        "process_minutes": 30,       # 30 分钟处理
    },
}

from datetime import datetime, timedelta


def check_ticket_sla(ticket, now=None):
    """
    检查单条工单是否发生 SLA 超时。

    接单超时：从 created_at 开始计算。
    处理超时：从 accepted_at 开始计算。

    now 参数用于测试时传入固定时间，
    不传时默认使用当前时间。
    """

    # 如果没有传入时间，就使用系统当前时间
    if now is None:
        now = datetime.now()

    # 根据工单优先级获取对应 SLA 规则
    rules = SLA_RULES[ticket.priority]

    # 初始化结果
    result = {
        "accept_timeout": False,
        "accept_deadline": None,
        "process_timeout": False,
        "process_deadline": None,
    }

    # -------------------------
    # 一、检查接单是否超时
    # -------------------------

    # 只有尚未接单的工单，才需要检查接单超时
    if ticket.accepted_at is None:
        accept_deadline = ticket.created_at + timedelta(
            minutes=rules["accept_minutes"]
        )

        result["accept_deadline"] = accept_deadline

        # 当前时间超过接单截止时间，判定为超时
        result["accept_timeout"] = now > accept_deadline

    # -------------------------
    # 二、检查维修处理是否超时
    # -------------------------

    # 已经接单，但尚未提交维修结果，才需要检查处理超时
    if ticket.accepted_at is not None and ticket.submitted_at is None:
        process_deadline = ticket.accepted_at + timedelta(
            minutes=rules["process_minutes"]
        )

        result["process_deadline"] = process_deadline

        # 当前时间超过处理截止时间，判定为超时
        result["process_timeout"] = now > process_deadline

    return result