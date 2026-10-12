from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, Field
from ...config.database import get_db
from ...models.api_key import ApiKey, ApiKeyType
from ...security.auth import require_primary_key, clear_key_cache, get_verified_key, require_primary_or_internal_secret
from ...services.api_key_service import generate_api_key, delete_api_key, list_api_keys, rotate_api_key
from ...services.audit_service import log_action
from ...config.settings import settings
from types import SimpleNamespace
import json
import os

router = APIRouter(prefix="/api-keys", tags=["API Key Management"])

def format_size(bytes_num: int) -> str:
    if bytes_num < 1024:
        return f"{bytes_num} B"
    elif bytes_num < 1024 * 1024:
        return f"{bytes_num / 1024:.2f} KB"
    elif bytes_num < 1024 * 1024 * 1024:
        return f"{bytes_num / (1024 * 1024):.2f} MB"
    else:
        return f"{bytes_num / (1024 * 1024 * 1024):.2f} GB"

MAX_SPATIAL_QUOTA_BYTES = settings.DEFAULT_QUOTA_MB * 1024 * 1024

def get_disk_usage_for_key(api_key_id, db: Session, quota_bytes: Optional[int] = None) -> dict:
    from ...models.spatial_data import RasterMetadata, VectorLayer
    from sqlalchemy import text
    
    if quota_bytes is None:
        key_obj = db.query(ApiKey).filter(ApiKey.id == api_key_id).first()
        quota_bytes = (key_obj.quota_bytes if key_obj and key_obj.quota_bytes else None) or (settings.DEFAULT_QUOTA_MB * 1024 * 1024)
        
    max_quota_bytes = max(1024 * 1024, quota_bytes)
    
    raster_rows = db.query(RasterMetadata).filter(RasterMetadata.api_key_id == api_key_id).all()
    vector_rows = db.query(VectorLayer).filter(VectorLayer.api_key_id == api_key_id).all()
    
    total_bytes = 0
    raster_bytes = 0
    vector_bytes = 0
    
    # 1. Hitung ukuran data raster dari file fisik di disk
    for r in raster_rows:
        if r.file_path and os.path.exists(r.file_path):
            try:
                sz = os.path.getsize(r.file_path)
                raster_bytes += sz
                total_bytes += sz
            except Exception:
                pass
                
    # 2. Hitung ukuran data vektor secara real-time dari tabel database PostGIS
    try:
        sql_vector_tbl_size = text("""
            SELECT COALESCE(SUM(pg_total_relation_size(to_regclass(format('%I', table_name)))), 0)
            FROM vector_layers
            WHERE api_key_id = :api_key_id
        """)
        postgis_tables_size = int(db.execute(sql_vector_tbl_size, {"api_key_id": api_key_id}).scalar() or 0)
    except Exception:
        postgis_tables_size = 0

    vector_bytes += postgis_tables_size
    total_bytes += postgis_tables_size

    # 3. Hitung juga file mentah vektor jika tersimpan di disk (/data_vector/)
    for v in vector_rows:
        if v.file_path and os.path.exists(v.file_path):
            try:
                sz = os.path.getsize(v.file_path)
                vector_bytes += sz
                total_bytes += sz
            except Exception:
                pass
                
    remaining_bytes = max(0, max_quota_bytes - total_bytes)
    usage_percent = round((total_bytes / max_quota_bytes) * 100, 2) if max_quota_bytes > 0 else 0.0

    return {
        "total_bytes": total_bytes,
        "total_readable": format_size(total_bytes),
        "max_quota_bytes": max_quota_bytes,
        "max_quota_readable": format_size(max_quota_bytes),
        "max_quota_mb": round(max_quota_bytes / (1024 * 1024)),
        "remaining_bytes": remaining_bytes,
        "remaining_readable": format_size(remaining_bytes),
        "usage_percent": min(100.0, usage_percent),
        "is_quota_exceeded": total_bytes >= max_quota_bytes,
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
    quota_mb: Optional[int] = Field(None, description="Batas kuota disk spasial dalam MB (opsional, default 500 MB)")

class UpdateQuotaRequest(BaseModel):
    quota_mb: int = Field(..., ge=1, le=102400, description="Batas kuota penyimpanan spasial dalam MB")

class UpdateKeyTypeRequest(BaseModel):
    key_type: ApiKeyType = Field(..., description="Tipe baru: PRIMARY atau STANDARD")

class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key_type: str
    key_prefix: str
    is_active: bool
    owner_info: Optional[str] = None
    created_at: str
    last_used_at: Optional[str] = None
    quota_bytes: Optional[int] = None
    quota_mb: Optional[int] = None
    disk_usage: Optional[dict] = None

@router.post("", status_code=status.HTTP_201_CREATED, include_in_schema=False)
def create_api_key(
    body: CreateApiKeyRequest,
    db: Session = Depends(get_db),
    caller: SimpleNamespace = Depends(require_primary_or_internal_secret)
):
    """
    Membuat API Key baru (Private S2S / Admin Only).
    Memeriksa identitas user agar 1 user hanya memiliki tepat 1 API Key.
    """
    if body.owner_info:
        try:
            owner_data = json.loads(body.owner_info) if isinstance(body.owner_info, str) else body.owner_info
            u_id = owner_data.get("user_id")
            if u_id is not None:
                existing_keys = db.query(ApiKey).all()
                for k in existing_keys:
                    if k.owner_info:
                        try:
                            k_owner = json.loads(k.owner_info) if isinstance(k.owner_info, str) else k.owner_info
                            if str(k_owner.get("user_id")) == str(u_id):
                                raise HTTPException(
                                    status_code=status.HTTP_409_CONFLICT,
                                    detail=f"User ID {u_id} sudah memiliki API Key terdaftar ({k.name}). 1 User hanya boleh memiliki 1 API Key."
                                )
                        except HTTPException:
                            raise
                        except Exception:
                            pass
        except HTTPException:
            raise
        except Exception:
            pass

    plain_key, new_key = generate_api_key(
        key_type=body.key_type,
        db=db,
        name=body.name,
        owner_info=body.owner_info
    )

    # Set custom quota jika diberikan
    if body.quota_mb:
        new_key.quota_bytes = body.quota_mb * 1024 * 1024
        db.commit()

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
        "quota_mb": round((new_key.quota_bytes or 524288000) / (1024 * 1024)),
        "warning": "Simpan API key ini sekarang! Nilai plain key tidak dapat dilihat lagi setelah ini."
    }

