from fastapi import FastAPI, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.openapi.docs import get_swagger_ui_html
from app.config.settings import settings
from app.config.database import ensure_postgis_extension, create_all_tables
from app.routes.v1 import api_v1_router
import logging

logging.basicConfig(level=logging.INFO if not settings.DEBUG else logging.DEBUG)
logger = logging.getLogger("geoserver_service")

app = FastAPI(
    title="GeoServer Microservice API",
    description="Layanan mikro terisolasi untuk integrasi GeoServer, ingest data spasial PostGIS, dan API Key Management mandiri",
    version="2.0.0",
    docs_url="/docs",
    openapi_url="/openapi.json"
)

# CORS
origins = [
    "http://localhost:5173",
    "http://localhost:5175",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5175",
    "http://localhost:3000",
    "http://localhost:8000",
    "http://localhost:8001",
    "http://localhost:8005",
    "https://voxagis.wefgis.com",
    "https://api-astragis.ikya.my.id",
    "https://flowgis.wefgis.com",
    "https://api-flowgis.wefgis.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_event():
    logger.info("Initializing GeoServer Microservice v2.0...")
    try:
        ensure_postgis_extension()
        create_all_tables()
    except Exception as e:
        logger.warning(f"Could not connect or initialize tables on startup: {e}")

    try:
        from sqlalchemy import text
        from .config.database import engine
        with engine.begin() as conn:
            conn.execute(text("UPDATE ingest_jobs SET status = 'FAILED', error_message = 'Job terminated unexpectedly due to service restart.' WHERE status IN ('QUEUED', 'RUNNING');"))
        logger.info("Stale ingest jobs marked as failed on startup.")
    except Exception as e:
        logger.warning(f"Could not update stale ingest jobs: {e}")

    try:
        import requests
        assets_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        if os.path.exists(assets_folder):
            for f in os.listdir(assets_folder):
                if f.lower().endswith(".svg"):
                    f_path = os.path.join(assets_folder, f)
                    with open(f_path, "rb") as fp:
                        content = fp.read()
                    requests.put(
                        f"{settings.GEOSERVER_URL.rstrip('/')}/rest/resource/styles/{f}",
                        data=content,
                        headers={"Content-Type": "image/svg+xml"},
                        auth=(settings.GEOSERVER_USER, settings.GEOSERVER_PASS),
                        timeout=5
                    )
            logger.info("SVG style assets synchronized to GeoServer resource store.")
    except Exception as e:
        logger.warning(f"Could not synchronize SVG assets to GeoServer: {e}")

# 1. Mount official V1 API Router
app.include_router(api_v1_router)

# Mount Static Assets (SVG icons for vector styling)
import os
from fastapi.staticfiles import StaticFiles
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
os.makedirs(ASSETS_DIR, exist_ok=True)
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

# 2. Dedicated Swagger UI and OpenAPI Schema for v1
@app.get("/api/v1/openapi.json", include_in_schema=False)
def get_v1_openapi():
    full_schema = get_openapi(
        title="GeoServer Microservice API - v1",
        version="1.0.0",
        description="Spesifikasi resmi API v1 untuk GeoServer Microservice dan Spatial Data Management",
        routes=app.routes
    )
    v1_paths = {p: item for p, item in full_schema.get("paths", {}).items() if p.startswith("/api/v1")}
    full_schema["paths"] = v1_paths
    return JSONResponse(full_schema)

@app.get("/api/v1/docs", include_in_schema=False)
def get_v1_docs():
    return get_swagger_ui_html(
        openapi_url="/api/v1/openapi.json",
        title="GeoServer Microservice API v1 Documentation"
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
