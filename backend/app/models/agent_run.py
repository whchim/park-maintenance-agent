from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class AgentRun(Base):
    """一次 Supervisor 或专业 Agent 执行记录。"""

    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(40), nullable=False, index=True)
    agent_name = Column(String(80), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="running")
    input_text = Column(Text, nullable=False)
    output_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.now, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class AgentToolCall(Base):
    """Agent 调用业务工具的可追踪记录。"""

    __tablename__ = "agent_tool_calls"

    id = Column(Integer, primary_key=True, index=True)
    agent_run_id = Column(
        Integer,
        ForeignKey("agent_runs.id"),
        nullable=False,
        index=True,
    )
    tool_name = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False, default="success")
    input_json = Column(Text, nullable=False)
    output_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.now, nullable=False)
    completed_at = Column(DateTime, nullable=True)
