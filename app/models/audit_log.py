import uuid
from sqlalchemy import Column, String, Text, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from ..config.database import Base
from .mixins import TimestampMixin

class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id = Column(UUID(as_uuid=True), nullable=False)   # ID key yang melakukan aksi
    api_key_name = Column(String(100), nullable=True)          # Nama key saat aksi
    api_key_type = Column(String(20), nullable=True)           # PRIMARY / STANDARD
    action = Column(String(100), nullable=False)               # PUBLISH_VECTOR, DELETE_LAYER, CREATE_API_KEY, dll
    resource_type = Column(String(50), nullable=True)          # LAYER, WORKSPACE, API_KEY, dll
    resource_id = Column(String(200), nullable=True)           # ID resource
    resource_name = Column(String(200), nullable=True)         # Nama resource
    status = Column(String(20), default="SUCCESS")             # SUCCESS / FAILED
    detail = Column(Text, nullable=True)                       # Detail tambahan / pesan error
    performed_at = Column(DateTime(timezone=True), server_default=func.now())
