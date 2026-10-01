from sqlalchemy.orm import Session
from ..models.audit_log import AuditLog
from ..models.api_key import ApiKey
from typing import Optional
import logging

logger = logging.getLogger("geoserver_service.audit")

def log_action(
    db: Session,
    api_key: ApiKey,
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    resource_name: Optional[str] = None,
    status: str = "SUCCESS",
    detail: Optional[str] = None
):
    try:
        log = AuditLog(
            api_key_id=api_key.id,
            api_key_name=api_key.name,
            api_key_type=api_key.key_type.value if hasattr(api_key.key_type, 'value') else str(api_key.key_type),
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
            resource_name=resource_name,
            status=status,
            detail=detail
        )
        db.add(log)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to record audit log: {e}")
        db.rollback()
