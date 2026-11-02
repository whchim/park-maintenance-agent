"""千问适配器和人工门禁的离线验证：不发送真实请求，不访问现有数据库。"""

import json
from urllib.error import HTTPError, URLError

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.customer import Customer
from app.models.repair_personnel import RepairPersonnel
from app.models.ticket_assignment import TicketAssignment
from app.schemas.department_ai import ServiceReview
from app.services import llm_service
from app.services.department_agent_service import review_repair


@pytest.fixture
def qwen(monkeypatch, offline_model_by_default):
    monkeypatch.setenv("PARK_AI_MODE", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-never-send")
    monkeypatch.setenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("QWEN_MODEL", "qwen-plus")
    monkeypatch.setenv("QWEN_TIMEOUT_SECONDS", "15")


@pytest.fixture
def memory_api():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)

    def dependency():
        with factory() as db:
            yield db

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = dependency
    try:
        with TestClient(app) as client:
            yield client, factory
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        Base.metadata.drop_all(engine)
        engine.dispose()


def completion(output, finish="stop", **message):
    return {"id": "test-request-1", "choices": [{"finish_reason": finish,
            "message": {"content": json.dumps(output, ensure_ascii=False), **message}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}}


def fact(value):
    return {"value": value, "evidence": value}


def tool_completion(name="list_engineers", arguments=None, call_id="call_directory"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments or {}),
            },
        }],
    }}], "usage": {"total_tokens": 10}}


def test_disabled_missing_key_and_invalid_endpoint_never_call_network(monkeypatch):
    def forbidden(*args):
        pytest.fail("缺少配置或配置不安全时不应访问网络")
    monkeypatch.setattr(llm_service, "_post_completion", forbidden)
    assert review_repair("A栋漏水").ai["fallback_reason"] == "disabled"
    monkeypatch.setenv("PARK_AI_MODE", "qwen")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert review_repair("A栋漏水").ai["fallback_reason"] == "missing_api_key"
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-never-send")
    monkeypatch.setenv("QWEN_BASE_URL", "https://another-provider.example/compatible-mode/v1")
    assert review_repair("A栋漏水").ai["fallback_reason"] == "invalid_configuration"
    assert "test-key-never-send" not in json.dumps(llm_service.runtime_status())


def test_grounded_repair_extracts_non_keyword_issue_and_keeps_raw_description(qwen, monkeypatch):
    text = "A栋302的顶灯忽明忽暗，请安排检查"
    monkeypatch.setattr(llm_service, "_post_completion", lambda *_: completion({
        "location": fact("A栋302"), "issue_type": fact("顶灯忽明忽暗"), "follow_up_question": "",
    }))
    result = review_repair(text)
    assert result.mode == "llm"
    assert result.location == "A栋302"
    assert result.issue_type == "顶灯忽明忽暗"
    assert result.description == text
    assert result.missing_fields == []
    assert result.ai["usage"]["total_tokens"] == 50
    assert result.evidence["location"] in text


@pytest.mark.parametrize("response", [
    completion({"location": fact("B栋999"), "issue_type": fact("漏水"), "follow_up_question": ""}),
    completion({"location": fact("A栋"), "issue_type": fact("漏水"), "follow_up_question": "", "status": "approved"}),
    completion({"location": fact("A栋"), "issue_type": fact("漏水"), "follow_up_question": ""}, finish="length"),
    completion({"location": fact("A栋"), "issue_type": fact("漏水"), "follow_up_question": ""}, tool_calls=[{"name": "write_database"}]),
    {"choices": []},
    {"choices": [{"finish_reason": "stop", "message": {"content": "不是JSON"}}]},
])
def test_invalid_or_ungrounded_output_is_explicitly_rejected(qwen, monkeypatch, response):
    monkeypatch.setattr(llm_service, "_post_completion", lambda *_: response)
    result = review_repair("A栋漏水")
    assert result.mode == "rule_fallback_demo"
    assert result.ai["fallback_reason"] == "invalid_output"
    assert result.location == "A栋"
    assert result.evidence == {}


