from datetime import datetime, timedelta
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models.repair_personnel import RepairPersonnel
from app.models.acceptance import AcceptanceRecord

# 创建独立的内存 SQLite 数据库
test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

# 创建测试数据库的 Session 工厂
TestingSessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=test_engine,
)


# 用测试数据库替换正式数据库依赖
def override_get_db():
    db = TestingSessionLocal()

    try:
        yield db
    finally:
        db.close()


# FastAPI 收到数据库请求时，改用上面的测试数据库
app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)


def seed_demo_repair_personnel():
    """为需要真实工程人员身份的 API 测试准备演示账号。"""

    db = TestingSessionLocal()
    db.add(
        RepairPersonnel(
            personnel_no="ENG-001",
            display_name="水电维修演示员",
            department="工程部",
            role="维修人员",
            skills='["漏水"]',
            responsible_area="A栋",
        )
    )
    db.commit()
    db.close()


@pytest.fixture(autouse=True)
def reset_test_database():
    """
    每个 API 测试开始前创建空表，
    测试结束后删除表，避免测试数据互相影响。
    """

    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    yield

    Base.metadata.drop_all(bind=test_engine)

def test_health_check():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_repair_workflow_waits_for_missing_information_and_confirmation():
    """AI 初审只列出缺项；确认前不应创建业务工单。"""
    response = client.post(
        "/api/v1/agent/runs",
        json={
            "message": "漏水了",
            "channel": "feishu",
            "requester_id": "ou_demo_001",
            "idempotency_key": "feishu-msg-001",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["channel"] == "feishu"
    assert data["status"] == "waiting_input"
    assert "location" in data["missing_fields"]
    assert client.get("/api/v1/tickets").json() == []

    continue_response = client.post(
        f"/api/v1/agent/runs/{data['workflow_id']}/messages",
        json={"message": "位置在A栋三楼卫生间", "idempotency_key": "feishu-msg-002"},
    )
    assert continue_response.status_code == 200
    assert continue_response.json()["status"] == "waiting_human"
    assert client.get("/api/v1/tickets").json() == []

    workflow_response = client.get(f"/api/v1/agent/runs/{data['workflow_id']}")
    assert workflow_response.status_code == 200
    assert any(event["event_type"] == "repair.intake.reviewed" for event in workflow_response.json()["events"])


def test_feishu_adapter_creates_idempotent_workflow_entry():
    first = client.post(
        "/api/v1/integrations/feishu/repair-events",
        json={"event_id": "feishu-event-001", "open_id": "ou-001", "text": "A栋一楼漏水"},
    )
    second = client.post(
        "/api/v1/integrations/feishu/repair-events",
        json={"event_id": "feishu-event-001", "open_id": "ou-001", "text": "A栋一楼漏水"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["workflow_id"] == second.json()["workflow_id"]
    assert first.json()["channel"] == "feishu"


def test_feishu_event_callback_verifies_token_and_extracts_text(monkeypatch):
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "demo-token")
    payload = {
        "header": {"event_id": "evt-real-001", "event_type": "im.message.receive_v1", "token": "demo-token"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou-real-001"}},
            "message": {
                "message_id": "om-real-001",
                "message_type": "text",
                "content": '{"text":"A栋三楼卫生间漏水"}',
            },
        },
    }

    response = client.post("/api/v1/integrations/feishu/events", json=payload)

    assert response.status_code == 200
    assert response.json()["channel"] == "feishu"
    assert response.json()["requester_id"] == "ou-real-001"
    assert response.json()["original_message"] == "A栋三楼卫生间漏水"


def test_feishu_url_verification_returns_challenge(monkeypatch):
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "demo-token")
    response = client.post(
        "/api/v1/integrations/feishu/events",
        json={"type": "url_verification", "token": "demo-token", "challenge": "challenge-001"},
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-001"}


def test_feishu_event_callback_verifies_encrypt_key_signature(monkeypatch):
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "demo-token")
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "demo-encrypt-key")
    payload = {
        "header": {"event_id": "evt-signature-001", "event_type": "im.message.receive_v1", "token": "demo-token"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou-signature-001"}},
            "message": {
                "message_id": "om-signature-001",
                "message_type": "text",
                "content": '{"text":"A栋一楼照明故障"}',
            },
        },
    }
    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = "1730000000"
    nonce = "nonce-001"
    signature = hashlib.sha256(
        f"{timestamp}{nonce}demo-encrypt-key".encode("utf-8") + raw_body
    ).hexdigest()

    response = client.post(
        "/api/v1/integrations/feishu/events",
        content=raw_body,
        headers={
            "content-type": "application/json",
            "x-lark-request-timestamp": timestamp,
            "x-lark-request-nonce": nonce,
            "x-lark-signature": signature,
        },
    )

    assert response.status_code == 200
    assert response.json()["requester_id"] == "ou-signature-001"


def test_repair_workflow_confirmation_is_idempotent_and_uses_confirmed_priority():
    """重复确认复用同一工单，优先级由确认请求决定。"""
    start = client.post(
        "/api/v1/agent/runs",
        json={
            "message": "A栋三楼卫生间漏水",
            "channel": "feishu",
            "idempotency_key": "feishu-msg-101",
        },
    )
    assert start.status_code == 200
    workflow_id = start.json()["workflow_id"]
    assert start.json()["status"] == "waiting_human"

    confirmation = {
        "location": "A栋三楼卫生间",
        "issue_type": "漏水",
        "description": "A栋三楼卫生间漏水",
        "priority": "normal",
        "idempotency_key": "confirm-101",
    }
    first = client.post(f"/api/v1/agent/runs/{workflow_id}/confirm-ticket", json=confirmation)
    second = client.post(f"/api/v1/agent/runs/{workflow_id}/confirm-ticket", json=confirmation)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["ticket_id"] == second.json()["ticket_id"]
    assert first.json()["ticket"]["priority"] == "normal"
    assert len(client.get("/api/v1/tickets").json()) == 1


def test_workflow_idempotency_key_cannot_cross_workflows():
    first = client.post(
        "/api/v1/agent/runs",
        json={"message": "A栋三楼卫生间漏水", "idempotency_key": "intake-a"},
    ).json()
    second = client.post(
        "/api/v1/agent/runs",
        json={"message": "B栋二楼照明故障", "idempotency_key": "intake-b"},
    ).json()
    confirm = {
        "location": "A栋三楼卫生间",
        "issue_type": "漏水",
        "description": "A栋三楼卫生间漏水",
        "priority": "normal",
        "idempotency_key": "confirm-a",
    }

    assert client.post(
        f"/api/v1/agent/runs/{first['workflow_id']}/confirm-ticket",
        json=confirm,
    ).status_code == 200
    confirm["location"] = "B栋二楼"
    confirm["issue_type"] = "照明故障"
    confirm["description"] = "B栋二楼照明故障"
    response = client.post(
        f"/api/v1/agent/runs/{second['workflow_id']}/confirm-ticket",
        json=confirm,
    )

    assert response.status_code == 409
    assert len(client.get("/api/v1/tickets").json()) == 1


def test_repair_handoff_and_rejected_acceptance_are_recorded_in_workflow():
    seed_demo_repair_personnel()
    start = client.post(
        "/api/v1/agent/runs",
        json={"message": "A栋三楼卫生间漏水", "idempotency_key": "cycle-intake"},
    ).json()
    workflow_id = start["workflow_id"]
    created = client.post(
        f"/api/v1/agent/runs/{workflow_id}/confirm-ticket",
        json={
            "location": "A栋三楼卫生间",
            "issue_type": "漏水",
            "description": "水管漏水，请检查接口",
            "priority": "urgent",
            "idempotency_key": "cycle-confirm",
        },
    ).json()
    ticket_id = created["ticket_id"]

    assert client.post(
        f"/api/v1/tickets/{ticket_id}/assignments",
        json={"personnel_no": "ENG-001", "assigned_by": "ENG-LEAD-001"},
    ).status_code == 201
    assert client.post(f"/api/v1/tickets/{ticket_id}/accept", json={"personnel_no": "ENG-001"}).status_code == 200
    assert client.post(f"/api/v1/tickets/{ticket_id}/start-processing", json={"personnel_no": "ENG-001"}).status_code == 200
    assert client.post(
        f"/api/v1/tickets/{ticket_id}/repair-records",
        json={"personnel_no": "ENG-001", "repair_description": "已更换接口垫片", "photo_urls": ["/demo/photo.jpg"]},
    ).status_code == 201

    rejected = client.post(
        f"/api/v1/tickets/{ticket_id}/acceptance",
        json={"acceptance_comment": "现场还有渗漏，请返工", "accepted_by": "企服演示员", "accepted": False},
    )
    assert rejected.status_code == 201
    assert rejected.json()["accepted"] is False
    assert client.get(f"/api/v1/tickets/{ticket_id}").json()["status"] == "processing"

    workflow = client.get(f"/api/v1/agent/runs/{workflow_id}").json()
    event_types = [event["event_type"] for event in workflow["events"]]
    assert workflow["status"] == "waiting_engineering"
    assert "repair.assignment.confirmed" in event_types
    assert "repair.completed.submitted" in event_types
    assert "repair.acceptance.decided" in event_types


def test_investment_agent_drafts_and_human_confirmation_is_idempotent():
    draft = client.post(
        "/api/v1/agents/investment/intake",
        json={"message": "客户海星科技明确意向，计划近期签约，联系人李先生电话13812345678", "source": "官网咨询"},
    )
    assert draft.status_code == 200
    assert draft.json()["mode"] == "rule_fallback_demo"
    assert draft.json()["customer_name"] == "海星科技"
    assert draft.json()["intent_level"] == "A"
    assert draft.json()["follow_up_interval_days"] == 1
    assert client.get("/api/v1/agents/investment/customers").json() == []

    payload = {
        "customer_name": "海星科技",
        "contact_name": "李先生",
        "contact_phone": "13812345678",
        "source": "官网咨询",
        "owner": "招商演示员",
        "intent_level": "A",
        "follow_up_subject": "确认办公室面积与预算",
        "follow_up_description": "客户计划了解园区办公空间",
        "idempotency_key": "investment-confirm-001",
    }
    first = client.post("/api/v1/agents/investment/confirm", json=payload)
    second = client.post("/api/v1/agents/investment/confirm", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["customer"]["id"] == second.json()["customer"]["id"]
    assert first.json()["follow_up_task"]["id"] == second.json()["follow_up_task"]["id"]
    assert first.json()["handoff_event_id"] is None
    assert first.json()["customer"]["intent_level"] == "A"
    assert first.json()["follow_up_task"]["due_at"]
    assert len(client.get("/api/v1/agents/investment/customers").json()) == 1
    assert len(client.get("/api/v1/agents/investment/follow-ups").json()) == 1

    task_id = first.json()["follow_up_task"]["id"]
    follow_up = {
        "actor": "招商演示员",
        "outcome": "已联系，客户需要再比较面积方案",
        "idempotency_key": "follow-up-record-001",
    }
    completed = client.post(
        f"/api/v1/agents/investment/follow-ups/{task_id}/complete",
        json=follow_up,
    )
    retried = client.post(
        f"/api/v1/agents/investment/follow-ups/{task_id}/complete",
        json=follow_up,
    )
    assert completed.status_code == 200
    assert retried.status_code == 200
    assert completed.json()["record"]["id"] == retried.json()["record"]["id"]
    assert completed.json()["next_task"]["due_at"]
    assert len(client.get("/api/v1/agents/investment/follow-ups").json()) == 1


@pytest.fixture
def follow_up_task():
    created = client.post('/api/v1/agents/investment/confirm', json={
        'customer_name': '回访测试企业', 'intent_level': 'B', 'owner': '招商演示员',
        'follow_up_subject': '确认需求', 'follow_up_description': '客户仍在比较方案',
        'idempotency_key': 'follow-up-fixture',
    })
    assert created.status_code == 200
    return created.json()['follow_up_task']


@pytest.mark.parametrize(('level', 'days'), [('A', 1), ('B', 3), ('C', 7)])
def test_follow_up_uses_confirmed_level_and_preserves_history(level, days):
    created = client.post('/api/v1/agents/investment/confirm', json={
        'customer_name': '回访周期测试企业', 'intent_level': level, 'owner': '招商演示员',
        'follow_up_subject': '确认需求', 'follow_up_description': '客户仍在比较方案',
        'idempotency_key': 'cycle-fixture',
    }).json()
    task_id = created['follow_up_task']['id']
    before = datetime.now()
    response = client.post(f'/api/v1/agents/investment/follow-ups/{task_id}/complete', json={
        'actor': '招商演示员', 'outcome': '已回访，客户下次继续讨论方案',
        'idempotency_key': 'record-cycle',
    })
    assert response.status_code == 200
    due = datetime.fromisoformat(response.json()['next_task']['due_at'])
    assert before + timedelta(days=days) <= due <= datetime.now() + timedelta(days=days)
    pending = client.get('/api/v1/agents/investment/follow-ups').json()
    assert len(pending) == 1
    assert pending[0]['id'] != task_id
    assert pending[0]['customer_name'] == '回访周期测试企业'
    history = client.get(
        f"/api/v1/agents/investment/customers/{created['customer']['id']}/follow-up-records"
    ).json()
    assert len(history) == 1
    assert history[0]['outcome'] == '已回访，客户下次继续讨论方案'


def test_follow_up_rejects_blank_and_cross_task_replay(follow_up_task):
    task_id = follow_up_task['id']
    endpoint = f'/api/v1/agents/investment/follow-ups/{task_id}/complete'
    payload = {'actor': '招商演示员', 'outcome': '   ', 'idempotency_key': 'record-once'}
    assert client.post(endpoint, json=payload).status_code == 409
    payload['outcome'] = '已确认客户仍有意向'
    completed = client.post(endpoint, json=payload)
    assert completed.status_code == 200
    next_id = completed.json()['next_task']['id']
    assert client.post(
        f'/api/v1/agents/investment/follow-ups/{next_id}/complete', json=payload,
    ).status_code == 409
    payload['idempotency_key'] = 'another-key'
    assert client.post(endpoint, json=payload).status_code == 409
    assert len(client.get('/api/v1/agents/investment/follow-ups').json()) == 1


def test_follow_up_rolls_back_record_and_next_task_on_failure(follow_up_task, monkeypatch):
    import app.main as main_module

    original = main_module.complete_follow_up

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError('模拟事务内失败')

    monkeypatch.setattr(main_module, 'complete_follow_up', fail_after_write)
    response = client.post(
        f"/api/v1/agents/investment/follow-ups/{follow_up_task['id']}/complete",
        json={'actor': '招商演示员', 'outcome': '实际回访内容', 'idempotency_key': 'rollback-key'},
    )
    assert response.status_code == 409
    pending = client.get('/api/v1/agents/investment/follow-ups').json()
    assert len(pending) == 1
    assert pending[0]['id'] == follow_up_task['id']
    assert client.get(
        f"/api/v1/agents/investment/customers/{follow_up_task['customer_id']}/follow-up-records"
    ).json() == []


def test_three_department_agent_capability_catalog_is_explicit():
    response = client.get("/api/v1/agents/capabilities")
    assert response.status_code == 200
    agents = {item["agent_name"]: item for item in response.json()}
    assert {"supervisor_agent", "service_agent", "engineering_agent", "investment_agent"} <= agents.keys()
    assert agents["investment_agent"]["status"] == "implemented_rule_fallback_mvp"


def test_critical_no_response_creates_one_phone_escalation_todo():
    created = client.post(
        "/api/v1/tickets",
        json={"location": "A栋配电室", "issue_type": "火灾", "description": "配电室发生火灾", "priority": "critical"},
    )
    ticket_id = created.json()["id"]
    db = TestingSessionLocal()
    ticket = db.query(__import__("app.models.ticket", fromlist=["Ticket"]).Ticket).filter_by(id=ticket_id).one()
    ticket.created_at = datetime.now() - timedelta(minutes=6)
    db.commit()
    db.close()

    first = client.post(f"/api/v1/tickets/{ticket_id}/escalation/scan")
    second = client.post(f"/api/v1/tickets/{ticket_id}/escalation/scan")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["notification_no"] == second.json()["notification_no"]
    pending = client.get("/api/v1/notifications/pending").json()
    assert len(pending) == 1

    resolved = client.post(
        f"/api/v1/notifications/{first.json()['id']}/resolve",
        json={"contacted_by": "工程负责人演示员", "contact_result": "电话已接通，安排立即处理"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "completed"
    assert client.get("/api/v1/notifications/pending").json() == []


def test_batch_escalation_scan_is_safe_to_retry():
    created = client.post(
        "/api/v1/tickets",
        json={"location": "B栋机房", "issue_type": "燃气泄漏", "description": "机房燃气泄漏", "priority": "critical"},
    )
    ticket_id = created.json()["id"]
    db = TestingSessionLocal()
    ticket = db.query(__import__("app.models.ticket", fromlist=["Ticket"]).Ticket).filter_by(id=ticket_id).one()
    ticket.created_at = datetime.now() - timedelta(minutes=7)
    db.commit()
    db.close()

    first = client.post("/api/v1/escalations/scan")
    second = client.post("/api/v1/escalations/scan")
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(first.json()) == 1
    assert second.json() == []


def test_supervisor_coordinates_repair_and_renovation():
    """Supervisor 必须真实编排专业 Agent，并创建两类业务任务。"""

    seed_demo_repair_personnel()

    response = client.post(
        "/api/v1/agent/coordinate",
        json={
            "message": "A栋302客户下周准备入驻，需要对房间进行工程改造，另外客户反馈空调存在漏水问题，希望尽快处理。",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["mode"] == "rule_fallback_demo"
    assert data["created_ticket"]["status"] == "pending"
    assert data["created_renovation_task"]["status"] == "pending"
    assert data["dispatch_recommendation"]["recommended_personnel"] == "ENG-001"
    assert data["steps"][1]["output"]["location"] == "A栋302"
    create_ticket_call = next(
        call for call in data["steps"][0]["tool_calls"]
        if call["tool_name"] == "create_repair_ticket"
    )
    assert create_ticket_call["output"]["ticket_no"] == data["created_ticket"]["ticket_no"]
    assert "<app.models" not in str(create_ticket_call["output"])
    assert [step["agent_name"] for step in data["steps"]] == [
        "supervisor_agent",
        "repair_intake_agent",
        "risk_assessment_agent",
        "dispatch_agent",
        "renovation_agent",
    ]
    assert data["steps"][1]["tool_calls"][0]["tool_name"] == "parse_repair_text"
    assert data["steps"][2]["tool_calls"][0]["tool_name"] == "classify_priority_and_sla"

    run_response = client.get(f"/api/v1/agent/runs/{data['run_id']}")
    assert run_response.status_code == 200
    assert len(run_response.json()) == 5


def test_supervisor_returns_completed_run_for_unknown_intent():
    """无法识别业务时也要留下可追踪的 Supervisor 结果。"""

    response = client.post(
        "/api/v1/agent/coordinate",
        json={"message": "请帮我看看今天的园区情况"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["created_ticket"] is None
    assert data["created_renovation_task"] is None
    assert data["steps"][0]["agent_name"] == "supervisor_agent"


def test_list_repair_personnel():
    # 当前测试数据库没有演示账号时，目录接口应返回空列表而不是报错
    response = client.get("/api/v1/repair-personnel")

    assert response.status_code == 200
    assert response.json() == []


def test_assign_ticket_requires_a_real_personnel_record():
    # 测试数据库没有演示人员时，不能把任意字符串当作工程人员
    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋1楼",
            "issue_type": "照明故障",
            "description": "走廊灯不亮",
            "priority": "normal",
        },
    )
    ticket_id = create_response.json()["id"]

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/assignments",
        json={
            "personnel_no": "ENG-001",
            "assigned_by": "ENG-LEAD-001",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "工程人员不存在或已停用"


def test_assign_ticket_success_keeps_ticket_pending():
    # 准备一名可分派的演示工程人员
    db = TestingSessionLocal()
    db.add(
        RepairPersonnel(
            personnel_no="ENG-001",
            display_name="水电维修演示员",
            department="工程部",
            role="维修人员",
            skills='["漏水"]',
            responsible_area="A栋",
        )
    )
    db.commit()
    db.close()

    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋1楼",
            "issue_type": "漏水",
            "description": "管道漏水",
            "priority": "urgent",
        },
    )
    ticket_id = create_response.json()["id"]

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/assignments",
        json={
            "personnel_no": "ENG-001",
            "assigned_by": "ENG-LEAD-001",
        },
    )

    assert response.status_code == 201
    assert response.json()["personnel_no"] == "ENG-001"
    assert response.json()["personnel_name"] == "水电维修演示员"
    assert response.json()["assigned_by"] == "ENG-LEAD-001"

    detail_response = client.get(f"/api/v1/tickets/{ticket_id}")
    assert detail_response.json()["status"] == "pending"
    assert detail_response.json()["assignee"] == "ENG-001"


def test_accept_ticket_requires_current_assignee():
    """只有当前分派人员可以确认接单。"""

    db = TestingSessionLocal()
    db.add_all([
        RepairPersonnel(
            personnel_no="ENG-001",
            display_name="水电维修演示员",
            department="工程部",
            role="维修人员",
            skills='["漏水"]',
            responsible_area="A栋",
        ),
        RepairPersonnel(
            personnel_no="ENG-002",
            display_name="空调设备演示员",
            department="工程部",
            role="维修人员",
            skills='["空调"]',
            responsible_area="B栋",
        ),
    ])
    db.commit()
    db.close()

    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋1楼",
            "issue_type": "漏水",
            "description": "管道漏水",
            "priority": "urgent",
        },
    )
    ticket_id = create_response.json()["id"]

    assignment_response = client.post(
        f"/api/v1/tickets/{ticket_id}/assignments",
        json={
            "personnel_no": "ENG-001",
            "assigned_by": "ENG-LEAD-001",
        },
    )
    assert assignment_response.status_code == 201

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/accept",
        json={"personnel_no": "ENG-002"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "该工程人员不是当前分派人员，不能接单"


def test_accept_ticket_records_real_person_event():
    """接单成功后记录工程人员身份和接单时间。"""

    db = TestingSessionLocal()
    db.add(
        RepairPersonnel(
            personnel_no="ENG-001",
            display_name="水电维修演示员",
            department="工程部",
            role="维修人员",
            skills='["漏水"]',
            responsible_area="A栋",
        )
    )
    db.commit()
    db.close()

    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋1楼",
            "issue_type": "漏水",
            "description": "管道漏水",
            "priority": "urgent",
        },
    )
    ticket_id = create_response.json()["id"]

    client.post(
        f"/api/v1/tickets/{ticket_id}/assignments",
        json={
            "personnel_no": "ENG-001",
            "assigned_by": "ENG-LEAD-001",
        },
    )
    response = client.post(
        f"/api/v1/tickets/{ticket_id}/accept",
        json={"personnel_no": "ENG-001"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["accepted_at"] is not None

    events = client.get(f"/api/v1/tickets/{ticket_id}/events").json()
    assert events[-1]["to_status"] == "accepted"
    assert events[-1]["actor_type"] == "person"
    assert events[-1]["actor_id"] == "ENG-001"
    assert events[-1]["reason"] == "工程人员接单"


def test_accept_ticket_rejects_unassigned_ticket():
    """没有当前分派记录时，即使人员存在也不能直接接单。"""

    db = TestingSessionLocal()
    db.add(
        RepairPersonnel(
            personnel_no="ENG-001",
            display_name="水电维修演示员",
            department="工程部",
            role="维修人员",
            skills='["漏水"]',
            responsible_area="A栋",
        )
    )
    db.commit()
    db.close()

    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋1楼",
            "issue_type": "漏水",
            "description": "管道漏水",
            "priority": "urgent",
        },
    )
    ticket_id = create_response.json()["id"]

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/accept",
        json={"personnel_no": "ENG-001"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "该工程人员不是当前分派人员，不能接单"


def test_start_processing_requires_accepted_ticket():
    """待接单工单不能跳过接单直接开始维修。"""

    db = TestingSessionLocal()
    db.add(
        RepairPersonnel(
            personnel_no="ENG-001",
            display_name="水电维修演示员",
            department="工程部",
            role="维修人员",
            skills='["漏水"]',
            responsible_area="A栋",
        )
    )
    db.commit()
    db.close()

    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋1楼",
            "issue_type": "漏水",
            "description": "管道漏水",
            "priority": "urgent",
        },
    )
    ticket_id = create_response.json()["id"]

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/start-processing",
        json={"personnel_no": "ENG-001"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "只有已接单状态的工单才能开始维修"


def test_start_processing_records_real_person_event():
    """当前分派且已接单的工程人员可以开始维修。"""

    db = TestingSessionLocal()
    db.add_all([
        RepairPersonnel(
            personnel_no="ENG-001",
            display_name="水电维修演示员",
            department="工程部",
            role="维修人员",
            skills='["漏水"]',
            responsible_area="A栋",
        ),
        RepairPersonnel(
            personnel_no="ENG-002",
            display_name="空调设备演示员",
            department="工程部",
            role="维修人员",
            skills='["空调"]',
            responsible_area="B栋",
        ),
    ])
    db.commit()
    db.close()

    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋1楼",
            "issue_type": "漏水",
            "description": "管道漏水",
            "priority": "urgent",
        },
    )
    ticket_id = create_response.json()["id"]

    client.post(
        f"/api/v1/tickets/{ticket_id}/assignments",
        json={
            "personnel_no": "ENG-001",
            "assigned_by": "ENG-LEAD-001",
        },
    )
    accept_response = client.post(
        f"/api/v1/tickets/{ticket_id}/accept",
        json={"personnel_no": "ENG-001"},
    )
    assert accept_response.status_code == 200

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/start-processing",
        json={"personnel_no": "ENG-001"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processing"

    events = client.get(f"/api/v1/tickets/{ticket_id}/events").json()
    assert events[-1]["to_status"] == "processing"
    assert events[-1]["actor_type"] == "person"
    assert events[-1]["actor_id"] == "ENG-001"
    assert events[-1]["reason"] == "工程人员开始维修"


def test_start_processing_rejects_non_assignee():
    """非当前分派人员不能开始维修。"""

    db = TestingSessionLocal()
    db.add_all([
        RepairPersonnel(
            personnel_no="ENG-001",
            display_name="水电维修演示员",
            department="工程部",
            role="维修人员",
            skills='["漏水"]',
            responsible_area="A栋",
        ),
        RepairPersonnel(
            personnel_no="ENG-002",
            display_name="空调设备演示员",
            department="工程部",
            role="维修人员",
            skills='["空调"]',
            responsible_area="B栋",
        ),
    ])
    db.commit()
    db.close()

    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋1楼",
            "issue_type": "漏水",
            "description": "管道漏水",
            "priority": "urgent",
        },
    )
    ticket_id = create_response.json()["id"]

    client.post(
        f"/api/v1/tickets/{ticket_id}/assignments",
        json={
            "personnel_no": "ENG-001",
            "assigned_by": "ENG-LEAD-001",
        },
    )
    client.post(
        f"/api/v1/tickets/{ticket_id}/accept",
        json={"personnel_no": "ENG-001"},
    )

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/start-processing",
        json={"personnel_no": "ENG-002"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "该工程人员不是当前分派人员，不能开始维修"

def test_root():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["version"] == "0.1.0"

def test_create_ticket():
    # 模拟用户提交一条报修请求
    payload = {
        "location": "A栋3楼卫生间",
        "issue_type": "漏水",
        "description": "地面积水",
        "priority": "urgent",
    }

    response = client.post(
        "/api/v1/tickets",
        json=payload,
    )

    # 验证创建成功
    assert response.status_code == 201

    data = response.json()

    # 验证后端自动生成了工单编号
    assert data["ticket_no"].startswith("WX")

    # 验证工单信息正确
    assert data["location"] == "A栋3楼卫生间"
    assert data["issue_type"] == "漏水"

    # 验证初始状态
    assert data["status"] == "pending"
    assert data["priority"] == "urgent"


def test_list_ticket_events():
    # 创建工单后，时间线应至少包含一条创建事件
    payload = {
        "location": "A栋1楼",
        "issue_type": "照明故障",
        "description": "走廊灯不亮",
        "priority": "normal",
    }

    create_response = client.post("/api/v1/tickets", json=payload)
    assert create_response.status_code == 201
    ticket_id = create_response.json()["id"]

    response = client.get(f"/api/v1/tickets/{ticket_id}/events")

    assert response.status_code == 200
    events = response.json()
    assert len(events) == 1
    assert events[0]["from_status"] is None
    assert events[0]["to_status"] == "pending"
    assert events[0]["actor_type"] == "system"
    assert events[0]["is_inferred"] is False


def test_list_ticket_events_for_nonexistent_ticket():
    response = client.get("/api/v1/tickets/999999/events")

    assert response.status_code == 404


def test_create_ticket_with_invalid_priority():
    # 模拟用户提交不合法的优先级
    payload = {
        "location": "A栋3楼卫生间",
        "issue_type": "漏水",
        "description": "地面积水",
        "priority": "super_urgent",
    }

    response = client.post(
        "/api/v1/tickets",
        json=payload,
    )

    # 不合法的优先级应被 Pydantic 拦截
    assert response.status_code == 422

def test_update_ticket_status_success():
    # 1. 创建一条新工单
    payload = {
        "location": "A栋2楼",
        "issue_type": "照明故障",
        "description": "走廊灯不亮",
        "priority": "normal",
    }

    create_response = client.post(
        "/api/v1/tickets",
        json=payload,
    )

    assert create_response.status_code == 201

    ticket_id = create_response.json()["id"]

    # 创建时，工单还没有接单和提交维修结果
    assert create_response.json()["accepted_at"] is None
    assert create_response.json()["submitted_at"] is None

    # 2. pending → accepted：记录接单时间
    response = client.patch(
        f"/api/v1/tickets/{ticket_id}/status",
        json={"status": "accepted"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["accepted_at"] is not None

    accepted_at = response.json()["accepted_at"]

    # 3. accepted → processing：接单时间应该保持不变
    response = client.patch(
        f"/api/v1/tickets/{ticket_id}/status",
        json={"status": "processing"},
    )

    assert response.status_code == 200
    assert response.json()["accepted_at"] == accepted_at

    # 4. processing → pending_review：记录提交时间
    response = client.patch(
        f"/api/v1/tickets/{ticket_id}/status",
        json={"status": "pending_review"},
    )

    assert response.status_code == 200
    assert response.json()["submitted_at"] is not None

    submitted_at = response.json()["submitted_at"]

    # 5. pending_review → completed：两个时间都应该保留
    response = client.patch(
        f"/api/v1/tickets/{ticket_id}/status",
        json={"status": "completed"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["accepted_at"] == accepted_at
    assert response.json()["submitted_at"] == submitted_at


def test_update_ticket_status_invalid_transition():
    # 1. 创建一条新工单，初始状态为 pending
    payload = {
        "location": "A栋2楼",
        "issue_type": "照明故障",
        "description": "走廊灯不亮",
        "priority": "normal",
    }

    create_response = client.post(
        "/api/v1/tickets",
        json=payload,
    )

    assert create_response.status_code == 201

    ticket_id = create_response.json()["id"]

    # 2. 尝试从 pending 直接更新为 completed
    response = client.patch(
        f"/api/v1/tickets/{ticket_id}/status",
        json={"status": "completed"},
    )

    # 3. 验证非法流转被拒绝
    assert response.status_code == 400

    # 4. 再次查询，确认原状态没有被修改
    detail_response = client.get(
        f"/api/v1/tickets/{ticket_id}",
    )

    assert detail_response.status_code == 200
    assert detail_response.json()["status"] == "pending"


def test_update_status_for_nonexistent_ticket():
    # 使用一个不存在的工单 ID
    response = client.patch(
        "/api/v1/tickets/999999/status",
        json={"status": "accepted"},
    )

    # 验证返回 404
    assert response.status_code == 404
    assert response.json()["detail"] == "工单不存在"

def test_create_ticket_preserves_confirmed_priority_over_keyword_suggestion():
    # 用户选择普通，但描述包含特急关键词
    payload = {
        "location": "A栋",
        "issue_type": "电梯故障",
        "description": "电梯困人，请立即处理",
        "priority": "normal",
    }

    response = client.post(
        "/api/v1/tickets",
        json=payload,
    )

    assert response.status_code == 201

    # 关键词只能给出建议，建单保留人工确认的普通等级。
    assert response.json()["priority"] == "normal"


def test_create_ticket_keep_higher_manual_priority():
    # 用户手动选择特急，描述本身只是普通问题
    payload = {
        "location": "A栋",
        "issue_type": "照明故障",
        "description": "走廊灯不亮",
        "priority": "critical",
    }

    response = client.post(
        "/api/v1/tickets",
        json=payload,
    )

    assert response.status_code == 201

    # 系统不能把用户手动提高的优先级降下来
    assert response.json()["priority"] == "critical"


@pytest.mark.parametrize("priority", ["normal", "urgent", "critical"])
@pytest.mark.parametrize("description", ["电梯困人，请立即处理", "走廊灯不亮"])
def test_manual_web_and_feishu_creation_share_confirmed_business_fields(priority, description):
    """高低关键词建议均不覆盖确认等级，各入口保留各自来源事件。"""
    payload = {
        "location": "A栋三楼",
        "issue_type": "设备故障",
        "description": description,
        "priority": priority,
    }
    manual = client.post("/api/v1/tickets", json=payload)
    assert manual.status_code == 201
    tickets = [manual.json()]
    for channel in ("web", "feishu"):
        start = client.post("/api/v1/agent/runs", json={
            "message": "A栋三楼卫生间漏水",
            "channel": channel,
            "idempotency_key": f"unified-start-{channel}",
        })
        assert start.status_code == 200
        assert start.json()["channel"] == channel
        assert start.json()["status"] == "waiting_human"
        workflow_id = start.json()["workflow_id"]
        confirmed = client.post(f"/api/v1/agent/runs/{workflow_id}/confirm-ticket", json={
            **payload, "idempotency_key": f"unified-confirm-{channel}",
        })
        assert confirmed.status_code == 200
        tickets.append(confirmed.json()["ticket"])

    assert len({ticket["ticket_no"] for ticket in tickets}) == 3
    for index, ticket in enumerate(tickets):
        for field, value in payload.items():
            assert ticket[field] == value
        assert ticket["status"] == "pending"
        assert ticket["assignee"] is None
        events = client.get(f"/api/v1/tickets/{ticket['id']}/events").json()
        assert len(events) == 1
        assert events[0]["from_status"] is None
        assert events[0]["to_status"] == "pending"
        assert events[0]["actor_type"] == ("system" if index == 0 else "企服_agent")

def test_list_overdue_tickets(monkeypatch):
    # 1. 创建一条特急工单，用于测试接单超时
    critical_payload = {
        "location": "A栋1楼",
        "issue_type": "设备故障",
        "description": "测试接单超时",
        "priority": "critical",
    }

    accept_response = client.post(
        "/api/v1/tickets",
        json=critical_payload,
    )

    assert accept_response.status_code == 201
    accept_ticket_id = accept_response.json()["id"]

    # 2. 再创建一条特急工单，用于测试处理超时
    process_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋2楼",
            "issue_type": "设备故障",
            "description": "测试处理超时",
            "priority": "critical",
        },
    )

    assert process_response.status_code == 201
    process_ticket_id = process_response.json()["id"]

    # 将第二条工单从 pending 更新为 accepted
    update_response = client.patch(
        f"/api/v1/tickets/{process_ticket_id}/status",
        json={"status": "accepted"},
    )

    assert update_response.status_code == 200

    # 3. 创建一条普通工单，用于测试未超时情况
    normal_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋3楼",
            "issue_type": "照明故障",
            "description": "测试未超时",
            "priority": "normal",
        },
    )

    assert normal_response.status_code == 201
    normal_ticket_id = normal_response.json()["id"]

    # 4. 模拟时间向后推移 1 小时
    # 特急接单时限为 5 分钟，处理时限为 30 分钟；
    # 普通工单接单时限为 24 小时。
    future_time = datetime.now() + timedelta(hours=1)

    class MockDateTime:
        @classmethod
        def now(cls):
            return future_time

    # 替换 main.py 中的 datetime，模拟当前时间
    import app.main as main_module
    monkeypatch.setattr(main_module, "datetime", MockDateTime)

    # 5. 查询超时工单
    response = client.get("/api/v1/tickets/overdue")

    assert response.status_code == 200

    overdue_tickets = response.json()

    # 只检查本测试创建的三条工单，避免其他测试遗留数据干扰
    result_by_id = {
        item["ticket_id"]: item
        for item in overdue_tickets
    }

    # 6. 验证接单超时
    assert accept_ticket_id in result_by_id
    assert result_by_id[accept_ticket_id]["timeout_type"] == "accept_timeout"

    # 7. 验证处理超时
    assert process_ticket_id in result_by_id
    assert result_by_id[process_ticket_id]["timeout_type"] == "process_timeout"

    # 8. 验证普通工单没有超时
    assert normal_ticket_id not in result_by_id    

def test_parse_repair_text_success():
    # 1. 模拟用户提交自然语言报修内容
    payload = {
        "text": "我刚刚发现A栋三楼卫生间漏水了，请尽快处理"
    }

    # 2. 调用自然语言报修解析接口
    response = client.post(
        "/api/v1/repairs/parse",
        json=payload,
    )

    # 3. 验证接口请求成功
    assert response.status_code == 200

    data = response.json()

    # 4. 验证报修位置提取正确
    assert data["location"] == "A栋三楼卫生间"

    # 5. 验证故障类型提取正确
    assert data["issue_type"] == "漏水"

    # 6. 验证原始描述被完整保留
    assert data["description"] == payload["text"]

    # 7. 验证优先级建议正确
    assert data["suggested_priority"] == "urgent"

    # 8. 验证没有缺失必要字段
    assert data["missing_fields"] == []   

def test_parse_repair_text_missing_location():
    # 模拟用户只描述故障，没有提供报修位置
    payload = {
        "text": "漏水了"
    }

    response = client.post(
        "/api/v1/repairs/parse",
        json=payload,
    )

    assert response.status_code == 200

    data = response.json()

    # 能识别故障类型，但无法提取位置
    assert data["location"] is None
    assert data["issue_type"] == "漏水"

    # 原始描述应保留
    assert data["description"] == "漏水了"

    # 漏水属于加急故障
    assert data["suggested_priority"] == "urgent"

    # 系统应提示缺少报修位置
    assert data["missing_fields"] == ["location"]    

def test_parse_repair_text_empty():
    # 模拟用户提交空报修内容
    payload = {
        "text": ""
    }

    response = client.post(
        "/api/v1/repairs/parse",
        json=payload,
    )

    assert response.status_code == 200

    data = response.json()

    # 空输入不应提取出任何业务信息
    assert data["location"] is None
    assert data["issue_type"] is None
    assert data["description"] == ""

    # 空输入无法判断优先级
    assert data["suggested_priority"] is None

    # 应提示缺少描述、位置和故障类型
    assert data["missing_fields"] == [
        "description",
        "location",
        "issue_type",
    ]    

def test_acceptance_history_preserves_multiple_rework_rounds_and_is_read_only():
    seed_demo_repair_personnel()
    payload = {"location": "A栋302", "issue_type": "漏水", "description": "水管漏水", "priority": "urgent"}
    created = client.post("/api/v1/tickets", json=payload)
    assert created.status_code == 201
    ticket_id = created.json()["id"]
    other = client.post("/api/v1/tickets", json=payload)
    assert other.status_code == 201
    assert client.get(f"/api/v1/tickets/{ticket_id}/acceptance-records").json() == []
    assert client.post(f"/api/v1/tickets/{ticket_id}/assignments", json={
        "personnel_no": "ENG-001", "assigned_by": "负责人",
    }).status_code == 201
    for action in ("accept", "start-processing"):
        assert client.post(f"/api/v1/tickets/{ticket_id}/{action}", json={"personnel_no": "ENG-001"}).status_code == 200

    decisions = [
        {"accepted": False, "accepted_by": "企服甲", "acceptance_comment": "接头仍漏水\n请更换密封件"},
        {"accepted": False, "accepted_by": None, "acceptance_comment": "压力测试未通过，请继续排查"},
        {"accepted": True, "accepted_by": "企服乙", "acceptance_comment": "现场复核正常，验收通过"},
    ]
    expected = []
    for index, decision in enumerate(decisions):
        repair = client.post(f"/api/v1/tickets/{ticket_id}/repair-records", json={
            "personnel_no": "ENG-001", "repair_description": f"第{index + 1}次维修，已处理并自检", "photo_urls": [],
        })
        assert repair.status_code == 201
        result = client.post(f"/api/v1/tickets/{ticket_id}/acceptance", json=decision)
        assert result.status_code == 201
        expected.append(result.json())
        history = client.get(f"/api/v1/tickets/{ticket_id}/acceptance-records")
        assert history.status_code == 200
        assert history.json() == expected
        assert client.get(f"/api/v1/tickets/{ticket_id}").json()["status"] == ("completed" if decision["accepted"] else "processing")

    # 相同时间以主键稳定排序，不把其他工单记录或重读产生的副作用混入。
    with TestingSessionLocal() as db:
        db.query(AcceptanceRecord).filter(AcceptanceRecord.ticket_id == ticket_id).update({"accepted_at": datetime(2026, 11, 9, 10, 0)})
        db.commit()
    before_ticket = client.get(f"/api/v1/tickets/{ticket_id}").json()
    before_events = client.get(f"/api/v1/tickets/{ticket_id}/events").json()
    first = client.get(f"/api/v1/tickets/{ticket_id}/acceptance-records").json()
    assert [row["id"] for row in first] == [row["id"] for row in expected]
    assert [row["accepted"] for row in first] == [False, False, True]
    assert client.get(f"/api/v1/tickets/{ticket_id}/acceptance-records").json() == first
    assert client.get(f"/api/v1/tickets/{other.json()['id']}/acceptance-records").json() == []
    assert client.get(f"/api/v1/tickets/{ticket_id}").json() == before_ticket
    assert client.get(f"/api/v1/tickets/{ticket_id}/events").json() == before_events
    assert len(client.get(f"/api/v1/tickets/{ticket_id}/repair-records").json()) == 3


def test_acceptance_history_returns_404_for_missing_ticket():
    response = client.get("/api/v1/tickets/999999/acceptance-records")
    assert response.status_code == 404
    assert response.json()["detail"] == "工单不存在"


def test_submit_repair_record_success():
    """处理中工单成功提交维修反馈"""

    seed_demo_repair_personnel()

    # 1. 创建工单
    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋3楼卫生间",
            "issue_type": "漏水",
            "description": "水管漏水",
            "priority": "urgent",
        },
    )

    assert create_response.status_code == 201
    ticket_id = create_response.json()["id"]

    # 2. 分派并由当前工程人员接单
    assignment_response = client.post(
        f"/api/v1/tickets/{ticket_id}/assignments",
        json={"personnel_no": "ENG-001", "assigned_by": "ENG-LEAD-001"},
    )
    assert assignment_response.status_code == 201

    accepted_response = client.post(
        f"/api/v1/tickets/{ticket_id}/accept",
        json={"personnel_no": "ENG-001"},
    )
    assert accepted_response.status_code == 200

    # 3. 由同一名工程人员开始维修
    processing_response = client.post(
        f"/api/v1/tickets/{ticket_id}/start-processing",
        json={"personnel_no": "ENG-001"},
    )
    assert processing_response.status_code == 200

    # 4. 提交维修反馈
    response = client.post(
        f"/api/v1/tickets/{ticket_id}/repair-records",
        json={
            "personnel_no": "ENG-001",
            "repair_description": "已更换破损水管，并完成漏水测试",
            "photo_urls": [
                "photos/before.jpg",
                "photos/after.jpg",
            ],
        },
    )

    # 5. 验证维修反馈创建成功
    assert response.status_code == 201

    data = response.json()
    assert data["ticket_id"] == ticket_id
    assert data["repair_description"] == "已更换破损水管，并完成漏水测试"
    assert data["photo_urls"] == [
        "photos/before.jpg",
        "photos/after.jpg",
    ]
    assert data["submitted_at"] is not None

    # 6. 验证工单进入待验收状态
    detail_response = client.get(
        f"/api/v1/tickets/{ticket_id}",
    )

    assert detail_response.status_code == 200
    assert detail_response.json()["status"] == "pending_review"

    # 7. 验证创建、接单、处理和提交维修均留下事件
    events_response = client.get(
        f"/api/v1/tickets/{ticket_id}/events",
    )

    assert events_response.status_code == 200
    assert [event["to_status"] for event in events_response.json()] == [
        "pending",
        "accepted",
        "processing",
        "pending_review",
    ]
    assert events_response.json()[-1]["actor_id"] == "ENG-001"


