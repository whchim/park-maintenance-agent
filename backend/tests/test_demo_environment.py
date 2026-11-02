"""演示必须使用隔离数据；正式引擎一旦连接就让测试失败。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.database import engine as business_engine
from scripts.run_demo import create_demo_app


@pytest.fixture(autouse=True)
def no_business_connection():
    def blocked(*args):
        pytest.fail("演示环境不得连接现有业务数据库")
    event.listen(business_engine, "connect", blocked)
    try:
        yield
    finally:
        event.remove(business_engine, "connect", blocked)


def create_ticket(client):
    response = client.post("/api/v1/tickets", json={
        "location": "A栋302", "issue_type": "漏水", "description": "演示用例：漏水", "priority": "normal",
    })
    assert response.status_code == 201
    return response.json()


def test_demo_restart_starts_empty_and_exposes_only_synthetic_environment():
    demo = create_demo_app()
    with TestClient(demo) as client:
        runtime = client.get("/api/v1/agents/runtime").json()
        assert runtime["environment"] == {"name": "isolated_demo", "synthetic_data": True, "volatile": True}
        assert runtime["configured_mode"] == "rule"
        assert runtime["api_features"] == ["image_attachments", "repair_feedback_read"]
        assert len(client.get("/api/v1/repair-personnel").json()) == 3
        create_ticket(client)
        assert len(client.get("/api/v1/tickets").json()) == 1
    with TestClient(demo) as client:
        assert client.get("/api/v1/tickets").json() == []
        assert len(client.get("/api/v1/repair-personnel").json()) == 3


def test_demo_instances_do_not_share_data_or_dependency_overrides():
    first, second = create_demo_app(), create_demo_app()
    with TestClient(first) as one, TestClient(second) as two:
        create_ticket(one)
        assert len(one.get("/api/v1/tickets").json()) == 1
        assert two.get("/api/v1/tickets").json() == []


def test_demo_web_workflow_and_suggestions_preserve_human_gates():
    with TestClient(create_demo_app()) as client:
        response = client.post("/api/v1/agent/runs", json={
            "message": "漏水", "channel": "web", "requester_id": "web_demo_user", "idempotency_key": "demo-start",
        })
        assert response.status_code == 200
        workflow = response.json()
        assert workflow["channel"] == "web"
        assert workflow["events"][0]["source_agent"] == "channel_adapter"
        base = f"/api/v1/agent/runs/{workflow['workflow_id']}"
        payload = {"location": "A栋302", "issue_type": "漏水", "description": "漏水；位置在A栋302",
                   "priority": "normal", "idempotency_key": "demo-confirm"}
        assert client.post(base + "/confirm-ticket", json=payload).status_code == 409
        assert client.get("/api/v1/tickets").json() == []
        supplemented = client.post(base + "/messages", json={"message": "位置在A栋302", "idempotency_key": "demo-location"})
        assert supplemented.json()["status"] == "waiting_human"
        assert client.get("/api/v1/tickets").json() == []
        confirmed = client.post(base + "/confirm-ticket", json=payload).json()
        ticket_id = confirmed["ticket_id"]
        assert ticket_id is not None
        assert client.post(base + "/confirm-ticket", json=payload).json()["ticket_id"] == ticket_id
        suggestion = client.post(f"/api/v1/tickets/{ticket_id}/dispatch-suggestion").json()
        assert suggestion["requires_human_confirmation"] is True
        assert client.get(f"/api/v1/tickets/{ticket_id}/assignments").json() == []
        tickets = client.get("/api/v1/tickets").json()
        assert len(tickets) == 1
        assert tickets[0]["assignee"] is None
        assert tickets[0]["status"] == "pending"


def test_restore_legacy_demo_keeps_public_repair_state_and_loads_new_image_routes():
    from io import BytesIO
    from PIL import Image
    snapshot = {"version": 1, "scope": "legacy_demo_repair_before_submission",
                "personnel": [{"id": 1, "personnel_no": "ENG-OLD", "display_name": "模拟维修员", "department": "工程部",
                               "role": "维修人员", "skills": ["空调"], "responsible_area": "A栋", "is_active": True}],
                "tickets": [{"id": 1, "ticket_no": "WX-OLD-001", "location": "A栋302", "issue_type": "空调漏水",
                             "description": "原报修内容", "priority": "normal", "status": "processing", "assignee": "ENG-OLD",
                             "created_at": "2026-11-01T12:00:00", "accepted_at": "2026-11-01T12:05:00"}],
                "ticket_events": [{"id": 1, "ticket_id": 1, "from_status": "accepted", "to_status": "processing",
                                   "actor_type": "person", "actor_id": "ENG-OLD", "reason": "原处理记录",
                                   "occurred_at": "2026-11-01T12:06:00", "is_inferred": False}],
                "assignments": [{"id": 1, "ticket_id": 1, "personnel_id": 1, "assigned_by": "原负责人", "is_current": True}],
                "workflows": [{"workflow_id": "wf_old", "channel": "web", "requester_id": "demo", "status": "waiting_engineering",
                               "current_step": "repair_execution", "original_message": "原报修内容", "draft": {},
                               "missing_fields": [], "ticket_id": 1, "events": []}]}
    with TestClient(create_demo_app(snapshot)) as client:
        ticket = client.get("/api/v1/tickets").json()[0]
        assert ticket["status"] == "processing" and ticket["ticket_no"] == "WX-OLD-001"
        assert ticket["accepted_at"] == "2026-11-01T12:05:00"
        assert client.get("/api/v1/agent/runs/wf_old").json()["current_step"] == "repair_execution"
        assert client.get("/api/v1/tickets/1/events").json()[0]["reason"] == "原处理记录"
        assert client.get("/api/v1/tickets/1/assignments").json()[0]["personnel_no"] == "ENG-OLD"
        assert client.get("/api/v1/tickets/1/repair-records").json() == []
        assert client.get("/api/v1/tickets/1/attachments").json() == []
        output = BytesIO()
        Image.new("RGB", (8, 8), "green").save(output, format="JPEG")
        uploaded = client.post("/api/v1/attachments/images?purpose=repair&filename=空调示意.jpg", content=output.getvalue(),
                               headers={"content-type": "image/jpeg"})
        assert uploaded.status_code == 201
        assert uploaded.json()["name"] == "空调示意.jpg"
