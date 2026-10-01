from pydantic import BaseModel, Field, computed_field
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime
from ...models.ingest_job import IngestSourceEnum, IngestStatusEnum

class IngestJobResponse(BaseModel):
    id: UUID
    owner_id: UUID
    created_by: Optional[str] = None
    source_type: IngestSourceEnum
    source_uri: Optional[str] = None
    workspace_name: str
    layer_name: str  # Original layer_name
    status: IngestStatusEnum
    progress: int = Field(default=0, ge=0, le=100)
    result_layer_name: Optional[str] = None  # Berisi geoserver_name unik
    result_metadata: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @computed_field
    @property
    def display_name(self) -> str:
        return self.layer_name

    @computed_field
    @property
    def geoserver_name(self) -> Optional[str]:
        return self.result_layer_name

    class Config:
        from_attributes = True


class IngestJobListResponse(BaseModel):
    success: bool = True
    total: int
    data: List[IngestJobResponse]

class BatchUploadItemResponse(BaseModel):
    id: UUID
    filename: str
    display_name: str
    geoserver_name: Optional[str] = None
    status: IngestStatusEnum
    error_message: Optional[str] = None

class BatchUploadResponse(BaseModel):
    success: bool = True
    total_files: int
    jobs: List[BatchUploadItemResponse]


class BatchStatusRequest(BaseModel):
    job_ids: List[UUID] = Field(..., description="Daftar UUID IngestJob untuk diperiksa")

class BatchStatusResponse(BaseModel):
    success: bool = True
    total: int
    jobs: List[IngestJobResponse]
