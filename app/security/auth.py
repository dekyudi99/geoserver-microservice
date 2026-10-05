import time
from typing import Optional, Dict, Any
from types import SimpleNamespace
from fastapi import Header, Query, HTTPException, status, Security, Depends
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from ..config.database import get_db
from ..services.api_key_service import get_api_key_by_plain
from ..models.api_key import ApiKey, ApiKeyType
import logging

logger = logging.getLogger("geoserver_service.auth")

_KEY_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL = 300  # 5 minutes

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)

def _get_from_cache(plain_key: str) -> Optional[SimpleNamespace]:
    if plain_key in _KEY_CACHE:
        entry = _KEY_CACHE[plain_key]
        if time.time() - entry["cached_at"] < _CACHE_TTL:
            return entry["api_key"]
        else:
            del _KEY_CACHE[plain_key]
    return None


def clear_key_cache():
    global _KEY_CACHE
    _KEY_CACHE.clear()
    logger.info("API Key cache cleared.")

def _set_cache(plain_key: str, cached_obj: SimpleNamespace):
    _KEY_CACHE[plain_key] = {
        "api_key": cached_obj,
        "cached_at": time.time()
    }

def get_verified_key(
    x_api_key: Optional[str] = Security(api_key_header),
    auth_bearer: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    api_key_query: Optional[str] = Query(None, alias="api_key"),
    db: Session = Depends(get_db)
) -> SimpleNamespace:
    """
    Self-contained API Key Verification:
    - Memvalidasi API key secara mandiri di GeoServer Microservice
    - Memverifikasi kunci terhadap PostGIS geoserver_spatial_db
    - Membedakan peran PRIMARY (service/admin) dan STANDARD (client)
    - Mengembalikan objek SimpleNamespace yang bebas dari DetachedInstanceError
    """
    token = x_api_key or (auth_bearer.credentials if auth_bearer else None) or api_key_query

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Header 'X-API-Key' atau token autentikasi diperlukan.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Cache hit check
    cached = _get_from_cache(token)
    if cached:
        return cached

    # Verify against database
    api_key = get_api_key_by_plain(token, db)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key tidak valid atau telah dinonaktifkan."
        )

    # Convert to detached object so session closure doesn't throw DetachedInstanceError
    cached_obj = SimpleNamespace(
        id=api_key.id,
        name=api_key.name,
        key_type=api_key.key_type,
        key_prefix=api_key.key_prefix,
        is_active=api_key.is_active
    )
    _set_cache(token, cached_obj)
    return cached_obj

def require_primary_key(api_key: SimpleNamespace = Depends(get_verified_key)) -> SimpleNamespace:
    if api_key.key_type != ApiKeyType.PRIMARY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operasi ini membutuhkan PRIMARY API Key."
        )
    return api_key

def require_any_key(api_key: SimpleNamespace = Depends(get_verified_key)) -> SimpleNamespace:
    return api_key

# Alias backward compatibility
verify_api_key = require_any_key
