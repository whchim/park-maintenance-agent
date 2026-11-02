"""真实图片解码与业务证据关联；所有操作只使用独立演示库和临时目录。"""

from datetime import datetime, timedelta
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event

from app.database import engine as business_engine
from app.services import attachment_service
from scripts.run_demo import create_demo_app


def picture(image_format="PNG"):
    output = BytesIO()
    Image.new("RGB", (80, 60), "#84b39b").save(output, format=image_format)
    return output.getvalue()


@pytest.fixture
def client():
    def blocked(*args):
        pytest.fail("附件测试不得连接现有数据库")
    event.listen(business_engine, "connect", blocked)
    try:
        with TestClient(create_demo_app()) as demo_client:
            yield demo_client
    finally:
        event.remove(business_engine, "connect", blocked)


def upload(client, purpose="intake", content=None, filename="现场.png", media_type="image/png"):
    return client.post("/api/v1/attachments/images", params={"filename": filename, "purpose": purpose},
                       content=picture() if content is None else content, headers={"content-type": media_type})


def start(client, images=None, message="A栋302漏水", key=None):
    return client.post("/api/v1/agent/runs", json={"message": message, "attachment_ids": images or [],
                                                 "idempotency_key": key})


def confirm(client, workflow):
    return client.post(f"/api/v1/agent/runs/{workflow}/confirm-ticket", json={
        "location": "A栋302", "issue_type": "漏水", "description": "A栋302漏水", "priority": "normal",
        "idempotency_key": workflow + "-confirm",
    })