def test_submit_repair_record_rejects_non_assignee():
    """非当前分派人员不能提交维修结果。"""

    db = TestingSessionLocal()
    db.add_all([
        RepairPersonnel(
            personnel_no="ENG-001",
            display_name="水电维修演示员",
            department="工程部",
            role="维修人员",
            skills='["漏水"]',
            responsible_area="A栋",
        ),
        RepairPersonnel(
            personnel_no="ENG-002",
            display_name="空调设备演示员",
            department="工程部",
            role="维修人员",
            skills='["空调"]',
            responsible_area="B栋",
        ),
    ])
    db.commit()
    db.close()

    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋3楼卫生间",
            "issue_type": "漏水",
            "description": "水管漏水",
            "priority": "urgent",
        },
    )
    ticket_id = create_response.json()["id"]

    assignment_response = client.post(
        f"/api/v1/tickets/{ticket_id}/assignments",
        json={"personnel_no": "ENG-001", "assigned_by": "ENG-LEAD-001"},
    )
    assert assignment_response.status_code == 201
    assert client.post(
        f"/api/v1/tickets/{ticket_id}/accept",
        json={"personnel_no": "ENG-001"},
    ).status_code == 200
    assert client.post(
        f"/api/v1/tickets/{ticket_id}/start-processing",
        json={"personnel_no": "ENG-001"},
    ).status_code == 200

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/repair-records",
        json={
            "personnel_no": "ENG-002",
            "repair_description": "尝试提交维修结果",
            "photo_urls": [],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "该工程人员不是当前分派人员，不能提交维修结果"
    assert client.get(f"/api/v1/tickets/{ticket_id}").json()["status"] == "processing"


def test_submit_repair_record_for_nonexistent_ticket():
    """不存在的工单不能提交维修反馈"""

    response = client.post(
        "/api/v1/tickets/999999/repair-records",
        json={
            "personnel_no": "ENG-001",
            "repair_description": "维修完成",
            "photo_urls": [],
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "工单不存在"


def test_submit_repair_record_with_wrong_status():
    """待接单状态的工单不能提交维修反馈"""

    # 1. 创建工单，初始状态为 pending
    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "B栋2楼",
            "issue_type": "照明故障",
            "description": "走廊灯不亮",
            "priority": "normal",
        },
    )

    assert create_response.status_code == 201
    ticket_id = create_response.json()["id"]

    # 2. 不进行接单和维修，直接提交维修反馈
    response = client.post(
        f"/api/v1/tickets/{ticket_id}/repair-records",
        json={
            "personnel_no": "ENG-001",
            "repair_description": "尝试直接提交维修结果",
            "photo_urls": [],
        },
    )

    # 3. 应被业务规则拒绝
    assert response.status_code == 400
    assert response.json()["detail"] == "只有处理中状态的工单才能提交维修结果"

    # 4. 确认工单状态没有被修改
    detail_response = client.get(
        f"/api/v1/tickets/{ticket_id}",
    )

    assert detail_response.status_code == 200
    assert detail_response.json()["status"] == "pending"    

def test_submit_acceptance_record_success():
    """待验收工单成功提交验收意见。"""

    seed_demo_repair_personnel()

    # 1. 创建工单
    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "A栋3楼卫生间",
            "issue_type": "漏水",
            "description": "水管漏水",
            "priority": "urgent",
        },
    )

    assert create_response.status_code == 201
    ticket_id = create_response.json()["id"]

    # 2. 分派并由工程人员接单
    assignment_response = client.post(
        f"/api/v1/tickets/{ticket_id}/assignments",
        json={"personnel_no": "ENG-001", "assigned_by": "ENG-LEAD-001"},
    )
    assert assignment_response.status_code == 201

    accepted_response = client.post(
        f"/api/v1/tickets/{ticket_id}/accept",
        json={"personnel_no": "ENG-001"},
    )
    assert accepted_response.status_code == 200

    # 3. 工程人员开始维修
    processing_response = client.post(
        f"/api/v1/tickets/{ticket_id}/start-processing",
        json={"personnel_no": "ENG-001"},
    )
    assert processing_response.status_code == 200

    # 4. 工程人员提交维修反馈
    repair_response = client.post(
        f"/api/v1/tickets/{ticket_id}/repair-records",
        json={
            "personnel_no": "ENG-001",
            "repair_description": "已更换破损水管，完成漏水测试",
            "photo_urls": ["photos/after.jpg"],
        },
    )
    assert repair_response.status_code == 201

    # 5. 企服提交验收意见
    response = client.post(
        f"/api/v1/tickets/{ticket_id}/acceptance",
        json={
            "acceptance_comment": "维修完成，现场确认正常",
            "accepted_by": "企服人员A",
        },
    )

    assert response.status_code == 201

    data = response.json()
    assert data["ticket_id"] == ticket_id
    assert data["acceptance_comment"] == "维修完成，现场确认正常"
    assert data["accepted_by"] == "企服人员A"
    assert data["accepted_at"] is not None

    # 6. 验证工单已经完成
    detail_response = client.get(
        f"/api/v1/tickets/{ticket_id}",
    )

    assert detail_response.status_code == 200
    assert detail_response.json()["status"] == "completed"

    # 7. 验证完整工单闭环留下五个状态事件
    events_response = client.get(
        f"/api/v1/tickets/{ticket_id}/events",
    )

    assert events_response.status_code == 200
    events = events_response.json()
    assert [event["to_status"] for event in events] == [
        "pending",
        "accepted",
        "processing",
        "pending_review",
        "completed",
    ]
    assert events[-1]["actor_id"] == "企服人员A"


