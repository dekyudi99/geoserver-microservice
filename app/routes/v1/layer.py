from pathlib import Path
from typing import List, Optional, Literal, Tuple
from pydantic import BaseModel
import logging
import os
import shutil
import uuid
import json
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, status, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional
from ...services.vector_service import vector_service
from ...services.raster_service import raster_service
from ...clients.geoserver_client import geoserver_client
from ...config.database import get_db, spatial_engine
from sqlalchemy import text
from ...config.settings import settings
from ...schemas.v1.spatial_data import VectorPublishResponse, RasterPublishResponse
from ...models.api_key import ApiKey, ApiKeyType
from ...models.spatial_data import VectorLayer, RasterMetadata, WorkspaceMetadata
from ...services.hash_id import encode_id, resolve_workspace
from ...security.auth import require_any_key
from ...security.rate_limiter import rate_limit
from .features import invalidate_schema_cache
from ...services.audit_service import log_action
from ...models.ingest_job import IngestJob
from .api_keys import get_disk_usage_for_key, MAX_SPATIAL_QUOTA_BYTES
from dotenv import load_dotenv

load_dotenv()

def build_dynamic_wms_url(workspace_name: str) -> str:
    """
    Menghasilkan WMS URL dinamis berbasis environment variable GEOSERVER_WMS_URL.
    Menggabungkan domain base dari .env dengan nama workspace sehingga sistem dapat
    berganti domain WMS secara dinamis (localhost, ngrok/tunnel, VPS production)
    tanpa terikat URL statis yang tersimpan di database.
    """
    base_wms = os.getenv("GEOSERVER_WMS_URL") or getattr(settings, "GEOSERVER_WMS_URL", None)
    base_wms = str(base_wms).rstrip("/")
    if base_wms.endswith("/wms"):
        base_wms = base_wms[:-4].rstrip("/")
    return f"{base_wms}/{workspace_name}/wms"

def build_dynamic_layer_wms_url(workspace_name: str, layer_name: str) -> str:
    """
    Menghasilkan URL direct GetMap lengkap yang menggabungkan base WMS, workspace, dan layer.
    """
    wms_endpoint = build_dynamic_wms_url(workspace_name)
    return f"{wms_endpoint}?service=WMS&version=1.1.1&request=GetMap&layers={workspace_name}:{layer_name}&format=image/png&transparent=true"

def build_dynamic_gwc_url() -> str:
    """
    Menghasilkan base GeoServer URL dinamis berbasis environment variable GEOSERVER_WMS_URL.
    Mengembalikan URL tanpa nama workspace dan tanpa trailing '/wms'.
    Contoh: 'http://localhost:8080/geoserver'
    """
    base_wms = os.getenv("GEOSERVER_WMS_URL") or getattr(settings, "GEOSERVER_WMS_URL", None) or "http://localhost:8080/geoserver"
    base_wms = str(base_wms).rstrip("/")
    if base_wms.endswith("/wms"):
        base_wms = base_wms[:-4].rstrip("/")
    return base_wms

class ServiceModePayload(BaseModel):
    mode: Literal["wms", "wmts"]
    layer_kind: Optional[Literal["raster", "vector"]] = None

logger = logging.getLogger("geoserver_service.v1_layer")

router = APIRouter(prefix="/layers", tags=["Layers"])

os.makedirs(settings.DATA_VECTOR_PATH, exist_ok=True)
os.makedirs(settings.DATA_RASTER_PATH, exist_ok=True)

@router.post(
    "/publish-vector",
    response_model=VectorPublishResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("upload"))]
)
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

        file_sz = os.path.getsize(saved_path)
        usage = get_disk_usage_for_key(caller.id, db)
        max_limit = usage.get("max_quota_bytes", MAX_SPATIAL_QUOTA_BYTES)
        if usage["total_bytes"] + file_sz > max_limit:
            if os.path.exists(saved_path):
                os.remove(saved_path)
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Batas kuota penyimpanan data spasial ({usage['max_quota_readable']}) untuk API Key ini telah tercapai. Total terpakai saat ini: {usage['total_readable']}, kuota: {usage['max_quota_readable']}."
            )

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

        # Truncate GWC bila data ditimpa / diupdate
        try:
            geoserver_client.truncate_gwc_layer(actual_ws_name, final_table_name)
        except Exception as gwc_err:
            logger.warning(f"GWC truncate notice on publish_vector: {gwc_err}")

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
            wms_url=build_dynamic_wms_url(actual_ws_name)

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

