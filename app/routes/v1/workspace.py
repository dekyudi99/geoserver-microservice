import os
import re
import uuid
import logging
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from ...clients.geoserver_client import geoserver_client
from ...schemas.v1.spatial_data import WorkspaceCreateRequest
from ...config.database import get_db, engine
from ...security.auth import get_verified_key
from ...models.spatial_data import WorkspaceMetadata, VectorLayer, RasterMetadata, LayerGroupMetadata
from ...models.ingest_job import IngestJob
from ...models.api_key import ApiKeyType
from ...services.hash_id import encode_id, decode_id, resolve_workspace

logger = logging.getLogger("geoserver_service.workspace")
router = APIRouter(prefix="/workspaces", tags=["Workspaces"])

def slugify_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower().strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "workspace"
    if not cleaned.startswith("ws_"):
        cleaned = f"ws_{cleaned}"
    return f"{cleaned[:35]}_{uuid.uuid4().hex[:6]}"

@router.get("", response_model=List[Dict[str, Any]])
def list_workspaces(
    all: bool = Query(False, description="Tampilkan semua workspace jika pemanggil adalah admin/primary"),
    db: Session = Depends(get_db),
    key = Depends(get_verified_key)
):
    """
    Mengambil seluruh workspace dari GeoServer yang sesuai dengan API Key pengguna (api_key_id == key.id).
    Primary Key (Admin) hanya melihat semua workspace jika query param all=True.
    """
    raw_workspaces = geoserver_client.get_workspaces()
    is_primary = getattr(key, "key_type", None) == ApiKeyType.PRIMARY

    if is_primary and all:
        meta_rows = db.query(WorkspaceMetadata).order_by(WorkspaceMetadata.created_at.desc()).all()
    else:
        meta_rows = db.query(WorkspaceMetadata).filter(
            WorkspaceMetadata.api_key_id == key.id
        ).order_by(WorkspaceMetadata.created_at.desc()).all()

    geoserver_names = {ws.get("name"): ws for ws in raw_workspaces if ws.get("name")}

    results = []
    for meta in meta_rows:
        ws_name = meta.workspace_name
        ws_geo = geoserver_names.get(ws_name)
        hashed_id = encode_id(meta.raw_id) if meta.raw_id is not None else ws_name

        results.append({
            "id": hashed_id,
            "hashed_id": hashed_id,
            "raw_id": meta.raw_id,
            "workspace_name": ws_name,
            "ws_name": ws_name,
            "name": meta.display_name or ws_name,
            "display_name": meta.display_name or ws_name,
            "visibility": meta.visibility or "private",
            "created_at": meta.created_at.isoformat() if meta.created_at else None,
            "href": ws_geo.get("href") if ws_geo else None
        })

    return results

@router.get("/{identifier}")
def get_workspace(
    identifier: str,
    db: Session = Depends(get_db),
    key = Depends(get_verified_key)
):
    meta = resolve_workspace(db, identifier)
    ws_name = meta.workspace_name if meta else identifier

    is_primary = getattr(key, "key_type", None) == ApiKeyType.PRIMARY
    if meta and not is_primary and meta.api_key_id != key.id:
        raise HTTPException(status_code=403, detail="Anda tidak memiliki akses ke workspace ini.")

    ws = geoserver_client.get_workspace(ws_name)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{identifier}' tidak ditemukan di GeoServer.")

    hashed_id = encode_id(meta.raw_id) if meta and meta.raw_id is not None else ws_name
    return {
        **ws,
        "id": hashed_id,
        "hashed_id": hashed_id,
        "raw_id": meta.raw_id if meta else None,
        "name": meta.display_name if meta else ws_name,
        "display_name": meta.display_name if meta else ws_name,
        "workspace_name": ws_name,
        "ws_name": ws_name,
        "visibility": meta.visibility if meta else "private",
        "created_at": meta.created_at.isoformat() if meta and meta.created_at else None,
    }

