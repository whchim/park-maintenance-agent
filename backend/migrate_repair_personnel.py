import json

from app.database import SessionLocal, engine
from app.models.repair_personnel import RepairPersonnel


DEMO_PERSONNEL = [
    {
        "personnel_no": "ENG-LEAD-001",
        "display_name": "工程部演示负责人",
        "department": "工程部",
        "role": "工程负责人",
        "skills": ["任务分配", "异常升级", "工程改造"],
        "responsible_area": "全园区",
    },
    {
        "personnel_no": "ENG-001",
        "display_name": "水电维修演示员",
        "department": "工程部",
        "role": "维修人员",
        "skills": ["漏水", "停电", "照明", "线路"],
        "responsible_area": "A栋、B栋",
    },
    {
        "personnel_no": "ENG-002",
        "display_name": "空调设备演示员",
        "department": "工程部",
        "role": "维修人员",
        "skills": ["空调", "通风", "设备故障"],
        "responsible_area": "A栋、公共区域",
    },
    {
        "personnel_no": "ENG-003",
        "display_name": "综合维修演示员",
        "department": "工程部",
        "role": "维修人员",
        "skills": ["门禁", "门窗", "基础设施", "临时任务"],
        "responsible_area": "B栋、公共区域",
    },
]


def migrate():
    """创建工程人员表并幂等写入演示账号。"""
    RepairPersonnel.__table__.create(bind=engine, checkfirst=True)

    db = SessionLocal()
    try:
        for values in DEMO_PERSONNEL:
            personnel = (
                db.query(RepairPersonnel)
                .filter(RepairPersonnel.personnel_no == values["personnel_no"])
                .first()
            )
            if personnel is None:
                personnel_values = dict(values)
                personnel_values["skills"] = json.dumps(
                    values["skills"],
                    ensure_ascii=False,
                )
                db.add(
                    RepairPersonnel(**personnel_values)
                )

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
