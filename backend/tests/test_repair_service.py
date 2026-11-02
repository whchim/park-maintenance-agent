from app.services.repair_service import parse_repair_text


def test_parse_repair_text():
    # 模拟用户提交正常的报修内容
    text = "A栋三楼卫生间漏水，地面积水严重"

    result = parse_repair_text(text)

    # 验证原始描述被正确保留
    assert result.description == text

    # 验证能正确提取报修位置
    assert result.location == "A栋三楼卫生间"

    # 验证能正确提取故障类型
    assert result.issue_type == "漏水"

    # 验证必要字段均已提取，不存在缺失项
    assert result.missing_fields == []

def test_parse_empty_repair_text():
    # 模拟用户没有填写报修内容
    result = parse_repair_text("")

    # 空输入应该被识别为缺少描述
    assert result.description == ""
    assert "description" in result.missing_fields

def test_parse_repair_text_without_location():
    # 用户只描述故障，没有提供任何位置信息
    result = parse_repair_text("漏水了")

    assert result.issue_type == "漏水"
    assert result.location is None
    assert "location" in result.missing_fields


def test_parse_repair_text_without_issue_type():
    # 用户提供了位置，但没有说明具体故障
    result = parse_repair_text("A栋三楼卫生间有问题")

    assert result.location is None
    assert result.issue_type is None
    assert "location" in result.missing_fields
    assert "issue_type" in result.missing_fields    

def test_parse_repair_text_critical_priority():
    # 电梯困人属于紧急情况
    result = parse_repair_text("A栋电梯困人，请立即处理")

    assert result.suggested_priority == "critical"


def test_parse_repair_text_urgent_priority():
    # 漏水属于加急情况
    result = parse_repair_text("A栋三楼卫生间漏水")

    assert result.suggested_priority == "urgent"


def test_parse_repair_text_normal_priority():
    # 没有匹配紧急或加急关键词，默认为普通
    result = parse_repair_text("A栋三楼卫生间墙面需要检查")

    assert result.suggested_priority == "normal"    

def test_parse_elevator_trapped():
    # 电梯困人应识别为紧急故障
    result = parse_repair_text("A栋电梯困人，请立即处理")

    assert result.issue_type == "电梯困人"
    assert result.suggested_priority == "critical"


def test_parse_fire():
    # 火灾应识别为特急故障
    result = parse_repair_text("A栋二楼发生火灾")

    assert result.issue_type == "火灾"
    assert result.suggested_priority == "critical"


def test_parse_gas_leak():
    # 燃气泄漏应识别为紧急故障
    result = parse_repair_text("食堂厨房燃气泄漏")

    assert result.issue_type == "燃气泄漏"
    assert result.suggested_priority == "critical"    

def test_parse_repair_text_with_natural_language_prefix():
    # 用户在位置前面加入了口语表达
    result = parse_repair_text(
        "我刚刚发现A栋三楼卫生间漏水了，请尽快处理"
    )

    assert result.location == "A栋三楼卫生间"
    assert result.issue_type == "漏水"
    assert result.suggested_priority == "urgent"

def test_parse_repair_text_with_appeared_prefix():
    # 用户在故障关键词前增加了“出现”这样的描述词
    result = parse_repair_text("A栋三楼卫生间，出现漏水")

    assert result.location == "A栋三楼卫生间"
    assert result.issue_type == "漏水"
    assert result.suggested_priority == "urgent"   

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.ticket import Ticket
from app.models.repair import RepairRecord
from app.models.repair_personnel import RepairPersonnel
from app.models.ticket_assignment import TicketAssignment
from app.schemas.repair import RepairRecordCreate
from app.schemas.ticket import TicketStatus
from app.services.repair_service import create_repair_record


@pytest.fixture
def db():
    """
    创建独立的内存数据库，供维修记录测试使用。

    StaticPool 确保测试过程中使用同一个内存数据库连接，
    避免不同连接访问到不同的空数据库。
    """
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    TestingSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=test_engine,
    )

    # 根据模型创建测试所需的所有表
    Base.metadata.create_all(bind=test_engine)

    session = TestingSessionLocal()

    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=test_engine)
        test_engine.dispose()


def create_processing_ticket(db):
    """
    创建一条处于 processing 状态的测试工单。
    """

    ticket = Ticket(
        ticket_no="WX-TEST-001",
        location="A栋三楼卫生间",
        issue_type="漏水",
        description="地面积水严重",
        priority="urgent",
        status=TicketStatus.PROCESSING.value,
    )

    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    personnel = RepairPersonnel(
        personnel_no="ENG-001",
        display_name="水电维修演示员",
        department="工程部",
        role="维修人员",
        skills='["漏水"]',
        responsible_area="A栋",
    )
    db.add(personnel)
    db.commit()
    db.refresh(personnel)

    db.add(
        TicketAssignment(
            ticket_id=ticket.id,
            personnel_id=personnel.id,
            assigned_by="ENG-LEAD-001",
            is_current=True,
        )
    )
    db.commit()

    return ticket


def test_create_repair_record_success(db):
    # 1. 创建一条处理中工单
    ticket = create_processing_ticket(db)

    # 2. 准备维修说明和照片路径
    request = RepairRecordCreate(
        personnel_no="ENG-001",
        repair_description="已更换损坏水管，并清理地面积水",
        photo_urls=[
            "uploads/repair/before.jpg",
            "uploads/repair/after.jpg",
        ],
    )

    # 3. 创建维修记录
    record = create_repair_record(db, ticket, request)

    # 4. 验证维修说明
    assert record.repair_description == "已更换损坏水管，并清理地面积水"

    # 5. 验证照片路径被保存为 JSON 字符串
    import json
    assert json.loads(record.photo_urls) == [
        "uploads/repair/before.jpg",
        "uploads/repair/after.jpg",
    ]

    # 6. 验证工单进入待验收状态
    db.refresh(ticket)
    assert ticket.status == TicketStatus.PENDING_REVIEW.value
    assert ticket.submitted_at is not None


def test_create_repair_record_rejects_empty_description(db):
    ticket = create_processing_ticket(db)

    request = RepairRecordCreate(
        personnel_no="ENG-001",
        repair_description="   ",
        photo_urls=[],
    )

    with pytest.raises(ValueError, match="维修处理说明不能为空"):
        create_repair_record(db, ticket, request)


def test_create_repair_record_rejects_wrong_ticket_status(db):
    ticket = create_processing_ticket(db)

    # 将工单改回待接单状态
    ticket.status = TicketStatus.PENDING.value
    db.commit()

    request = RepairRecordCreate(
        personnel_no="ENG-001",
        repair_description="维修完成",
        photo_urls=[],
    )

    with pytest.raises(ValueError, match="只有处理中状态的工单才能提交维修结果"):
        create_repair_record(db, ticket, request)


def test_repair_record_is_saved_in_database(db):
    ticket = create_processing_ticket(db)

    request = RepairRecordCreate(
        personnel_no="ENG-001",
        repair_description="已完成维修",
        photo_urls=["uploads/repair/result.jpg"],
    )

    record = create_repair_record(db, ticket, request)

    # 从数据库重新查询，确认记录确实持久化
    saved_record = (
        db.query(RepairRecord)
        .filter(RepairRecord.id == record.id)
        .first()
    )

    assert saved_record is not None
    assert saved_record.ticket_id == ticket.id
    assert saved_record.repair_description == "已完成维修"         