@router.get("", response_model=List[ApiKeyResponse], include_in_schema=False)
def get_all_api_keys(
    db: Session = Depends(get_db),
    caller: SimpleNamespace = Depends(require_primary_or_internal_secret)
):
    """Melihat daftar seluruh API Key terdaftar (Hanya Primary Key / Internal Secret)."""
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
            quota_bytes=k.quota_bytes or (settings.DEFAULT_QUOTA_MB * 1024 * 1024),
            quota_mb=round((k.quota_bytes or (settings.DEFAULT_QUOTA_MB * 1024 * 1024)) / (1024 * 1024)),
            disk_usage=get_disk_usage_for_key(k.id, db, k.quota_bytes)
        )
        for k in keys
    ]
    return result_list

@router.delete("/{key_id}", status_code=status.HTTP_200_OK, include_in_schema=False)
def deactivate_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    caller: SimpleNamespace = Depends(require_primary_or_internal_secret)
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

@router.patch("/{key_id}/toggle", status_code=status.HTTP_200_OK, include_in_schema=False)
def toggle_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    caller: SimpleNamespace = Depends(require_primary_or_internal_secret)
):
    """
    Mengaktifkan atau menonaktifkan API Key apa pun (termasuk Primary Key).
    Hanya dapat dilakukan oleh pemegang Primary Key atau Internal Service.
    """
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

