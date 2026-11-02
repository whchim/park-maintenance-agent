from app.database import engine
from app.models.repair_personnel import RepairPersonnel
from app.models.ticket import Ticket
from app.models.ticket_assignment import TicketAssignment


def migrate():
    """创建工单分派记录表，不修改已有工单数据。"""
    TicketAssignment.__table__.create(bind=engine, checkfirst=True)


if __name__ == "__main__":
    migrate()
