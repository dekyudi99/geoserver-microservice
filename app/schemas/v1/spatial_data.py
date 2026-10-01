from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class WorkspaceCreateRequest(BaseModel):
    workspace_name: Optional[str] = Field(None, description="Nama teknis workspace GeoServer (opsional, auto-slug)")
    display_name: Optional[str] = Field(None, description="Nama tampilan workspace yang ramah bagi pengguna")
    name_workspace: Optional[str] = Field(None, description="Alias untuk kompatibilitas frontend")
    visibility: Optional[str] = Field("private", description="Visibilitas (private/public)")

class ColorMapEntrySchema(BaseModel):
    color: str
    quantity: float
    opacity: Optional[float] = 1.0
    label: Optional[str] = None

class RasterStyleConfig(BaseModel):
    style_name: Optional[str] = None
    style_type: str = "intervals"  # 'intervals', 'values', 'ramp'
    colors: List[ColorMapEntrySchema]

class StyleApplyRequest(BaseModel):
    workspace_name: str
    layer_name: str
    style_name: str
    sld_xml: Optional[str] = None

class VectorPublishResponse(BaseModel):
    success: bool
    workspace_name: str
    table_name: str
    title: str
    geom_type: str
    feature_count: int
    bbox: List[float]
    srid: int
    simplification: Optional[Dict[str, Any]] = None
    wms_url: str

class RasterPublishResponse(BaseModel):
    success: bool
    workspace_name: str
    store_name: str
    title: str
    epsg: int
    bbox: Dict[str, float]
    dimensions: Dict[str, int]
    wms_url: str
    symbology: Optional[Dict[str, Any]] = None

class LayerInfoResponse(BaseModel):
    name: str
    workspace: str
    type: str  # 'vector' | 'raster'
    wms_url: str
    bbox: Optional[Dict[str, float]] = None
