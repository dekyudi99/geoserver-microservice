import io
import math
import uuid
import logging
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...config.database import get_db
from ...models.spatial_data import VectorLayer, RasterMetadata
from ...models.api_key import ApiKey, ApiKeyType
from ...security.auth import require_any_key
from ...security.rate_limiter import rate_limit
from ...clients.geoserver_client import geoserver_client
from ...services.raster_ops import (
    validate_bbox,
    transform_point_to_native,
    compute_pixel_index,
    build_1x1_wcs_subset,
    build_area_wcs_subsets,
    parse_1x1_coverage_tiff,
    parse_coverage_stats,
    convert_indexed_geotiff_to_rgba,
    mask_geotiff_by_polygon,
    INTERPOLATION_NEAREST_URI,
    RasterOpsError
)
from ...schemas.v1.spatial_data import (
    RasterIdentifyRequest,
    RasterStatsRequest,
    RasterClipRequest
)

logger = logging.getLogger("geoserver_service.v1_raster")

router = APIRouter(prefix="/layers", tags=["Raster"])

# Concurrency semaphore to avoid overwhelming GeoServer with simultaneous GetCoverage requests
_WCS_SEMAPHORE = asyncio.Semaphore(5)

def _get_raster_or_404_403(
    layer_id: str,
    db: Session,
    caller: ApiKey
) -> RasterMetadata:
    """Helper validasi kepemilikan dan tipe layer raster."""
    try:
        layer_uuid = uuid.UUID(str(layer_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="layer_id tidak valid.") from exc

    ras = db.query(RasterMetadata).filter(RasterMetadata.id == layer_uuid).first()
    if not ras:
        # Periksa apakah ini layer vector
        vec = db.query(VectorLayer).filter(VectorLayer.id == layer_uuid).first()
        if vec:
            raise HTTPException(
                status_code=400,
                detail="Operasi raster hanya mendukung layer raster. Layer ini bertipe vector."
            )
        raise HTTPException(status_code=404, detail="Layer raster tidak ditemukan.")

    if ras.api_key_id != caller.id and caller.key_type != ApiKeyType.PRIMARY:
        raise HTTPException(status_code=403, detail="Anda tidak memiliki akses ke layer raster ini.")

    return ras

def _resolve_coverage_id(ras: RasterMetadata) -> str:
    """Menentukan coverageId untuk WCS request di workspace layer."""
    return ras.store_name or ras.layer_name

def _parse_origin_resolution(desc: Dict[str, Any], ras: RasterMetadata) -> Tuple[float, float, float, float, int, int]:
    """
    Mengekstrak origin_x, origin_y, res_x, res_y, width, height dari DescribeCoverage & RasterMetadata.
    """
    dims = ras.dimensions or {}
    width = int(dims.get("width") or 1)
    height = int(dims.get("height") or 1)

    lower_corner = (desc.get("lower_corner") or "").split()
    upper_corner = (desc.get("upper_corner") or "").split()

    if len(lower_corner) >= 2 and len(upper_corner) >= 2:
        c1_low, c2_low = float(lower_corner[0]), float(lower_corner[1])
        c1_up, c2_up = float(upper_corner[0]), float(upper_corner[1])

        axis_labels = [a.upper() for a in desc.get("axis_labels", [])]
        if "LAT" in axis_labels:
            # Lat adalah c1 (Y), Long adalah c2 (X)
            origin_x = c2_low
            origin_y = c1_up
            extent_w = abs(c2_up - c2_low)
            extent_h = abs(c1_up - c1_low)
        else:
            # c1 adalah X (Easting), c2 adalah Y (Northing)
            origin_x = c1_low
            origin_y = c2_up
            extent_w = abs(c1_up - c1_low)
            extent_h = abs(c2_up - c2_low)

        res_x = extent_w / max(1, width)
        res_y = extent_h / max(1, height)
    else:
        # Fallback ke metadata DB bbox
        bbox = ras.bbox or {}
        min_x = float(bbox.get("minx") or bbox.get("left") or 0.0)
        max_x = float(bbox.get("maxx") or bbox.get("right") or 1.0)
        min_y = float(bbox.get("miny") or bbox.get("bottom") or 0.0)
        max_y = float(bbox.get("maxy") or bbox.get("top") or 1.0)
        origin_x = min_x
        origin_y = max_y
        res_x = abs(max_x - min_x) / max(1, width)
        res_y = abs(max_y - min_y) / max(1, height)

    return origin_x, origin_y, res_x, res_y, width, height


@router.get("/{layer_id}/raster/info")
def get_raster_info(
    layer_id: str,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Mengambil metadata teknis raster via DescribeCoverage:
    band, tipe data, nodata, CRS native, resolusi, dimensi, dan extent.
    """
    ras = _get_raster_or_404_403(layer_id, db, caller)
    coverage_id = _resolve_coverage_id(ras)

    try:
        desc = geoserver_client.describe_coverage(ras.workspace_name, coverage_id)
    except Exception as e:
        logger.error(f"Failed to describe coverage {coverage_id}: {e}")
        raise HTTPException(status_code=502, detail=f"GeoServer DescribeCoverage error: {str(e)}")

    origin_x, origin_y, res_x, res_y, width, height = _parse_origin_resolution(desc, ras)

    native_crs = desc.get("srs_name") or f"EPSG:{ras.epsg}"
    if "/" in native_crs:
        # e.g. http://www.opengis.net/def/crs/EPSG/0/32647 -> EPSG:32647
        native_crs = f"EPSG:{native_crs.split('/')[-1]}"

    bands = []
    for idx, f in enumerate(desc.get("fields", []), start=1):
        bands.append({
            "index": idx,
            "name": f.get("name") or f"Band {idx}",
            "data_type": f.get("definition") or "unknown",
            "nodata": f.get("nil_value")
        })

    return {
        "layer_id": str(ras.id),
        "layer_name": ras.layer_name,
        "store_name": ras.store_name,
        "workspace_name": ras.workspace_name,
        "native_crs": native_crs,
        "axis_labels": desc.get("axis_labels", []),
        "origin": {"x": origin_x, "y": origin_y},
        "resolution": {"x": res_x, "y": res_y},
        "dimensions": {"width": width, "height": height},
        "extent": {
            "lower_corner": desc.get("lower_corner"),
            "upper_corner": desc.get("upper_corner")
        },
        "bands": bands
    }


@router.post("/{layer_id}/raster/identify")
async def identify_raster_pixel(
    layer_id: str,
    req: RasterIdentifyRequest,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Mengekstrak nilai pixel per band pada titik (lat, lng) menggunakan WCS 2.0.1 GetCoverage window 1x1.
    Pre-check extent dilakukan dari DescribeCoverage (di luar -> outside: true tanpa memanggil GeoServer).
    Nodata dikembalikan sebagai nodata: true dengan status 200.
    """
    ras = _get_raster_or_404_403(layer_id, db, caller)
    coverage_id = _resolve_coverage_id(ras)

    try:
        desc = geoserver_client.describe_coverage(ras.workspace_name, coverage_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GeoServer DescribeCoverage error: {str(e)}")

    origin_x, origin_y, res_x, res_y, width, height = _parse_origin_resolution(desc, ras)

    native_crs = desc.get("srs_name") or f"EPSG:{ras.epsg}"
    if "/" in native_crs:
        native_crs = f"EPSG:{native_crs.split('/')[-1]}"

    # Konversi lat/lng ke native coords
    try:
        x_nat, y_nat = transform_point_to_native(req.lat, req.lng, native_crs)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Gagal memproyeksikan koordinat ke CRS native {native_crs}: {str(e)}")

    # Hitung indeks pixel deterministik
    col, row, is_outside = compute_pixel_index(
        x=x_nat,
        y=y_nat,
        origin_x=origin_x,
        origin_y=origin_y,
        res_x=res_x,
        res_y=res_y,
        width=width,
        height=height
    )

    if is_outside:
        return {
            "outside": True,
            "nodata": False,
            "bands": [],
            "pixel": None,
            "native_crs": native_crs,
            "coords": {"x": x_nat, "y": y_nat, "lat": req.lat, "lng": req.lng}
        }

    # Bentuk subset 1x1 tepat pada pixel
    subsets = build_1x1_wcs_subset(
        col=col,
        row=row,
        origin_x=origin_x,
        origin_y=origin_y,
        res_x=res_x,
        res_y=res_y,
        native_crs_str=native_crs,
        axis_labels=desc.get("axis_labels", [])
    )

    target_coverage_id = desc.get("coverage_id") or coverage_id

    # Ambil 1x1 GeoTIFF via WCS dengan semaphore
    async with _WCS_SEMAPHORE:
        try:
            loop = asyncio.get_event_loop()
            tiff_bytes = await loop.run_in_executor(
                None,
                lambda: geoserver_client.get_coverage(
                    workspace_name=ras.workspace_name,
                    coverage_id=target_coverage_id,
                    subsets=subsets,
                    format_type="image/tiff",
                    timeout=15
                )
            )
        except Exception as e:
            logger.error(f"WCS GetCoverage error for {coverage_id}: {e}")
            raise HTTPException(status_code=502, detail=f"GeoServer GetCoverage error: {str(e)}")

    # Parse 1x1 GeoTIFF
    try:
        is_nodata, bands_res = parse_1x1_coverage_tiff(tiff_bytes)
    except Exception as e:
        logger.error(f"Failed to parse 1x1 GeoTIFF: {e}")
        raise HTTPException(status_code=502, detail=f"Gagal memproses respons GeoTIFF: {str(e)}")

    return {
        "outside": False,
        "nodata": is_nodata,
        "bands": bands_res,
        "pixel": {"col": col, "row": row},
        "native_crs": native_crs,
        "coords": {"x": x_nat, "y": y_nat, "lat": req.lat, "lng": req.lng}
    }


@router.post("/{layer_id}/raster/stats")
async def calculate_raster_area_stats(
    layer_id: str,
    req: RasterStatsRequest,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Menghitung statistik nilai raster pada area bounding box:
    min, max, mean, std, persentil, dan histogram.
    Memakai resampling otomatis (approximate: true) jika luas area melebihi 4 juta pixel.
    """
    ras = _get_raster_or_404_403(layer_id, db, caller)
    coverage_id = _resolve_coverage_id(ras)

    # Validasi BBox
    try:
        bbox_clean = validate_bbox(req.bbox)
    except RasterOpsError as re:
        raise HTTPException(status_code=400, detail=str(re))

    try:
        desc = geoserver_client.describe_coverage(ras.workspace_name, coverage_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GeoServer DescribeCoverage error: {str(e)}")

    origin_x, origin_y, res_x, res_y, width, height = _parse_origin_resolution(desc, ras)

    native_crs = desc.get("srs_name") or f"EPSG:{ras.epsg}"
    if "/" in native_crs:
        native_crs = f"EPSG:{native_crs.split('/')[-1]}"

    try:
        subsets, scalefactor, approximate, native_bbox = build_area_wcs_subsets(
            bbox_wgs84=bbox_clean,
            origin_x=origin_x,
            origin_y=origin_y,
            res_x=res_x,
            res_y=res_y,
            native_crs_str=native_crs,
            axis_labels=desc.get("axis_labels", []),
            max_pixels=4_000_000
        )
    except ValueError as ve:
        raise HTTPException(status_code=413, detail=str(ve))
    except RasterOpsError as re:
        raise HTTPException(status_code=400, detail=str(re))

    if approximate and scalefactor and scalefactor <= 0.02:
        raise HTTPException(
            status_code=413,
            detail="Area yang diminta terlalu besar (> 100 juta pixel). Silakan perkecil area BBox."
        )

    target_coverage_id = desc.get("coverage_id") or coverage_id
    interpolation = INTERPOLATION_NEAREST_URI if scalefactor is not None else None

    # Ambil GeoTIFF via WCS dengan semaphore
    async with _WCS_SEMAPHORE:
        try:
            loop = asyncio.get_event_loop()
            tiff_bytes = await loop.run_in_executor(
                None,
                lambda: geoserver_client.get_coverage(
                    workspace_name=ras.workspace_name,
                    coverage_id=target_coverage_id,
                    subsets=subsets,
                    scalefactor=scalefactor,
                    interpolation=interpolation,
                    format_type="image/tiff",
                    timeout=30
                )
            )
        except Exception as e:
            logger.error(f"WCS GetCoverage stats error for {coverage_id}: {e}")
            raise HTTPException(status_code=502, detail=f"GeoServer GetCoverage error: {str(e)}")

    try:
        stats, actual_res = parse_coverage_stats(
            tiff_bytes=tiff_bytes,
            band_idx=req.band or 1,
            bins=req.bins or 20
        )
    except Exception as e:
        logger.error(f"Failed to calculate stats from GeoTIFF: {e}")
        raise HTTPException(status_code=502, detail=f"Gagal memproses statistik GeoTIFF: {str(e)}")

    stats["approximate"] = approximate
    stats["resolution"] = {"x": actual_res[0], "y": actual_res[1]}
    stats["bbox"] = list(bbox_clean)
    return stats


@router.post("/{layer_id}/raster/clip")
async def clip_raster_coverage(
    layer_id: str,
    req: RasterClipRequest,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    """
    Mengunduh potongan GeoTIFF pada area BBox tertentu via WCS GetCoverage.
    """
    ras = _get_raster_or_404_403(layer_id, db, caller)
    coverage_id = _resolve_coverage_id(ras)

    try:
        bbox_clean = validate_bbox(req.bbox)
    except RasterOpsError as re:
        raise HTTPException(status_code=400, detail=str(re))

    try:
        desc = geoserver_client.describe_coverage(ras.workspace_name, coverage_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GeoServer DescribeCoverage error: {str(e)}")

    origin_x, origin_y, res_x, res_y, width, height = _parse_origin_resolution(desc, ras)

    native_crs = desc.get("srs_name") or f"EPSG:{ras.epsg}"
    if "/" in native_crs:
        native_crs = f"EPSG:{native_crs.split('/')[-1]}"

    # Batas maksimum untuk clip adalah 10 juta pixel
    subsets, scalefactor, approximate, _ = build_area_wcs_subsets(
        bbox_wgs84=bbox_clean,
        origin_x=origin_x,
        origin_y=origin_y,
        res_x=res_x,
        res_y=res_y,
        native_crs_str=native_crs,
        axis_labels=desc.get("axis_labels", []),
        max_pixels=10_000_000
    )

    if approximate and scalefactor and scalefactor < 0.2:
        raise HTTPException(
            status_code=413,
            detail="Area potongan terlalu besar (> 10 juta pixel). Silakan perkecil area kotak BBox."
        )

    safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in ras.layer_name)

    # 1. Jalur Styled GeoTIFF (WMS GetMap + RGB ColorMap)
    if req.styled:
        style_name = None
        if isinstance(ras.symbology, dict):
            style_name = ras.symbology.get("style_name")

        dx = abs(bbox_clean[2] - bbox_clean[0])
        dy = abs(bbox_clean[3] - bbox_clean[1])
        deg_res_x = res_x if "4326" in native_crs else (res_x / 111320.0)
        deg_res_y = res_y if "4326" in native_crs else (res_y / 111320.0)
        out_w = min(4000, max(64, int(dx / max(1e-6, deg_res_x))))
        out_h = min(4000, max(64, int(dy / max(1e-6, deg_res_y))))

        try:
            wms_path = f"{ras.workspace_name}/wms"
            wms_params = {
                "service": "WMS",
                "version": "1.1.1",
                "request": "GetMap",
                "layers": f"{ras.workspace_name}:{ras.store_name}",
                "styles": style_name or "",
                "bbox": f"{bbox_clean[0]},{bbox_clean[1]},{bbox_clean[2]},{bbox_clean[3]}",
                "width": str(out_w),
                "height": str(out_h),
                "srs": "EPSG:4326",
                "format": "image/geotiff"
            }
            wms_res = geoserver_client._request("GET", wms_path, params=wms_params, timeout=45)
            content_type = wms_res.headers.get("Content-Type", "")
            if wms_res.status_code == 200 and "image/geotiff" in content_type and len(wms_res.content) > 500:
                out_bytes = convert_indexed_geotiff_to_rgba(wms_res.content, mask_polygon=req.mask_polygon, symbology=ras.symbology)
                filename = f"{safe_name}_clip_styled.tif"
                return StreamingResponse(
                    io.BytesIO(out_bytes),
                    media_type="image/tiff",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'}
                )
        except Exception as e:
            logger.warning(f"Styled WMS GetMap failed, falling back to raw WCS with symbology: {e}")

    # 2. Jalur Raw GeoTIFF (WCS GetCoverage)
    target_coverage_id = desc.get("coverage_id") or coverage_id
    interpolation = INTERPOLATION_NEAREST_URI if scalefactor is not None else None

    async with _WCS_SEMAPHORE:
        try:
            loop = asyncio.get_event_loop()
            tiff_bytes = await loop.run_in_executor(
                None,
                lambda: geoserver_client.get_coverage(
                    workspace_name=ras.workspace_name,
                    coverage_id=target_coverage_id,
                    subsets=subsets,
                    scalefactor=scalefactor,
                    interpolation=interpolation,
                    format_type="image/tiff",
                    timeout=45
                )
            )
        except Exception as e:
            logger.error(f"WCS GetCoverage clip error for {coverage_id}: {e}")
            raise HTTPException(status_code=502, detail=f"GeoServer GetCoverage error: {str(e)}")

    if req.styled:
        # Jika styled aktif tapi WMS GetMap gagal/kosong, warnai tiff mentah memakai metadata symbology
        tiff_bytes = convert_indexed_geotiff_to_rgba(tiff_bytes, mask_polygon=req.mask_polygon, symbology=ras.symbology)
        filename = f"{safe_name}_clip_styled.tif"
    else:
        if req.mask_polygon and len(req.mask_polygon) >= 3:
            try:
                tiff_bytes = mask_geotiff_by_polygon(tiff_bytes, req.mask_polygon)
            except Exception as e:
                logger.warning(f"Polygon masking on raw tiff failed: {e}")
        filename = f"{safe_name}_clip_raw.tif"

    return StreamingResponse(
        io.BytesIO(tiff_bytes),
        media_type="image/tiff",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

