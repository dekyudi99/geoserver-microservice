from fastapi import APIRouter, HTTPException, Query, Depends, status
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from ...clients.geoserver_client import geoserver_client
from ...config.database import get_db
from ...security.auth import get_verified_key
from ...models.api_key import ApiKeyType
from ...models.spatial_data import RasterMetadata, WorkspaceMetadata

router = APIRouter(prefix="/coverage-stores", tags=["Coverage Stores"])

@router.get("")
def list_coverage_stores(
    workspace: Optional[str] = Query(None),
    key = Depends(get_verified_key)
):
    return geoserver_client.get_coverage_stores(workspace_name=workspace)

@router.get("/{workspace}/{store_name}")
def get_coverage_store(
    workspace: str,
    store_name: str,
    key = Depends(get_verified_key)
):
    store = geoserver_client.get_coverage_store(workspace_name=workspace, store_name=store_name)
    if not store:
        raise HTTPException(status_code=404, detail="Coverage store tidak ditemukan.")
    return store

@router.delete("/{workspace}/{store_name}")
def delete_coverage_store(
    workspace: str,
    store_name: str,
    recurse: bool = True,
    db: Session = Depends(get_db),
    key = Depends(get_verified_key)
):
    is_primary = getattr(key, "key_type", None) == ApiKeyType.PRIMARY
    if not is_primary:
        raster = db.query(RasterMetadata).filter(
            RasterMetadata.workspace_name == workspace,
            RasterMetadata.store_name == store_name
        ).first()
        ws_meta = db.query(WorkspaceMetadata).filter(
            WorkspaceMetadata.workspace_name == workspace
        ).first()

        owned = False
        if raster and raster.api_key_id == key.id:
            owned = True
        elif ws_meta and ws_meta.api_key_id == key.id:
            owned = True

        if not owned:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Anda tidak memiliki izin menghapus coverage store ini."
            )

    success = geoserver_client.delete_coverage_store(workspace_name=workspace, store_name=store_name, recurse=recurse)
    if not success:
        raise HTTPException(status_code=500, detail="Gagal menghapus coverage store.")
    return {"success": True, "detail": f"Coverage store '{store_name}' berhasil dihapus."}

