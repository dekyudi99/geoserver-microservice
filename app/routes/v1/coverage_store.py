from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List, Dict, Any
from ...clients.geoserver_client import geoserver_client

router = APIRouter(prefix="/coverage-stores", tags=["Coverage Stores"])

@router.get("")
def list_coverage_stores(workspace: Optional[str] = Query(None)):
    return geoserver_client.get_coverage_stores(workspace_name=workspace)

@router.get("/{workspace}/{store_name}")
def get_coverage_store(workspace: str, store_name: str):
    store = geoserver_client.get_coverage_store(workspace_name=workspace, store_name=store_name)
    if not store:
        raise HTTPException(status_code=404, detail="Coverage store tidak ditemukan.")
    return store

@router.delete("/{workspace}/{store_name}")
def delete_coverage_store(workspace: str, store_name: str, recurse: bool = True):
    success = geoserver_client.delete_coverage_store(workspace_name=workspace, store_name=store_name, recurse=recurse)
    if not success:
        raise HTTPException(status_code=500, detail="Gagal menghapus coverage store.")
    return {"success": True, "detail": f"Coverage store '{store_name}' berhasil dihapus."}
