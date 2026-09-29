import time
import requests
from typing import Optional, Dict, Any
from fastapi import Header, Query, HTTPException, status, Security
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from ..config.settings import settings
import logging

logger = logging.getLogger("geoserver_service.auth")

# Cache for verified user keys: fingerprint/key -> {info, cached_at}
_VERIFIED_KEYS_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL = 300  # 5 minutes

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)

def verify_api_key(
    x_api_key: Optional[str] = Security(api_key_header),
    auth_bearer: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    api_key_query: Optional[str] = Query(None, alias="api_key")
) -> Dict[str, Any]:
    """
    Verifikasi API Key untuk GeoServer Microservice:
    1. Mendukung Internal S2S Key (master key AstraGIS).
    2. Mendukung User API Key (berawalan 'agis_sk_') yang dibuat di Frontend Web AstraGIS
       dengan mendelegasikan verifikasi ke endpoint AstraGIS (/api-key/verify)
       sehingga TIDAK ADA duplikasi data/tabel user.
    """
    token = x_api_key or (auth_bearer.credentials if auth_bearer else None) or api_key_query

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Header 'X-API-Key' atau token autentikasi diperlukan untuk mengakses GeoServer Service.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # 1. Cek Master Internal S2S API Key
    if token == settings.API_KEY:
        return {
            "type": "internal_s2s",
            "role": "admin",
            "client": "astragis_core"
        }

    # 2. Cek User API Key AstraGIS (agis_sk_...)
    if token.startswith("agis_sk_"):
        now = time.time()
        # Periksa cache lokal
        if token in _VERIFIED_KEYS_CACHE:
            entry = _VERIFIED_KEYS_CACHE[token]
            if now - entry["cached_at"] < _CACHE_TTL:
                return entry["info"]
            else:
                _VERIFIED_KEYS_CACHE.pop(token, None)

        # Verifikasi ke AstraGIS API
        try:
            res = requests.get(
                settings.ASTRAGIS_VERIFY_KEY_URL,
                headers={"X-API-Key": token},
                timeout=5
            )
            if res.status_code == 200:
                data = res.json()
                key_info = {
                    "type": "astragis_user",
                    "project_id": data.get("project_id"),
                    "key_id": data.get("key_id"),
                    "name": data.get("name")
                }
                _VERIFIED_KEYS_CACHE[token] = {
                    "info": key_info,
                    "cached_at": now
                }
                return key_info
            elif res.status_code in (401, 403, 404):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="API Key AstraGIS tidak valid atau telah dinonaktifkan."
                )
        except requests.RequestException as e:
            logger.error(f"Failed to reach AstraGIS verification service at {settings.ASTRAGIS_VERIFY_KEY_URL}: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Layanan verifikasi API Key AstraGIS sedang tidak dapat dihubungi."
            )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API Key tidak dikenali atau tidak memiliki izin akses."
    )
