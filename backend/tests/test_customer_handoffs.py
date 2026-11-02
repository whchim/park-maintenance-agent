"""签约客户交接的人工门禁、版本、幂等和事务边界。只使用内存数据库。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.customer import Customer
from app.models.workflow_event import WorkflowEvent
from app.services import customer_handoff_service as service


@pytest.fixture
def setup():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)

    def dependency():
        with factory() as db:
            yield db

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = dependency
    with TestClient(app) as client:
        yield client, factory
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous
    Base.metadata.drop_all(engine)
    engine.dispose()


def lead(client, key="lead"):
    response = client.post("/api/v1/agents/investment/confirm", json={
        "customer_name": f"交接测试企业-{key}", "contact_name": "测试联系人",
        "contact_phone": "演示号码", "intent_level": "B", "owner": "招商演示员",
        "follow_up_subject": "需求确认", "follow_up_description": "仍在沟通", "idempotency_key": key,
    })
    assert response.status_code == 200
    return response.json()


DOCUMENT = {
    "contract_no": "DEMO-HT-001", "contract_version": "V1",
    "contact_sheet_ref": "演示客户联系单编号-001", "contact_sheet_version": "V1",
    "location": "A栋301", "contact_name": "测试联系人", "contact_phone": "演示号码",
    "service_requirements": "安排入驻联系", "agreements": "无其他约定",
}


def saved(client, customer_id):
    response = client.post(f"/api/v1/customers/{customer_id}/handoff")
    assert response.status_code == 200
    handoff = response.json()
    response = client.put(f"/api/v1/customer-handoffs/{handoff['id']}", json={
        "expected_revision": handoff["revision"], "document": DOCUMENT,
    })
    assert response.status_code == 200
    return response.json()


def submission(handoff, key="submit"):
    return {"expected_revision": handoff["revision"], "actor": "招商演示员",
            "contract_signed": True, "idempotency_key": key}


def decision(handoff, accepted, key):
    return {"expected_revision": handoff["revision"], "actor": "企服演示员",
            "accepted": accepted, "comment": "资料已核对" if accepted else "请更正房间号", "idempotency_key": key}


def test_lead_creation_does_not_start_formal_handoff(setup):
    client, factory = setup
    result = lead(client)
    assert result["customer"]["stage"] == "lead"
    assert result["handoff_event_id"] is None
    assert client.get("/api/v1/customer-handoffs").json() == []
    with factory() as db:
        assert db.query(WorkflowEvent).filter(WorkflowEvent.event_type.like("customer.handoff.%")).count() == 0


def test_missing_fields_unsigned_and_wrong_department_flow_are_blocked(setup):
    client, _ = setup
    customer = lead(client)["customer"]
    handoff = client.post(f"/api/v1/customers/{customer['id']}/handoff").json()
    assert "contract_no" in handoff["check"]["missing_fields"]
    assert client.post(f"/api/v1/customer-handoffs/{handoff['id']}/submit", json=submission(handoff)).status_code == 409
    complete = saved(client, customer["id"])
    request = submission(complete)
    request["contract_signed"] = False
    assert client.post(f"/api/v1/customer-handoffs/{complete['id']}/submit", json=request).status_code == 409
    # 客户交接会话不能误用报修补充/确认接口建维修工单。
    workflow_id = complete["workflow_id"]
    assert client.post(f"/api/v1/agent/runs/{workflow_id}/messages", json={"message": "位置在A栋"}).status_code == 409
    assert client.post(f"/api/v1/agent/runs/{workflow_id}/confirm-ticket", json={
        "location": "A栋", "issue_type": "漏水", "description": "漏水", "priority": "normal", "idempotency_key": "wrong-flow",
    }).status_code == 409
    assert client.get('/api/v1/tickets').json() == []


def test_return_correction_resubmit_and_accept_preserve_snapshots(setup):
    client, _ = setup
    customer_id = lead(client)["customer"]["id"]
    handoff = saved(client, customer_id)
    base = f"/api/v1/customer-handoffs/{handoff['id']}"
    posted = client.post(base + "/submit", json=submission(handoff)).json()
    assert posted["status"] == "submitted"
    assert client.put(base, json={"expected_revision": posted["revision"], "document": DOCUMENT}).status_code == 409
    returned = client.post(base + "/decide", json=decision(posted, False, "return")).json()
    assert returned["status"] == "returned"
    corrected = {**DOCUMENT, "location": "A栋302", "contact_sheet_version": "V2"}
    saved_again = client.put(base, json={"expected_revision": returned["revision"], "document": corrected}).json()
    reposted = client.post(base + "/submit", json=submission(saved_again, "resubmit")).json()
    accepted = client.post(base + "/decide", json=decision(reposted, True, "accept")).json()
    assert accepted["status"] == "accepted"
    assert accepted["decided_by"] == "企服演示员"
    events = accepted["events"]
    assert [e["event_type"] for e in events] == [
        "customer.handoff.submitted", "customer.handoff.returned",
        "customer.handoff.submitted", "customer.handoff.accepted",
    ]
    assert events[0]["payload"]["document"]["location"] == "A栋301"
    assert events[2]["payload"]["document"]["location"] == "A栋302"
    assert client.post(base + "/decide", json=decision(accepted, False, "late-return")).status_code == 409
    workflow = client.get(f"/api/v1/agent/runs/{accepted['workflow_id']}").json()
    assert workflow["status"] == "completed"


def test_submit_retries_and_old_versions_do_not_duplicate_events(setup):
    client, _ = setup
    created = lead(client)
    handoff = saved(client, created["customer"]["id"])
    base = f"/api/v1/customer-handoffs/{handoff['id']}"
    stale = {**submission(handoff), "expected_revision": 1}
    assert client.post(base + "/submit", json=stale).status_code == 409
    request = submission(handoff)
    first = client.post(base + "/submit", json=request)
    repeated = client.post(base + "/submit", json=request)
    assert first.status_code == repeated.status_code == 200
    assert len(repeated.json()["events"]) == 1
    assert client.get("/api/v1/agents/investment/follow-ups").json() == []
    task_id = created["follow_up_task"]["id"]
    assert client.post(f"/api/v1/agents/investment/follow-ups/{task_id}/complete", json={
        "actor": "招商演示员", "outcome": "已签约", "idempotency_key": "cancelled-task",
    }).status_code == 409
    other = saved(client, lead(client, "other")["customer"]["id"])
    assert client.post(f"/api/v1/customer-handoffs/{other['id']}/submit", json=submission(other)).status_code == 409


def test_receipt_requires_comment_and_is_idempotent(setup):
    client, _ = setup
    handoff = saved(client, lead(client)["customer"]["id"])
    base = f"/api/v1/customer-handoffs/{handoff['id']}"
    assert client.post(base + "/decide", json=decision(handoff, True, "early")).status_code == 409
    posted = client.post(base + "/submit", json=submission(handoff)).json()
    request = decision(posted, True, "receipt")
    assert client.post(base + "/decide", json={**request, "comment": "   "}).status_code == 422
    first = client.post(base + "/decide", json=request)
    second = client.post(base + "/decide", json=request)
    assert first.status_code == second.status_code == 200
    assert len(second.json()["events"]) == 2
    assert client.post(base + "/decide", json={**request, "accepted": False}).status_code == 409


def test_submit_rolls_back_contract_stage_tasks_and_status_if_event_fails(setup, monkeypatch):
    client, factory = setup
    customer_id = lead(client)["customer"]["id"]
    handoff = saved(client, customer_id)

    def fail_event(*args, **kwargs):
        raise ValueError("模拟事件持久化失败")

    monkeypatch.setattr(service, "_event", fail_event)
    response = client.post(f"/api/v1/customer-handoffs/{handoff['id']}/submit", json=submission(handoff))
    assert response.status_code == 409
    assert len(client.get("/api/v1/agents/investment/follow-ups").json()) == 1
    current = client.get("/api/v1/customer-handoffs").json()[0]
    assert current["status"] == "draft"
    assert current["revision"] == handoff["revision"]
    assert current["events"] == []
    with factory() as db:
        assert db.get(Customer, customer_id).stage == "lead"
