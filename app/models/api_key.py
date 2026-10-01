import uuid
from sqlalchemy import Column, String, Boolean, Text, Enum as SAEnum, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from ..config.database import Base
import enum

class ApiKeyType(str, enum.Enum):
    PRIMARY = "PRIMARY"     # Bisa semua hal: buat key, hapus key, lihat logs, semua fitur GeoServer
    STANDARD = "STANDARD"   # Hanya akses fitur GeoServer, tidak bisa manage key lain

class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)                      # Label/nama key
    key_hash = Column(String(255), nullable=False, unique=True)     # bcrypt hash dari plain key
    key_prefix = Column(String(20), nullable=False)                 # e.g. "gsvc_pk_" / "gsvc_sk_"
    key_type = Column(SAEnum(ApiKeyType), nullable=False)           # PRIMARY atau STANDARD
    owner_info = Column(Text, nullable=True)                        # JSON string: info pemilik (opsional)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)
