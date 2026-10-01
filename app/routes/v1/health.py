import time
from fastapi import APIRouter
from ...config.settings import settings
from ...clients.geoserver_client import geoserver_client

router = APIRouter(tags=["Health"])

@router.get("/health")
def health_check():
    start = time.time()
    geoserver_ok = False
    geo_version = None
    try:
        ver_data = geoserver_client.get_version()
        if ver_data and "about" in ver_data:
            geoserver_ok = True
            resources = ver_data.get("about", {}).get("resource", [])
            for r in resources:
                if r.get("@name") == "GeoServer":
                    geo_version = str(r.get("Version", "2.28.2"))
                    break
            if not geo_version:
                geo_version = "2.28.2"
    except Exception:
        geoserver_ok = False

    latency_ms = round((time.time() - start) * 1000, 2)

    return {
        "status": "ok" if geoserver_ok else "degraded",
        "service": "geoserver-microservice",
        "geoserver_connected": geoserver_ok,
        "geoserver_version": geo_version,
        "latency_ms": latency_ms,
        "geoserver_url": settings.GEOSERVER_URL,
        "timestamp": time.time()
    }

@router.get("/ping")
def ping():
    return {
        "status": "pong",
        "service": "geoserver-microservice",
        "time": time.time()
    }
