"""仅新增附件元数据表；不会搬移、删除图片或修改既有工单。需单独安排执行。"""

from app.database import Base, engine
from app.models.ticket import Ticket  # 注册外键依赖，不创建这些表
from app.models.workflow_session import WorkflowSession
from app.models.repair import RepairRecord
from app.models.attachment import Attachment


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine, tables=[Attachment.__table__])
    print("attachments ready; existing data unchanged")
