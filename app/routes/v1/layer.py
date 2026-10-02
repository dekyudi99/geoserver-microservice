from pathlib import Path
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
import logging
import os
import shutil
import uuid
import json
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, status, Depends
from sqlalchemy.orm import Session
from typing import Optional, Dict, Any
from ...services.vector_service import vector_service
from ...services.raster_service import raster_service
from ...clients.geoserver_client import geoserver_client
from ...config.database import get_db, engine
from sqlalchemy import text
from ...config.settings import settings
from ...schemas.v1.spatial_data import VectorPublishResponse, RasterPublishResponse
from ...models.api_key import ApiKey, ApiKeyType
from ...models.spatial_data import VectorLayer, RasterMetadata, WorkspaceMetadata
from ...services.hash_id import encode_id, decode_id, resolve_workspace
from ...security.auth import require_any_key
from ...services.audit_service import log_action
from ...models.ingest_job import IngestJob

logger = logging.getLogger("geoserver_service.v1_layer")

router = APIRouter(prefix="/layers", tags=["Layers"])

os.makedirs(settings.DATA_VECTOR_PATH, exist_ok=True)
os.makedirs(settings.DATA_RASTER_PATH, exist_ok=True)

@router.post("/publish-vector", response_model=VectorPublishResponse, status_code=status.HTTP_201_CREATED)
async def publish_vector(
    workspace_name: str = Form(...),
    layer_name: str = Form(...),
    table_name: Optional[str] = Form(None),
    simplify_tolerance: Optional[float] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    ext = os.path.splitext(file.filename)[1].lower()
    final_table_name = table_name or f"vec_{uuid.uuid4().hex[:10]}"
    unique_file = f"{final_table_name}{ext}"
    saved_path = os.path.normpath(os.path.join(settings.DATA_VECTOR_PATH, unique_file))

    try:
        with open(saved_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 1. Read vector
        gdf, format_name = vector_service.read_vector_file_to_gdf(saved_path)

        # 2. Simplify
        tol = simplify_tolerance if simplify_tolerance is not None else settings.SIMPLIFY_TOLERANCE
        simplified_gdf, stats = vector_service.simplify_vector_gdf(gdf, tolerance=tol)

        ws_meta = resolve_workspace(db, workspace_name)
        actual_ws_name = ws_meta.workspace_name if ws_meta else workspace_name

        is_primary = getattr(caller, "key_type", None) == ApiKeyType.PRIMARY
        if not is_primary and ws_meta and ws_meta.api_key_id != caller.id:
            raise HTTPException(
                status_code=403,
                detail=f"Anda tidak memiliki izin mengunggah layer ke workspace '{actual_ws_name}'."
            )

        # 3. Publish to PostGIS and GeoServer
        res = vector_service.publish_vector_to_geoserver(
            gdf=simplified_gdf,
            workspace_name=actual_ws_name,
            table_name=final_table_name,
            title=layer_name,
            geom_type=stats.get("geometry_type", "Polygon")
        )

        # 4. Save metadata with owner api_key_id
        vector_layer = VectorLayer(
            api_key_id=caller.id,
            workspace_name=actual_ws_name,
            table_name=final_table_name,
            layer_name=layer_name,
            geom_type=res.get("geom_type"),
            feature_count=res.get("feature_count"),
            bbox=res.get("bbox"),
            srid=res.get("srid"),
            file_path=saved_path,
            wms_url=res.get("wms_url")
        )
        db.add(vector_layer)
        db.commit()

        # 5. Audit Log
        log_action(
            db=db,
            api_key=caller,
            action="PUBLISH_VECTOR",
            resource_type="LAYER",
            resource_id=str(vector_layer.id),
            resource_name=layer_name,
            status="SUCCESS"
        )

        return VectorPublishResponse(
            success=True,
            workspace_name=actual_ws_name,
            table_name=final_table_name,
            title=layer_name,
            geom_type=res["geom_type"],
            feature_count=res["feature_count"],
            bbox=res["bbox"],
            srid=res["srid"],
            simplification=stats,
            wms_url=res["wms_url"]
        )
    except Exception as e:
        log_action(
            db=db,
            api_key=caller,
            action="PUBLISH_VECTOR",
            resource_type="LAYER",
            resource_name=layer_name,
            status="FAILED",
            detail=str(e)
        )
        raise HTTPException(status_code=400, detail=f"Gagal memproses data vektor: {str(e)}")

@router.post("/publish-raster", response_model=RasterPublishResponse, status_code=status.HTTP_201_CREATED)
async def publish_raster(
    workspace_name: str = Form(...),
    layer_name: str = Form(...),
    store_name: Optional[str] = Form(None),
    style_config: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ('.tif', '.tiff'):
        raise HTTPException(status_code=400, detail="Hanya format .tif dan .tiff yang didukung untuk raster.")

    final_store_name = store_name or f"ras_{uuid.uuid4().hex[:10]}"
    unique_file = f"{final_store_name}{ext}"
    saved_path = os.path.normpath(os.path.join(settings.DATA_RASTER_PATH, unique_file))

    try:
        with open(saved_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        parsed_style = json.loads(style_config) if style_config else None

        ws_meta = resolve_workspace(db, workspace_name)
        actual_ws_name = ws_meta.workspace_name if ws_meta else workspace_name

        is_primary = getattr(caller, "key_type", None) == ApiKeyType.PRIMARY
        if not is_primary and ws_meta and ws_meta.api_key_id != caller.id:
            raise HTTPException(
                status_code=403,
                detail=f"Anda tidak memiliki izin mengunggah layer ke workspace '{actual_ws_name}'."
            )

        res = raster_service.publish_raster_to_geoserver(
            file_path=saved_path,
            workspace_name=actual_ws_name,
            store_name=final_store_name,
            title=layer_name,
            style_config=parsed_style
        )

        raster_meta = RasterMetadata(
            api_key_id=caller.id,
            workspace_name=actual_ws_name,
            store_name=final_store_name,
            layer_name=layer_name,
            epsg=res.get("epsg"),
            bbox=res.get("bbox"),
            dimensions=res.get("dimensions"),
            file_path=saved_path,
            wms_url=res.get("wms_url"),
            symbology=res.get("symbology")
        )
        db.add(raster_meta)
        db.commit()

        log_action(
            db=db,
            api_key=caller,
            action="PUBLISH_RASTER",
            resource_type="LAYER",
            resource_id=str(raster_meta.id),
            resource_name=layer_name,
            status="SUCCESS"
        )

        return RasterPublishResponse(
            success=True,
            workspace_name=actual_ws_name,
            store_name=final_store_name,
            title=layer_name,
            epsg=res["epsg"],
            bbox=res["bbox"],
            dimensions=res["dimensions"],
            wms_url=res["wms_url"],
            symbology=res.get("symbology")
        )
    except Exception as e:
        log_action(
            db=db,
            api_key=caller,
            action="PUBLISH_RASTER",
            resource_type="LAYER",
            resource_name=layer_name,
            status="FAILED",
            detail=str(e)
        )
        raise HTTPException(status_code=400, detail=f"Gagal memproses raster: {str(e)}")

def _detect_file_format(file_path: Optional[str], default_format: str) -> str:
    if not file_path:
        return default_format
    ext = Path(file_path).suffix.lower()
    if ext in [".tif", ".tiff"]:
        return "tif"
    elif ext in [".geojson", ".json"]:
        return "geojson"
    elif ext in [".csv"]:
        return "csv"
    elif ext in [".kml"]:
        return "kml"
    elif ext in [".kmz"]:
        return "kmz"
    elif ext in [".zip", ".shp"]:
        return "shp"
    return default_format

@router.get("/my-layers")
def get_my_layers(
    layer_type: Optional[str] = None,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Mengambil semua data layer milik API Key yang sedang memanggil.
    Menjamin isolasi kepemilikan data antar akun/sistem eksternal.
    Urutan default: data terbaru di awal (created_at DESC).
    """
    result = []
    if layer_type != "raster":
        vectors = db.query(VectorLayer).filter(VectorLayer.api_key_id == caller.id).order_by(VectorLayer.created_at.desc()).all()
        for v in vectors:
            v_fmt = _detect_file_format(v.file_path, "shp")
            result.append({
                "id": str(v.id),
                "type": "vector",
                "data_type": v_fmt,
                "file_format": v_fmt,
                "workspace_name": v.workspace_name,
                "table_name": v.table_name,
                "geoserver_name": v.table_name,
                "layer_name": v.layer_name,
                "display_name": v.layer_name,
                "geom_type": v.geom_type,
                "feature_count": v.feature_count,
                "bbox": v.bbox,
                "srid": v.srid or 4326,
                "epsg": v.srid or 4326,
                "wms_url": v.wms_url,
                "created_at": str(v.created_at)
            })
    if layer_type != "vector":
        rasters = db.query(RasterMetadata).filter(RasterMetadata.api_key_id == caller.id).order_by(RasterMetadata.created_at.desc()).all()
        for r in rasters:
            r_fmt = _detect_file_format(r.file_path, "tif")
            result.append({
                "id": str(r.id),
                "type": "raster",
                "data_type": r_fmt,
                "file_format": r_fmt,
                "workspace_name": r.workspace_name,
                "store_name": r.store_name,
                "geoserver_name": r.store_name,
                "layer_name": r.layer_name,
                "display_name": r.layer_name,
                "bbox": r.bbox,
                "dimensions": r.dimensions,
                "epsg": r.epsg or 4326,
                "wms_url": r.wms_url,
                "created_at": str(r.created_at)
            })
    # Enrich with Workspace Metadata (Hashed ID and Display Name)
    ws_metas = {m.workspace_name: m for m in db.query(WorkspaceMetadata).all()}
    for item in result:
        meta = ws_metas.get(item["workspace_name"])
        hashed_ws_id = encode_id(meta.raw_id) if meta and meta.raw_id is not None else item["workspace_name"]
        item["workspace_id"] = hashed_ws_id
        item["workspace_hashed_id"] = hashed_ws_id
        item["workspace_display_name"] = meta.display_name if meta else item["workspace_name"]

    # Urutkan seluruh layer (vektor + raster) berdasarkan created_at descending (terbaru di atas)
    result.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)

    return {"total": len(result), "data": result}


def _is_valid_uuid(val: str) -> bool:
    try:
        uuid.UUID(str(val))
        return True
    except Exception:
        return False

class BatchDeleteLayersRequest(BaseModel):
    layer_ids: List[str]

@router.post("/batch-delete")
def batch_delete_layers(
    req: BatchDeleteLayersRequest,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Menghapus banyak layer sekaligus berdasarkan daftar ID layer terpilih.
    Membersihkan GeoServer, PostGIS table, storage disk, dan PostgreSQL.
    """
    if not req.layer_ids:
        return {"success": True, "deleted_count": 0, "detail": "Tidak ada layer yang dipilih untuk dihapus."}

    deleted_count = 0
    errors = []

    for lid in req.layer_ids:
        try:
            is_uuid = _is_valid_uuid(lid)
            vec = db.query(VectorLayer).filter(VectorLayer.api_key_id == caller.id).filter(
                (VectorLayer.id == uuid.UUID(lid) if is_uuid else False) |
                (VectorLayer.table_name == lid) |
                (VectorLayer.layer_name == lid)
            ).first()

            if vec:
                actual_ws = vec.workspace_name
                # Hapus layer resource dari GeoServer
                try:
                    geoserver_client.delete_layer(workspace_name=actual_ws, layer_name=vec.table_name, recurse=True)
                except Exception:
                    pass
                # Hapus FeatureType dari datastore PostGIS di GeoServer
                try:
                    geoserver_client.delete_feature_type(workspace_name=actual_ws, store_name="postgis_default", feature_type_name=vec.table_name, recurse=True)
                except Exception:
                    pass
                # Hapus tabel PostGIS
                try:
                    with engine.begin() as conn:
                        conn.execute(text(f'DROP TABLE IF EXISTS "{vec.table_name}" CASCADE;'))
                except Exception:
                    pass
                # Hapus file fisik jika ada
                if vec.file_path and os.path.exists(vec.file_path):
                    try:
                        os.remove(vec.file_path)
                    except Exception:
                        pass
                # Hapus staging file dari IngestJob yang terkait
                ingest_jobs = db.query(IngestJob).filter(
                    IngestJob.owner_id == caller.id,
                    IngestJob.result_layer_name == vec.table_name
                ).all()
                for ij in ingest_jobs:
                    if ij.staging_file_path and os.path.exists(ij.staging_file_path):
                        try:
                            os.remove(ij.staging_file_path)
                        except Exception:
                            pass
                    db.delete(ij)
                db.delete(vec)
                db.commit()
                deleted_count += 1
                continue

            ras = db.query(RasterMetadata).filter(RasterMetadata.api_key_id == caller.id).filter(
                (RasterMetadata.id == uuid.UUID(lid) if is_uuid else False) |
                (RasterMetadata.store_name == lid) |
                (RasterMetadata.layer_name == lid) |
                (RasterMetadata.store_name == f"store_{lid}")
            ).first()

            if ras:
                actual_ws = ras.workspace_name
                for target_lyr in [ras.store_name, ras.layer_name, ras.store_name.replace("store_", "")]:
                    try:
                        geoserver_client.delete_layer(workspace_name=actual_ws, layer_name=target_lyr, recurse=True)
                    except Exception:
                        pass
                try:
                    geoserver_client.delete_coverage_store(workspace_name=actual_ws, store_name=ras.store_name, recurse=True)
                except Exception:
                    pass
                if ras.file_path and os.path.exists(ras.file_path):
                    try:
                        os.remove(ras.file_path)
                    except Exception:
                        pass
                db.delete(ras)
                db.commit()
                deleted_count += 1
                continue

            # Fallback jika orphan di GeoServer
            deleted_count += 1
        except Exception as e:
            logger.warning(f"Error batch deleting layer {lid}: {e}")
            errors.append(f"{lid}: {str(e)}")

    log_action(
        db=db,
        api_key=caller,
        action="BATCH_DELETE_LAYERS",
        resource_type="LAYER",
        resource_name=f"{deleted_count} layers",
        status="SUCCESS"
    )
    return {
        "success": True,
        "deleted_count": deleted_count,
        "detail": f"{deleted_count} layer berhasil dihapus."
    }


@router.delete("/{workspace_name}/{layer_name}")
def delete_layer(
    workspace_name: str,
    layer_name: str,
    layer_id: Optional[str] = None,
    recurse: bool = True,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Menghapus layer secara komprehensif:
    1. Mencari di database (VectorLayer / RasterMetadata) milik caller.
    2. Menghapus konfigurasi di GeoServer (layer & coverage store / feature type).
    3. Menghapus tabel di PostGIS (jika vector).
    4. Menghapus file fisik di storage lokal.
    5. Menghapus record baris di database PostgreSQL.
    Toleran terhadap layer yang sudah terhapus di GeoServer (tidak melempar 500).
    """
    ws_meta = resolve_workspace(db, workspace_name)
    actual_ws_name = ws_meta.workspace_name if ws_meta else workspace_name

    vec = None
    ras = None

    # Cari vector layer
    vec_query = db.query(VectorLayer).filter(VectorLayer.api_key_id == caller.id)
    if layer_id:
        try:
            vec = vec_query.filter(VectorLayer.id == uuid.UUID(layer_id)).first()
        except Exception:
            pass
    if not vec:
        try:
            potential_uuid = uuid.UUID(layer_name)
            vec = vec_query.filter(VectorLayer.id == potential_uuid).first()
        except Exception:
            pass
    if not vec:
        vec = vec_query.filter(
            (VectorLayer.table_name == layer_name) |
            (VectorLayer.layer_name == layer_name)
        ).first()

    # Cari raster jika bukan vector
    if not vec:
        ras_query = db.query(RasterMetadata).filter(RasterMetadata.api_key_id == caller.id)
        if layer_id:
            try:
                ras = ras_query.filter(RasterMetadata.id == uuid.UUID(layer_id)).first()
            except Exception:
                pass
        if not ras:
            try:
                potential_uuid = uuid.UUID(layer_name)
                ras = ras_query.filter(RasterMetadata.id == potential_uuid).first()
            except Exception:
                pass
        if not ras:
            ras = ras_query.filter(
                (RasterMetadata.store_name == layer_name) |
                (RasterMetadata.layer_name == layer_name) |
                (RasterMetadata.store_name == f"store_{layer_name}")
            ).first()

    deleted_something = False

    # 2. Proses penghapusan Vector
    if vec:
        # Hapus layer resource dari GeoServer
        try:
            geoserver_client.delete_layer(workspace_name=actual_ws_name, layer_name=vec.table_name, recurse=recurse)
        except Exception as e:
            logger.warning(f"GeoServer delete_layer vector notice: {e}")
        # Hapus FeatureType dari datastore PostGIS di GeoServer
        try:
            geoserver_client.delete_feature_type(workspace_name=actual_ws_name, store_name="postgis_default", feature_type_name=vec.table_name, recurse=recurse)
        except Exception as e:
            logger.warning(f"GeoServer delete_feature_type vector notice: {e}")

        try:
            with engine.begin() as conn:
                conn.execute(text(f'DROP TABLE IF EXISTS "{vec.table_name}" CASCADE;'))
        except Exception as e:
            logger.warning(f"PostGIS drop table notice: {e}")

        if vec.file_path and os.path.exists(vec.file_path):
            try:
                os.remove(vec.file_path)
            except Exception:
                pass

        # Hapus staging file dari IngestJob yang terkait
        ingest_jobs = db.query(IngestJob).filter(
            IngestJob.owner_id == caller.id,
            IngestJob.result_layer_name == vec.table_name
        ).all()
        for ij in ingest_jobs:
            if ij.staging_file_path and os.path.exists(ij.staging_file_path):
                try:
                    os.remove(ij.staging_file_path)
                except Exception:
                    pass
            db.delete(ij)

        db.delete(vec)
        db.commit()
        deleted_something = True

    # 3. Proses penghapusan Raster
    elif ras:
        targets = set([ras.store_name, ras.layer_name, ras.store_name.replace("store_", "")])
        for target_lyr in targets:
            try:
                geoserver_client.delete_layer(workspace_name=actual_ws_name, layer_name=target_lyr, recurse=recurse)
            except Exception:
                pass
        try:
            geoserver_client.delete_coverage_store(workspace_name=actual_ws_name, store_name=ras.store_name, recurse=recurse)
        except Exception as e:
            logger.warning(f"GeoServer delete_coverage_store notice: {e}")

        if ras.file_path and os.path.exists(ras.file_path):
            try:
                os.remove(ras.file_path)
            except Exception:
                pass

        db.delete(ras)
        db.commit()
        deleted_something = True

    # 4. Fallback jika tidak tercatat di DB (misal orphan di GeoServer)
    if not deleted_something:
        try:
            geoserver_client.delete_layer(workspace_name=actual_ws_name, layer_name=layer_name, recurse=recurse)
        except Exception:
            pass
        try:
            geoserver_client.delete_layer(workspace_name=actual_ws_name, layer_name=f"store_{layer_name}", recurse=recurse)
        except Exception:
            pass
        try:
            geoserver_client.delete_coverage_store(workspace_name=actual_ws_name, store_name=layer_name, recurse=recurse)
        except Exception:
            pass
        try:
            geoserver_client.delete_coverage_store(workspace_name=actual_ws_name, store_name=f"store_{layer_name}", recurse=recurse)
        except Exception:
            pass

    log_action(
        db=db,
        api_key=caller,
        action="DELETE_LAYER",
        resource_type="LAYER",
        resource_name=layer_name,
        status="SUCCESS"
    )
    return {"success": True, "detail": f"Layer '{layer_name}' berhasil dihapus."}

@router.get("/{layer_name}")
def get_layer_metadata(
    layer_name: str,
    workspace: Optional[str] = None,
    caller: ApiKey = Depends(require_any_key)
):
    layer_info = geoserver_client.get_layer(layer_name=layer_name, workspace_name=workspace)
    if not layer_info:
        raise HTTPException(status_code=404, detail=f"Layer '{layer_name}' tidak ditemukan di GeoServer.")
    return layer_info


