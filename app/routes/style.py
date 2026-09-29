from typing import Optional
from fastapi import APIRouter, HTTPException, status
from ..services.style_service import style_service
from ..schemas.spatial import StyleApplyRequest

router = APIRouter(prefix="/styles", tags=["Styles"])

@router.post("/apply", status_code=status.HTTP_200_OK)
def apply_style(req: StyleApplyRequest):
    try:
        success = style_service.apply_style(
            workspace=req.workspace_name,
            layer_name=req.layer_name,
            style_name=req.style_name,
            sld_xml=req.sld_xml
        )
        return {"success": success, "detail": f"Style '{req.style_name}' berhasil diaplikasikan ke layer '{req.layer_name}'."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/{style_name}")
def delete_style(style_name: str, workspace: Optional[str] = None, recurse: bool = True):
    from ..clients.geoserver_client import geoserver_client
    success = geoserver_client.delete_style(style_name=style_name, workspace_name=workspace, recurse=recurse)
    if not success:
        raise HTTPException(status_code=500, detail=f"Gagal menghapus style '{style_name}'.")
    return {"success": True, "detail": f"Style '{style_name}' berhasil dihapus."}
