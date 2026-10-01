from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List, Dict, Any
from ...clients.geoserver_client import geoserver_client

router = APIRouter(prefix="/stores", tags=["Datastores"])

@router.get("")
def list_stores(workspace: Optional[str] = Query(None)):
    return geoserver_client.get_datastores(workspace_name=workspace)

@router.get("/{workspace}/{store_name}")
def get_store(workspace: str, store_name: str):
    store = geoserver_client.get_datastore(workspace_name=workspace, store_name=store_name)
    if not store:
        raise HTTPException(status_code=404, detail="Datastore tidak ditemukan.")
    return store
