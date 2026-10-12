import time
import threading
from typing import Optional, Callable
from collections import defaultdict
from fastapi import Request, Response, HTTPException, status
from ..config.settings import settings

# Thread-safe in-memory sliding window storage:
# Format: _storage[category][client_id] = [timestamp1, timestamp2, ...]
_lock = threading.Lock()
_storage = defaultdict(lambda: defaultdict(list))


def get_client_identifier(request: Request) -> str:
    """
    Mengidentifikasi klien pemanggil secara presisi:
    1. Header X-API-Key (prioritas utama)
    2. Header Authorization Bearer
    3. Query parameter api_key
    4. Fallback: IP client (X-Forwarded-For atau client.host)
    """
    # 1. Header X-API-Key
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{api_key.strip()}"

    # 2. Authorization Bearer
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        return f"bearer:{auth[7:].strip()}"

    # 3. Query Param api_key
    query_key = request.query_params.get("api_key")
    if query_key:
        return f"query:{query_key.strip()}"

    # 4. Fallback ke IP address
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    else:
        client_ip = request.client.host if request.client else "unknown"
    return f"ip:{client_ip}"


def rate_limit(
    category: str = "custom",
    max_requests: Optional[int] = None,
    window_seconds: int = 60
) -> Callable:
    """
    FastAPI Dependency generator untuk rate limiting sliding-window.

    Cara mudah mengatur batas limit:
    1. Kategori preset (nilai default otomatis diambil dari settings / .env):
       - "upload" : settings.RATE_LIMIT_UPLOAD_PER_MINUTE (default 10 req/menit)
       - "read"   : settings.RATE_LIMIT_READ_PER_MINUTE   (default 120 req/menit)
       - "global" : settings.RATE_LIMIT_GLOBAL_PER_MINUTE (default 300 req/menit)

    2. Kustom manual langsung di router:
       - rate_limit(category="custom", max_requests=5, window_seconds=30)
    """
    async def _rate_limiter_dependency(request: Request, response: Response):
        if not settings.RATE_LIMIT_ENABLED:
            return

        # Ambil limit efektif secara dinamis dari settings
        if max_requests is None:
            if category == "upload":
                effective_limit = settings.RATE_LIMIT_UPLOAD_PER_MINUTE
            elif category == "read":
                effective_limit = settings.RATE_LIMIT_READ_PER_MINUTE
            elif category == "global":
                effective_limit = settings.RATE_LIMIT_GLOBAL_PER_MINUTE
            else:
                effective_limit = 60
        else:
            effective_limit = max_requests

        if effective_limit <= 0:
            return

        client_id = get_client_identifier(request)
        now = time.time()

        with _lock:
            history = _storage[category][client_id]
            # Buang hit yang sudah kedaluwarsa di luar jendela waktu
            history = [t for t in history if now - t < window_seconds]
            _storage[category][client_id] = history

            if len(history) >= effective_limit:
                oldest_timestamp = history[0]
                retry_after = max(1, int(window_seconds - (now - oldest_timestamp)))
                reset_timestamp = int(oldest_timestamp + window_seconds)

                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        f"Rate limit tercapai untuk operasi '{category}'. "
                        f"Batas: {effective_limit} request per {window_seconds} detik. "
                        f"Silakan coba lagi dalam {retry_after} detik."
                    ),
                    headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Limit": str(effective_limit),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(reset_timestamp),
                    }
                )

            history.append(now)
            remaining = max(0, effective_limit - len(history))
            reset_timestamp = int(history[0] + window_seconds) if history else int(now + window_seconds)

        response.headers["X-RateLimit-Limit"] = str(effective_limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_timestamp)

    return _rate_limiter_dependency

