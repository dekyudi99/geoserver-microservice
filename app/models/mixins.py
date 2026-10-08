from sqlalchemy import Column, DateTime
from sqlalchemy.sql import func


class TimestampMixin:
    """Kolom created_at & updated_at standar untuk semua model metadata."""

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )
