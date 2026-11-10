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
    id: Optional[str] = None
    layer_id: Optional[str] = None
    layer_name: Optional[str] = None
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

# ============================================================================
# WFS 2.0.0 Feature Data Schemas (Schema, Query, Identify)
# ============================================================================

class FeatureFilterItem(BaseModel):
    field: str
    op: str
    value: Optional[Any] = None

class FeatureSortItem(BaseModel):
    field: str
    dir: Optional[str] = "ASC"

class FeatureQueryRequest(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=500)
    sort: Optional[List[FeatureSortItem]] = None
    filters: Optional[List[FeatureFilterItem]] = None
    bbox: Optional[List[float]] = None

class ColumnSchemaItem(BaseModel):
    name: str
    type: str
    local_type: str
    nillable: bool
    is_geometry: bool
    is_pk: bool
    hidden: bool = False

class LayerSchemaResponse(BaseModel):
    layer_id: str
    table_name: str
    workspace_name: str
    geom_type: Optional[str] = None
    geom_column: str = "geometry"
    has_pk: bool
    pk_column: Optional[str] = None
    columns: List[ColumnSchemaItem]

class FeatureQueryRow(BaseModel):
    fid: str
    properties: Dict[str, Any]

class FeatureQueryResponse(BaseModel):
    layer_id: str
    total: int
    page: int
    page_size: int
    total_pages: int
    columns: List[str]
    rows: List[FeatureQueryRow]

class FeatureIdentifyRequest(BaseModel):
    lng: float
    lat: float
    zoom: float
    tolerance_px: Optional[float] = 8.0

class FeatureIdentifyResponse(BaseModel):
    layer_id: str
    layer_name: str
    count: int
    features: List[Dict[str, Any]]
