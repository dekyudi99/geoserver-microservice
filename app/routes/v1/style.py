from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
import uuid
import datetime

from ...services.style_service import style_service
from ...models.spatial_data import RasterMetadata, VectorLayer
from ...models.api_key import ApiKey
from ...security.auth import require_any_key
from ...config.database import get_db
from ...services.audit_service import log_action

router = APIRouter(prefix="/styles", tags=["Styles"])

class StyleApplyPayload(BaseModel):
    workspace_name: Optional[str] = None
    layer_name: Optional[str] = None
    layer_id: Optional[str] = None
    style_name: Optional[str] = None
    sld_xml: Optional[str] = None
    style_type: Optional[str] = "intervals"
    colors: Optional[List[Dict[str, Any]]] = None
    classification_method: Optional[str] = None
    classes_count: Optional[int] = None
    color_ramp: Optional[str] = None

@router.post("/apply", status_code=status.HTTP_200_OK)
def apply_style(
    req: StyleApplyPayload,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    try:
        ws_name = req.workspace_name
        lyr_name = req.layer_name
        raster_obj = None

        # Resolve layer if layer_id given
        if req.layer_id:
            try:
                lid = uuid.UUID(str(req.layer_id))
                raster_obj = db.query(RasterMetadata).filter(RasterMetadata.id == lid).first()
                if raster_obj:
                    ws_name = raster_obj.workspace_name
                    lyr_name = raster_obj.store_name
                else:
                    vec_obj = db.query(VectorLayer).filter(VectorLayer.id == lid).first()
                    if vec_obj:
                        ws_name = vec_obj.workspace_name
                        lyr_name = vec_obj.table_name
            except Exception:
                pass

        if not ws_name or not lyr_name:
            if req.layer_name:
                raster_obj = db.query(RasterMetadata).filter(
                    (RasterMetadata.store_name == req.layer_name) | (RasterMetadata.layer_name == req.layer_name)
                ).first()
                if raster_obj:
                    ws_name = raster_obj.workspace_name
                    lyr_name = raster_obj.store_name

        if not ws_name or not lyr_name:
            raise HTTPException(status_code=400, detail="workspace_name dan layer_name diperlukan.")

        st_name = req.style_name or f"style_{lyr_name}"
        sld_xml = req.sld_xml

        if not sld_xml and req.colors:
            sld_xml = style_service.generate_raster_sld(
                style_name=st_name,
                color_entries=req.colors,
                style_type=req.style_type or "intervals"
            )

        success = style_service.apply_style(
            workspace=ws_name,
            layer_name=lyr_name,
            style_name=st_name,
            sld_xml=sld_xml
        )

        if raster_obj and req.colors:
            raster_obj.symbology = {
                "classes": req.colors,
                "style_type": req.style_type,
                "classification_method": req.classification_method,
                "classes_count": req.classes_count or len(req.colors),
                "color_ramp": req.color_ramp,
                "style_name": st_name,
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }
            db.commit()

        log_action(
            db=db,
            api_key=caller,
            action="APPLY_STYLE",
            resource_type="STYLE",
            resource_id=str(req.layer_id or lyr_name),
            resource_name=st_name,
            status="SUCCESS" if success else "FAILED"
        )

        return {"success": success, "detail": f"Style '{st_name}' berhasil diaplikasikan ke layer '{lyr_name}'."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/raster-info/{layer_id}")
def get_raster_info(
    layer_id: str,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    raster_obj = None
    try:
        lid = uuid.UUID(str(layer_id))
        raster_obj = db.query(RasterMetadata).filter(RasterMetadata.id == lid).first()
    except Exception:
        raster_obj = db.query(RasterMetadata).filter(
            (RasterMetadata.store_name == layer_id) | (RasterMetadata.layer_name == layer_id)
        ).first()

    if not raster_obj:
        return {"statistics": {"min": 0, "max": 100, "mean": 50, "std": 10}, "saved_symbology": None}

    stats = None
    if raster_obj.file_path:
        try:
            import rasterio
            import numpy as np
            with rasterio.open(raster_obj.file_path) as src:
                arr = src.read(1, masked=True)
                valid = arr.compressed()
                if len(valid) > 0:
                    stats = {
                        "min": float(np.min(valid)),
                        "max": float(np.max(valid)),
                        "mean": float(np.mean(valid)),
                        "std": float(np.std(valid))
                    }
        except Exception:
            pass

    if not stats:
        stats = {"min": 0, "max": 100, "mean": 50, "std": 10}

    return {
        "id": str(raster_obj.id),
        "workspace_name": raster_obj.workspace_name,
        "layer_name": raster_obj.store_name,
        "statistics": stats,
        "saved_symbology": raster_obj.symbology,
        "dimensions": raster_obj.dimensions,
        "bbox": raster_obj.bbox
    }

@router.delete("/{style_name}")
def delete_style(style_name: str, workspace: Optional[str] = None, recurse: bool = True):
    from ...clients.geoserver_client import geoserver_client
    success = geoserver_client.delete_style(style_name=style_name, workspace_name=workspace, recurse=recurse)
    if not success:
        raise HTTPException(status_code=500, detail=f"Gagal menghapus style '{style_name}'.")
    return {"success": True, "detail": f"Style '{style_name}' berhasil dihapus."}