@router.post("", status_code=status.HTTP_201_CREATED)
def create_workspace(
    body: WorkspaceCreateRequest,
    db: Session = Depends(get_db),
    key = Depends(get_verified_key)
):
    display = (body.display_name or body.name_workspace or body.workspace_name or "Workspace").strip()

    if body.workspace_name and re.match(r"^[a-zA-Z0-9_\-]+$", body.workspace_name):
        technical_name = body.workspace_name
    else:
        technical_name = slugify_name(display)

    # 1. Buat di GeoServer
    success = geoserver_client.create_workspace(technical_name)
    if not success:
        raise HTTPException(status_code=500, detail=f"Gagal membuat workspace '{technical_name}' di GeoServer.")

    # 2. Simpan atau perbarui metadata di database spasial
    meta = db.query(WorkspaceMetadata).filter(WorkspaceMetadata.workspace_name == technical_name).first()
    if not meta:
        meta = WorkspaceMetadata(
            id=uuid.uuid4(),
            api_key_id=getattr(key, "id", None),
            workspace_name=technical_name,
            display_name=display,
            visibility=body.visibility or "private"
        )
        db.add(meta)
    else:
        meta.display_name = display
        meta.visibility = body.visibility or "private"

    try:
        db.commit()
        db.refresh(meta)
    except Exception as e:
        db.rollback()
        logger.error(f"Error saving WorkspaceMetadata for '{technical_name}': {e}", exc_info=True)
        # Rollback workspace di GeoServer jika gagal di database
        geoserver_client.delete_workspace(technical_name, recurse=True)
        raise HTTPException(status_code=500, detail=f"Gagal menyimpan metadata workspace ke database: {str(e)}")

    hashed_id = encode_id(meta.raw_id) if meta and meta.raw_id is not None else technical_name

    return {
        "success": True,
        "id": hashed_id,
        "hashed_id": hashed_id,
        "raw_id": meta.raw_id if meta else None,
        "name": display,
        "workspace_name": technical_name,
        "ws_name": technical_name,
        "display_name": display,
        "visibility": meta.visibility,
        "created_at": meta.created_at.isoformat() if meta.created_at else None,
        "detail": f"Workspace '{display}' berhasil dibuat."
    }

@router.delete("/{identifier}")
def delete_workspace(
    identifier: str,
    recurse: bool = True,
    db: Session = Depends(get_db),
    key = Depends(get_verified_key)
):
    """
    Mendukung penghapusan workspace menggunakan Hashed ID atau nama teknis GeoServer.
    Menghapus:
    1. Workspace di GeoServer (beserta seluruh store & layernya via recurse=True).
    2. Seluruh vector layer di PostGIS (drop table & staging file & metadata).
    3. Seluruh raster layer (file & metadata).
    4. Seluruh layer group di GeoServer & database.
    5. WorkspaceMetadata di database.
    """
    meta = resolve_workspace(db, identifier)
    ws_name = meta.workspace_name if meta else identifier

    is_primary = getattr(key, "key_type", None) == ApiKeyType.PRIMARY
    if meta and not is_primary and meta.api_key_id != key.id:
        raise HTTPException(status_code=403, detail="Anda tidak memiliki izin untuk menghapus workspace ini.")

    # 1. Hapus dari GeoServer (recurse=True menghapus semua datastore dan coverage store di GeoServer)
    try:
        geoserver_deleted = geoserver_client.delete_workspace(ws_name, recurse=recurse)
        logger.info(f"GeoServer delete workspace '{ws_name}' result: {geoserver_deleted}")
    except Exception as e:
        logger.warning(f"GeoServer delete_workspace exception for '{ws_name}': {e}")

    # 2. Hapus layer vector terkait dari PostGIS, disk, dan database
    try:
        vec_layers = db.query(VectorLayer).filter(VectorLayer.workspace_name == ws_name).all()
        for vec in vec_layers:
            try:
                with engine.begin() as conn:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{vec.table_name}" CASCADE;'))
            except Exception as e:
                logger.warning(f"Drop table {vec.table_name} warning: {e}")
            if vec.file_path and os.path.exists(vec.file_path):
                try:
                    os.remove(vec.file_path)
                except Exception:
                    pass
            try:
                jobs = db.query(IngestJob).filter(IngestJob.result_layer_name == vec.table_name).all()
                for j in jobs:
                    if j.staging_file_path and os.path.exists(j.staging_file_path):
                        try:
                            os.remove(j.staging_file_path)
                        except Exception:
                            pass
                    db.delete(j)
            except Exception:
                pass
            db.delete(vec)

        # 3. Hapus layer raster terkait dari disk dan database
        ras_layers = db.query(RasterMetadata).filter(RasterMetadata.workspace_name == ws_name).all()
        for ras in ras_layers:
            if ras.file_path and os.path.exists(ras.file_path):
                try:
                    os.remove(ras.file_path)
                except Exception:
                    pass
            db.delete(ras)

        # 4. Hapus Layer Groups terkait
        groups = db.query(LayerGroupMetadata).filter(LayerGroupMetadata.workspace_name == ws_name).all()
        for g in groups:
            db.delete(g)

        # 5. Hapus baris metadata workspace
        db.query(WorkspaceMetadata).filter(WorkspaceMetadata.workspace_name == ws_name).delete()
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error cleaning up workspace '{ws_name}' records in database: {e}", exc_info=True)

    display_title = meta.display_name if meta else ws_name
    return {
        "success": True,
        "detail": f"Workspace '{display_title}' ({ws_name}) dan seluruh layernya berhasil dihapus dari GeoServer dan database."
    }
