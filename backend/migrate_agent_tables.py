"""为多 Agent 执行记录和工程改造任务创建新增表，不修改既有数据。"""

from app.database import Base, engine
from app.models.agent_run import AgentRun, AgentToolCall
from app.models.renovation_task import RenovationTask


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine, tables=[
        AgentRun.__table__,
        AgentToolCall.__table__,
        RenovationTask.__table__,
    ])
    print("agent_runs, agent_tool_calls, renovation_tasks ready")
