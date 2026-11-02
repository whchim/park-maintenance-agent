import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.repair_personnel import RepairPersonnel
from app.models.ticket import Ticket
from app.models.repair import RepairRecord
from app.models.ticket_assignment import TicketAssignment
from app.schemas.repair import RepairParseResult, RepairRecordCreate
from app.schemas.ticket import TicketStatus
from app.services.priority_service import classify_priority
from app.services.ticket_service import transition_ticket_with_event

def parse_repair_text(text: str) -> RepairParseResult:
    """
    将用户输入的自然语言报修内容转换为结构化结果。
    当前使用关键词规则提取，后续再接入大模型。
    """

    # 去除用户输入前后的空格
    text = text.strip()

    # 1. 初始化解析结果
    result = RepairParseResult(
        description=text,
    )

    # 2. 检查用户是否输入了报修内容
    if not text:
    # 空输入时，描述、位置和故障类型都属于缺失信息
        result.missing_fields.extend([
            "description",
            "location",
            "issue_type",
        ])
        return result

    # 3. 根据完整报修描述，生成优先级建议
    # .value 将 Priority 枚举转换为普通字符串
    result.suggested_priority = classify_priority(text).value

    # 4. 定义当前支持识别的故障类型
    # 当前支持识别的故障类型关键词
    issue_keywords = [
        # 特急故障
        "电梯困人",
        "人员被困",
        "严重漏电",
        "燃气泄漏",
        "触电",
        "火灾",
        "起火",

        # 加急故障
        "漏水",
        "停电",
        "空调故障",
        "设备故障",
        "门禁故障",

        # 普通故障
        "照明故障",
        "灯不亮",
        "电梯故障",
    ]
    
    # 5. 从文本中查找故障类型
    for keyword in issue_keywords:
        if keyword in text:
            result.issue_type = keyword

            # 暂时将故障关键词前面的内容作为报修位置
            location = text.split(keyword, 1)[0].strip("，,。；; ")

            # 删除位置前面常见的口语表达
            prefix_keywords = [
                "我刚刚发现",
                "刚刚发现",
                "我发现",
                "发现",
                "帮忙看一下",
                "帮我看一下",
                "麻烦看一下",
                "请检查一下",
                "位置在",
            ]

            for prefix in prefix_keywords:
                if location.startswith(prefix):
                    location = location[len(prefix):].strip()
                    break

            # 只有清理后仍然有内容，才记录为位置
            if location:
                result.location = location

            # 删除位置末尾常见的故障描述词
            # 例如："A栋三楼卫生间，出现" → "A栋三楼卫生间"
            suffix_keywords = [
                "出现",
                "发生",
                "发现",
            ]

            for suffix in suffix_keywords:
                if location.endswith(suffix):
                    location = location[:-len(suffix)].strip("，,。；; ")
                    break

            # 只有清理后仍然有内容，才记录为位置
            if location:
                result.location = location

            break

    # 6. 检查是否成功提取位置和故障类型
    if result.location is None:
        result.missing_fields.append("location")

    if result.issue_type is None:
        result.missing_fields.append("issue_type")

    return result

def create_repair_record(
    db: Session,
    ticket: Ticket,
    request: RepairRecordCreate,
) -> RepairRecord:
    """
    创建维修记录，并将工单提交到待验收状态。
    """

    # 1. 检查工单是否处于处理中状态
    if ticket.status != TicketStatus.PROCESSING.value:
        raise ValueError("只有处理中状态的工单才能提交维修结果")

    personnel = (
        db.query(RepairPersonnel)
        .filter(
            RepairPersonnel.personnel_no == request.personnel_no,
            RepairPersonnel.is_active.is_(True),
        )
        .first()
    )
    if personnel is None:
        raise ValueError("工程人员不存在或已停用")

    # 维修结果只能由当前分派的工程人员提交
    current_assignment = (
        db.query(TicketAssignment)
        .filter(
            TicketAssignment.ticket_id == ticket.id,
            TicketAssignment.personnel_id == personnel.id,
            TicketAssignment.is_current.is_(True),
        )
        .first()
    )
    if current_assignment is None:
        raise ValueError("该工程人员不是当前分派人员，不能提交维修结果")

    # 2. 检查维修说明是否为空
    repair_description = request.repair_description.strip()

    if not repair_description:
        raise ValueError("维修处理说明不能为空")

    # 3. 将照片路径列表转换为 JSON 字符串
    photo_urls_json = json.dumps(
        request.photo_urls,
        ensure_ascii=False,
    )

    # 4. 创建维修记录对象
    now = datetime.now()

    repair_record = RepairRecord(
        ticket_id=ticket.id,
        repair_description=repair_description,
        photo_urls=photo_urls_json,
        submitted_at=now,
    )

    # 5. 将维修记录添加到数据库会话
    db.add(repair_record)

    # 6. 通过统一状态机进入待验收状态
    if not transition_ticket_with_event(
        db,
        ticket,
        TicketStatus.PENDING_REVIEW,
        actor_type="person",
        actor_id=personnel.personnel_no,
        reason="维修人员提交维修结果",
        now=now,
    ):
        raise ValueError("只有处理中状态的工单才能提交维修结果")

    # 由调用方统一提交事务，但先 flush 以返回可追踪的维修记录编号。
    db.flush()
    if request.attachment_ids:
        from app.services.attachment_service import bind_images, image_response
        images = bind_images(db, request.attachment_ids, purpose="repair", ticket_id=ticket.id,
                             repair_record_id=repair_record.id)
        repair_record.photo_urls = json.dumps(request.photo_urls + [image_response(image)["url"] for image in images], ensure_ascii=False)
    return repair_record
