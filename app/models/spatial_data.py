import uuid
from sqlalchemy import Column, String, Integer, Text, DateTime, JSON, Sequence, FetchedValue
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from ..config.database import Base

class VectorLayer(Base):
    """
    Metadata vector layer yang dipublish ke GeoServer PostGIS.
    api_key_id = ownership boundary.
    """
    __tablename__ = "vector_layers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id = Column(UUID(as_uuid=True), nullable=False)    # Owner
    workspace_name = Column(String(100), nullable=False)
    table_name = Column(String(100), nullable=False, unique=True)  # Tabel PostGIS
    layer_name = Column(String(100), nullable=False)
    geom_type = Column(String(50), nullable=True)
    feature_count = Column(Integer, nullable=True)
    bbox = Column(JSON, nullable=True)
    srid = Column(Integer, nullable=True)
    file_path = Column(Text, nullable=True)
    wms_url = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class RasterMetadata(Base):
    """
    Metadata raster yang dipublish ke GeoServer.
    api_key_id = ownership boundary.
    """
    __tablename__ = "raster_metadata"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id = Column(UUID(as_uuid=True), nullable=False)    # Owner
    workspace_name = Column(String(100), nullable=False)
    store_name = Column(String(100), nullable=False, unique=True)
    layer_name = Column(String(100), nullable=False)
    epsg = Column(Integer, nullable=True)
    bbox = Column(JSON, nullable=True)
    dimensions = Column(JSON, nullable=True)
    file_path = Column(Text, nullable=True)
    wms_url = Column(Text, nullable=True)
    symbology = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class WorkspaceMetadata(Base):
    """
    Metadata workspace untuk memetakan GeoServer workspace_name
    ke nama ramah pengguna (display_name), kepemilikan api_key, dan visibilitas.
    """
    __tablename__ = "workspace_metadata"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    raw_id = Column(Integer, Sequence('workspace_metadata_raw_id_seq'), server_default=FetchedValue())
    api_key_id = Column(UUID(as_uuid=True), nullable=True)
    workspace_name = Column(String(100), nullable=False, unique=True, index=True)
    display_name = Column(String(200), nullable=False)
    visibility = Column(String(50), default="private")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class LayerGroupMetadata(Base):
    """
    Metadata layer group GeoServer yang dimiliki oleh API Key tertentu.
    """
    __tablename__ = "layer_groups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id = Column(UUID(as_uuid=True), nullable=False)
    workspace_name = Column(String(100), nullable=False)
    name = Column(String(100), nullable=False)
    title = Column(String(150), nullable=False)
    abstract_text = Column(Text, nullable=True)
    mode = Column(String(50), default="single")
    layer_ids = Column(JSON, default=list)
    wms_url = Column(Text, nullable=True)
    wms_layers_param = Column(Text, nullable=True)
    bbox = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
