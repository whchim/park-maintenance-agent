import json

from sqlalchemy.orm import Session

from app.models.repair_personnel import RepairPersonnel


def to_personnel_response(personnel: RepairPersonnel) -> dict:
    """将数据库中的技能 JSON 转换为 API 使用的列表。"""
    return {
        "id": personnel.id,
        "personnel_no": personnel.personnel_no,
        "display_name": personnel.display_name,
        "department": personnel.department,
        "role": personnel.role,
        "skills": json.loads(personnel.skills or "[]"),
        "responsible_area": personnel.responsible_area,
        "is_active": personnel.is_active,
        "created_at": personnel.created_at,
    }


def list_active_personnel(db: Session) -> list[dict]:
    """返回当前可分配任务的工程人员。"""
    personnel = (
        db.query(RepairPersonnel)
        .filter(RepairPersonnel.is_active.is_(True))
        .order_by(RepairPersonnel.personnel_no.asc())
        .all()
    )
    return [to_personnel_response(item) for item in personnel]
