from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.customer_handoff import CustomerHandoff
from app.schemas.customer_handoff import HandoffDecisionRequest, HandoffSaveRequest, HandoffSubmitRequest
from app.services import customer_handoff_service as service


router = APIRouter(prefix="/api/v1", tags=["客户签约交接（演示身份）"])


def _write(db: Session, action):
    try:
        handoff = action()
        db.commit()
        db.refresh(handoff)
        return service.handoff_response(db, handoff)
    except service.HandoffNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        detail = str(exc) if isinstance(exc, ValueError) else "交接数据已被其他请求更新，请刷新"
        raise HTTPException(status_code=409, detail=detail) from exc
    except Exception:
        db.rollback()
        raise


@router.post("/customers/{customer_id}/handoff")
def create(customer_id: int, db: Session = Depends(get_db)):
    """只建立草稿，既不证明已签约，也不代表企服已接收。"""
    return _write(db, lambda: service.create_handoff(db, customer_id))


@router.get("/customer-handoffs")
def list_handoffs(db: Session = Depends(get_db)):
    return [service.handoff_response(db, row) for row in db.query(CustomerHandoff).order_by(CustomerHandoff.updated_at.desc()).all()]


@router.put("/customer-handoffs/{handoff_id}")
def save(handoff_id: int, request: HandoffSaveRequest, db: Session = Depends(get_db)):
    def action():
        handoff = service.get_handoff(db, handoff_id)
        service.save_document(db, handoff, request)
        return handoff
    return _write(db, action)


@router.post("/customer-handoffs/{handoff_id}/submit")
def submit(handoff_id: int, request: HandoffSubmitRequest, db: Session = Depends(get_db)):
    def action():
        handoff = service.get_handoff(db, handoff_id)
        service.submit_handoff(db, handoff, request)
        return handoff
    return _write(db, action)


@router.post("/customer-handoffs/{handoff_id}/decide")
def decide(handoff_id: int, request: HandoffDecisionRequest, db: Session = Depends(get_db)):
    """actor 目前是演示身份记录，正式部署需连接登录鉴权与部门角色。"""
    def action():
        handoff = service.get_handoff(db, handoff_id)
        service.decide_handoff(db, handoff, request)
        return handoff
    return _write(db, action)
