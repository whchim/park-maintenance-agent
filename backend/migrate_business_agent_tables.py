"""为招商 Agent 和 SLA 电话待办新增表，不修改既有数据。"""

from app.database import Base, engine
from app.models.customer import Customer
from app.models.customer_handoff import CustomerHandoff
from app.models.follow_up_task import FollowUpTask
from app.models.follow_up_record import FollowUpRecord
from app.models.notification import Notification
from app.models.ticket import Ticket
from app.models.workflow_event import WorkflowEvent
from app.models.workflow_session import WorkflowSession
from sqlalchemy import inspect, text


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine, tables=[
        WorkflowSession.__table__,
        WorkflowEvent.__table__,
        Customer.__table__,
        CustomerHandoff.__table__,
        FollowUpTask.__table__,
        FollowUpRecord.__table__,
        Notification.__table__,
    ])
    notification_columns = {
        column["name"] for column in inspect(engine).get_columns("notifications")
    }
    with engine.begin() as connection:
        for name, definition in {
            "contact_result": "TEXT",
            "contacted_by": "VARCHAR(100)",
        }.items():
            if name not in notification_columns:
                connection.execute(text(
                    f"ALTER TABLE notifications ADD COLUMN {name} {definition}"
                ))
    customer_columns = {
        column["name"] for column in inspect(engine).get_columns("customers")
    }
    if "intent_level" not in customer_columns:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE customers ADD COLUMN intent_level VARCHAR(1)"
            ))
    print("customers, follow_up_tasks, notifications ready")
