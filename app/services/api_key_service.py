import secrets
import bcrypt
from typing import Optional, Tuple, List
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from ..models.api_key import ApiKey, ApiKeyType
import logging

logger = logging.getLogger("geoserver_service.api_key")

PREFIX_PRIMARY = "gsvc_pk_"
PREFIX_STANDARD = "gsvc_sk_"

def _hash_key(plain_key: str) -> str:
    return bcrypt.hashpw(plain_key.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def _verify_key_hash(plain_key: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain_key.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def generate_api_key(key_type: ApiKeyType, db: Session, name: str, owner_info: Optional[str] = None) -> Tuple[str, ApiKey]:
    prefix = PREFIX_PRIMARY if key_type == ApiKeyType.PRIMARY else PREFIX_STANDARD
    plain_key = prefix + secrets.token_urlsafe(36)
    hashed = _hash_key(plain_key)

    api_key = ApiKey(
        name=name,
        key_hash=hashed,
        key_prefix=prefix,
        key_type=key_type,
        owner_info=owner_info,
        is_active=True
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    logger.info(f"API Key '{name}' ({key_type}) created.")
    return plain_key, api_key

def get_api_key_by_plain(plain_key: str, db: Session) -> Optional[ApiKey]:
    if not plain_key:
        return None

    if plain_key.startswith(PREFIX_PRIMARY):
        prefix = PREFIX_PRIMARY
    elif plain_key.startswith(PREFIX_STANDARD):
        prefix = PREFIX_STANDARD
    else:
        return None

    candidates = db.query(ApiKey).filter(
        ApiKey.key_prefix == prefix,
        ApiKey.is_active == True
    ).all()

    for candidate in candidates:
        if _verify_key_hash(plain_key, candidate.key_hash):
            try:
                candidate.last_used_at = func.now()
                db.commit()
            except Exception:
                db.rollback()
            return candidate

    return None

def delete_api_key(key_id: str, db: Session) -> bool:
    from ..security.auth import clear_key_cache
    api_key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not api_key:
        return False
    api_key.is_active = False
    db.commit()
    clear_key_cache()
    return True

def list_api_keys(db: Session) -> List[ApiKey]:
    return db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