@router.post(
    "/publish-raster",
    response_model=RasterPublishResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("upload"))]
)
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

        file_sz = os.path.getsize(saved_path)
        usage = get_disk_usage_for_key(caller.id, db)
        max_limit = usage.get("max_quota_bytes", MAX_SPATIAL_QUOTA_BYTES)
        if usage["total_bytes"] + file_sz > max_limit:
            if os.path.exists(saved_path):
                os.remove(saved_path)
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Batas kuota penyimpanan data spasial ({usage['max_quota_readable']}) untuk API Key ini telah tercapai. Total terpakai saat ini: {usage['total_readable']}, kuota: {usage['max_quota_readable']}."
            )

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

        # Truncate GWC bila data ditimpa / diupdate
        try:
            geoserver_client.truncate_gwc_layer(actual_ws_name, final_store_name)
        except Exception as gwc_err:
            logger.warning(f"GWC truncate notice on publish_raster: {gwc_err}")

        raster_meta = RasterMetadata(
            api_key_id=caller.id,
            workspace_name=actual_ws_name,
            store_name=final_store_name,
            layer_name=layer_name,
            epsg=res.get("epsg"),
            bbox=res.get("bbox"),
            dimensions=res.get("dimensions"),
            file_path=saved_path,

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
            id=str(raster_meta.id),
            layer_id=str(raster_meta.id),
            layer_name=layer_name,
            success=True,
            workspace_name=actual_ws_name,
            store_name=final_store_name,
            title=layer_name,
            epsg=res["epsg"],
            bbox=res["bbox"],
            dimensions=res["dimensions"],
            wms_url=build_dynamic_wms_url(actual_ws_name),

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

@router.get(
    "",
    dependencies=[Depends(rate_limit("read"))]
)
def list_layers(
    layer_type: Optional[str] = Query(None, description="Filter 'vector' atau 'raster'"),
    workspace: Optional[str] = Query(None, description="Filter berdasarkan nama teknis atau hashed ID workspace"),
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Mengambil semua data layer (opsional difilter berdasarkan workspace atau layer_type).
    """
    return get_my_layers(layer_type=layer_type, workspace=workspace, db=db, caller=caller)

@router.get(
    "/my-layers",
    dependencies=[Depends(rate_limit("read"))]
)
def get_my_layers(
    layer_type: Optional[str] = None,
    workspace: Optional[str] = Query(None, description="Filter berdasarkan nama teknis atau hashed ID workspace"),
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Mengambil semua data layer milik API Key yang sedang memanggil.
    Mendukung filter opsional berdasarkan workspace_name atau hashed ID workspace.
    Menjamin isolasi kepemilikan data antar akun/sistem eksternal.
    Urutan default: data terbaru di awal (created_at DESC).
    """
    ws_filter_name = None
    if workspace:
        ws_meta = resolve_workspace(db, workspace)
        ws_filter_name = ws_meta.workspace_name if ws_meta else workspace

    result = []
    if layer_type != "raster":
        vec_q = db.query(VectorLayer).filter(VectorLayer.api_key_id == caller.id)
        if ws_filter_name:
            vec_q = vec_q.filter(VectorLayer.workspace_name == ws_filter_name)
        vectors = vec_q.order_by(VectorLayer.created_at.desc()).all()
        for v in vectors:
            v_fmt = v.file_format or _detect_file_format(v.file_path, "shp")
            result.append({
                "id": str(v.id),
                "type": "vector",
                "layer_type": "vector",
                "data_type": v_fmt,
                "file_format": v_fmt,
                "workspace_name": v.workspace_name,
                "table_name": v.table_name,
                "geoserver_name": v.table_name,
                "layer_name": v.layer_name,
                "display_name": v.layer_name,
                "geom_type": v.geom_type,
                "geometry_type": v.geom_type,
                "feature_count": v.feature_count,
                "bbox": v.bbox,
                "srid": v.srid or 4326,
                "epsg": v.srid or 4326,
                "wms_url": build_dynamic_wms_url(v.workspace_name),
                "direct_wms_url": build_dynamic_layer_wms_url(v.workspace_name, v.table_name),
                "wms_layers_param": f"{v.workspace_name}:{v.table_name}",
                "service_mode": getattr(v, "service_mode", "wms") or "wms",
                "gwc_ready": bool(getattr(v, "gwc_ready", False)),
                "gwc_url": build_dynamic_gwc_url(),
                "symbology": v.symbology,
                "style_name": v.symbology.get("style_name") if isinstance(v.symbology, dict) else None,
                "created_at": str(v.created_at)
            })
    if layer_type != "vector":
        ras_q = db.query(RasterMetadata).filter(RasterMetadata.api_key_id == caller.id)
        if ws_filter_name:
            ras_q = ras_q.filter(RasterMetadata.workspace_name == ws_filter_name)
        rasters = ras_q.order_by(RasterMetadata.created_at.desc()).all()
        for r in rasters:
            r_fmt = _detect_file_format(r.file_path, "tif")
            result.append({
                "id": str(r.id),
                "type": "raster",
                "layer_type": "raster",
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
                "wms_url": build_dynamic_wms_url(r.workspace_name),
                "direct_wms_url": build_dynamic_layer_wms_url(r.workspace_name, r.store_name),
                "wms_layers_param": f"{r.workspace_name}:{r.store_name}",
                "service_mode": getattr(r, "service_mode", "wms") or "wms",
                "gwc_ready": bool(getattr(r, "gwc_ready", False)),
                "gwc_url": build_dynamic_gwc_url(),
                "symbology": r.symbology,
                "style_name": r.symbology.get("style_name") if isinstance(r.symbology, dict) else None,
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
    orphan_cleanup: bool = False

def _is_table_referenced_elsewhere(
    db: Session,
    table_name: str,
    exclude_vec_id: Optional[uuid.UUID] = None,
    exclude_ws: Optional[str] = None,
    exclude_ft: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    """
    Memeriksa apakah nama tabel PostGIS masih direferensikan oleh:
    1. VectorLayer lain di PostgreSQL.
    2. FeatureType di datastore GeoServer yang mengarah ke database spasial yang SAMA.
    Membandingkan (host, port, database, schema, table).
    Jika pengecekan ke GeoServer gagal, mengembalikan (True, reason) sebagai fail-safe.
    """
    if not table_name:
        return False, None

    # 1. Cek DB VectorLayer
    query = db.query(VectorLayer).filter(VectorLayer.table_name == table_name)
    if exclude_vec_id:
        query = query.filter(VectorLayer.id != exclude_vec_id)
    other_vec = query.first()
    if other_vec:
        return True, f"Direferensikan oleh VectorLayer lain di DB (id={other_vec.id})"

    # 2. Cek GeoServer datastores yang menunjuk ke database & schema yang sama
    target_host = settings.POSTGIS_HOST or "db-central-postgres"
    target_db = settings.POSTGIS_DB or "spatial"
    target_schema = "public"

    try:
        raw_workspaces = geoserver_client.get_workspaces()
        for ws_info in raw_workspaces:
            ws = ws_info.get("name")
            if not ws:
                continue
            datastores = geoserver_client.get_datastores(ws)
            for ds in datastores:
                ds_name = ds.get("name")
                ds_detail = geoserver_client.get_datastore(ws, ds_name)
                if not ds_detail:
                    continue
                conn_entries = ds_detail.get("connectionParameters", {}).get("entry", [])
                params = {e.get("@key"): e.get("$") for e in conn_entries if isinstance(e, dict)}
                ds_host = params.get("host")
                ds_database = params.get("database")
                ds_schema = params.get("schema", "public")

                is_same_db = (
                    ds_database == target_db and
                    ds_schema == target_schema and
                    (ds_host == target_host or ds_host in ("127.0.0.1", "localhost", "db-central-postgres"))
                )
                if is_same_db:
                    fts = geoserver_client.get_feature_types(ws, ds_name)
                    for ft in fts:
                        ft_name = ft.get("name")
                        if exclude_ws and exclude_ft and ws == exclude_ws and ft_name == exclude_ft:
                            continue
                        if ft_name == table_name:
                            return True, f"Direferensikan oleh GeoServer FeatureType {ws}:{ft_name} pada datastore {ds_name}"
    except Exception as e:
        logger.warning(f"GeoServer catalog check failed during table reference check for '{table_name}': {e}")
        return True, f"Pengecekan katalog GeoServer gagal ({e}); tabel fisik dipertahankan sebagai pengaman."

    return False, None

def _is_file_referenced_elsewhere(
    db: Session,
    file_path: str,
    exclude_ras_id: Optional[uuid.UUID] = None,
    exclude_ws: Optional[str] = None,
    exclude_store: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    """
    Memeriksa apakah file raster fisik masih direferensikan oleh:
    1. RasterMetadata lain di PostgreSQL.
    2. CoverageStore manapun di SEMUA workspace GeoServer (path dinormalisasi).
    Jika pengecekan ke GeoServer gagal, mengembalikan (True, reason) sebagai fail-safe.
    """
    if not file_path:
        return False, None

    norm_target = os.path.basename(file_path.replace("file:", "").strip())

    # 1. Cek DB RasterMetadata
    query = db.query(RasterMetadata).filter(RasterMetadata.file_path.isnot(None))
    if exclude_ras_id:
        query = query.filter(RasterMetadata.id != exclude_ras_id)
    other_ras = query.all()
    for r in other_ras:
        if r.file_path:
            norm_db = os.path.basename(r.file_path.replace("file:", "").strip())
            if norm_db == norm_target:
                return True, f"Direferensikan oleh RasterMetadata lain di DB (id={r.id}, store={r.store_name})"

    # 2. Cek semua coverage store di semua workspace GeoServer
    try:
        raw_workspaces = geoserver_client.get_workspaces()
        for ws_info in raw_workspaces:
            ws = ws_info.get("name")
            if not ws:
                continue
            cstores = geoserver_client.get_coverage_stores(ws)
            for cs in cstores:
                cs_name = cs.get("name")
                if exclude_ws and exclude_store and ws == exclude_ws and cs_name == exclude_store:
                    continue
                cs_detail = geoserver_client.get_coverage_store(ws, cs_name)
                if cs_detail:
                    cs_url = cs_detail.get("url", "")
                    clean_cs_file = os.path.basename(cs_url.replace("file:", "").strip())
                    if clean_cs_file == norm_target:
                        return True, f"Direferensikan oleh GeoServer CoverageStore {ws}:{cs_name}"
    except Exception as e:
        logger.warning(f"GeoServer catalog check failed during file reference check for '{norm_target}': {e}")
        return True, f"Pengecekan katalog GeoServer gagal ({e}); file fisik dipertahankan sebagai pengaman."

    return False, None

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
            # Resolusi layer tanpa filter pemanggil
            vec_query = db.query(VectorLayer)
            if is_uuid:
                vec = vec_query.filter(
                    (VectorLayer.id == uuid.UUID(lid)) |
                    (VectorLayer.table_name == lid) |
                    (VectorLayer.layer_name == lid)
                ).first()
            else:
                vec = vec_query.filter(
                    (VectorLayer.table_name == lid) |
                    (VectorLayer.layer_name == lid)
                ).first()

            if vec:
                # Otorisasi: hanya owner atau PRIMARY yang boleh menghapus
                if caller.key_type != ApiKeyType.PRIMARY and vec.api_key_id != caller.id:
                    continue

                actual_ws = vec.workspace_name
                # 1. Hapus entri GeoWebCache (GWC) terlebih dahulu
                try:
                    geoserver_client.delete_gwc_layer(actual_ws, vec.table_name)
                except Exception as e:
                    logger.warning(f"GWC delete vector notice: {e}")

                # 2. Hapus layer & feature type dari GeoServer (idempotent, toleran 404/500)
                try:
                    geoserver_client.delete_layer(workspace_name=actual_ws, layer_name=vec.table_name, recurse=True)
                except Exception as e:
                    logger.warning(f"GeoServer delete_layer vector notice: {e}")
                try:
                    geoserver_client.delete_feature_type(workspace_name=actual_ws, store_name="postgis_default", feature_type_name=vec.table_name, recurse=True)
                except Exception as e:
                    logger.warning(f"GeoServer delete_feature_type vector notice: {e}")

                # 3. Hapus tabel PostGIS jika tidak direferensikan layer lain di DB maupun datastore
                is_ref, reason = _is_table_referenced_elsewhere(db, vec.table_name, exclude_vec_id=vec.id, exclude_ws=actual_ws, exclude_ft=vec.table_name)
                if not is_ref:
                    try:
                        with spatial_engine.begin() as conn:
                            conn.execute(text(f'DROP TABLE IF EXISTS "{vec.table_name}" CASCADE;'))
                    except Exception as e:
                        logger.warning(f"PostGIS drop table notice: {e}")
                else:
                    logger.info(f"Tabel {vec.table_name} masih direferensikan ({reason}), dilewati DROP TABLE.")

                # Hapus file fisik jika ada
                if vec.file_path and os.path.exists(vec.file_path):
                    f_ref, f_reason = _is_file_referenced_elsewhere(db, vec.file_path)
                    if not f_ref:
                        try:
                            os.remove(vec.file_path)
                        except Exception:
                            pass

                # Hapus staging file dari IngestJob yang terkait
                ingest_jobs = db.query(IngestJob).filter(
                    IngestJob.result_layer_name == vec.table_name
                ).all()
                for ij in ingest_jobs:
                    if ij.staging_file_path and os.path.exists(ij.staging_file_path):
                        try:
                            os.remove(ij.staging_file_path)
                        except Exception:
                            pass
                    db.delete(ij)

                # 4. Baris metadata PostgreSQL TERAKHIR
                invalidate_schema_cache(str(vec.id))
                db.delete(vec)
                db.commit()
                deleted_count += 1
                continue

            ras_query = db.query(RasterMetadata)
            if is_uuid:
                ras = ras_query.filter(
                    (RasterMetadata.id == uuid.UUID(lid)) |
                    (RasterMetadata.store_name == lid) |
                    (RasterMetadata.layer_name == lid) |
                    (RasterMetadata.store_name == f"store_{lid}")
                ).first()
            else:
                ras = ras_query.filter(
                    (RasterMetadata.store_name == lid) |
                    (RasterMetadata.layer_name == lid) |
                    (RasterMetadata.store_name == f"store_{lid}")
                ).first()

            if ras:
                if caller.key_type != ApiKeyType.PRIMARY and ras.api_key_id != caller.id:
                    continue

                actual_ws = ras.workspace_name
                targets = [ras.store_name, ras.layer_name, ras.store_name.replace("store_", "")]
                # 1. Hapus entri GeoWebCache (GWC) terlebih dahulu
                for target_lyr in targets:
                    try:
                        geoserver_client.delete_gwc_layer(actual_ws, target_lyr)
                    except Exception:
                        pass

                # 2. Hapus layer & coverage store dari GeoServer
                for target_lyr in targets:
                    try:
                        geoserver_client.delete_layer(workspace_name=actual_ws, layer_name=target_lyr, recurse=True)
                    except Exception:
                        pass
                try:
                    geoserver_client.delete_coverage_store(workspace_name=actual_ws, store_name=ras.store_name, recurse=True)
                except Exception as e:
                    logger.warning(f"GeoServer delete_coverage_store notice: {e}")

                # 3. Hapus file fisik raster jika tidak direferensikan di DB
                if ras.file_path and os.path.exists(ras.file_path):
                    f_ref, f_reason = _is_file_referenced_elsewhere(db, ras.file_path, exclude_ras_id=ras.id, exclude_ws=actual_ws, exclude_store=ras.store_name)
                    if not f_ref:
                        try:
                            os.remove(ras.file_path)
                        except Exception:
                            pass

                # 4. Baris metadata PostgreSQL TERAKHIR
                db.delete(ras)
                db.commit()
                deleted_count += 1
                continue

            # Fallback jika orphan di GeoServer / DB: HANYA jika PRIMARY dan req.orphan_cleanup=True
            if caller.key_type == ApiKeyType.PRIMARY and req.orphan_cleanup:
                try:
                    geoserver_client.delete_layer(workspace_name="ws_flowgis_ba3523", layer_name=lid, recurse=True)
                except Exception:
                    pass
                try:
                    geoserver_client.delete_feature_type(workspace_name="ws_flowgis_ba3523", store_name="postgis_default", feature_type_name=lid, recurse=True)
                except Exception:
                    pass
                is_ref, _ = _is_table_referenced_elsewhere(db, lid)
                if not is_ref:
                    try:
                        with spatial_engine.begin() as conn:
                            conn.execute(text(f'DROP TABLE IF EXISTS "{lid}" CASCADE;'))
                    except Exception:
                        pass
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
    orphan_cleanup: bool = False,
    recurse: bool = True,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Menghapus layer secara komprehensif:
    1. Mencari di database (VectorLayer / RasterMetadata) tanpa filter awal.
    2. Jika ditemukan: verifikasi kepemilikan (owner atau PRIMARY). Jika bukan -> 404.
    3. Verifikasi workspace: actual_ws_name diambil dari metadata DB. Jika tidak cocok -> 404.
    4. Jika TIDAK ditemukan di DB: pembersihan orphan HANYA diizinkan jika caller PRIMARY dan orphan_cleanup=True. Jika tidak -> 404.
    5. Menghapus konfigurasi di GeoServer (layer & coverage store / feature type, GWC).
    6. Menghapus tabel PostGIS / file fisik storage lokal jika tidak direferensikan elsewhere.
    7. Mengembalikan status storage_retained dan alasan jika ada referensi silang.
    8. Menghapus record baris di PostgreSQL.
    """
    ws_meta = resolve_workspace(db, workspace_name)
    resolved_url_ws = ws_meta.workspace_name if ws_meta else workspace_name

    vec = None
    ras = None

    # 1. Resolusi VectorLayer TANPA filter pemanggil
    vec_query = db.query(VectorLayer)
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

    # 2. Resolusi RasterMetadata TANPA filter pemanggil
    if not vec:
        ras_query = db.query(RasterMetadata)
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

    target_obj = vec or ras

    if target_obj:
        # Cek otorisasi kepemilikan
        is_owner = (target_obj.api_key_id == caller.id)
        is_primary = (caller.key_type == ApiKeyType.PRIMARY)
        if not (is_owner or is_primary):
            # Tolak 404 agar tidak membocorkan keberadaan layer milik user lain
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Layer '{layer_name}' tidak ditemukan."
            )

        # Cek workspace kecocokan: actual_ws_name dari DB metadata
        actual_ws_name = target_obj.workspace_name
        if actual_ws_name != workspace_name and actual_ws_name != resolved_url_ws:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Layer '{layer_name}' tidak ditemukan pada workspace '{workspace_name}'."
            )
    else:
        # Tidak ditemukan di DB: hanya boleh orphan cleanup oleh PRIMARY dengan flag eksplisit
        if caller.key_type != ApiKeyType.PRIMARY or not orphan_cleanup:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Layer '{layer_name}' tidak ditemukan."
            )
        actual_ws_name = resolved_url_ws

    storage_retained = False
    storage_retention_reason = None
    deleted_something = False

    # 2. Proses penghapusan Vector
    if vec:
        target_table = vec.table_name
        # 1. Hapus konfigurasi tile cache GeoWebCache (best effort)
        try:
            geoserver_client.delete_gwc_layer(actual_ws_name, target_table)
        except Exception:
            pass

        # 2. Hapus layer & feature type dari GeoServer (idempotent, toleran 404/500)
        try:
            geoserver_client.delete_layer(workspace_name=actual_ws_name, layer_name=target_table, recurse=recurse)
        except Exception as e:
            logger.warning(f"GeoServer delete_layer vector notice: {e}")
        try:
            geoserver_client.delete_feature_type(workspace_name=actual_ws_name, store_name="postgis_default", feature_type_name=target_table, recurse=recurse)
        except Exception as e:
            logger.warning(f"GeoServer delete_feature_type vector notice: {e}")

        # 3. Hapus tabel PostGIS jika tidak direferensikan layer lain di DB maupun datastore
        is_ref, reason = _is_table_referenced_elsewhere(db, target_table, exclude_vec_id=vec.id, exclude_ws=actual_ws_name, exclude_ft=target_table)
        if not is_ref:
            try:
                with spatial_engine.begin() as conn:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{target_table}" CASCADE;'))
            except Exception as e:
                logger.warning(f"PostGIS drop table notice: {e}")
        else:
            storage_retained = True
            storage_retention_reason = reason
            logger.info(f"Tabel PostGIS '{target_table}' dipertahankan: {reason}")

        if vec.file_path and os.path.exists(vec.file_path):
            f_ref, f_reason = _is_file_referenced_elsewhere(db, vec.file_path)
            if not f_ref:
                try:
                    os.remove(vec.file_path)
                except Exception:
                    pass
            else:
                storage_retained = True
                storage_retention_reason = storage_retention_reason or f_reason

        # Hapus staging file dari IngestJob yang terkait
        ingest_jobs = db.query(IngestJob).filter(
            IngestJob.result_layer_name == target_table
        ).all()
        for ij in ingest_jobs:
            if ij.staging_file_path and os.path.exists(ij.staging_file_path):
                try:
                    os.remove(ij.staging_file_path)
                except Exception:
                    pass
            db.delete(ij)

        # 4. Baris metadata PostgreSQL TERAKHIR
        invalidate_schema_cache(str(vec.id))
        db.delete(vec)
        db.commit()
        deleted_something = True

    # 3. Proses penghapusan Raster
    elif ras:
        targets = set([ras.store_name, ras.layer_name, ras.store_name.replace("store_", "")])
        # 1. Hapus konfigurasi tile cache GeoWebCache (best effort)
        for target_lyr in targets:
            try:
                geoserver_client.delete_gwc_layer(actual_ws_name, target_lyr)
            except Exception:
                pass

        # 2. Hapus layer & coverage store dari GeoServer
        for target_lyr in targets:
            try:
                geoserver_client.delete_layer(workspace_name=actual_ws_name, layer_name=target_lyr, recurse=recurse)
            except Exception:
                pass
        try:
            geoserver_client.delete_coverage_store(workspace_name=actual_ws_name, store_name=ras.store_name, recurse=recurse)
        except Exception as e:
            logger.warning(f"GeoServer delete_coverage_store notice: {e}")

        # 3. Hapus file fisik jika tidak direferensikan di DB maupun coverage store
        if ras.file_path and os.path.exists(ras.file_path):
            f_ref, f_reason = _is_file_referenced_elsewhere(db, ras.file_path, exclude_ras_id=ras.id, exclude_ws=actual_ws_name, exclude_store=ras.store_name)
            if not f_ref:
                try:
                    os.remove(ras.file_path)
                except Exception:
                    pass
            else:
                storage_retained = True
                storage_retention_reason = f_reason
                logger.info(f"File raster '{ras.file_path}' dipertahankan: {f_reason}")

        # 4. Baris metadata PostgreSQL TERAKHIR
        db.delete(ras)
        db.commit()
        deleted_something = True

    # 4. Fallback jika tidak tercatat di DB (HANYA jika PRIMARY dan orphan_cleanup=True)
    elif caller.key_type == ApiKeyType.PRIMARY and orphan_cleanup:
        try:
            geoserver_client.delete_layer(workspace_name=actual_ws_name, layer_name=layer_name, recurse=recurse)
        except Exception:
            pass
        try:
            geoserver_client.delete_layer(workspace_name=actual_ws_name, layer_name=f"store_{layer_name}", recurse=recurse)
        except Exception:
            pass
        try:
            geoserver_client.delete_feature_type(workspace_name=actual_ws_name, store_name="postgis_default", feature_type_name=layer_name, recurse=recurse)
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
        try:
            geoserver_client.delete_gwc_layer(actual_ws_name, layer_name)
            geoserver_client.delete_gwc_layer(actual_ws_name, f"store_{layer_name}")
        except Exception:
            pass

        # PostGIS table clean jika tidak direferensikan di DB
        is_ref, reason = _is_table_referenced_elsewhere(db, layer_name)
        if not is_ref:
            try:
                with spatial_engine.begin() as conn:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{layer_name}" CASCADE;'))
            except Exception:
                pass
        else:
            storage_retained = True
            storage_retention_reason = reason

        # Raster file clean jika tidak direferensikan di DB
        disk_file = f"/data_raster/{layer_name}.tif"
        if os.path.exists(disk_file):
            f_ref, f_reason = _is_file_referenced_elsewhere(db, disk_file)
            if not f_ref:
                try:
                    os.remove(disk_file)
                except Exception:
                    pass
            else:
                storage_retained = True
                storage_retention_reason = storage_retention_reason or f_reason
        deleted_something = True

    audit_detail = f"deleted (storage_retained={storage_retained})"
    if storage_retention_reason:
        audit_detail += f" reason: {storage_retention_reason}"

    log_action(
        db=db,
        api_key=caller,
        action="DELETE_LAYER",
        resource_type="LAYER",
        resource_name=f"{actual_ws_name}:{layer_name}",
        status="SUCCESS",
        detail=audit_detail
    )

    return {
        "success": True,
        "detail": f"Layer '{layer_name}' berhasil dihapus.",
        "storage_retained": storage_retained,
        "storage_retention_reason": storage_retention_reason
    }

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

def _calculate_layer_center_tile(raw_bbox, epsg=4326, zoom=10):
    if not raw_bbox:
        return None
    try:
        import math
        if isinstance(raw_bbox, dict):
            minx = float(raw_bbox.get("left") or raw_bbox.get("minx") or raw_bbox.get("west"))
            miny = float(raw_bbox.get("bottom") or raw_bbox.get("miny") or raw_bbox.get("south"))
            maxx = float(raw_bbox.get("right") or raw_bbox.get("maxx") or raw_bbox.get("east"))
            maxy = float(raw_bbox.get("top") or raw_bbox.get("maxy") or raw_bbox.get("north"))
        elif isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) >= 4:
            minx, miny, maxx, maxy = [float(v) for v in raw_bbox[:4]]
        else:
            return None

        # Cek apakah koordinat dalam rentang derajat WGS84
        is_degree = (-180.0 <= minx <= 180.0) and (-180.0 <= maxx <= 180.0) and (-90.0 <= miny <= 90.0) and (-90.0 <= maxy <= 90.0)
        if not is_degree and epsg not in (4326, "4326", None):
            try:
                from pyproj import Transformer
                transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
                minx, miny = transformer.transform(minx, miny)
                maxx, maxy = transformer.transform(maxx, maxy)
            except Exception:
                pass

        center_lng = (minx + maxx) / 2.0
        center_lat = (miny + maxy) / 2.0

        n = 2.0 ** zoom
        col = int(math.floor(((center_lng + 180.0) / 360.0) * n))
        lat_rad = math.radians(max(min(center_lat, 85.0511), -85.0511))
        row = int(math.floor(((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0) * n))
        return {"z": zoom, "x": col, "y": row}
    except Exception:
        return None

@router.patch("/{layer_id}/service-mode", status_code=status.HTTP_200_OK)
def set_service_mode(
    layer_id: str,
    payload: ServiceModePayload,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Mengubah mode layanan layer: 'wms' (dinamis) atau 'wmts' (tile cached).
    Jika beralih ke 'wmts', backend memanggil ensure_gwc_layer untuk memastikan
    layer terdaftar di GeoWebCache dengan gridset EPSG:900913 dan format image/png.
    Setelah itu dilakukan probe GetTile server-side ke tile tengah bbox;
    gwc_ready diset True hanya jika hasilnya 200 image/png.
    Jika GWC gagal, mengembalikan status 502 dan database tidak diubah.
    """
    try:
        layer_uuid = uuid.UUID(str(layer_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="layer_id tidak valid.") from exc

    target_obj = None
    layer_kind = payload.layer_kind

    if layer_kind == "raster":
        target_obj = db.query(RasterMetadata).filter(RasterMetadata.id == layer_uuid).first()
    elif layer_kind == "vector":
        target_obj = db.query(VectorLayer).filter(VectorLayer.id == layer_uuid).first()
    else:
        target_obj = db.query(RasterMetadata).filter(RasterMetadata.id == layer_uuid).first()
        if not target_obj:
            target_obj = db.query(VectorLayer).filter(VectorLayer.id == layer_uuid).first()
            if target_obj:
                layer_kind = "vector"
        else:
            layer_kind = "raster"

    if not target_obj:
        raise HTTPException(status_code=404, detail="Layer tidak ditemukan.")

    if target_obj.api_key_id != caller.id and caller.key_type != ApiKeyType.PRIMARY:
        raise HTTPException(status_code=403, detail="Anda tidak memiliki izin mengubah mode layer ini.")

    ws_name = target_obj.workspace_name
    lyr_name = target_obj.store_name if isinstance(target_obj, RasterMetadata) else target_obj.table_name

    if payload.mode == "wmts":
        ok = geoserver_client.ensure_gwc_layer(ws_name, lyr_name, gridset="EPSG:900913", fmt="image/png")
        if not ok:
            raise HTTPException(
                status_code=502,
                detail=f"GeoWebCache gagal menyiapkan layer cache untuk '{ws_name}:{lyr_name}'."
            )
        
        # Server-side validation probe GetTile ke tile tengah bbox
        gwc_probe_ok = False
        try:
            layer_epsg = getattr(target_obj, "srid", None) or getattr(target_obj, "epsg", None) or 4326
            tile_coords = _calculate_layer_center_tile(target_obj.bbox, layer_epsg, zoom=10)
            if tile_coords:
                gwc_probe_ok = geoserver_client.probe_gwc_tile(
                    ws_name, lyr_name, z=tile_coords["z"], x=tile_coords["x"], y=tile_coords["y"]
                )
            else:
                gwc_probe_ok = True
        except Exception as e:
            logger.warning(f"Server-side probe exception for {ws_name}:{lyr_name}: {e}")
            gwc_probe_ok = False

        target_obj.gwc_ready = gwc_probe_ok
        target_obj.service_mode = "wmts"
    else:
        target_obj.service_mode = "wms"

    db.commit()
    db.refresh(target_obj)

    log_action(
        db=db,
        api_key=caller,
        action="UPDATE_SERVICE_MODE",
        resource_type="LAYER",
        resource_id=str(layer_uuid),
        resource_name=lyr_name,
        status="SUCCESS"
    )

    return {
        "success": True,
        "layer_id": str(target_obj.id),
        "layer_name": lyr_name,
        "workspace_name": ws_name,
        "layer_kind": layer_kind,
        "service_mode": target_obj.service_mode,
        "gwc_ready": bool(target_obj.gwc_ready),
        "gwc_url": build_dynamic_gwc_url(),
        "message": f"Mode tampilan layer '{lyr_name}' berhasil diubah ke {target_obj.service_mode.upper()}."
    }



