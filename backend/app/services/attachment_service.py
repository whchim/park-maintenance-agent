"""图片校验、受控存储和业务绑定；不调用模型，也不持有飞书凭据。"""

from datetime import datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
import warnings
from uuid import uuid4

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models.attachment import Attachment


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_IMAGES = 5
DEFAULT_UPLOAD_DIR = Path(__file__).resolve().parents[2] / "var" / "uploads"
FORMATS = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}


class ImageStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, storage_key: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}\.(jpg|png|webp)", storage_key):
            raise HTTPException(404, "图片不存在")
        return self.root / storage_key

    def put(self, storage_key: str, content: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path(storage_key).open("xb") as output:
            output.write(content)

    def discard_new(self, storage_key: str) -> None:
        # 只回收本次上传失败产生的新文件，绝不清理其他附件。
        self.path(storage_key).unlink(missing_ok=True)


def schema_ready(db: Session) -> bool:
    # 使用当前事务连接，不能让StaticPool上的另一个连接上下文回滚正在建单的事务。
    return inspect(db.connection()).has_table(Attachment.__tablename__)


def ensure_schema(db: Session) -> None:
    if not schema_ready(db):
        raise HTTPException(503, "附件表尚未准备好；请使用隔离演示入口，现有库迁移需另行安排")


def image_response(image: Attachment) -> dict:
    return {
        "id": image.id, "name": image.original_name, "media_type": image.media_type,
        "size_bytes": image.size_bytes, "width": image.width, "height": image.height,
        "purpose": image.purpose, "source_channel": image.source_channel,
        "url": f"/api/v1/attachments/{image.id}/content",
        "workflow_id": image.workflow_id, "ticket_id": image.ticket_id,
        "repair_record_id": image.repair_record_id, "created_at": image.created_at,
    }


def validate_image(content: bytes, declared_type: str) -> tuple[bytes, str, str, int, int]:
    if not content:
        raise HTTPException(400, "图片内容为空")
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "每张图片最多10MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                image_format = image.format
                if image_format not in FORMATS or getattr(image, "n_frames", 1) != 1:
                    raise HTTPException(415, "仅支持静态JPEG、PNG、WebP图片")
                media_type, extension = FORMATS[image_format]
                if declared_type != media_type:
                    raise HTTPException(415, "图片内容与声明格式不一致")
                width, height = image.size
                if width * height > MAX_PIXELS:
                    raise HTTPException(413, "图片最多2000万像素")
                image.verify()
            with Image.open(BytesIO(content)) as image:
                # 按手机照片方向旋转；重新生成像素图，不保留EXIF、注释或文件尾部数据。
                from PIL import ImageOps
                pixels = ImageOps.exif_transpose(image).convert("RGBA" if image_format != "JPEG" else "RGB")
                clean = Image.frombytes(pixels.mode, pixels.size, pixels.tobytes())
                output = BytesIO()
                clean.save(output, format=image_format)
                normalized = output.getvalue()
                if len(normalized) > MAX_IMAGE_BYTES:
                    raise HTTPException(413, "处理后的图片超过10MiB，请缩小后再上传")
                return normalized, media_type, extension, clean.width, clean.height
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise HTTPException(415, "图片无法解码或已经损坏") from exc
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise HTTPException(413, "图片像素过大") from exc


def save_image(db: Session, store: ImageStore, *, content: bytes, name: str, media_type: str,
               purpose: str, source_channel: str = "web", source_reference: str | None = None) -> Attachment:
    ensure_schema(db)
    normalized, actual_type, extension, width, height = validate_image(content, media_type)
    image_id = uuid4().hex
    # 文件名仅用于展示；磁盘键始终由服务端生成，兼容浏览器传来的Windows路径。
    safe_name = name.replace("\\", "/").rsplit("/", 1)[-1]
    safe_name = "".join(char for char in safe_name if char.isprintable())[:180] or f"image{extension}"
    image = Attachment(id=image_id, original_name=safe_name, media_type=actual_type,
                       storage_key=image_id + extension, size_bytes=len(normalized), width=width, height=height,
                       sha256=sha256(normalized).hexdigest(), purpose=purpose, source_channel=source_channel,
                       source_reference=source_reference)
    try:
        store.put(image.storage_key, normalized)
        db.add(image)
        db.flush()
    except Exception:
        store.discard_new(image.storage_key)
        raise
    return image


def list_images(db: Session, *, workflow_id: str | None = None, ticket_id: int | None = None) -> list[dict]:
    # 未迁移的原环境仍可使用旧的文字流程；上传明确返回503，不自动建表。
    if not schema_ready(db):
        return []
    query = db.query(Attachment)
    if workflow_id is not None:
        query = query.filter(Attachment.workflow_id == workflow_id)
    elif ticket_id is not None:
        query = query.filter(Attachment.ticket_id == ticket_id)
    else:
        return []
    return [image_response(image) for image in query.order_by(Attachment.created_at, Attachment.id).all()]


def bind_images(db: Session, image_ids: list[str], *, purpose: str, workflow_id: str | None = None,
                ticket_id: int | None = None, repair_record_id: int | None = None) -> list[Attachment]:
    """调用方提交业务事务；条件更新防止同一暂存附件被并发绑定到不同业务。"""
    if not image_ids:
        return []
    ensure_schema(db)
    if len(image_ids) > MAX_IMAGES or len(set(image_ids)) != len(image_ids):
        raise ValueError("每次最多5张图片，不能重复引用")
    if purpose == "intake":
        existing = list_images(db, workflow_id=workflow_id, ticket_id=ticket_id)
        if len({item["id"] for item in existing if item["purpose"] == "intake"} | set(image_ids)) > MAX_IMAGES:
            raise ValueError("每个报修流程最多5张现场图片")
    result = []
    for image_id in image_ids:
        image = db.get(Attachment, image_id)
        if image is None or image.purpose != purpose:
            raise ValueError("图片不存在或用途不匹配")
        is_staged = image.workflow_id is None and image.ticket_id is None and image.repair_record_id is None
        same_owner = (image.workflow_id == workflow_id and image.ticket_id == ticket_id
                      and image.repair_record_id == repair_record_id)
        if not is_staged and not same_owner:
            raise ValueError("图片已绑定其他流程、工单或维修记录")
        if is_staged and image.created_at < datetime.now() - timedelta(hours=24):
            raise ValueError("暂存图片已超过24小时，请重新上传")
        changed = db.query(Attachment).filter(
            Attachment.id == image_id, Attachment.workflow_id == image.workflow_id,
            Attachment.ticket_id == image.ticket_id, Attachment.repair_record_id == image.repair_record_id,
        ).update({"workflow_id": workflow_id, "ticket_id": ticket_id, "repair_record_id": repair_record_id}, synchronize_session=False)
        if changed != 1:
            raise ValueError("图片关联已变化，请刷新重试")
        db.refresh(image)
        result.append(image)
    return result


def link_workflow_ticket(db: Session, workflow_id: str, ticket_id: int) -> None:
    if schema_ready(db):
        db.query(Attachment).filter(Attachment.workflow_id == workflow_id).update({"ticket_id": ticket_id})
