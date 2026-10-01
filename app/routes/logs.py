from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional
from ..config.database import get_db
from ..models.audit_log import AuditLog
from ..models.api_key import ApiKey
from ..security.auth import require_primary_key

router = APIRouter(prefix="/logs", tags=["Audit Logs"])

@router.get("")
def get_audit_logs(
    api_key_id: Optional[str] = Query(None, description="Filter berdasarkan ID API Key"),
    action: Optional[str] = Query(None, description="Filter berdasarkan jenis aksi"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_primary_key)
):
    """
    Mengambil catatan aktivitas (Audit Logs).
    Hanya dapat diakses oleh PRIMARY API Key.
    """
    query = db.query(AuditLog)
    if api_key_id:
        query = query.filter(AuditLog.api_key_id == api_key_id)
    if action:
        query = query.filter(AuditLog.action == action)

    total = query.count()
    logs = query.order_by(AuditLog.performed_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "data": [
            {
                "id": str(l.id),
                "api_key_id": str(l.api_key_id),
                "api_key_name": l.api_key_name,
                "api_key_type": l.api_key_type,
                "action": l.action,
                "resource_type": l.resource_type,
                "resource_id": l.resource_id,
                "resource_name": l.resource_name,
                "status": l.status,
                "detail": l.detail,
                "performed_at": str(l.performed_at)
            }
            for l in logs
        ]
    }
