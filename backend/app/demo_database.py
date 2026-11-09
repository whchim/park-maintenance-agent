"""只初始化新演示库；已有不兼容结构直接报错，不自动迁移或清空。"""

import json

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.database import Base
from app.models.repair_personnel import RepairPersonnel


def initialize_demo_database(engine):
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if tables:
        missing = []
        for table in Base.metadata.sorted_tables:
            columns = {item["name"] for item in inspector.get_columns(table.name)} if table.name in tables else set()
            missing.extend(f"{table.name}.{column.name}" for column in table.columns if column.name not in columns)
        if missing:
            raise RuntimeError("演示数据库结构不兼容，请先备份并安排迁移；未修改现有数据：" + ", ".join(missing))
        return
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for number, name, skills, area in [
            ("ENG-DEMO-001", "空调维修模拟员", ["空调", "设备"], "A栋"),
            ("ENG-DEMO-002", "水电维修模拟员", ["漏水", "照明", "水电"], "A栋"),
            ("ENG-DEMO-003", "机电维修模拟员", ["设备", "门禁"], "B栋"),
        ]:
            db.add(RepairPersonnel(personnel_no=number, display_name=name, department="工程部",
                                   role="维修人员", skills=json.dumps(skills, ensure_ascii=False), responsible_area=area))
        db.commit()