@pytest.mark.parametrize("image_format,media_type", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_upload_reencodes_and_serves_only_a_random_key(client, image_format, media_type):
    response = upload(client, content=picture(image_format) + b"private trailing content", filename="../../private.png", media_type=media_type)
    assert response.status_code == 201
    image = response.json()
    assert image["name"] == "private.png"
    assert image["width"] == 80 and image["height"] == 60
    assert "storage_key" not in image and image["ticket_id"] is None
    retrieved = client.get(image["url"])
    assert retrieved.status_code == 200
    assert b"private trailing content" not in retrieved.content
    assert retrieved.headers["x-content-type-options"] == "nosniff"
    assert retrieved.headers["cache-control"] == "private, no-store"
    assert client.get("/api/v1/tickets").json() == []


@pytest.mark.parametrize("content,media_type,status", [
    (b"<svg><script/></svg>", "image/svg+xml", 415),
    (b"<script>fake png</script>", "image/png", 415),
    (b"", "image/png", 400), (picture(), "image/jpeg", 415), (picture()[:30], "image/png", 415),
])
def test_rejects_unsupported_empty_corrupt_or_disguised_images(client, content, media_type, status):
    assert upload(client, content=content, media_type=media_type).status_code == status
    assert list(client.app.state.image_store.root.iterdir()) == []


def test_upload_limits_file_and_decoded_pixels(client, monkeypatch):
    from app.api import attachments
    monkeypatch.setattr(attachments, "MAX_IMAGE_BYTES", 100)
    assert upload(client, content=b"x" * 101).status_code == 413
    monkeypatch.setattr(attachments, "MAX_IMAGE_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(attachment_service, "MAX_PIXELS", 100)
    assert upload(client).status_code == 413
    assert list(client.app.state.image_store.root.iterdir()) == []


def test_image_only_intake_stays_incomplete_and_survives_supplement_and_confirm(client):
    image = upload(client).json()
    workflow = start(client, [image["id"]], message="", key="photo-only").json()
    assert workflow["status"] == "waiting_input"
    assert set(workflow["missing_fields"]) == {"location", "issue_type"}
    assert "当前AI仅处理文字" in workflow["draft"]["follow_up_question"]
    assert workflow["attachments"][0]["id"] == image["id"]
    assert confirm(client, workflow["workflow_id"]).status_code == 409
    assert client.get("/api/v1/tickets").json() == []
    base = f"/api/v1/agent/runs/{workflow['workflow_id']}"
    updated = client.post(base + "/messages", json={"message": "A栋302漏水"}).json()
    assert updated["status"] == "waiting_human"
    confirmed = confirm(client, workflow["workflow_id"]).json()
    assert confirmed["attachments"][0]["ticket_id"] == confirmed["ticket_id"]
    assert client.get(base).json()["attachments"][0]["id"] == image["id"]
    assert client.get(f"/api/v1/tickets/{confirmed['ticket_id']}/attachments").json()[0]["id"] == image["id"]
    assert confirm(client, workflow["workflow_id"]).json()["ticket_id"] == confirmed["ticket_id"]


def test_cross_workflow_duplicate_wrong_purpose_and_over_limit_are_rejected(client):
    image = upload(client).json()
    first = start(client, [image["id"]]).json()
    assert start(client, [image["id"]]).status_code == 409
    repair = upload(client, purpose="repair").json()
    assert start(client, [repair["id"]]).status_code == 409
    staged = upload(client).json()
    assert start(client, [staged["id"], staged["id"]]).status_code == 409
    assert start(client, [staged["id"]] * 6).status_code == 422
    assert start(client, ["../secret"]).status_code == 422
    assert client.get(f"/api/v1/agent/runs/{first['workflow_id']}").json()["attachments"][0]["id"] == image["id"]
    assert start(client, [staged["id"]]).status_code == 200


def test_failed_binding_rolls_back_earlier_claims_and_manual_ticket(client):
    image = upload(client).json()
    payload = {"location": "A栋302", "issue_type": "漏水", "description": "模拟漏水", "priority": "normal",
               "attachment_ids": [image["id"], "f" * 32]}
    assert client.post("/api/v1/tickets", json=payload).status_code == 409
    assert client.get("/api/v1/tickets").json() == []
    payload["attachment_ids"] = [image["id"]]
    created = client.post("/api/v1/tickets", json=payload)
    assert created.status_code == 201
    assert client.get(f"/api/v1/tickets/{created.json()['id']}/attachments").json()[0]["id"] == image["id"]


def test_repair_images_bind_only_with_successful_repair_submission(client):
    workflow = start(client).json()["workflow_id"]
    ticket_id = confirm(client, workflow).json()["ticket_id"]
    image = upload(client, purpose="repair").json()
    payload = {"personnel_no": "ENG-DEMO-001", "repair_description": "模拟维修完成", "attachment_ids": [image["id"]]}
    base = f"/api/v1/tickets/{ticket_id}"
    assert client.post(base + "/repair-records", json=payload).status_code == 400
    assert client.get(base + "/attachments").json() == []
    assert client.post(base + "/assignments", json={"personnel_no": "ENG-DEMO-001", "assigned_by": "负责人"}).status_code == 201
    assert client.post(base + "/accept", json={"personnel_no": "ENG-DEMO-001"}).status_code == 200
    assert client.post(base + "/start-processing", json={"personnel_no": "ENG-DEMO-001"}).status_code == 200
    invalid = {**payload, "attachment_ids": [image["id"], "f" * 32]}
    assert client.post(base + "/repair-records", json=invalid).status_code == 400
    assert client.get("/api/v1/tickets").json()[0]["status"] == "processing"
    record = client.post(base + "/repair-records", json=payload)
    assert record.status_code == 201
    assert record.json()["photo_urls"] == [image["url"]]
    saved = client.get(base + "/attachments").json()[0]
    assert saved["repair_record_id"] == record.json()["id"]
    assert client.get(image["url"]).status_code == 200
    saved_records = client.get(base + "/repair-records").json()
    assert saved_records[0]["repair_description"] == payload["repair_description"]
    assert saved_records[0]["photo_urls"] == [image["url"]]
    # 返工也不能把首次提交的图片重新绑定到另一份维修记录。
    assert client.post(base + "/acceptance", json={"accepted": False, "acceptance_comment": "模拟返工", "accepted_by": "企服"}).status_code == 201
    assert client.post(base + "/repair-records", json=payload).status_code == 400
    assert len(client.get(base + "/repair-records").json()) == 1


def test_failed_upload_database_write_removes_only_the_new_file(client, monkeypatch):
    existing = upload(client).json()
    original_flush = attachment_service.Session.flush
    def fail_attachment_flush(db, *args, **kwargs):
        from app.models.attachment import Attachment
        if any(isinstance(row, Attachment) for row in db.new):
            raise RuntimeError("synthetic write failure")
        return original_flush(db, *args, **kwargs)
    monkeypatch.setattr(attachment_service.Session, "flush", fail_attachment_flush)
    with pytest.raises(RuntimeError, match="synthetic write failure"):
        upload(client)
    assert len(list(client.app.state.image_store.root.iterdir())) == 1
    assert client.get(existing["url"]).status_code == 200


def test_expired_staging_is_rejected(client, monkeypatch):
    image = upload(client).json()
    class Tomorrow(datetime):
        @classmethod
        def now(cls):
            return datetime.now() + timedelta(days=2)
    monkeypatch.setattr(attachment_service, "datetime", Tomorrow)
    assert start(client, [image["id"]]).status_code == 409


def test_exif_orientation_is_applied_but_private_metadata_is_not_kept(client):
    source = Image.new("RGB", (80, 60), "green")
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "private test author"
    output = BytesIO()
    source.save(output, format="JPEG", exif=exif)
    response = upload(client, content=output.getvalue(), media_type="image/jpeg")
    assert response.status_code == 201
    assert (response.json()["width"], response.json()["height"]) == (60, 80)
    saved = client.get(response.json()["url"])
    with Image.open(BytesIO(saved.content)) as clean:
        assert len(clean.getexif()) == 0


def test_demo_parallel_gallery_reads_keep_the_ticket_and_evidence_consistent(client):
    image = upload(client).json()
    ticket = client.post("/api/v1/tickets", json={"location": "A栋302", "issue_type": "漏水", "description": "模拟并行读取",
                                                "attachment_ids": [image["id"]]}).json()
    base = f"/api/v1/tickets/{ticket['id']}"
    paths = [base + suffix for suffix in ["/events", "/attachments", "/repair-records"]] * 8
    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(client.get, paths))
    assert all(response.status_code == 200 for response in responses)
    for response in responses[1::3]:
        assert response.json()[0]["id"] == image["id"]
        assert response.json()[0]["ticket_id"] == ticket["id"]
    assert client.get("/api/v1/tickets").json()[0]["id"] == ticket["id"]
