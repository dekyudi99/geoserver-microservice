from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from .config.settings import settings
from .config.database import ensure_postgis_extension
from .security.auth import verify_api_key
from .routes import (
    health,
    workspace,
    store,
    coverage_store,
    layer,
    style,
    wms
)
import logging

logging.basicConfig(level=logging.INFO if not settings.DEBUG else logging.DEBUG)
logger = logging.getLogger("geoserver_service")

app = FastAPI(
    title="GeoServer Microservice API",
    description="Layanan mikro terisolasi untuk integrasi GeoServer, ingest data spasial PostGIS, dan WMS/WFS",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup_event():
    logger.info("Initializing GeoServer Microservice...")
    try:
        ensure_postgis_extension()
    except Exception as e:
        logger.warning(f"Could not connect to PostGIS on startup: {e}")

# 1. Health check is public (for Docker daemon / monitoring)
app.include_router(health.router)

# 2. All functional routers are protected with API Key verification
#    (Supports internal S2S Master Key + user API keys created in AstraGIS Web Frontend)
auth_dependency = [Depends(verify_api_key)]
app.include_router(workspace.router, dependencies=auth_dependency)
app.include_router(store.router, dependencies=auth_dependency)
app.include_router(coverage_store.router, dependencies=auth_dependency)
app.include_router(layer.router, dependencies=auth_dependency)
app.include_router(style.router, dependencies=auth_dependency)
app.include_router(wms.router, dependencies=auth_dependency)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
