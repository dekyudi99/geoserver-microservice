from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, Field
from ..config.database import get_db
from ..models.api_key import ApiKey, ApiKeyType
from ..security.auth import require_primary_key, clear_key_cache, get_verified_key
from ..services.api_key_service import generate_api_key, delete_api_key, list_api_keys
from ..services.audit_service import log_action

router = APIRouter(prefix="/api-keys", tags=["API Key Management"])

import os

def format_size(bytes_num: int) -> str:
    if bytes_num < 1024:
        return f"{bytes_num} B"
    elif bytes_num < 1024 * 1024:
        return f"{bytes_num / 1024:.2f} KB"
    elif bytes_num < 1024 * 1024 * 1024:
        return f"{bytes_num / (1024 * 1024):.2f} MB"
    else:
        return f"{bytes_num / (1024 * 1024 * 1024):.2f} GB"

def get_disk_usage_for_key(api_key_id, db: Session) -> dict:
    from ..models.spatial_data import RasterMetadata, VectorLayer
    raster_rows = db.query(RasterMetadata).filter(RasterMetadata.api_key_id == api_key_id).all()
    vector_rows = db.query(VectorLayer).filter(VectorLayer.api_key_id == api_key_id).all()
    
    total_bytes = 0
    raster_bytes = 0
    vector_bytes = 0
    
    for r in raster_rows:
        if r.file_path and os.path.exists(r.file_path):
            try:
                sz = os.path.getsize(r.file_path)
                raster_bytes += sz
                total_bytes += sz
            except Exception:
                pass
                
    for v in vector_rows:
        if v.file_path and os.path.exists(v.file_path):
            try:
                sz = os.path.getsize(v.file_path)
                vector_bytes += sz
                total_bytes += sz
            except Exception:
                pass
                
    return {
        "total_bytes": total_bytes,
        "total_readable": format_size(total_bytes),
        "raster_bytes": raster_bytes,
        "raster_readable": format_size(raster_bytes),
        "vector_bytes": vector_bytes,
        "vector_readable": format_size(vector_bytes),
        "total_layers": len(raster_rows) + len(vector_rows),
        "raster_count": len(raster_rows),
        "vector_count": len(vector_rows)
    }


class CreateApiKeyRequest(BaseModel):
    name: str = Field(..., description="Nama atau identitas pemilik API Key")
    key_type: ApiKeyType = Field(ApiKeyType.STANDARD, description="Tipe key: PRIMARY atau STANDARD")
    owner_info: Optional[str] = Field(None, description="Metadata JSON pemilik data/sistem eksternal")

class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key_type: str
    key_prefix: str
    is_active: bool
    owner_info: Optional[str] = None
    created_at: str
    last_used_at: Optional[str] = None
    disk_usage: Optional[dict] = None

@router.post("", status_code=status.HTTP_201_CREATED)
def create_api_key(
    body: CreateApiKeyRequest,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_primary_key)
):
    """
    Membuat API Key baru.
    Hanya dapat dilakukan oleh pemegang PRIMARY API Key.
    """
    plain_key, new_key = generate_api_key(
        key_type=body.key_type,
        db=db,
        name=body.name,
        owner_info=body.owner_info
    )

    log_action(
        db=db,
        api_key=caller,
        action="CREATE_API_KEY",
        resource_type="API_KEY",
        resource_id=str(new_key.id),
        resource_name=new_key.name,
        status="SUCCESS"
    )

    return {
        "success": True,
        "id": str(new_key.id),
        "name": new_key.name,
        "key_type": new_key.key_type.value,
        "api_key": plain_key,
        "warning": "Simpan API key ini sekarang! Nilai plain key tidak dapat dilihat lagi setelah ini."
    }

@router.get("", response_model=List[ApiKeyResponse])
def get_all_api_keys(
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_primary_key)
):
    """Melihat daftar seluruh API Key aktif."""
    keys = list_api_keys(db)
    result_list = [
        ApiKeyResponse(
            id=str(k.id),
            name=k.name,
            key_type=k.key_type.value,
            key_prefix=k.key_prefix,
            is_active=k.is_active,
            owner_info=k.owner_info,
            created_at=str(k.created_at),
            last_used_at=str(k.last_used_at) if k.last_used_at else None,
            disk_usage=get_disk_usage_for_key(k.id, db)
        )
        for k in keys
    ]
    return result_list

@router.delete("/{key_id}", status_code=status.HTTP_200_OK)
def deactivate_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_primary_key)
):
    """Menonaktifkan API Key."""
    success = delete_api_key(key_id, db)
    if not success:
        raise HTTPException(status_code=404, detail="API Key tidak ditemukan atau sudah nonaktif.")

    log_action(
        db=db,
        api_key=caller,
        action="DEACTIVATE_API_KEY",
        resource_type="API_KEY",
        resource_id=key_id,
        status="SUCCESS"
    )

    return {"success": True, "detail": f"API Key {key_id} berhasil dinonaktifkan."}


@router.patch("/{key_id}/toggle", status_code=status.HTTP_200_OK)
def toggle_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_primary_key)
):
    """Mengaktifkan atau menonaktifkan API Key (hanya Primary Key)."""
    api_key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not api_key:
        raise HTTPException(status_code=404, detail="API Key tidak ditemukan.")

    api_key.is_active = not api_key.is_active
    db.commit()
    clear_key_cache()

    log_action(
        db=db,
        api_key=caller,
        action="TOGGLE_API_KEY",
        resource_type="API_KEY",
        resource_id=key_id,
        status="SUCCESS"
    )

    return {
        "success": True,
        "id": str(api_key.id),
        "is_active": api_key.is_active,
        "detail": f"Status API Key diubah menjadi {'AKTIF' if api_key.is_active else 'NONAKTIF'}."
    }


@router.get("/usage")
def get_caller_key_usage(
    db: Session = Depends(get_db),
    caller = Depends(get_verified_key)
):
    """Mendapatkan total disk / storage usage untuk pemegang API Key saat ini."""
    return get_disk_usage_for_key(caller.id, db)
