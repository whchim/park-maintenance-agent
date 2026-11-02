"""固定模拟资料的 Agent 验收，不创建 Session、不连接或修改业务数据库。

默认强制离线；只有显式 --online 且本机配置就绪才调用真实千问。
验收不覆盖人工建单、正式分派、完整业务闭环或图检查点恢复。
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


# 允许从任意目录用绝对脚本路径运行，配置仍由 app 从固定 backend/.env 加载。
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def run_checks(*, online: bool = False) -> tuple[dict, int]:
    previous_mode = os.environ.get("PARK_AI_MODE")
    if not online:
        os.environ["PARK_AI_MODE"] = "rule"
    try:
        return _run_checks(online=online)
    finally:
        if not online:
            if previous_mode is None:
                os.environ.pop("PARK_AI_MODE", None)
            else:
                os.environ["PARK_AI_MODE"] = previous_mode


def _run_checks(*, online: bool) -> tuple[dict, int]:
    # 延迟导入，确保离线开关早于 app 配置加载；不导入 main 或业务 API。
    from app.agents.engineering import run_engineering
    from app.agents.repair_intake import run_intake
    from app.services.llm_service import runtime_status

    configuration = runtime_status()
    report = {
        "report_version": "agent-acceptance-v1",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "requested_mode": "online" if online else "offline",
        "configuration": configuration,
        "synthetic_data": True,
        "database_access": False,
        "cases": [],
        "status": "not_run",
        "human_review_required": True,
        "scope": "企服缺项分支、补充后的人工门禁、工程只读工具建议；不覆盖业务写入和语义质量终审",
    }
    if online and not configuration["ready"]:
        report.update(status="configuration_not_ready", reason=configuration["reason"])
        return report, 2

    expected_mode = "llm" if online else "rule_fallback_demo"

    def record(name: str, result: dict, checks: dict) -> bool:
        checks = {"expected_execution_mode": result["mode"] == expected_mode, **checks}
        case = {"name": name, "passed": all(checks.values()), "checks": checks, "result": result}
        report["cases"].append(case)
        if not case["passed"]:
            report["status"] = "checks_failed"
        return case["passed"]

    first = run_intake("漏水").model_dump()
    if not record("企服：缺少位置时追问", first, {
        "missing_location": "location" in first["missing_fields"],
        "question_present": bool(first["follow_up_question"]),
        "waits_for_input": first["orchestration"]["steps"][-1]["node"] == "wait_for_input",
    }):
        return report, 1

    supplemented = run_intake("漏水；位置在A栋302", "位置在A栋302").model_dump()
    if not record("企服：补齐信息后等待人工确认", supplemented, {
        "location_from_fixture": supplemented["location"] == "A栋302",
        "issue_present": bool(supplemented["issue_type"]),
        "no_missing_fields": supplemented["missing_fields"] == [],
        "no_extra_question": supplemented["follow_up_question"] == "",
        "waits_for_human": supplemented["orchestration"]["steps"][-1]["node"] == "wait_for_confirmation",
    }):
        return report, 1

    # 明确虚构人员与任务计数，绝不从现有工单或真实人员库取数据发送给模型。
    number = "ENG-VERIFY-001"
    suggestion = run_engineering(
        {"ticket_id": "VERIFY-ONLY", "location": "A栋302", "issue_type": "漏水",
         "description": "固定验收用例：水管漏水（模拟资料）", "priority": "normal"},
        [{"personnel_no": number, "display_name": "验收模拟水电员", "skills": ["漏水", "水电"], "responsible_area": "A栋"}],
        {number: 2},
        {"mode": "rule_fallback_demo", "recommended_personnel": number, "recommended_name": "验收模拟水电员",
         "reason": "固定模拟目录的规则基线，请人工核对", "requires_human_confirmation": True},
    )
    trace = suggestion["orchestration"]
    checks = {
        "known_candidate_or_none": suggestion["recommended_personnel"] in (None, number),
        "human_confirmation_required": suggestion["requires_human_confirmation"] is True,
        "bounded_calls": trace["model_calls"] <= 3 and trace["tool_calls"] <= 4,
    }
    if online:
        checks.update(
            queried_directory=any(step.get("tool") == "list_engineers" and step["status"] == "completed" for step in trace["steps"]),
            read_tool_result=2 <= trace["model_calls"] <= 3,
            waits_for_human=trace["stop_reason"] == "waiting_human",
        )
    else:
        checks.update(no_model_request=trace["model_calls"] == 0, disabled_reason=suggestion["ai"]["fallback_reason"] == "disabled")
    if not record("工程：查询证据后仅建议，等待人工分派", suggestion, checks):
        return report, 1
    report["status"] = "online_checks_passed" if online else "offline_checks_passed"
    return report, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="使用本机配置进行真实千问调用，最多5次请求；失败立即停止")
    parser.add_argument("--output", type=Path, help="保存 UTF-8 JSON 验收报告；只创建新 .json 文件，不覆盖已有文件")
    args = parser.parse_args()
    if args.output and (args.output.suffix.lower() != ".json" or args.output.exists()):
        parser.error("报告路径必须是尚不存在的 .json 文件")
    try:
        report, code = run_checks(online=args.online)
    except Exception:
        # 不输出原始异常、供应商正文或包含本机配置的 traceback。
        report, code = {"status": "unexpected_error", "human_review_required": True}, 1
    if args.output:
        try:
            # x 模式防止参数检查后出现同名文件时覆盖；不创建或改动任何配置文件。
            with args.output.open("x", encoding="utf-8") as target:
                json.dump(report, target, ensure_ascii=False, indent=2)
        except OSError:
            print(json.dumps({"status": "report_write_failed", "check_status": report["status"]}))
            return 1
        summary = {"status": report["status"], "cases": [
            {"name": case["name"], "passed": case["passed"], "mode": case["result"]["mode"],
             "fallback_reason": case["result"].get("ai", {}).get("fallback_reason")}
            for case in report.get("cases", [])
        ]}
        print(json.dumps(summary, ensure_ascii=True, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
