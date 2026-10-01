from fastapi import APIRouter, Depends
from .health import router as health_router
from .workspace import router as workspace_router
from .layer import router as layer_router
from .layer_group import router as layer_group_router
from .style import router as style_router
from .store import router as store_router
from .coverage_store import router as coverage_store_router
from .wms import router as wms_router
from .api_keys import router as api_keys_router
from .logs import router as logs_router
from .ingest import router as ingest_router
from ...security.auth import require_any_key

api_v1_router = APIRouter(prefix="/api/v1")

# Health
api_v1_router.include_router(health_router)

# Functional routers
auth_dep = [Depends(require_any_key)]
api_v1_router.include_router(workspace_router, dependencies=auth_dep)
api_v1_router.include_router(store_router, dependencies=auth_dep)
api_v1_router.include_router(coverage_store_router, dependencies=auth_dep)
api_v1_router.include_router(layer_router)
api_v1_router.include_router(layer_group_router, dependencies=auth_dep)
api_v1_router.include_router(style_router, dependencies=auth_dep)
api_v1_router.include_router(wms_router, dependencies=auth_dep)
api_v1_router.include_router(api_keys_router)
api_v1_router.include_router(logs_router)

api_v1_router.include_router(ingest_router)
