from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
import uuid

from ..config.database import get_db
from ..config.settings import settings
from ..models.api_key import ApiKey
from ..models.spatial_data import LayerGroupMetadata, WorkspaceMetadata, VectorLayer, RasterMetadata
from ..services.hash_id import encode_id, decode_id, resolve_workspace
from ..clients.geoserver_client import geoserver_client
from ..security.auth import require_any_key
from ..services.audit_service import log_action

router = APIRouter(prefix="/layer-groups", tags=["Layer Groups"])

class LayerGroupCreatePayload(BaseModel):
    workspace_id: Optional[str] = None
    workspace_name: Optional[str] = None
    name: str
    title: str
    abstract_text: Optional[str] = ""
    mode: Optional[str] = "single"
    layer_ids: List[str] = []
    keywords: Optional[List[str]] = []

class LayerGroupUpdatePayload(BaseModel):
    title: Optional[str] = None
    abstract_text: Optional[str] = ""
    mode: Optional[str] = "single"
    layer_ids: List[str] = []

class AddLayerPayload(BaseModel):
    layer_id: str

def resolve_layer_details(db: Session, layer_ids: List[str]):
    resolved = []
    bboxes = []
    for lid in layer_ids:
        layer_obj = None
        try:
            u = uuid.UUID(str(lid))
            layer_obj = db.query(VectorLayer).filter(VectorLayer.id == u).first()
            if not layer_obj:
                layer_obj = db.query(RasterMetadata).filter(RasterMetadata.id == u).first()
        except Exception:
            layer_obj = db.query(VectorLayer).filter(
                (VectorLayer.table_name == lid) | (VectorLayer.layer_name == lid)
            ).first()
            if not layer_obj:
                layer_obj = db.query(RasterMetadata).filter(
                    (RasterMetadata.store_name == lid) | (RasterMetadata.layer_name == lid)
                ).first()

        if layer_obj:
            geoserver_name = getattr(layer_obj, "table_name", None) or getattr(layer_obj, "store_name", None)
            resolved.append({
                "id": str(layer_obj.id),
                "layer_id": str(layer_obj.id),
                "layer_name": layer_obj.layer_name,
                "geoserver_name": geoserver_name,
                "workspace_name": layer_obj.workspace_name,
                "bbox": layer_obj.bbox
            })
            if layer_obj.bbox:
                if isinstance(layer_obj.bbox, list) and len(layer_obj.bbox) >= 4:
                    bboxes.append(layer_obj.bbox)
                elif isinstance(layer_obj.bbox, dict):
                    b = layer_obj.bbox
                    left = b.get("left") or b.get("minx")
                    bottom = b.get("bottom") or b.get("miny")
                    right = b.get("right") or b.get("maxx")
                    top = b.get("top") or b.get("maxy")
                    if left is not None and bottom is not None:
                        bboxes.append([left, bottom, right, top])
    
    # Combined bbox
    combined_bbox = None
    if bboxes:
        minx = min(b[0] for b in bboxes)
        miny = min(b[1] for b in bboxes)
        maxx = max(b[2] for b in bboxes)
        maxy = max(b[3] for b in bboxes)
        combined_bbox = [minx, miny, maxx, maxy]

    return resolved, combined_bbox

