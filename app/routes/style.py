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
