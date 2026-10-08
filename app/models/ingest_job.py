from .api_key import ApiKey
import uuid
import enum
from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, Enum
from sqlalchemy.dialects.postgresql import UUID, JSON
from ..config.database import Base
from .mixins import TimestampMixin

class IngestSourceEnum(str, enum.Enum):
    LOCAL_UPLOAD = "LOCAL_UPLOAD"
    NEXTCLOUD = "NEXTCLOUD"
    GOOGLE_DRIVE = "GOOGLE_DRIVE"
    RCLONE_ADMIN = "RCLONE_ADMIN"

class IngestStatusEnum(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class IngestJob(TimestampMixin, Base):
    __tablename__ = "ingest_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="RESTRICT"), nullable=False, index=True)
    created_by = Column(String(150), nullable=True, index=True)
    source_type = Column(Enum(IngestSourceEnum), nullable=False)
    workspace_name = Column(String(100), nullable=False)
    layer_name = Column(String(150), nullable=False)
    status = Column(Enum(IngestStatusEnum), nullable=False, default=IngestStatusEnum.QUEUED, index=True)
    progress = Column(Integer, default=0, nullable=False)
    source_uri = Column(String(500), nullable=True)
    staging_file_path = Column(String(500), nullable=True)
    result_layer_name = Column(String(150), nullable=True)
    result_metadata = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
