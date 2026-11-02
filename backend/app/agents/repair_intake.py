"""企服完整性工作流；有条件分支，不宣称拥有自主工具决策。"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.schemas.repair import RepairParseResult
from app.services.department_agent_service import review_repair


class IntakeState(TypedDict, total=False):
    conversation: str
    supplement: str
    parsed: RepairParseResult
    steps: list[dict]


def _review(state: IntakeState) -> dict:
    parsed = review_repair(state["conversation"])
    # 补充消息可能只包含“位置在...”而不重复故障关键词。
    # 保留原先规则模式的显式地址补充能力；模型模式仍以原文证据校验为准。
    if parsed.mode != "llm":
        for marker in ("位置在", "地点在", "地点是"):
            if marker in state.get("supplement", ""):
                location = state["supplement"].split(marker, 1)[1].strip("，,。；; ")
                if location:
                    parsed.location = location
                    parsed.missing_fields = [field for field in parsed.missing_fields if field != "location"]
                break
        if not parsed.missing_fields:
            parsed.follow_up_question = ""
    return {"parsed": parsed, "steps": [{"node": "service_review", "status": "completed",
            "summary": "企服检查信息完整性", "mode": parsed.mode}]}


def _wait_input(state: IntakeState) -> dict:
    return {"steps": state["steps"] + [{"node": "wait_for_input", "status": "waiting",
            "summary": "等待补充：" + "、".join(state["parsed"].missing_fields)}]}


def _wait_human(state: IntakeState) -> dict:
    return {"steps": state["steps"] + [{"node": "wait_for_confirmation", "status": "waiting",
            "summary": "信息齐全，等待人工核对建单；未创建工单"}]}


def _build_graph():
    graph = StateGraph(IntakeState)
    graph.add_node("service_review", _review)
    graph.add_node("wait_for_input", _wait_input)
    graph.add_node("wait_for_confirmation", _wait_human)
    graph.add_edge(START, "service_review")
    graph.add_conditional_edges("service_review", lambda state: (
        "missing" if state["parsed"].missing_fields else "complete"
    ), {"missing": "wait_for_input", "complete": "wait_for_confirmation"})
    graph.add_edge("wait_for_input", END)
    graph.add_edge("wait_for_confirmation", END)
    return graph.compile()


intake_graph = _build_graph()


def run_intake(conversation: str, supplement: str = "") -> RepairParseResult:
    state = intake_graph.invoke({"conversation": conversation, "supplement": supplement})
    parsed = state["parsed"]
    parsed.orchestration = {"framework": "langgraph", "graph_version": "repair-intake-v1",
                            "persistence": "application_events", "steps": state["steps"]}
    return parsed