def test_submit_acceptance_record_for_nonexistent_ticket():
    """不存在的工单不能提交验收。"""

    response = client.post(
        "/api/v1/tickets/999999/acceptance",
        json={
            "acceptance_comment": "维修完成，现场确认正常",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "工单不存在"


def test_submit_acceptance_record_with_wrong_status():
    """处理中工单不能直接提交验收。"""

    # 1. 创建工单
    create_response = client.post(
        "/api/v1/tickets",
        json={
            "location": "B栋2楼",
            "issue_type": "照明故障",
            "description": "走廊灯不亮",
            "priority": "normal",
        },
    )

    assert create_response.status_code == 201
    ticket_id = create_response.json()["id"]

    # 2. 将工单更新为处理中
    client.patch(
        f"/api/v1/tickets/{ticket_id}/status",
        json={"status": "accepted"},
    )

    processing_response = client.patch(
        f"/api/v1/tickets/{ticket_id}/status",
        json={"status": "processing"},
    )

    assert processing_response.status_code == 200

    # 3. 尝试跳过维修反馈，直接验收
    response = client.post(
        f"/api/v1/tickets/{ticket_id}/acceptance",
        json={
            "acceptance_comment": "尝试直接验收",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "只有待验收状态的工单才能进行验收"

    # 4. 确认工单仍处于处理中
    detail_response = client.get(
        f"/api/v1/tickets/{ticket_id}",
    )

    assert detail_response.status_code == 200
    assert detail_response.json()["status"] == "processing"    