@router.post("", status_code=status.HTTP_201_CREATED)
@router.post("/create", status_code=status.HTTP_201_CREATED)
def create_layer_group(
    payload: LayerGroupCreatePayload,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    raw_ws = payload.workspace_name or payload.workspace_id
    if not raw_ws:
        raise HTTPException(status_code=400, detail="workspace_name diperlukan.")
    ws_meta = resolve_workspace(db, raw_ws)
    ws_name = ws_meta.workspace_name if ws_meta else raw_ws

    resolved_layers, combined_bbox = resolve_layer_details(db, payload.layer_ids)
    if not resolved_layers:
        raise HTTPException(status_code=400, detail="Minimal satu layer valid diperlukan untuk membuat Layer Group.")

    layer_names_for_geoserver = [l["geoserver_name"] for l in resolved_layers if l["geoserver_name"]]

    success = geoserver_client.create_or_update_layer_group(
        workspace_name=ws_name,
        group_name=payload.name,
        title=payload.title,
        mode=payload.mode or "single",
        layer_names=layer_names_for_geoserver
    )

    wms_url = f"{settings.GEOSERVER_WMS_URL}/{ws_name}/wms"
    wms_layers_param = f"{ws_name}:{payload.name}"

    group_obj = LayerGroupMetadata(
        api_key_id=caller.id,
        workspace_name=ws_name,
        name=payload.name,
        title=payload.title,
        abstract_text=payload.abstract_text,
        mode=payload.mode,
        layer_ids=payload.layer_ids,
        wms_url=wms_url,
        wms_layers_param=wms_layers_param,
        bbox=combined_bbox
    )
    db.add(group_obj)
    db.commit()
    db.refresh(group_obj)

    log_action(
        db=db,
        api_key=caller,
        action="CREATE_LAYER_GROUP",
        resource_type="LAYER_GROUP",
        resource_id=str(group_obj.id),
        resource_name=group_obj.title,
        status="SUCCESS"
    )

    return {
        "success": True,
        "detail": f"Layer Group '{payload.title}' berhasil dibuat di GeoServer!",
        "data": {
            "id": str(group_obj.id),
            "name": group_obj.name,
            "title": group_obj.title,
            "workspace_name": group_obj.workspace_name,
            "wms_url": group_obj.wms_url,
            "wms_layers_param": group_obj.wms_layers_param,
            "bbox": group_obj.bbox,
            "layer_count": len(payload.layer_ids),
            "mode": group_obj.mode
        }
    }

@router.get("")
@router.get("/list")
def list_layer_groups(
    workspace_id: Optional[str] = None,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    query = db.query(LayerGroupMetadata)
    is_primary = getattr(caller, "is_primary", False) or getattr(caller, "role", "") == "PRIMARY"
    if not is_primary:
        query = query.filter(LayerGroupMetadata.api_key_id == caller.id)

    if workspace_id:
        ws_meta = resolve_workspace(db, workspace_id)
        ws_target = ws_meta.workspace_name if ws_meta else workspace_id
        query = query.filter(
            (LayerGroupMetadata.workspace_name == ws_target) |
            (LayerGroupMetadata.workspace_name == str(workspace_id))
        )

    groups = query.order_by(LayerGroupMetadata.created_at.desc()).all()
    ws_map = {m.workspace_name: m for m in db.query(WorkspaceMetadata).all()}

    result = []
    for g in groups:
        lids = g.layer_ids if isinstance(g.layer_ids, list) else []
        result.append({
            "id": str(g.id),
            "name": g.name,
            "title": g.title,
            "abstract_text": g.abstract_text,
            "workspace_name": g.workspace_name,
            "workspace_id": encode_id(ws_map.get(g.workspace_name).raw_id) if ws_map.get(g.workspace_name) and ws_map.get(g.workspace_name).raw_id is not None else g.workspace_name,
            "workspace_hashed_id": encode_id(ws_map.get(g.workspace_name).raw_id) if ws_map.get(g.workspace_name) and ws_map.get(g.workspace_name).raw_id is not None else g.workspace_name,
            "mode": g.mode or "single",
            "wms_url": g.wms_url,
            "wms_layers_param": g.wms_layers_param,
            "bbox": g.bbox,
            "layer_count": len(lids),
            "layer_ids": lids,
            "created_at": g.created_at.isoformat() if g.created_at else None
        })

    return {"success": True, "total": len(result), "data": result}

@router.get("/{group_id}")
def get_layer_group_detail(
    group_id: str,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    group_obj = None
    try:
        u = uuid.UUID(str(group_id))
        group_obj = db.query(LayerGroupMetadata).filter(LayerGroupMetadata.id == u).first()
    except Exception:
        group_obj = db.query(LayerGroupMetadata).filter(
            (LayerGroupMetadata.name == group_id) | (LayerGroupMetadata.title == group_id)
        ).first()

    if not group_obj:
        raise HTTPException(status_code=404, detail="Layer Group tidak ditemukan.")

    lids = group_obj.layer_ids if isinstance(group_obj.layer_ids, list) else []
    resolved_layers, combined_bbox = resolve_layer_details(db, lids)

    return {
        "success": True,
        "data": {
            "id": str(group_obj.id),
            "name": group_obj.name,
            "title": group_obj.title,
            "abstract_text": group_obj.abstract_text,
            "workspace_id": group_obj.workspace_name,
            "workspace_name": group_obj.workspace_name,
            "mode": group_obj.mode or "single",
            "wms_url": group_obj.wms_url,
            "wms_layers_param": group_obj.wms_layers_param,
            "bbox": group_obj.bbox or combined_bbox,
            "layer_count": len(lids),
            "layers": resolved_layers,
            "created_at": group_obj.created_at.isoformat() if group_obj.created_at else None
        }
    }

@router.put("/{group_id}")
def update_layer_group(
    group_id: str,
    payload: LayerGroupUpdatePayload,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    group_obj = None
    try:
        u = uuid.UUID(str(group_id))
        group_obj = db.query(LayerGroupMetadata).filter(LayerGroupMetadata.id == u).first()
    except Exception:
        group_obj = db.query(LayerGroupMetadata).filter(LayerGroupMetadata.name == group_id).first()

    if not group_obj:
        raise HTTPException(status_code=404, detail="Layer Group tidak ditemukan.")

    resolved_layers, combined_bbox = resolve_layer_details(db, payload.layer_ids)
    layer_names_for_geoserver = [l["geoserver_name"] for l in resolved_layers if l["geoserver_name"]]

    geoserver_client.create_or_update_layer_group(
        workspace_name=group_obj.workspace_name,
        group_name=group_obj.name,
        title=payload.title or group_obj.title,
        mode=payload.mode or group_obj.mode or "single",
        layer_names=layer_names_for_geoserver
    )

    if payload.title:
        group_obj.title = payload.title
    if payload.abstract_text is not None:
        group_obj.abstract_text = payload.abstract_text
    if payload.mode:
        group_obj.mode = payload.mode
    group_obj.layer_ids = payload.layer_ids
    if combined_bbox:
        group_obj.bbox = combined_bbox

    db.commit()
    db.refresh(group_obj)

    return {"success": True, "detail": "Layer Group berhasil diperbarui!", "data": {"id": str(group_obj.id)}}

@router.delete("/{group_id}")
def delete_layer_group(
    group_id: str,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    group_obj = None
    try:
        u = uuid.UUID(str(group_id))
        group_obj = db.query(LayerGroupMetadata).filter(LayerGroupMetadata.id == u).first()
    except Exception:
        group_obj = db.query(LayerGroupMetadata).filter(LayerGroupMetadata.name == group_id).first()

    if not group_obj:
        raise HTTPException(status_code=404, detail="Layer Group tidak ditemukan.")

    geoserver_client.delete_layer_group(
        workspace_name=group_obj.workspace_name,
        group_name=group_obj.name
    )

    db.delete(group_obj)
    db.commit()

    return {"success": True, "detail": f"Layer Group '{group_obj.title}' berhasil dihapus."}
