from fastapi import APIRouter, Depends
from .v1.health import router as health_router
from .v1.workspace import router as workspace_router
from .v1.layer import router as layer_router
from .v1.layer_group import router as layer_group_router
from .v1.style import router as style_router
from .v1.store import router as store_router
from .v1.coverage_store import router as coverage_store_router
from .v1.wms import router as wms_router
from .v1.api_keys import router as api_keys_router
from .v1.logs import router as logs_router
from .v1.ingest import router as ingest_router
from ..security.auth import require_any_key

# Router legacy untuk kompatibilitas sementara selama masa transisi
# Semua endpoint ditandai deprecated=True agar terlihat jelas di OpenAPI
legacy_router = APIRouter()

auth_dep = [Depends(require_any_key)]
legacy_router.include_router(health_router, deprecated=True)
legacy_router.include_router(workspace_router, dependencies=auth_dep, deprecated=True)
legacy_router.include_router(store_router, dependencies=auth_dep, deprecated=True)
legacy_router.include_router(coverage_store_router, dependencies=auth_dep, deprecated=True)
legacy_router.include_router(layer_router, deprecated=True)
legacy_router.include_router(layer_group_router, dependencies=auth_dep, deprecated=True)
legacy_router.include_router(style_router, dependencies=auth_dep, deprecated=True)
legacy_router.include_router(wms_router, dependencies=auth_dep, deprecated=True)
legacy_router.include_router(api_keys_router, deprecated=True)
legacy_router.include_router(logs_router, deprecated=True)

legacy_router.include_router(ingest_router, deprecated=True)
