from datetime import datetime

from pydantic import BaseModel, Field
from app.schemas.attachment import AttachmentId


class RepairParseResult(BaseModel):
    """
    自然语言报修规则解析结果
    """

    # 报修位置，例如：A栋三楼卫生间
    location: str | None = None

    # 故障类型，例如：漏水、照明故障
    issue_type: str | None = None

    # 用户原始问题描述
    description: str

    # 规则建议的优先级，仅供人工核对
    suggested_priority: str | None = None

   # 尚未提取到的必要信息
    missing_fields: list[str] = Field(default_factory=list)

    # 每次返回实际运行模式，不能将模型失败后的规则结果展示为大模型输出。
    mode: str = "rule_fallback_demo"
    ai: dict = Field(default_factory=dict)
    follow_up_question: str = ""
    evidence: dict[str, str] = Field(default_factory=dict)
    # 实际图节点轨迹；人工续办使用已有流程事件，不代表原生检查点恢复。
    orchestration: dict = Field(default_factory=dict)

class RepairParseRequest(BaseModel):
    """
    用户提交的自然语言报修请求
    """

    # 用户输入的原始报修内容
    text: str    

class RepairRecordCreate(BaseModel):
    """
    维修人员提交维修结果时使用的请求模型。
    """

    # 使用工程人员稳定编号，确保维修结果可追溯到真实提交人
    personnel_no: str = Field(min_length=1, description="提交维修结果的工程人员编号")

    # 维修处理说明，不能为空
    repair_description: str = Field(
        min_length=1,
        description="维修处理说明",
    )

    # 维修现场照片路径列表，可以暂时不传
    photo_urls: list[str] = Field(
        default_factory=list,
        description="维修照片路径列表",
    )
    # 新上传通过附件ID绑定；旧照片路径继续兼容，不冒充已验证上传文件。
    attachment_ids: list[AttachmentId] = Field(default_factory=list, max_length=5)


class RepairRecordResponse(BaseModel):
    """
    维修记录接口返回的数据模型。
    """

    id: int
    ticket_id: int
    repair_description: str
    photo_urls: list[str]
    submitted_at: datetime    
