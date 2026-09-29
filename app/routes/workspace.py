from fastapi import APIRouter, HTTPException, status
from typing import List, Dict, Any
from ..clients.geoserver_client import geoserver_client
from ..schemas.spatial import WorkspaceCreateRequest

router = APIRouter(prefix="/workspaces", tags=["Workspaces"])

@router.get("", response_model=List[Dict[str, Any]])
def list_workspaces():
    return geoserver_client.get_workspaces()

@router.get("/{workspace_name}")
def get_workspace(workspace_name: str):
    ws = geoserver_client.get_workspace(workspace_name)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_name}' tidak ditemukan di GeoServer.")
    return ws

@router.post("", status_code=status.HTTP_201_CREATED)
def create_workspace(body: WorkspaceCreateRequest):
    success = geoserver_client.create_workspace(body.workspace_name)
    if not success:
        raise HTTPException(status_code=500, detail=f"Gagal membuat workspace '{body.workspace_name}'.")
    return {"success": True, "workspace_name": body.workspace_name}

@router.delete("/{workspace_name}")
def delete_workspace(workspace_name: str, recurse: bool = True):
    success = geoserver_client.delete_workspace(workspace_name, recurse=recurse)
    if not success:
        raise HTTPException(status_code=500, detail=f"Gagal menghapus workspace '{workspace_name}'.")
    return {"success": True, "detail": f"Workspace '{workspace_name}' berhasil dihapus."}
