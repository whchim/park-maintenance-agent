"""本机容器验收；随机临时口令/端口/数据卷，结束只清理本次测试资源。"""

import base64
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import secrets
import socket
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / "backend" / ".local"


def command(args, *, stdin=None, check=True):
    result = subprocess.run(args, cwd=ROOT, input=stdin, text=True, encoding="utf-8", errors="replace",
                            capture_output=True, timeout=240)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr[-2500:]}")
    return result


def main():
    LOCAL.mkdir(exist_ok=True)
    project = "park-verify-" + secrets.token_hex(5)
    password = secrets.token_urlsafe(24)
    # 仅本机一次性随机测试密码，既不是部署口令也不写入报告；实际配置使用交互输入。
    hashed = command(["docker", "run", "--rm", "park-portfolio-gateway:verify", "caddy", "hash-password",
                      "--bcrypt-cost", "4", "--plaintext", password]).stdout.strip()
    assert re.fullmatch(r"\$2[aby]\$04\$[./A-Za-z0-9]{53}", hashed), "test hash generation failed"
    # 低成本哈希只用于本次本机临时验收，生产口令应使用更高成本。
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    report = {"project": project, "checked_at": datetime.now(timezone.utc).isoformat(), "checks": []}
    with TemporaryDirectory(prefix="deploy-check-", dir=LOCAL) as temporary:
        directory = Path(temporary)
        env = directory / "test.env"
        env.write_text(f"DEMO_PASSWORD_HASH='{hashed}'\nDEMO_USER=demo\nDEMO_HTTP_PORT={port}\n"
                       "PARK_AI_MODE=rule\nDASHSCOPE_API_KEY=\nDEMO_DOMAIN=demo.example.invalid\n", encoding="utf-8")
        override = directory / "images.yaml"
        override.write_text("services:\n  backend:\n    image: park-portfolio-backend:verify\n"
                            "  gateway:\n    image: park-portfolio-gateway:verify\n", encoding="utf-8")
        compose = ["docker", "compose", "--project-name", project, "--env-file", str(env),
                   "-f", str(ROOT / "compose.yaml"), "-f", str(override)]
        token = base64.b64encode(("demo:" + password).encode()).decode()
        url = f"http://127.0.0.1:{port}"

        def http(path, *, method="GET", payload=None, raw=None, authenticated=True, expected=200, headers=None):
            request_headers = {"Authorization": "Basic " + token} if authenticated else {}
            if payload is not None:
                raw = json.dumps(payload).encode()
                request_headers["Content-Type"] = "application/json"
            request_headers.update(headers or {})
            request = Request(url + path, data=raw, headers=request_headers, method=method)
            try:
                response = urlopen(request, timeout=15)
            except HTTPError as exc:
                response = exc
            with response:
                body = response.read()
                assert response.status == expected, f"{method} {path}: {response.status}, {body[:300]!r}"
                return json.loads(body) if response.headers.get("content-type", "").startswith("application/json") else body

        def ready():
            deadline = time.monotonic() + 60
            last_error = ""
            while time.monotonic() < deadline:
                try:
                    if http("/health")["status"] == "ok":
                        return
                except (URLError, AssertionError, TimeoutError) as exc:
                    last_error = str(exc)
                    time.sleep(1)
            raise RuntimeError("temporary deployment did not become healthy: " + last_error[:300])

        try:
            # 配置只读解析，绝不打印包含环境变量的完整配置。
            configuration = json.loads(command(compose + ["config", "--format", "json"]).stdout)
            assert not configuration["services"]["backend"].get("ports"), "backend must not publish ports"
            assert configuration["services"]["gateway"]["ports"][0]["host_ip"] == "127.0.0.1"
            command(compose + ["-f", str(ROOT / "deploy" / "compose.public.yaml"), "config", "--quiet"])
            command(compose + ["run", "--rm", "--no-deps", "gateway", "caddy", "validate", "--config", "/etc/caddy/Caddyfile"])
            report["checks"].append("compose_private_backend_and_caddy_config")
            command(compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "90"])
            ready()
            for path in ["/", "/api/v1/tickets", "/api/v1/attachments/unknown/content", "/health", "/docs", "/missing"]:
                http(path, authenticated=False, expected=401)
            wrong = base64.b64encode(b"demo:wrong-test-password").decode()
            http("/api/v1/tickets", headers={"Authorization": "Basic " + wrong}, expected=401)
            report["checks"].append("all_gateway_paths_require_password")
            page = http("/")
            assert b'<div id="app"></div>' in page
            for asset in re.findall(rb'(?:src|href)="(/assets/[^\"]+)"', page):
                http(asset.decode())
                http(asset.decode(), authenticated=False, expected=401)
            runtime = http("/api/v1/agents/runtime")
            assert runtime["environment"]["name"] == "portfolio_demo" and runtime["configured_mode"] == "rule"
            assert http("/api/v1/tickets") == []
            assert http("/api/v1/agents/investment/customers") == []
            report["checks"].append("frontend_assets_and_nested_api_routes")
            image_bytes = io.BytesIO()
            Image.new("RGB", (8, 8), "blue").save(image_bytes, "PNG")
            image = http("/api/v1/attachments/images?filename=demo.png&purpose=intake", method="POST",
                         raw=image_bytes.getvalue(), headers={"Content-Type": "image/png"}, expected=201)
            ticket = http("/api/v1/tickets", method="POST", expected=201, payload={
                "location": "A栋模拟区", "issue_type": "漏水", "description": "部署验证模拟报修", "priority": "normal",
                "attachment_ids": [image["id"]],
            })
            path = f"/api/v1/tickets/{ticket['id']}"
            http(path + "/assignments", method="POST", expected=201,
                 payload={"personnel_no": "ENG-DEMO-002", "assigned_by": "模拟负责人"})
            http(path + "/accept", method="POST", payload={"personnel_no": "ENG-DEMO-002"})
            http(path + "/start-processing", method="POST", payload={"personnel_no": "ENG-DEMO-002"})
            for accepted in [False, True]:
                http(path + "/repair-records", method="POST", expected=201, payload={
                    "personnel_no": "ENG-DEMO-002", "repair_description": "部署模拟维修", "photo_urls": [],
                })
                http(path + "/acceptance", method="POST", expected=201, payload={
                    "accepted": accepted, "accepted_by": "模拟验收员",
                    "acceptance_comment": "模拟验收通过" if accepted else "模拟复验需返工",
                })
            report["checks"].append("create_dispatch_repair_rework_acceptance")
            http(image["url"], authenticated=False, expected=401)
            command(compose + ["restart", "backend"])
            ready()
            assert http("/api/v1/tickets")[0]["status"] == "completed"
            assert [item["accepted"] for item in http(path + "/acceptance-records")] == [False, True]
            assert len(http(path + "/repair-records")) == 2
            assert len(http("/api/v1/repair-personnel")) == 3
            http(image["url"])
            report["checks"].append("restart_preserves_tickets_history_and_images")
            parsed = http("/api/v1/repairs/parse", method="POST", payload={"text": "A栋1楼水管漏水"})
            assert parsed["mode"] == "rule_fallback_demo"
            report["checks"].append("offline_model_mode_is_truthful")
            report["passed"] = True
        except Exception as exc:
            report["failure"] = str(exc)[:500]
            diagnostics = command(compose + ["logs", "--tail", "60", "backend", "gateway"], check=False)
            (LOCAL / "deployment-failure.log").write_text(diagnostics.stdout + diagnostics.stderr, encoding="utf-8")
            raise
        finally:
            # project 是本脚本生成的唯一值；只清理该临时Compose项目和它的卷。
            cleaned = command(compose + ["down", "--volumes", "--remove-orphans"], check=False)
            report["cleanup_succeeded"] = cleaned.returncode == 0
            (LOCAL / "deployment-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
