"""只新增 Supervisor 流程会话和交接事件表，不修改既有业务数据。"""

from app.database import Base, engine
from app.models.ticket import Ticket  # 注册工单表，解析流程表的外键依赖
from app.models.workflow_event import WorkflowEvent
from app.models.workflow_session import WorkflowSession


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine, tables=[
        WorkflowSession.__table__,
        WorkflowEvent.__table__,
    ])
    print("workflow_sessions, workflow_events ready")
