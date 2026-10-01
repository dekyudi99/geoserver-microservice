from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from ...clients.geoserver_client import geoserver_client

router = APIRouter(prefix="/wms", tags=["WMS OGC"])

@router.get("/capabilities")
def get_wms_capabilities(workspace: Optional[str] = Query(None)):
    try:
        return geoserver_client.get_wms_capabilities(workspace_name=workspace)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
