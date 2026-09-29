import os
import shutil
import uuid
import json
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, status
from typing import Optional, Dict, Any
from ..services.vector_service import vector_service
from ..services.raster_service import raster_service
from ..clients.geoserver_client import geoserver_client
from ..config.settings import settings
from ..schemas.spatial import VectorPublishResponse, RasterPublishResponse

router = APIRouter(prefix="/layers", tags=["Layers"])

os.makedirs(settings.DATA_VECTOR_PATH, exist_ok=True)
os.makedirs(settings.DATA_RASTER_PATH, exist_ok=True)

@router.post("/publish-vector", response_model=VectorPublishResponse, status_code=status.HTTP_201_CREATED)
async def publish_vector(
    workspace_name: str = Form(...),
    layer_name: str = Form(...),
    table_name: Optional[str] = Form(None),
    simplify_tolerance: Optional[float] = Form(None),
    file: UploadFile = File(...)
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

        # 3. Publish to PostGIS and GeoServer
        res = vector_service.publish_vector_to_geoserver(
            gdf=simplified_gdf,
            workspace_name=workspace_name,
            table_name=final_table_name,
            title=layer_name,
            geom_type=stats.get("geometry_type", "Polygon")
        )

        return VectorPublishResponse(
            success=True,
            workspace_name=workspace_name,
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
        raise HTTPException(status_code=400, detail=f"Gagal memproses data vektor: {str(e)}")

@router.post("/publish-raster", response_model=RasterPublishResponse, status_code=status.HTTP_201_CREATED)
async def publish_raster(
    workspace_name: str = Form(...),
    layer_name: str = Form(...),
    store_name: Optional[str] = Form(None),
    style_config: Optional[str] = Form(None),
    file: UploadFile = File(...)
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

        res = raster_service.publish_raster_to_geoserver(
            file_path=saved_path,
            workspace_name=workspace_name,
            store_name=final_store_name,
            title=layer_name,
            style_config=parsed_style
        )

        return RasterPublishResponse(
            success=True,
            workspace_name=workspace_name,
            store_name=final_store_name,
            title=layer_name,
            epsg=res["epsg"],
            bbox=res["bbox"],
            dimensions=res["dimensions"],
            wms_url=res["wms_url"],
            symbology=res.get("symbology")
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Gagal memproses raster: {str(e)}")

@router.delete("/{workspace_name}/{layer_name}")
def delete_layer(workspace_name: str, layer_name: str, recurse: bool = True):
    success = geoserver_client.delete_layer(workspace_name=workspace_name, layer_name=layer_name, recurse=recurse)
    if not success:
        raise HTTPException(status_code=500, detail="Gagal menghapus layer di GeoServer.")
    return {"success": True, "detail": f"Layer '{layer_name}' berhasil dihapus."}
