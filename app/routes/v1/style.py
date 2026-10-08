from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
import uuid
import datetime

from ...services.style_service import style_service
from ...models.spatial_data import RasterMetadata, VectorLayer
from ...models.api_key import ApiKey, ApiKeyType
from ...security.auth import require_any_key
from ...config.database import get_db
from ...services.audit_service import log_action

router = APIRouter(prefix="/styles", tags=["Styles"])

class StyleApplyPayload(BaseModel):
    layer_kind: Optional[str] = None
    workspace_name: Optional[str] = None
    layer_name: Optional[str] = None
    layer_id: Optional[str] = None
    style_name: Optional[str] = None
    sld_xml: Optional[str] = None
    style_sld: Optional[str] = None
    style_type: Optional[str] = "intervals"
    colors: Optional[List[Dict[str, Any]]] = None
    classification_method: Optional[str] = None
    classes_count: Optional[int] = None
    color_ramp: Optional[str] = None
    geometry_type: Optional[str] = None
    style_mode: Optional[str] = None
    symbol: Optional[Dict[str, Any]] = None

@router.post("/apply", status_code=status.HTTP_200_OK)
def apply_style(
    req: StyleApplyPayload,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    try:
        provided_sld = req.sld_xml or req.style_sld
        if req.layer_kind not in (None, "raster", "vector"):
            raise HTTPException(status_code=422, detail="layer_kind harus 'raster' atau 'vector'.")
        raster_obj = None
        vector_obj = None
        layer_uuid = None
        if req.layer_id:
            try:
                layer_uuid = uuid.UUID(str(req.layer_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise HTTPException(status_code=400, detail="layer_id tidak valid.") from exc

        if layer_uuid:
            raster_obj = db.query(RasterMetadata).filter(RasterMetadata.id == layer_uuid).first()
        elif req.layer_name and req.workspace_name and req.layer_kind in (None, "raster"):
            raster_obj = db.query(RasterMetadata).filter(
                RasterMetadata.workspace_name == req.workspace_name,
                (RasterMetadata.store_name == req.layer_name) | (RasterMetadata.layer_name == req.layer_name),
            ).first()

        if raster_obj:
            if raster_obj.api_key_id != caller.id and caller.key_type != ApiKeyType.PRIMARY:
                raise HTTPException(status_code=403, detail="Anda tidak memiliki izin mengubah style raster ini.")
            if req.layer_kind == "vector":
                raise HTTPException(status_code=400, detail="Tipe layer tidak cocok: layer_id ini adalah raster.")
            ws_name = raster_obj.workspace_name
            lyr_name = raster_obj.store_name
            layer_kind = "raster"
        else:
            if layer_uuid:
                vector_obj = db.query(VectorLayer).filter(VectorLayer.id == layer_uuid).first()
            elif req.layer_name and req.workspace_name and req.layer_kind in (None, "vector"):
                vector_obj = db.query(VectorLayer).filter(
                    VectorLayer.workspace_name == req.workspace_name,
                    (VectorLayer.table_name == req.layer_name) | (VectorLayer.layer_name == req.layer_name),
                ).first()
            if not vector_obj:
                raise HTTPException(status_code=404, detail="Layer tidak ditemukan.")
            if vector_obj.api_key_id != caller.id and caller.key_type != ApiKeyType.PRIMARY:
                raise HTTPException(status_code=403, detail="Anda tidak memiliki izin mengubah style vector ini.")
            if req.layer_kind == "raster":
                raise HTTPException(status_code=400, detail="Tipe layer tidak cocok: layer_id ini adalah vector.")
            ws_name = vector_obj.workspace_name
            lyr_name = vector_obj.table_name
            layer_uuid = vector_obj.id
            layer_kind = "vector"

        if raster_obj:
            layer_uuid = raster_obj.id

        st_name = req.style_name or f"style_{lyr_name}"
        if len(st_name) > 100 or not st_name.replace("_", "").replace("-", "").isalnum():
            raise HTTPException(status_code=400, detail="style_name hanya boleh berisi huruf, angka, '-' atau '_'.")

        if layer_kind == "raster":
            if req.symbol:
                raise HTTPException(status_code=400, detail="Raster menerima colors, bukan symbol vector.")
            if not provided_sld and not req.colors:
                raise HTTPException(status_code=400, detail="Raster memerlukan colors atau sld_xml.")
            sld_xml = provided_sld
            if not sld_xml and req.colors:
                if len(req.colors) > 256:
                    raise HTTPException(status_code=400, detail="Style raster maksimal memiliki 256 kelas.")
                sld_xml = style_service.generate_raster_sld(
                    style_name=st_name,
                    color_entries=req.colors,
                    style_type=req.style_type or "intervals"
                )
        else:
            if req.colors:
                raise HTTPException(status_code=400, detail="Vector tidak menerima colors.")
            geometry_type = (vector_obj.geom_type or "").strip()
            symbol = req.symbol or {}

            if provided_sld:
                sld_xml = provided_sld
            else:
                if not symbol:
                    raise HTTPException(status_code=400, detail="Vector memerlukan symbol atau sld_xml.")
                normalized_geometry = geometry_type.lower().replace(" ", "")
                requested_geometry = (req.geometry_type or geometry_type).lower().replace(" ", "")
                if requested_geometry != normalized_geometry:
                    raise HTTPException(status_code=400, detail="geometry_type tidak cocok dengan metadata layer.")
                allowed_symbol_fields = {
                    "fill_color", "fill_opacity", "stroke_color", "stroke_width",
                    "stroke_opacity", "point_size", "mark", "stroke_dasharray",
                }
                unknown_fields = set(symbol) - allowed_symbol_fields
                if unknown_fields:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Properti symbol tidak didukung: {', '.join(sorted(unknown_fields))}.",
                    )
                if req.style_mode not in (None, "single"):
                    raise HTTPException(status_code=400, detail="Vector saat ini hanya mendukung style_mode 'single'.")
                try:
                    sld_xml = style_service.generate_vector_sld(
                        style_name=st_name,
                        geom_type=geometry_type,
                        fill_color=symbol.get("fill_color", "#0d9488"),
                        stroke_color=symbol.get("stroke_color", "#0f766e"),
                        stroke_width=symbol.get("stroke_width", 1.5),
                        fill_opacity=symbol.get("fill_opacity", 0.45),
                        stroke_opacity=symbol.get("stroke_opacity", 1.0),
                        point_size=symbol.get("point_size", 8),
                        mark=symbol.get("mark", "circle"),
                        stroke_dasharray=symbol.get("stroke_dasharray"),
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc

        success = style_service.apply_style(
            workspace=ws_name,
            layer_name=lyr_name,
            style_name=st_name,
            sld_xml=sld_xml
        )
        if not success:
            raise HTTPException(status_code=502, detail="GeoServer gagal menerapkan style ke layer.")

        updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if layer_kind == "raster" and req.colors:
            raster_obj.symbology = {
                "layer_kind": "raster",
                "classes": req.colors,
                "style_type": req.style_type,
                "classification_method": req.classification_method,
                "classes_count": req.classes_count or len(req.colors),
                "color_ramp": req.color_ramp,
                "style_name": st_name,
                "updated_at": updated_at
            }
        elif layer_kind == "vector":
            vector_obj.symbology = {
                "layer_kind": "vector",
                "style_mode": "single",
                "geometry_type": geometry_type,
                "symbol": {
                    "fill_color": symbol.get("fill_color", "#0d9488"),
                    "fill_opacity": float(symbol.get("fill_opacity", 0.45)),
                    "stroke_color": symbol.get("stroke_color", "#0f766e"),
                    "stroke_width": float(symbol.get("stroke_width", 1.5)),
                    "stroke_opacity": float(symbol.get("stroke_opacity", 1.0)),
                    "point_size": float(symbol.get("point_size", 8)),
                    "mark": symbol.get("mark", "circle"),
                    "stroke_dasharray": symbol.get("stroke_dasharray"),
                },
                "style_name": st_name,
                "updated_at": updated_at,
            }
        db.commit()

        log_action(
            db=db,
            api_key=caller,
            action="APPLY_STYLE",
            resource_type="STYLE",
            resource_id=str(layer_uuid),
            resource_name=st_name,
            status="SUCCESS"
        )

        return {
            "success": True,
            "layer_id": str(layer_uuid),
            "layer_kind": layer_kind,
            "style_name": st_name,
            "symbology": raster_obj.symbology if layer_kind == "raster" else vector_obj.symbology,
            "detail": f"Style '{st_name}' berhasil diaplikasikan ke layer '{lyr_name}'.",
        }
    except HTTPException:
        db.rollback()
        raise
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Gagal menerapkan style: {exc}") from exc

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
        raise HTTPException(status_code=404, detail="Raster tidak ditemukan.")
    if raster_obj.api_key_id != caller.id and caller.key_type != ApiKeyType.PRIMARY:
        raise HTTPException(status_code=403, detail="Anda tidak memiliki izin membaca metadata raster ini.")

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
