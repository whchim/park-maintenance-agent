"""企服与招商的结构化模型任务；工程工具循环见 app.agents.engineering。"""

from app.schemas.department_ai import InvestmentReview, ServiceReview
from app.schemas.repair import RepairParseResult
from app.services.llm_service import run_structured
from app.services.repair_service import parse_repair_text


def _grounded(review, names: tuple[str, ...], text: str) -> None:
    for name in names:
        fact = getattr(review, name)
        if fact and (fact.evidence not in text or fact.value not in fact.evidence):
            raise ValueError("提取值及其依据必须出现在原文中")


def review_repair(text: str) -> RepairParseResult:
    """企服检查信息完整性，保留原始描述和规则优先级建议，不决定报修是否通过。"""
    result = parse_repair_text(text)
    review, metadata = run_structured(
        "service_agent",
        "职责：从报修对话按时间顺序提取位置和实际故障现象。最新明确更正优先。"
        "value 必须直接摘取原文，evidence 必须是包含 value 的原文片段。"
        "只有地址没有故障现象时 issue_type 为 null；没有具体位置时 location 为 null。"
        "缺失位置或故障时生成简短、针对性的中文追问；信息齐全时 follow_up_question 为空。"
        "不输出通过/拒绝结论，不承诺处理时效，不决定优先级。",
        {"conversation": text}, ServiceReview,
        lambda output: _grounded(output, ("location", "issue_type"), text),
    )
    if review:
        result.location = review.location.value if review.location else None
        result.issue_type = review.issue_type.value if review.issue_type else None
        result.missing_fields = [name for name in ("location", "issue_type") if getattr(result, name) is None]
        if not text.strip():
            result.missing_fields.insert(0, "description")
        result.follow_up_question = review.follow_up_question if result.missing_fields else ""
        result.evidence = {name: getattr(review, name).evidence for name in ("location", "issue_type") if getattr(review, name)}
    if result.missing_fields and not result.follow_up_question:
        labels = {"location": "具体位置", "issue_type": "故障现象", "description": "报修描述"}
        result.follow_up_question = "请补充" + "、".join(labels[name] for name in result.missing_fields) + "。"
    result.mode, result.ai = metadata["mode"], metadata
    return result


def draft_investment(message: str, source: str | None, rules: dict) -> dict:
    review, metadata = run_structured(
        "investment_agent",
        "职责：整理招商客户意向，提取客户企业名称、联系人和联系方式。"
        "value 直接摘取原文，evidence 为包含 value 的原文片段；未提供则为 null。"
        "按明确意向/考虑中/初步了解分别建议 A/B/C，不足以判断时为 null，并说明理由。"
        "follow_up_subject 只起草跟进任务主题，不虚构已联系、已签约或企服已接收。"
        "不决定客户归属，不生成正式合同，不承诺租金。",
        {"message": message, "source": source}, InvestmentReview,
        lambda output: _grounded(output, ("customer_name", "contact_name", "contact_phone"), message),
    )
    if review:
        for name in ("customer_name", "contact_name", "contact_phone"):
            rules[name] = getattr(review, name).value if getattr(review, name) else None
        rules.update(intent_level=review.intent_level, intent_reason=review.intent_reason,
                     follow_up_subject=review.follow_up_subject,
                     follow_up_interval_days={"A": 1, "B": 3, "C": 7}.get(review.intent_level),
                     missing_fields=[] if rules["customer_name"] else ["customer_name"],
                     evidence={name: getattr(review, name).evidence for name in ("customer_name", "contact_name", "contact_phone") if getattr(review, name)})
    # 任务描述始终保存原文；模型的等级是建议，确认接口仍接受人工选择。
    return {**rules, "mode": metadata["mode"], "ai": metadata}


