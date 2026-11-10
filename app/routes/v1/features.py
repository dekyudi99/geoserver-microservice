import math
import time
import uuid
import logging
from typing import Dict, Any, List, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import text
import requests

from ...config.database import get_db, spatial_engine
from ...models.spatial_data import VectorLayer, RasterMetadata
from ...models.api_key import ApiKey, ApiKeyType
from ...security.auth import require_any_key
from ...security.rate_limiter import rate_limit
from ...clients.geoserver_client import geoserver_client
from ...services.cql_builder import (
    build_cql,
    build_sort_by,
    build_identify_cql,
    CqlBuildError
)
from ...schemas.v1.spatial_data import (
    FeatureQueryRequest,
    FeatureQueryResponse,
    FeatureQueryRow,
    FeatureIdentifyRequest,
    FeatureIdentifyResponse,
    LayerSchemaResponse,
    ColumnSchemaItem
)

logger = logging.getLogger("geoserver_service.v1_features")

router = APIRouter(prefix="/layers", tags=["Features"])

# In-memory schema cache: layer_id -> (timestamp, LayerSchemaResponse)
_SCHEMA_CACHE: Dict[str, Tuple[float, LayerSchemaResponse]] = {}
SCHEMA_CACHE_TTL = 300.0  # 5 minutes

def invalidate_schema_cache(layer_id: str):
    """Mengosongkan cache skema untuk layer tertentu saat layer di-republish atau dihapus."""
    _SCHEMA_CACHE.pop(str(layer_id), None)

def _get_vector_or_404_403(
    layer_id: str,
    db: Session,
    caller: ApiKey
) -> VectorLayer:
    """Helper untuk validasi kepemilikan dan tipe layer vector."""
    try:
        layer_uuid = uuid.UUID(str(layer_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="layer_id tidak valid.") from exc

    vec = db.query(VectorLayer).filter(VectorLayer.id == layer_uuid).first()
    if not vec:
        # Periksa apakah ini layer raster
        ras = db.query(RasterMetadata).filter(RasterMetadata.id == layer_uuid).first()
        if ras:
            raise HTTPException(
                status_code=400,
                detail="Operasi WFS / tabel atribut hanya mendukung layer vector. Layer ini adalah raster."
            )
        raise HTTPException(status_code=404, detail="Layer vector tidak ditemukan.")

    if vec.api_key_id != caller.id and caller.key_type != ApiKeyType.PRIMARY:
        raise HTTPException(status_code=403, detail="Anda tidak memiliki akses ke layer vector ini.")

    return vec

def _check_db_pk(table_name: str) -> Optional[str]:
    """Cek apakah tabel fisik di PostgreSQL public schema memiliki PRIMARY KEY."""
    try:
        with spatial_engine.connect() as conn:
            query = text("""
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                JOIN pg_class c ON c.oid = i.indrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = :tbl
                  AND n.nspname = 'public'
                  AND i.indisprimary;
            """)
            res = conn.execute(query, {"tbl": table_name}).fetchone()
            return res[0] if res else None
    except Exception as e:
        logger.warning(f"Error checking PK for table {table_name}: {e}")
        return None

def _fetch_layer_schema(vec: VectorLayer) -> LayerSchemaResponse:
    """Mengambil skema dari cache atau memanggil DescribeFeatureType GeoServer."""
    cache_key = str(vec.id)
    now = time.time()
    cached = _SCHEMA_CACHE.get(cache_key)
    if cached and (now - cached[0] < SCHEMA_CACHE_TTL):
        return cached[1]

    ws = vec.workspace_name
    tbl = vec.table_name

    try:
        dft = geoserver_client.describe_feature_type(ws, tbl)
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail="GeoServer DescribeFeatureType timeout (30s).") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GeoServer request failed: {exc}") from exc

    raw_props = []
    if dft and isinstance(dft.get("featureTypes"), list) and len(dft["featureTypes"]) > 0:
        raw_props = dft["featureTypes"][0].get("properties", [])

    pk_col = _check_db_pk(tbl)
    has_pk = bool(pk_col)

    columns: List[ColumnSchemaItem] = []
    geom_col_name = "geometry"

    for p in raw_props:
        name = p.get("name", "")
        col_type = p.get("type", "xsd:string")
        local_type = p.get("localType", "string")
        nillable = bool(p.get("nillable", True))

        is_geom = any(k in col_type.lower() for k in ("geometry", "point", "polygon", "linestring")) or name.lower() in ("geometry", "the_geom")
        if is_geom:
            geom_col_name = name

        is_pk = bool(pk_col and name.lower() == pk_col.lower())
        # Sembunyikan gid/pk dari tabel atribut dan filter builder sesuai Keputusan A.5
        hidden = is_pk or name.lower() == "gid"

        columns.append(ColumnSchemaItem(
            name=name,
            type=col_type,
            local_type=local_type,
            nillable=nillable,
            is_geometry=is_geom,
            is_pk=is_pk,
            hidden=hidden
        ))

    response = LayerSchemaResponse(
        layer_id=str(vec.id),
        table_name=tbl,
        workspace_name=ws,
        geom_type=vec.geom_type,
        geom_column=geom_col_name,
        has_pk=has_pk,
        pk_column=pk_col,
        columns=columns
    )

    _SCHEMA_CACHE[cache_key] = (now, response)
    return response

