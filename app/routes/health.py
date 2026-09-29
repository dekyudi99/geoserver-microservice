from fastapi import APIRouter
from ..config.settings import settings
from ..clients.geoserver_client import geoserver_client

router = APIRouter(tags=["Health"])

@router.get("/health")
def health_check():
    geoserver_ok = False
    try:
        workspaces = geoserver_client.get_workspaces()
        geoserver_ok = True
    except Exception:
        geoserver_ok = False

    return {
        "status": "ok",
        "service": "geoserver-microservice",
        "geoserver_connected": geoserver_ok,
        "geoserver_url": settings.GEOSERVER_URL
    }
