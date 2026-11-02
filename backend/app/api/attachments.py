"""Web传图入口；其他渠道复用附件服务，不在这里抓取任意远程URL。"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_db
from app.models.attachment import Attachment
from app.models.ticket import Ticket
from app.schemas.attachment import AttachmentId, AttachmentResponse
from app.services.attachment_service import (
    DEFAULT_UPLOAD_DIR, MAX_IMAGE_BYTES, ImageStore, ensure_schema, image_response, list_images, save_image,
)


router = APIRouter(prefix="/api/v1", tags=["图片附件"])


def get_image_store(request: Request) -> ImageStore:
    return getattr(request.app.state, "image_store", None) or ImageStore(DEFAULT_UPLOAD_DIR)


@router.post("/attachments/images", response_model=AttachmentResponse, status_code=201)
async def upload_image(request: Request, filename: str = Query(min_length=1, max_length=240),
                       purpose: Literal["intake", "repair"] = "intake", db: Session = Depends(get_db),
                       store: ImageStore = Depends(get_image_store)):
    declared_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if declared_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(415, "仅支持JPEG、PNG、WebP图片")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "每张图片最多10MiB")
    image = None
    try:
        image = await run_in_threadpool(save_image, db, store, content=bytes(content), name=filename,
                                        media_type=declared_type, purpose=purpose)
        db.commit()
        return image_response(image)
    except Exception:
        db.rollback()
        if image is not None:
            store.discard_new(image.storage_key)
        raise


@router.get("/attachments/{image_id}/content")
def read_image(image_id: AttachmentId, db: Session = Depends(get_db), store: ImageStore = Depends(get_image_store)):
    ensure_schema(db)
    image = db.get(Attachment, image_id)
    if image is None or not store.path(image.storage_key).is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(store.path(image.storage_key), media_type=image.media_type,
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
                                 "Content-Security-Policy": "default-src 'none'; sandbox"})


@router.get("/tickets/{ticket_id}/attachments", response_model=list[AttachmentResponse])
def ticket_images(ticket_id: int, db: Session = Depends(get_db)):
    if db.get(Ticket, ticket_id) is None:
        raise HTTPException(404, "工单不存在")
    return list_images(db, ticket_id=ticket_id)