@router.get("/{layer_id}/schema", response_model=LayerSchemaResponse, dependencies=[Depends(rate_limit("read"))])
def get_layer_schema(
    layer_id: str,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Mengambil metadata skema atribut layer vector dari WFS 2.0.0 DescribeFeatureType.
    Mendeteksi kolom Primary Key (gid), tipe data, dan kolom geometri.
    """
    vec = _get_vector_or_404_403(layer_id, db, caller)
    return _fetch_layer_schema(vec)

@router.post("/{layer_id}/features/query", response_model=FeatureQueryResponse, dependencies=[Depends(rate_limit("read"))])
def query_layer_features(
    layer_id: str,
    req: FeatureQueryRequest,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Query tabel atribut layer vector via WFS 2.0.0 GetFeature.
    Mendukung paginasi, sorting aman dengan PK tie-breaker, filter terstruktur, dan bbox.
    Kolom geometri tidak dikembalikan untuk menghemat bandwidth.
    """
    vec = _get_vector_or_404_403(layer_id, db, caller)
    schema_res = _fetch_layer_schema(vec)

    # Keputusan Desain A.5:
    # Jika layer belum memiliki primary key (gid), tolak dengan 409 Conflict
    # agar tidak terjadi paginasi non-deterministik yang salah.
    if not schema_res.has_pk:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Layer '{vec.layer_name}' belum memiliki primary key (gid). Jalankan migrasi 'scripts/add_gid_pk.py' terlebih dahulu agar paginasi deterministik dan aman."
        )

    # Validasi dan susun CQL filter
    try:
        filter_dicts = [f.model_dump() for f in req.filters] if req.filters else None
        schema_dicts = [c.model_dump() for c in schema_res.columns]
        cql_filter = build_cql(
            filters=filter_dicts,
            schema=schema_dicts,
            bbox=req.bbox,
            geom_column=schema_res.geom_column
        )
    except CqlBuildError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Validasi dan susun sortBy dengan tie-breaker PK
    try:
        sort_dicts = [s.model_dump() for s in req.sort] if req.sort else None
        sort_by_str = build_sort_by(
            sort_items=sort_dicts,
            schema=schema_dicts,
            pk_column=schema_res.pk_column or "gid"
        )
    except CqlBuildError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ws = vec.workspace_name
    tbl = vec.table_name

    # 1. Ambil total hits dengan filter yang sama
    try:
        hits_res = geoserver_client.get_features(
            ws, tbl,
            resultType="hits",
            cql_filter=cql_filter
        )
        total_matched = int(hits_res.get("numberMatched", 0))
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail="GeoServer WFS hits timeout (30s).") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GeoServer hits request failed: {exc}") from exc

    # 2. Ambil data halaman (tanpa kolom geometri dan tanpa kolom PK tersembunyi di propertyName)
    non_geom_cols = [c.name for c in schema_res.columns if not c.is_geometry and not c.is_pk]
    start_index = (req.page - 1) * req.page_size

    query_params = {
        "count": req.page_size,
        "startIndex": start_index,
        "propertyName": ",".join(non_geom_cols),
        "cql_filter": cql_filter
    }
    if sort_by_str:
        query_params["sortBy"] = sort_by_str

    try:
        data_res = geoserver_client.get_features(
            ws, tbl,
            **query_params
        )
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail="GeoServer WFS data timeout (30s).") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GeoServer data request failed: {exc}") from exc

    features = data_res.get("features", [])
    rows = [FeatureQueryRow(fid=f.get("id", ""), properties=f.get("properties", {})) for f in features]
    total_pages = math.ceil(total_matched / req.page_size) if req.page_size > 0 else 1

    return FeatureQueryResponse(
        layer_id=str(vec.id),
        total=total_matched,
        page=req.page,
        page_size=req.page_size,
        total_pages=total_pages,
        columns=non_geom_cols,
        rows=rows
    )

