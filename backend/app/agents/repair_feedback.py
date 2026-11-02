"""工程反馈整理图：理解原文、核对真实材料，始终停在人工确认之前。"""

from typing import Literal, TypedDict
import re

from langgraph.graph import END, START, StateGraph

from app.schemas.department_ai import ExtractedFact, ModelOutput
from app.services.llm_service import run_structured


class FeedbackReview(ModelOutput):
    intent: Literal['repair_feedback', 'progress_query', 'other']
    description: ExtractedFact | None


class FeedbackState(TypedDict, total=False):
    messages: list[str]
    ticket: dict
    photo_count: int
    description: str | None
    intent: str
    ai: dict
    missing_fields: list[str]
    steps: list[str]


def _understand(state):
    latest = state['messages'][-1].strip()

    def grounded(review):
        if review.description and (review.description.evidence not in latest
                or review.description.value not in review.description.evidence):
            raise ValueError('feedback_must_be_grounded')
        if review.intent != 'repair_feedback' and review.description is not None:
            raise ValueError('unexpected_feedback_description')

    review, metadata = run_structured('engineering_feedback_agent',
        '当前操作者正在处理已绑定的维修任务。判断最新消息是维修反馈、进度询问还是无关内容。'
        '“漏水已维修完毕”等自然表达属于维修反馈，不能要求用户输入固定口令。'
        'description仅逐字摘取用户提供的处理说明或实际结果，evidence必须为包含value的原文。'
        'description及evidence从最新一条消息摘取，历史仅供理解，最新明确更正优先；不能从原故障编造已更换零件、已试机、维修耗时或效果。'
        '无法提取说明填null，不把“确认”“提交”“你好”等控制或闲聊文字当维修说明。'
        '不能决定验收或关闭，不能认为照片已证明维修效果。',
        {'ticket': {key: state['ticket'][key] for key in ('ticket_no', 'location', 'issue_type', 'status')},
         'messages': state['messages'], 'saved_photo_count': state['photo_count']}, FeedbackReview, grounded)
    if review:
        intent = review.intent
        description = review.description.value if review.description else None
    else:
        latest = state['messages'][-1].strip()
        progress = latest in ('进度', '状态', '现在到哪了', '现在什么状态')
        feedback = bool(re.search(r'维修|处理|清理|更换|修好|修完|完毕|恢复|不漏|排水|堵塞', latest))
        intent = 'progress_query' if progress else 'repair_feedback' if feedback else 'other'
        # 回退保留完整原文，不能截掉末尾的否定或更正。
        description = latest if intent == 'repair_feedback' else None
    return {'intent': intent, 'description': description, 'ai': metadata, 'steps': ['理解当前维修反馈']}


def _materials(state):
    missing = []
    if not state.get('description'): missing.append('description')
    if state['photo_count'] < 1: missing.append('photos')
    return {'missing_fields': missing, 'steps': state['steps'] + ['按实际保存记录核对照片与说明']}


def _wait(state):
    return {'steps': state['steps'] + ['等待补充材料' if state['missing_fields'] else '等待人工核对提交']}


def _build():
    graph = StateGraph(FeedbackState)
    graph.add_node('understand', _understand)
    graph.add_node('materials', _materials)
    graph.add_node('wait', _wait)
    graph.add_edge(START, 'understand')
    graph.add_edge('understand', 'materials')
    graph.add_edge('materials', 'wait')
    graph.add_edge('wait', END)
    return graph.compile()


feedback_graph = _build()


def review_feedback(ticket: dict, messages: list[str], photo_count: int) -> dict:
    result = feedback_graph.invoke({'ticket': ticket, 'messages': messages[-8:], 'photo_count': photo_count})
    return {key: result[key] for key in ('intent', 'description', 'ai', 'missing_fields', 'steps')}
