from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.database import Base


class RepairPersonnel(Base):
    """工程部维修人员目录，当前使用明确标注的演示账号。"""

    __tablename__ = "repair_personnel"

    # 人员主键
    id = Column(Integer, primary_key=True, index=True)

    # 对外稳定使用的人员编号
    personnel_no = Column(String(30), unique=True, nullable=False, index=True)

    # 演示显示名称，不代表真实园区员工
    display_name = Column(String(100), nullable=False)

    # 所属部门和岗位
    department = Column(String(50), nullable=False, default="工程部")
    role = Column(String(50), nullable=False)

    # 技能和负责区域暂以文本保存，后续可拆成关联表
    skills = Column(Text, nullable=False)
    responsible_area = Column(String(200), nullable=True)

    # 是否仍可参与任务分配
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
