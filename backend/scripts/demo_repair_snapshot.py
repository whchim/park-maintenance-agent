"""只读导出旧内存演示的公开报修数据，恢复仅写入新的内存实例。

这不是正式数据库备份或迁移工具，不包含未通过API公开的内部字段和其他业务域。
旧实例必须保留，以便核对及回退；已提交维修结果或含其他业务数据时拒绝导出。
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen


def export_snapshot(source: str, workflow_ids: list[str]) -> dict:
    url = urlsplit(source)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password or url.path or url.query or url.fragment:
        raise ValueError("只能从本机127.0.0.1演示入口导出")

    def read(path):
        with urlopen(source + path, timeout=10) as response:
            return json.load(response)

    environment = read("/api/v1/agents/runtime").get("environment", {})
    if environment != {"name": "isolated_demo", "synthetic_data": True, "volatile": True}:
        raise ValueError("来源不是旧版隔离内存演示，拒绝操作")
    for path in ["/api/v1/agents/investment/customers", "/api/v1/agents/investment/follow-ups", "/api/v1/customer-handoffs"]:
        if read(path):
            raise ValueError("存在其他业务数据，不能使用限定报修快照升级")
    tickets = read("/api/v1/tickets")
    workflows = [read("/api/v1/agent/runs/" + workflow_id) for workflow_id in workflow_ids]
    if {item["ticket_id"] for item in workflows} != {item["id"] for item in tickets}:
        raise ValueError("需明确提供全部已有工单的流程ID")
    if len({item["workflow_id"] for item in workflows}) != len(workflows):
        raise ValueError("不能重复提供流程ID")
    events, assignments = [], []
    for ticket in tickets:
        if ticket["submitted_at"] or ticket["status"] not in {"pending", "accepted", "processing"}:
            raise ValueError("已有维修提交，公开接口不足以完整备份，保留原实例并停止升级")
        history = read(f"/api/v1/tickets/{ticket['id']}/events")
        if any(item["to_status"] in {"pending_review", "completed"} for item in history):
            raise ValueError("存在维修提交历史，不能使用此限定快照")
        events.extend(history)
        assignments.extend(read(f"/api/v1/tickets/{ticket['id']}/assignments"))
    if any(item.get("attachments") for item in workflows):
        raise ValueError("已有文件附件，不能用仅元数据的旧版快照恢复")
    # 旧流程接口的ticket包含updated_at，可按原值保留，不推断历史时间。
    ticket_rows = [item["ticket"] for item in workflows]
    snapshot = {"version": 1, "scope": "legacy_demo_repair_before_submission", "source": source,
                "captured_at": datetime.now().isoformat(), "personnel": read("/api/v1/repair-personnel"),
                "tickets": ticket_rows, "ticket_events": events, "assignments": assignments, "workflows": workflows}
    if read("/api/v1/tickets") != tickets:
        raise ValueError("导出时工单发生变化，请重新导出")
    for workflow in workflows:
        if read("/api/v1/agent/runs/" + workflow["workflow_id"]) != workflow:
            raise ValueError("导出时流程记录发生变化，请重新导出")
    return snapshot


def restore_snapshot(db, snapshot: dict) -> None:
    """只供run_demo在新建空内存库内调用；不连接来源或正式引擎。"""
    from sqlalchemy import DateTime
    from app.models.repair_personnel import RepairPersonnel
    from app.models.ticket import Ticket
    from app.models.ticket_assignment import TicketAssignment
    from app.models.ticket_event import TicketEvent
    from app.models.workflow_session import WorkflowSession
    from app.models.workflow_event import WorkflowEvent

    if snapshot.get("version") != 1 or snapshot.get("scope") != "legacy_demo_repair_before_submission":
        raise ValueError("不支持的演示快照格式")
    if db.query(Ticket).first() or db.query(RepairPersonnel).first():
        raise ValueError("只能恢复到新的空演示实例")

    def add_rows(model, rows):
        for source_row in rows:
            values = {}
            for column in model.__table__.columns:
                if column.name in source_row:
                    value = source_row[column.name]
                    if value is not None and isinstance(column.type, DateTime):
                        value = datetime.fromisoformat(value)
                    values[column.name] = value
            db.add(model(**values))
        db.flush()

    personnel = [{**row, "skills": json.dumps(row["skills"], ensure_ascii=False)} for row in snapshot["personnel"]]
    add_rows(RepairPersonnel, personnel)
    add_rows(Ticket, snapshot["tickets"])
    add_rows(TicketEvent, snapshot["ticket_events"])
    add_rows(TicketAssignment, snapshot["assignments"])
    for workflow in snapshot["workflows"]:
        add_rows(WorkflowSession, [{**workflow, "draft_json": json.dumps(workflow["draft"], ensure_ascii=False),
                                   "missing_fields_json": json.dumps(workflow["missing_fields"])}])
        add_rows(WorkflowEvent, [{**row, "payload_json": json.dumps(row["payload"], ensure_ascii=False)}
                                 for row in workflow["events"]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--workflow", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = export_snapshot(args.source, args.workflow)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(snapshot, output, ensure_ascii=False, indent=2)
    print(f"saved {len(snapshot['tickets'])} demo ticket(s); source unchanged")


if __name__ == "__main__":
    main()