@pytest.mark.parametrize("error,reason", [
    (TimeoutError("must-not-expose-test-key-never-send"), "timeout"),
    (URLError("must-not-expose-test-key-never-send"), "network_error"),
    (HTTPError("https://example.invalid", 401, "test-key-never-send", {}, None), "authentication_failed"),
    (HTTPError("https://example.invalid", 429, "test-key-never-send", {}, None), "rate_limited"),
    (HTTPError("https://example.invalid", 503, "test-key-never-send", {}, None), "provider_http_error"),
])
def test_model_failure_falls_back_without_disclosing_provider_body(qwen, monkeypatch, error, reason):
    def fail(*args):
        raise error
    monkeypatch.setattr(llm_service, "_post_completion", fail)
    result = review_repair("A栋漏水")
    assert result.mode == "rule_fallback_demo"
    assert result.ai["fallback_reason"] == reason
    assert "test-key-never-send" not in result.model_dump_json()


def test_transport_uses_qwen_json_contract_and_does_not_follow_redirects(qwen, monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return json.dumps(completion({"location": None, "issue_type": None, "follow_up_question": "请补充位置和故障"})).encode()

    class Opener:
        def open(self, request, timeout):
            seen.update(url=request.full_url, authorization=request.get_header("Authorization"),
                        body=json.loads(request.data), timeout=timeout)
            return Response()

    monkeypatch.setattr(llm_service, "build_opener", lambda handler: Opener())
    output, metadata = llm_service.run_structured("service_agent", "检查缺项", {"message": "请帮忙"}, ServiceReview)
    assert output.location is None
    assert metadata["mode"] == "llm"
    assert seen["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert seen["authorization"] == "Bearer test-key-never-send"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["enable_thinking"] is False
    assert seen["timeout"] == 15
    assert llm_service._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid") is None


def test_repair_restores_all_supplements_and_waits_for_human_confirmation(qwen, memory_api, monkeypatch):
    client, factory = memory_api
    conversations = []

    def respond(settings, body):
        conversation = json.loads(body["messages"][1]["content"])["conversation"]
        conversations.append(conversation)
        location = fact("A栋302") if "A栋302" in conversation else None
        issue = fact("顶灯忽明忽暗") if "顶灯忽明忽暗" in conversation else None
        return completion({"location": location, "issue_type": issue, "follow_up_question": "请描述故障现象"})

    monkeypatch.setattr(llm_service, "_post_completion", respond)
    response = client.post("/api/v1/agent/runs", json={"message": "想报修", "idempotency_key": "start"})
    assert response.status_code == 200
    workflow = response.json()
    assert workflow["draft"]["orchestration"]["steps"][-1]["node"] == "wait_for_input"
    base = f"/api/v1/agent/runs/{workflow['workflow_id']}"
    response = client.post(base + "/messages", json={"message": "位置在A栋302", "idempotency_key": "location"})
    assert response.status_code == 200
    assert response.json()["status"] == "waiting_input"
    request = {"message": "顶灯忽明忽暗", "idempotency_key": "issue"}
    response = client.post(base + "/messages", json=request)
    assert response.status_code == 200
    assert response.json()["status"] == "waiting_human"
    assert response.json()["draft"]["orchestration"]["steps"][-1]["node"] == "wait_for_confirmation"
    assert response.json()["draft"]["location"] == "A栋302"
    assert response.json()["draft"]["follow_up_question"] == ""
    assert "位置在A栋302" in conversations[-1]
    assert client.get("/api/v1/tickets").json() == []
    assert client.post(base + "/messages", json=request).status_code == 200
    assert len(conversations) == 3  # 重试不重复消耗模型调用。
    events = response.json()["events"]
    assert [event for event in events if event["event_type"] == "repair.intake.reviewed"][-1]["payload"]["ai"]["mode"] == "llm"
    confirmed = client.post(base + "/confirm-ticket", json={
        "location": "A栋302", "issue_type": "顶灯忽明忽暗", "description": conversations[-1],
        "priority": "normal", "idempotency_key": "confirm",
    })
    assert confirmed.status_code == 200
    assert confirmed.json()["ticket_id"] is not None
    assert len(conversations) == 3  # 建单写事务不等待工程模型。
    with factory() as db:
        assert db.query(TicketAssignment).count() == 0


def test_investment_intake_is_grounded_draft_and_never_creates_customer(qwen, memory_api, monkeypatch):
    client, factory = memory_api
    monkeypatch.setattr(llm_service, "_post_completion", lambda *_: completion({
        "customer_name": fact("海星科技"), "contact_name": fact("李先生"), "contact_phone": None,
        "intent_level": "B", "intent_reason": "客户仍在比较园区", "follow_up_subject": "跟进客户选址需求",
    }))
    response = client.post("/api/v1/agents/investment/intake", json={
        "message": "海星科技的李先生仍在比较园区", "source": "渠道推荐",
    })
    assert response.status_code == 200
    assert response.json()["mode"] == "llm"
    assert response.json()["customer_name"] == "海星科技"
    assert response.json()["contact_phone"] is None
    assert response.json()["follow_up_interval_days"] == 3
    assert response.json()["source"] == "渠道推荐"
    with factory() as db:
        assert db.query(Customer).count() == 0


@pytest.mark.parametrize("number,expected_mode", [("ENG-001", "llm"), ("INVENTED-999", "rule_fallback_demo")])
def test_engineering_suggestion_cannot_invent_personnel_or_assign(qwen, memory_api, monkeypatch, number, expected_mode):
    client, factory = memory_api
    with factory() as db:
        db.add(RepairPersonnel(personnel_no="ENG-001", display_name="水电演示员", department="工程部",
                               role="维修人员", skills='["漏水"]', responsible_area="A栋"))
        db.commit()
    created = client.post("/api/v1/tickets", json={"location": "A栋302", "issue_type": "漏水",
                          "description": "水龙头漏水", "priority": "normal"})
    assert created.status_code == 201
    ticket_id = created.json()["id"]
    def respond(settings, body):
        if not any(message["role"] == "tool" for message in body["messages"]):
            return tool_completion()
        return completion({"recommended_personnel": number,
                           "reason": "目录技能包含漏水且区域为A栋；请负责人核对"})
    monkeypatch.setattr(llm_service, "_post_completion", respond)
    response = client.post(f"/api/v1/tickets/{ticket_id}/dispatch-suggestion")
    assert response.status_code == 200
    assert response.json()["mode"] == expected_mode
    assert response.json()["recommended_personnel"] == "ENG-001"
    assert response.json()["requires_human_confirmation"] is True
    assert response.json()["orchestration"]["tool_calls"] == 1
    with factory() as db:
        assert db.query(TicketAssignment).count() == 0


def test_runtime_reports_configuration_without_claiming_connectivity(qwen, memory_api):
    client, _ = memory_api
    response = client.get("/api/v1/agents/runtime")
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["connectivity_verified"] is False
    assert "test-key-never-send" not in response.text
    capabilities = client.get("/api/v1/agents/capabilities").json()
    statuses = {item["agent_name"]: item["status"] for item in capabilities}
    assert statuses["service_agent"] == "llm_configured_not_verified"
    assert statuses["supervisor_agent"] == "implemented_rule_fallback"


@pytest.fixture
def engineering_inputs():
    return {
        "context": {"ticket_id": 42, "location": "A栋302", "issue_type": "漏水", "description": "水管漏水", "priority": "normal"},
        "personnel": [{"personnel_no": "ENG-001", "display_name": "工程演示员", "skills": ["漏水"], "responsible_area": "A栋"}],
        "open_assignments": {"ENG-001": 2},
        "rules": {"mode": "rule_fallback_demo", "recommended_personnel": "ENG-001", "reason": "规则技能排序",
                  "requires_human_confirmation": True},
    }


@pytest.mark.parametrize("first_tool", ["list_engineers", "get_open_assignments"])
def test_engineering_model_selects_tool_order_and_reads_results(qwen, monkeypatch, engineering_inputs, first_tool):
    from app.agents.engineering import run_engineering
    seen, tool_order = [], [first_tool, "get_open_assignments" if first_tool == "list_engineers" else "list_engineers"]

    def respond(settings, body):
        seen.append(body)
        observations = [json.loads(message["content"]) for message in body["messages"] if message["role"] == "tool"]
        if len(observations) < 2:
            tool = tool_order[len(observations)]
            return tool_completion(tool, {"personnel_no": "ENG-001"} if tool == "get_open_assignments" else {},
                                   call_id=f"call_{len(observations)}")
        assert any(result.get("open_assignments") == 2 for result in observations)
        assert any(result.get("personnel", [{}])[0].get("personnel_no") == "ENG-001" for result in observations)
        assert body["response_format"] == {"type": "json_object"}
        assert "tools" not in body and "tool_choice" not in body
        return completion({"recommended_personnel": "ENG-001", "reason": "目录技能符合；系统记录2单未结束，仍需负责人确认"})

    monkeypatch.setattr(llm_service, "_post_completion", respond)
    result = run_engineering(**engineering_inputs)
    assert result["mode"] == "llm"
    assert result["orchestration"]["model_calls"] == 3
    assert result["orchestration"]["tool_calls"] == 2
    assert [step["tool"] for step in result["orchestration"]["steps"] if "tool" in step] == tool_order
    assert result["ai"]["usage"]["total_tokens"] == 70
    assert result["requires_human_confirmation"] is True
    assert result["explanation_source"] == "tool_snapshot"
    assert "本次查询的未结束分派数：2 单" in result["reason"]
    assert "personnel" not in json.loads(seen[0]["messages"][1]["content"])
    assert seen[0]["tool_choice"] == "auto"
    assert "response_format" not in seen[0]  # 工具轮不混用旧的一次 JSON 草稿协议。


@pytest.mark.parametrize("response", [
    tool_completion("write_database", {"sql": "DROP TABLE tickets"}),
    tool_completion("list_engineers", {"include_inactive": True}),
    tool_completion("get_open_assignments", {"personnel_no": "INVENTED-999"}),
    tool_completion("get_open_assignments", {"personnel_no": 123}),
])
def test_engineering_rejects_unknown_tools_and_invalid_arguments(qwen, monkeypatch, engineering_inputs, response):
    from app.agents.engineering import run_engineering
    monkeypatch.setattr(llm_service, "_post_completion", lambda *_: response)
    result = run_engineering(**engineering_inputs)
    assert result["mode"] == "rule_fallback_demo"
    assert result["ai"]["fallback_reason"] == "invalid_tool_call"
    assert result["orchestration"]["tool_calls"] == 0
    assert result["recommended_personnel"] == "ENG-001"
    assert "DROP TABLE" not in json.dumps(result)


def test_engineering_rejects_answer_without_query_evidence(qwen, monkeypatch, engineering_inputs):
    from app.agents.engineering import run_engineering
    monkeypatch.setattr(llm_service, "_post_completion", lambda *_: completion({
        "recommended_personnel": "ENG-001", "reason": "我已经查过资料", "status": "assigned",
    }))
    result = run_engineering(**engineering_inputs)
    assert result["ai"]["fallback_reason"] == "invalid_output"
    monkeypatch.setattr(llm_service, "_post_completion", lambda *_: completion({
        "recommended_personnel": "ENG-001", "reason": "我已经查过资料",
    }))
    result = run_engineering(**engineering_inputs)
    assert result["ai"]["fallback_reason"] == "missing_tool_evidence"
    assert result["mode"] == "rule_fallback_demo"


def test_engineering_repeated_queries_hit_budget(qwen, monkeypatch, engineering_inputs):
    from app.agents.engineering import run_engineering
    calls = []

    def respond(settings, body):
        calls.append(body)
        return tool_completion(call_id=f"call_{len(calls)}")

    monkeypatch.setattr(llm_service, "_post_completion", respond)
    result = run_engineering(**engineering_inputs)
    assert len(calls) == 3
    assert result["ai"]["fallback_reason"] == "budget_exceeded"
    assert result["orchestration"]["tool_calls"] == 2
    assert result["requires_human_confirmation"] is True


def test_engineering_duplicate_call_ids_are_rejected(qwen, monkeypatch, engineering_inputs):
    from app.agents.engineering import run_engineering
    monkeypatch.setattr(llm_service, "_post_completion", lambda *_: tool_completion())
    result = run_engineering(**engineering_inputs)
    assert result["ai"]["fallback_reason"] == "invalid_tool_call"
    assert result["orchestration"]["model_calls"] == 2
    assert result["orchestration"]["tool_calls"] == 1


def test_engineering_offline_does_not_call_model(monkeypatch, engineering_inputs):
    from app.agents.engineering import run_engineering
    monkeypatch.setattr(llm_service, "_post_completion", lambda *_: pytest.fail("离线模式不得联网"))
    result = run_engineering(**engineering_inputs)
    assert result["ai"]["fallback_reason"] == "disabled"
    assert result["orchestration"]["model_calls"] == 0
    assert result["orchestration"]["tool_calls"] == 0


def test_engineering_timeout_after_query_keeps_auditable_fallback(qwen, monkeypatch, engineering_inputs):
    from app.agents.engineering import run_engineering

    def respond(settings, body):
        if any(message["role"] == "tool" for message in body["messages"]):
            raise TimeoutError("test-key-never-send")
        return tool_completion()

    monkeypatch.setattr(llm_service, "_post_completion", respond)
    result = run_engineering(**engineering_inputs)
    assert result["ai"]["fallback_reason"] == "timeout"
    assert result["orchestration"]["tool_calls"] == 1
    assert result["ai"]["usage"]["total_tokens"] == 10
    assert "test-key-never-send" not in json.dumps(result)


def test_engineering_empty_directory_allows_no_candidate(qwen, monkeypatch, engineering_inputs):
    from app.agents.engineering import run_engineering
    engineering_inputs.update(personnel=[], open_assignments={})

    def respond(settings, body):
        if not any(message["role"] == "tool" for message in body["messages"]):
            return tool_completion()
        return completion({"recommended_personnel": None, "reason": "当前目录为空，请工程负责人核对名单"})

    monkeypatch.setattr(llm_service, "_post_completion", respond)
    result = run_engineering(**engineering_inputs)
    assert result["recommended_personnel"] is None
    assert result["mode"] == "llm"


def test_engineering_display_does_not_turn_zero_tasks_into_availability(qwen, monkeypatch, engineering_inputs):
    from app.agents.engineering import run_engineering
    engineering_inputs["open_assignments"] = {"ENG-001": 0}
    calls = []

    def respond(settings, body):
        calls.append(body)
        if len(calls) == 1:
            return tool_completion()
        if len(calls) == 2:
            return tool_completion("get_open_assignments", {"personnel_no": "ENG-001"}, "call_load")
        return completion({"recommended_personnel": "ENG-001", "reason": "任务0单，所以人员空闲且具备处理条件"})

    monkeypatch.setattr(llm_service, "_post_completion", respond)
    result = run_engineering(**engineering_inputs)
    assert result["mode"] == "llm"
    assert "本次查询的未结束分派数：0 单" in result["reason"]
    assert "人员空闲" not in result["reason"]
    assert "具备处理条件" not in result["reason"]
    assert "不代表实际负载" in result["reason"]


def test_engineering_display_does_not_claim_unqueried_counts(qwen, monkeypatch, engineering_inputs):
    from app.agents.engineering import run_engineering

    def respond(settings, body):
        if not any(message["role"] == "tool" for message in body["messages"]):
            return tool_completion()
        return completion({"recommended_personnel": "ENG-001", "reason": "该人员当前未结束工单2单"})

    monkeypatch.setattr(llm_service, "_post_completion", respond)
    result = run_engineering(**engineering_inputs)
    assert "本次未查询该人员的未结束分派数" in result["reason"]
    assert "2 单" not in result["reason"]


def test_engineering_snapshot_excludes_finished_and_historical_assignments(qwen, memory_api, monkeypatch):
    from app.models.ticket import Ticket
    client, factory = memory_api
    with factory() as db:
        person = RepairPersonnel(personnel_no="ENG-001", display_name="工程演示员", department="工程部",
                                 role="维修人员", skills='["漏水"]', responsible_area="A栋")
        db.add(person)
        db.flush()
        for index, (status, current) in enumerate([("processing", True), ("completed", True), ("accepted", False)]):
            ticket = Ticket(ticket_no=f"snapshot-{index}", location="A栋", issue_type="漏水",
                            description="水管漏水", priority="normal", status=status)
            db.add(ticket)
            db.flush()
            db.add(TicketAssignment(ticket_id=ticket.id, personnel_id=person.id, assigned_by="demo", is_current=current))
        db.commit()
    ticket = client.post("/api/v1/tickets", json={"location": "A栋302", "issue_type": "漏水", "description": "水管漏水"}).json()
    observed = []

    def respond(settings, body):
        observations = [json.loads(message["content"]) for message in body["messages"] if message["role"] == "tool"]
        if not observations:
            return tool_completion()
        if len(observations) == 1:
            return tool_completion("get_open_assignments", {"personnel_no": "ENG-001"}, "call_tasks")
        observed.append(observations[-1]["open_assignments"])
        return completion({"recommended_personnel": "ENG-001", "reason": "技能匹配，系统记录1单未结束，请人工核对"})

    monkeypatch.setattr(llm_service, "_post_completion", respond)
    response = client.post(f"/api/v1/tickets/{ticket['id']}/dispatch-suggestion")
    assert response.status_code == 200
    assert response.json()["mode"] == "llm"
    assert observed == [1]
    with factory() as db:
        assert db.query(TicketAssignment).count() == 3
        assert db.get(Ticket, ticket["id"]).status == "pending"


def test_intake_handoff_and_engineering_graph_keep_separate_human_gates(qwen, memory_api, monkeypatch):
    from app.models.ticket import Ticket
    client, factory = memory_api
    calls = []
    with factory() as db:
        db.add(RepairPersonnel(personnel_no="ENG-001", display_name="空调演示员", department="工程部",
                               role="维修人员", skills='["空调"]', responsible_area="A栋"))
        db.commit()

    def respond(settings, body):
        calls.append(body)
        if "tools" in body:
            if not any(message["role"] == "tool" for message in body["messages"]):
                return tool_completion()
            return completion({"recommended_personnel": "ENG-001", "reason": "空调技能与A栋区域匹配，请工程负责人确认"})
        text = json.loads(body["messages"][1]["content"])["conversation"]
        return completion({"location": fact("A栋302") if "A栋302" in text else None,
                           "issue_type": fact("空调一直滴水"), "follow_up_question": "请补充具体位置"})

    monkeypatch.setattr(llm_service, "_post_completion", respond)
    started = client.post("/api/v1/agent/runs", json={"message": "空调一直滴水", "idempotency_key": "chain-start"}).json()
    base = f"/api/v1/agent/runs/{started['workflow_id']}"
    assert started["status"] == "waiting_input"
    assert client.post(base + "/confirm-ticket", json={"location": "A栋302", "issue_type": "空调一直滴水",
           "description": "空调一直滴水", "priority": "normal", "idempotency_key": "too-early"}).status_code == 409
    supplemented = client.post(base + "/messages", json={"message": "A栋302", "idempotency_key": "chain-location"}).json()
    assert supplemented["status"] == "waiting_human"
    reloaded = client.get(base).json()
    assert reloaded["draft"]["orchestration"]["steps"][-1]["node"] == "wait_for_confirmation"
    with factory() as db:
        assert db.query(Ticket).count() == 0
    payload = {"location": "A栋302", "issue_type": "空调一直滴水", "description": "空调一直滴水；A栋302",
               "priority": "normal", "idempotency_key": "chain-confirm"}
    confirmed = client.post(base + "/confirm-ticket", json=payload).json()
    ticket_id = confirmed["ticket_id"]
    assert ticket_id is not None
    assert len(calls) == 2  # 人工建单事务不执行工程模型。
    assert any(event["event_type"] == "repair.assignment.requested" for event in confirmed["events"])
    assert client.post(base + "/confirm-ticket", json=payload).json()["ticket_id"] == ticket_id
    suggestion = client.post(f"/api/v1/tickets/{ticket_id}/dispatch-suggestion").json()
    assert suggestion["mode"] == "llm"
    assert suggestion["orchestration"]["stop_reason"] == "waiting_human"
    with factory() as db:
        assert db.query(Ticket).count() == 1
        assert db.query(TicketAssignment).count() == 0
        assert db.get(Ticket, ticket_id).status == "pending"


@pytest.mark.parametrize("response", [
    tool_completion() | {"choices": [{"finish_reason": "length", "message": {"tool_calls": []}}]},
    {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"type": "function"}]}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "{broken-json"}}]},
])
def test_malformed_engineering_protocol_falls_back(qwen, monkeypatch, engineering_inputs, response):
    from app.agents.engineering import run_engineering
    monkeypatch.setattr(llm_service, "_post_completion", lambda *_: response)
    result = run_engineering(**engineering_inputs)
    assert result["mode"] == "rule_fallback_demo"
    assert result["ai"]["fallback_reason"] == "invalid_output"
    assert result["orchestration"]["tool_calls"] == 0
