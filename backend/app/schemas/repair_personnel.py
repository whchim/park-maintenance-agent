from datetime import datetime

from pydantic import BaseModel


class RepairPersonnelResponse(BaseModel):
    """工程人员目录接口返回的数据。"""

    model_config = {"from_attributes": True}

    id: int
    personnel_no: str
    display_name: str
    department: str
    role: str
    skills: list[str]
    responsible_area: str | None
    is_active: bool
    created_at: datetime
