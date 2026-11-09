"""验证独立部署入口；临时目录、离线模型，访问保护在Caddy另行验收。"""

from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.deployment import create_app


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PARK_DEMO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PARK_MODEL_DAILY_LIMIT", "100")
    monkeypatch.setenv("PARK_MODEL_BUDGET_DB", str(tmp_path / "model-budget.sqlite3"))
    return tmp_path


def checked(client, method, url, payload, expected=201):
    response = client.request(method, url, json=payload)
    assert response.status_code == expected, response.text
    return response.json()


def test_deployed_rework_cycle_attachments_and_restart(data_dir):
    with TestClient(create_app()) as client:
        assert client.get("/health").json()["status"] == "ok"
        runtime = client.get("/api/v1/agents/runtime").json()
        assert runtime["environment"] == {"name": "portfolio_demo", "synthetic_data": True, "volatile": False}
        assert runtime["configured_mode"] == "rule"
        assert client.get("/docs").status_code == 404
        assert len(client.get("/api/v1/repair-personnel").json()) == 3
        content = BytesIO()
        Image.new("RGB", (8, 8), color="blue").save(content, format="PNG")
        uploaded = client.post("/api/v1/attachments/images?filename=demo.png&purpose=intake",
                               content=content.getvalue(), headers={"Content-Type": "image/png"})
        assert uploaded.status_code == 201, uploaded.text
        image = uploaded.json()
        ticket = checked(client, "POST", "/api/v1/tickets", {
            "location": "A栋模拟区", "issue_type": "漏水", "description": "展示测试漏水",
            "priority": "normal", "attachment_ids": [image["id"]],
        })
        path = f"/api/v1/tickets/{ticket['id']}"
        checked(client, "POST", path + "/assignments", {"personnel_no": "ENG-DEMO-002", "assigned_by": "演示负责人"})
        checked(client, "POST", path + "/accept", {"personnel_no": "ENG-DEMO-002"}, 200)
        checked(client, "POST", path + "/start-processing", {"personnel_no": "ENG-DEMO-002"}, 200)
        checked(client, "POST", path + "/repair-records", {
            "personnel_no": "ENG-DEMO-002", "repair_description": "模拟首次维修", "photo_urls": [],
        })
        checked(client, "POST", path + "/acceptance", {
            "accepted": False, "accepted_by": "演示验收员", "acceptance_comment": "模拟复验仍漏水，请返工",
        })
        assert client.get("/api/v1/tickets").json()[0]["status"] == "processing"
        checked(client, "POST", path + "/repair-records", {
            "personnel_no": "ENG-DEMO-002", "repair_description": "模拟再次维修", "photo_urls": [],
        })
        checked(client, "POST", path + "/acceptance", {
            "accepted": True, "accepted_by": "演示验收员", "acceptance_comment": "模拟复验通过",
        })
    # 创建全新应用/引擎，确保数据与图片没有依赖进程内状态。
    with TestClient(create_app()) as restarted:
        assert restarted.get("/api/v1/tickets").json()[0]["status"] == "completed"
        history = restarted.get(path + "/acceptance-records").json()
        assert [record["accepted"] for record in history] == [False, True]
        assert len(restarted.get(path + "/repair-records").json()) == 2
        assert len(restarted.get("/api/v1/repair-personnel").json()) == 3
        assert restarted.get(image["url"]).status_code == 200
        assert len(restarted.get(path + "/attachments").json()) == 1


def test_no_implicit_deployment_path(monkeypatch):
    monkeypatch.delenv("PARK_DEMO_DATA_DIR", raising=False)
    with pytest.raises(KeyError):
        create_app()


@pytest.mark.parametrize("setting,value", [("PARK_MODEL_DAILY_LIMIT", "0"), ("PARK_MODEL_PER_MINUTE_LIMIT", "0"),
                                           ("PARK_MODEL_DAILY_LIMIT", "abc")])
def test_invalid_budget_rejected_at_startup(data_dir, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(RuntimeError, match="正整数"):
        create_app()


def test_cannot_use_business_database_for_budget(data_dir, monkeypatch):
    monkeypatch.setenv("PARK_MODEL_BUDGET_DB", str(data_dir / "park.db"))
    with pytest.raises(RuntimeError):
        create_app()
