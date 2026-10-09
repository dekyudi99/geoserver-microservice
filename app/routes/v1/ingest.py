import uuid
import json
import re
import logging
from pathlib import Path
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, BackgroundTasks, status
from sqlalchemy.orm import Session
from sqlalchemy import desc

from ...config.database import get_db
from ...config.settings import settings
from ...models.api_key import ApiKeyType
from ...models.ingest_job import IngestJob, IngestStatusEnum, IngestSourceEnum
from ...schemas.v1.ingest import (
    IngestJobResponse,
    IngestJobListResponse,
    BatchUploadResponse,
    BatchUploadItemResponse,
    BatchStatusRequest,
    BatchStatusResponse
)
from ...services.ingest.providers.local import LocalUploadProvider
from ...services.ingest.runners.background import BackgroundTasksRunner
from ...services.hash_id import resolve_workspace
from ...security.auth import get_verified_key

logger = logging.getLogger("geoserver_service.ingest_router")

router = APIRouter(prefix="/ingest", tags=["Data Ingestion"])

@router.post(
    "/uploads",
    response_model=IngestJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload & Ingest Spatial Data dari Perangkat (Single File)"
)
def upload_and_ingest(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="File GeoTIFF (.tif/.tiff), Shapefile (.zip), atau GeoJSON (.geojson/.json)"),
    workspace_name: str = Form(..., description="Hashed ID atau nama teknis workspace target"),
    layer_name: str = Form(..., description="Nama layer tampilan (display_name)"),
    description: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    caller = Depends(get_verified_key)
):
    """
    Menerima unggahan satu berkas spasial dari perangkat, men-stream ke disk staging,
    dan mendaftarkan pekerjaan ingest ke BackgroundTasks.
    """
    ws_meta = resolve_workspace(db, workspace_name)
    actual_ws_name = ws_meta.workspace_name if ws_meta else workspace_name

    is_primary = getattr(caller, "key_type", None) == ApiKeyType.PRIMARY
    if not is_primary and ws_meta and ws_meta.api_key_id != caller.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Anda tidak memiliki izin mengunggah layer ke workspace '{actual_ws_name}'."
        )

    job_id = uuid.uuid4()
    job_staging_dir = Path(settings.INGEST_STAGING_DIR) / f"job_{job_id}"

    provider = LocalUploadProvider(file)
    try:
        downloaded = provider.download(job_staging_dir)
    except ValueError as val_err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(val_err))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Gagal menyimpan berkas upload: {e}")

    created_by_val = getattr(caller, "name", None) or getattr(caller, "owner_info", None)
    job = IngestJob(
        id=job_id,
        owner_id=caller.id,
        created_by=str(created_by_val) if created_by_val else None,
        source_type=IngestSourceEnum.LOCAL_UPLOAD,
        workspace_name=actual_ws_name,
        layer_name=layer_name.strip(),
        status=IngestStatusEnum.QUEUED,
        progress=0,
        staging_file_path=str(downloaded.local_path),
        source_uri=downloaded.original_filename
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    runner = BackgroundTasksRunner(background_tasks)
    runner.submit_job(job.id)

    job.display_name = job.layer_name
    job.geoserver_name = job.result_layer_name
    return job

@router.post(
    "/batch-uploads",
    response_model=BatchUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload & Ingest Banyak Berkas Spasial Sekaligus (Multi-File)"
)
def batch_upload_and_ingest(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(..., description="Daftar berkas spasial (.tif, .tiff, .zip, .geojson, .json)"),
    workspace_name: Optional[str] = Form(None, description="Hashed ID atau nama teknis workspace target"),
    display_names: Optional[str] = Form(None, description="JSON array string berisi display_name untuk tiap file"),
    description: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    caller = Depends(get_verified_key)
):
    """
    Menerima banyak berkas spasial sekaligus dalam satu form submission, memvalidasi kuota/ukuran,
    menghasilkan IngestJob independen untuk setiap berkas, dan menjalankannya secara sekuensial/antrean.
    """
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tidak ada berkas yang diunggah.")

    if len(files) > settings.MAX_FILES_PER_BATCH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Jumlah berkas melebihi batas maksimum {settings.MAX_FILES_PER_BATCH} file per batch."
        )

    # Parsing nama tampilan per file jika disediakan
    parsed_names: List[str] = []
    if display_names:
        try:
            parsed = json.loads(display_names)
            if isinstance(parsed, list):
                parsed_names = [str(n).strip() for n in parsed]
        except Exception:
            parsed_names = [s.strip() for s in display_names.split(",") if s.strip()]

    target_ws = workspace_name or "astragis"
    ws_meta = resolve_workspace(db, target_ws)
    actual_ws_name = ws_meta.workspace_name if ws_meta else target_ws

    is_primary = getattr(caller, "key_type", None) == ApiKeyType.PRIMARY
    if not is_primary and ws_meta and ws_meta.api_key_id != caller.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Anda tidak memiliki izin mengunggah layer ke workspace '{actual_ws_name}'."
        )

    created_by_val = getattr(caller, "name", None) or getattr(caller, "owner_info", None)

    runner = BackgroundTasksRunner(background_tasks)
    created_items: List[BatchUploadItemResponse] = []
    total_batch_bytes = 0
    max_batch_bytes = settings.MAX_BATCH_SIZE_MB * 1024 * 1024

    for idx, f in enumerate(files):
        # Tentukan nama tampilan per file (dari display_names atau diturunkan dari file stem)
        if idx < len(parsed_names) and parsed_names[idx]:
            item_display_name = parsed_names[idx]
        else:
            raw_stem = Path(f.filename or "layer").stem
            clean_stem = re.sub(r"[-_]+", " ", raw_stem).strip()
            item_display_name = clean_stem.title() or f"Layer {idx + 1}"

        job_id = uuid.uuid4()
        job_staging_dir = Path(settings.INGEST_STAGING_DIR) / f"job_{job_id}"

        provider = LocalUploadProvider(f)
        downloaded = None
        item_error = None
        try:
            downloaded = provider.download(job_staging_dir)
            total_batch_bytes += downloaded.file_size
            if max_batch_bytes > 0 and total_batch_bytes > max_batch_bytes:
                item_error = f"Total ukuran batch melebihi batas {settings.MAX_BATCH_SIZE_MB}MB."
        except ValueError as val_err:
            item_error = str(val_err)
        except Exception as e:
            item_error = f"Gagal menyimpan file: {e}"

        if item_error or not downloaded:
            job = IngestJob(
                id=job_id,
                owner_id=caller.id,
                created_by=str(created_by_val) if created_by_val else None,
                source_type=IngestSourceEnum.LOCAL_UPLOAD,
                workspace_name=actual_ws_name,
                layer_name=item_display_name,
                status=IngestStatusEnum.FAILED,
                progress=0,
                staging_file_path="",
                source_uri=f.filename,
                error_message=item_error or "Unknown error"
            )
            db.add(job)
            db.commit()
            created_items.append(BatchUploadItemResponse(
                id=job.id,
                filename=f.filename or "unknown",
                display_name=item_display_name,
                geoserver_name=None,
                status=job.status,
                error_message=item_error
            ))
            continue

        job = IngestJob(
            id=job_id,
            owner_id=caller.id,
            created_by=str(created_by_val) if created_by_val else None,
            source_type=IngestSourceEnum.LOCAL_UPLOAD,
            workspace_name=actual_ws_name,
            layer_name=item_display_name,
            status=IngestStatusEnum.QUEUED,
            progress=0,
            staging_file_path=str(downloaded.local_path),
            source_uri=downloaded.original_filename
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        # Daftarkan ke worker background
        runner.submit_job(job.id)

        created_items.append(BatchUploadItemResponse(
            id=job.id,
            filename=f.filename or "unknown",
            display_name=item_display_name,
            geoserver_name=None,
            status=job.status,
            error_message=None
        ))


    return BatchUploadResponse(
        success=True,
        total_files=len(created_items),
        jobs=created_items
    )

@router.post(
    "/jobs/batch-status",
    response_model=BatchStatusResponse,
    summary="Cek Status Sekumpulan Ingest Job Sekaligus"
)
def get_batch_job_status(
    payload: BatchStatusRequest,
    db: Session = Depends(get_db),
    caller = Depends(get_verified_key)
):
    """Mengecek status kemajuan beberapa job sekaligus untuk pemantauan efisien multi-file."""
    query = db.query(IngestJob).filter(IngestJob.id.in_(payload.job_ids))
    if caller.key_type != ApiKeyType.PRIMARY:
        query = query.filter(IngestJob.owner_id == caller.id)

    jobs = query.all()
    for j in jobs:
        j.display_name = j.layer_name
        j.geoserver_name = j.result_layer_name

    return BatchStatusResponse(
        success=True,
        total=len(jobs),
        jobs=jobs
    )

@router.get(
    "/jobs/{job_id}",
    response_model=IngestJobResponse,
    summary="Cek Status Ingest Job"
)
def get_job_status(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    caller = Depends(get_verified_key)
):
    """Mengecek status dan kemajuan proses ingest job berdasarkan ID."""
    job = db.query(IngestJob).filter(IngestJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ingest Job '{job_id}' tidak ditemukan.")

    if caller.key_type != ApiKeyType.PRIMARY and job.owner_id != caller.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Anda tidak memiliki akses ke job ini.")

    job.display_name = job.layer_name
    job.geoserver_name = job.result_layer_name
    return job

@router.get(
    "/jobs",
    response_model=IngestJobListResponse,
    summary="Daftar Riwayat Ingest Jobs"
)
def list_jobs(
    status_filter: Optional[IngestStatusEnum] = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    caller = Depends(get_verified_key)
):
    """Melihat daftar riwayat job ingest milik user saat ini dengan filter dan paginasi."""
    query = db.query(IngestJob)
    if caller.key_type != ApiKeyType.PRIMARY:
        query = query.filter(IngestJob.owner_id == caller.id)

    if status_filter:
        query = query.filter(IngestJob.status == status_filter)

    total = query.count()
    jobs = query.order_by(desc(IngestJob.created_at)).offset(offset).limit(limit).all()

    for j in jobs:
        j.display_name = j.layer_name
        j.geoserver_name = j.result_layer_name

    return IngestJobListResponse(success=True, total=total, data=jobs)