@router.post("/{layer_id}/features/identify", response_model=FeatureIdentifyResponse, dependencies=[Depends(rate_limit("read"))])
def identify_layer_features(
    layer_id: str,
    req: FeatureIdentifyRequest,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Fitur identify spasial pada layer vector saat peta diklik.
    Menghitung toleransi piksel ke meter berdasarkan latitude dan zoom saat ini.
    - Untuk Poligon: coba INTERSECTS dulu; jika kosong fallback ke DWITHIN.
    - Untuk Titik/Garis: panggil DWITHIN langsung.
    Maksimal mengembalikan 10 fitur lengkap dengan geometri GeoJSON (WGS84 [lon, lat]).
    """
    vec = _get_vector_or_404_403(layer_id, db, caller)
    schema_res = _fetch_layer_schema(vec)

    ws = vec.workspace_name
    tbl = vec.table_name

    # Hitung toleransi dalam meter berdasarkan resolusi zoom
    lat_rad = math.radians(req.lat)
    res_m_px = 156543.03392 * math.cos(lat_rad) / (2.0 ** req.zoom)
    tol_meters = max(1.0, (req.tolerance_px or 8.0) * res_m_px)

    is_polygon = "polygon" in (vec.geom_type or "").lower()
    features = []

    try:
        if is_polygon:
            # 1. Coba INTERSECTS point-in-polygon
            cql_intersect = build_identify_cql(
                lat=req.lat,
                lng=req.lng,
                tolerance_meters=tol_meters,
                geom_type=vec.geom_type,
                geom_column=schema_res.geom_column,
                mode="intersect_first"
            )
            res_int = geoserver_client.get_features(
                ws, tbl,
                count=10,
                cql_filter=cql_intersect
            )
            features = res_int.get("features", [])

            # 2. Jika point-in-polygon kosong, fallback ke DWITHIN dengan toleransi piksel
            if not features:
                cql_dwithin = build_identify_cql(
                    lat=req.lat,
                    lng=req.lng,
                    tolerance_meters=tol_meters,
                    geom_type=vec.geom_type,
                    geom_column=schema_res.geom_column,
                    mode="dwithin"
                )
                res_dw = geoserver_client.get_features(
                    ws, tbl,
                    count=10,
                    cql_filter=cql_dwithin
                )
                features = res_dw.get("features", [])
        else:
            # Point atau Line: DWITHIN langsung
            cql_dwithin = build_identify_cql(
                lat=req.lat,
                lng=req.lng,
                tolerance_meters=tol_meters,
                geom_type=vec.geom_type,
                geom_column=schema_res.geom_column,
                mode="dwithin"
            )
            res_dw = geoserver_client.get_features(
                ws, tbl,
                count=10,
                cql_filter=cql_dwithin
            )
            features = res_dw.get("features", [])
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail="GeoServer WFS identify timeout (30s).") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GeoServer identify request failed: {exc}") from exc

    # Sederhanakan geometri untuk respon identify agar tidak membebani browser di zoom rendah.
    # Desain Efisien: Geometri hanya disertakan untuk fitur pertama (yang langsung di-highlight),
    # disederhanakan dengan Douglas-Peucker sesuai resolusi layar.
    # Fitur lain hanya membawa metadata/properties; geometrinya diambil secara lazy via GET /features/{fid}.
    simp_deg = res_m_px / 111320.0
    out_features = []
    try:
        import shapely.geometry
        for idx, f in enumerate(features[:10]):
            f_copy = dict(f)
            if idx == 0:
                geom = f.get("geometry")
                if geom and isinstance(geom, dict):
                    try:
                        s = shapely.geometry.shape(geom)
                        if not s.is_empty:
                            simp = s.simplify(simp_deg, preserve_topology=True)
                            f_copy["geometry"] = shapely.geometry.mapping(simp)
                    except Exception:
                        pass
            else:
                f_copy["geometry"] = None
            out_features.append(f_copy)
    except Exception:
        for idx, f in enumerate(features[:10]):
            f_copy = dict(f)
            if idx > 0:
                f_copy["geometry"] = None
            out_features.append(f_copy)

    return FeatureIdentifyResponse(
        layer_id=str(vec.id),
        layer_name=vec.layer_name or tbl,
        count=len(out_features),
        features=out_features
    )

@router.get("/{layer_id}/features/{fid:path}", dependencies=[Depends(rate_limit("read"))])
def get_single_feature(
    layer_id: str,
    fid: str,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Mengambil satu fitur lengkap dengan geometri GeoJSON berdasarkan feature ID (fid).
    Digunakan untuk highlight dan fly-to dari baris tabel atribut.
    """
    vec = _get_vector_or_404_403(layer_id, db, caller)
    schema_res = _fetch_layer_schema(vec)

    ws = vec.workspace_name
    tbl = vec.table_name

    try:
        res = geoserver_client.get_features(ws, tbl, count=1, featureID=fid)
        features = res.get("features", [])
        if not features:
            # Fallback coba storedQuery GetFeatureById
            res = geoserver_client.get_features(
                ws, tbl,
                count=1,
                storedQuery_id="urn:ogc:def:query:OGC-WFS::GetFeatureById",
                ID=fid
            )
            features = res.get("features", [])

        if not features:
            raise HTTPException(status_code=404, detail=f"Fitur '{fid}' tidak ditemukan pada layer ini.")

        return features[0]
    except HTTPException:
        raise
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail="GeoServer WFS timeout (30s).") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GeoServer request failed: {exc}") from exc