@router.post("/{key_id}/rotate", status_code=status.HTTP_200_OK, include_in_schema=False)
def rotate_api_key_endpoint(
    key_id: str,
    db: Session = Depends(get_db),
    caller: SimpleNamespace = Depends(require_primary_or_internal_secret)
):
    """
    Merotasi rahasia API Key yang ada secara in-place tanpa menghapus ID/record.
    UUID (ID) tetap sama sehingga seluruh relasi layer spasial dan workspace tetap utuh.
    """
    try:
        new_plain_key, api_key = rotate_api_key(key_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    log_action(
        db=db,
        api_key=caller,
        action="ROTATE_API_KEY",
        resource_type="API_KEY",
        resource_id=str(api_key.id),
        resource_name=api_key.name,
        status="SUCCESS"
    )

    return {
        "success": True,
        "id": str(api_key.id),
        "name": api_key.name,
        "key_type": api_key.key_type.value,
        "api_key": new_plain_key,
        "detail": "Kunci berhasil dirotasi secara in-place. Seluruh relasi data spasial dan workspace Anda tetap aman."
    }

@router.patch("/{key_id}/quota", status_code=status.HTTP_200_OK, include_in_schema=False)
def update_key_quota(
    key_id: str,
    body: UpdateQuotaRequest,
    db: Session = Depends(get_db),
    caller: SimpleNamespace = Depends(require_primary_or_internal_secret)
):
    """
    Mengubah batas kuota disk spasial untuk API Key tertentu (MB).
    Hanya dapat dilakukan oleh pemegang PRIMARY API Key atau Internal Service.
    """
    api_key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not api_key:
        raise HTTPException(status_code=404, detail="API Key tidak ditemukan.")

    new_bytes = body.quota_mb * 1024 * 1024
    api_key.quota_bytes = new_bytes
    db.commit()
    clear_key_cache()

    log_action(
        db=db,
        api_key=caller,
        action="UPDATE_API_KEY_QUOTA",
        resource_type="API_KEY",
        resource_id=key_id,
        resource_name=api_key.name,
        status="SUCCESS",
        detail=f"Batas kuota disk berhasil diubah menjadi {body.quota_mb} MB ({format_size(new_bytes)})"
    )

    return {
        "success": True,
        "id": str(api_key.id),
        "quota_mb": body.quota_mb,
        "quota_bytes": new_bytes,
        "quota_readable": format_size(new_bytes),
        "detail": f"Batas kuota disk berhasil diubah menjadi {body.quota_mb} MB."
    }

@router.patch("/{key_id}/type", status_code=status.HTTP_200_OK, include_in_schema=False)
def update_key_type(
    key_id: str,
    body: UpdateKeyTypeRequest,
    db: Session = Depends(get_db),
    caller: SimpleNamespace = Depends(require_primary_or_internal_secret)
):
    """
    Mengubah tipe API Key (PRIMARY <-> STANDARD) saat role user berubah.
    Hanya dapat dilakukan oleh pemegang PRIMARY API Key atau Internal Service.
    """
    api_key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not api_key:
        raise HTTPException(status_code=404, detail="API Key tidak ditemukan.")

    api_key.key_type = body.key_type
    api_key.key_prefix = "gsvc_pk_" if body.key_type == ApiKeyType.PRIMARY else "gsvc_sk_"
    db.commit()
    clear_key_cache()

    log_action(
        db=db,
        api_key=caller,
        action="UPDATE_API_KEY_TYPE",
        resource_type="API_KEY",
        resource_id=key_id,
        resource_name=api_key.name,
        status="SUCCESS",
        detail=f"Tipe API Key berhasil diubah menjadi {body.key_type.value}"
    )

    return {
        "success": True,
        "id": str(api_key.id),
        "key_type": api_key.key_type.value,
        "key_prefix": api_key.key_prefix,
        "detail": f"Tipe API Key berhasil diubah menjadi {api_key.key_type.value}."
    }

@router.get("/usage")
def get_caller_key_usage(
    db: Session = Depends(get_db),
    caller = Depends(get_verified_key)
):
    """Mendapatkan total disk / storage usage untuk pemegang API Key saat ini."""
    return get_disk_usage_for_key(caller.id, db)



