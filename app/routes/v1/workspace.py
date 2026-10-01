import re
import uuid
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query
from sqlalchemy.orm import Session
from ...clients.geoserver_client import geoserver_client
from ...schemas.v1.spatial_data import WorkspaceCreateRequest
from ...config.database import get_db
from ...security.auth import get_verified_key
from ...models.spatial_data import WorkspaceMetadata
from ...models.api_key import ApiKeyType
from ...services.hash_id import encode_id, decode_id, resolve_workspace

router = APIRouter(prefix="/workspaces", tags=["Workspaces"])

def slugify_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower().strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "workspace"
    if not cleaned.startswith("ws_"):
        cleaned = f"ws_{cleaned}"
    # Truncate and add random hash
    return f"{cleaned[:35]}_{uuid.uuid4().hex[:6]}"

@router.get("", response_model=List[Dict[str, Any]])
def list_workspaces(
    db: Session = Depends(get_db),
    key = Depends(get_verified_key)
):
    """
    Mengambil seluruh workspace dari GeoServer yang diperkaya dengan hashed ID dan display_name.
    Memisahkan batasan hak akses: Primary Key (Admin) melihat semua workspace,
    sedangkan Standard Key hanya melihat workspace miliknya sendiri atau yang bersifat public.
    """
    raw_workspaces = geoserver_client.get_workspaces()
    is_primary = getattr(key, "key_type", None) == ApiKeyType.PRIMARY

    if is_primary:
        meta_rows = db.query(WorkspaceMetadata).all()
    else:
        meta_rows = db.query(WorkspaceMetadata).filter(
            (WorkspaceMetadata.api_key_id == key.id) | (WorkspaceMetadata.visibility == "public")
        ).all()

    meta_map = {m.workspace_name: m for m in meta_rows}

    results = []
    for ws in raw_workspaces:
        ws_name = ws.get("name")
        if not ws_name:
            continue
        # Standard Key tidak dapat melihat workspace yang bukan miliknya / bukan public
        if not is_primary and ws_name not in meta_map:
            continue

        meta = meta_map.get(ws_name)
        hashed_id = encode_id(meta.raw_id) if meta and meta.raw_id is not None else ws_name

        results.append({
            "id": hashed_id,
            "hashed_id": hashed_id,
            "raw_id": meta.raw_id if meta else None,
            "workspace_name": ws_name,
            "ws_name": ws_name,
            "name": meta.display_name if meta else ws_name,
            "display_name": meta.display_name if meta else ws_name,
            "visibility": meta.visibility if meta else "private",
            "created_at": meta.created_at.isoformat() if meta and meta.created_at else None,
            "href": ws.get("href")
        })
    return results

@router.get("/{identifier}")
def get_workspace(
    identifier: str,
    db: Session = Depends(get_db),
    key = Depends(get_verified_key)
):
    """
    Mendukung pengambilan detail workspace menggunakan:
    - Hashed ID (misal: 'UkLWZg9DAJQ7Xlrz')
    - Nama teknis GeoServer (misal: 'ws_pemetaan_...')
    - UUID
    """
    meta = resolve_workspace(db, identifier)
    ws_name = meta.workspace_name if meta else identifier

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
    
    # Tentukan nama teknis GeoServer
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
    """
    meta = resolve_workspace(db, identifier)
    ws_name = meta.workspace_name if meta else identifier

    # 1. Hapus dari GeoServer
    success = geoserver_client.delete_workspace(ws_name, recurse=recurse)
    if not success:
        raise HTTPException(status_code=500, detail=f"Gagal menghapus workspace '{ws_name}' dari GeoServer.")
    
    # 2. Hapus metadata dari database
    try:
        db.query(WorkspaceMetadata).filter(WorkspaceMetadata.workspace_name == ws_name).delete()
        db.commit()
    except Exception as e:
        db.rollback()

    display_title = meta.display_name if meta else ws_name
    return {"success": True, "detail": f"Workspace '{display_title}' berhasil dihapus."}
