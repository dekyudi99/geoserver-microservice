from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
from .settings import settings
import logging

logger = logging.getLogger("geoserver_service.db")

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def ensure_postgis_extension():
    """Memastikan ekstensi PostGIS aktif di database spatial."""
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
            logger.info("PostGIS extension ensured in spatial DB.")
    except Exception as e:
        logger.warning(f"Could not initialize PostGIS extension: {e}")

def create_all_tables():
    """Membuat semua tabel metadata & auth jika belum ada serta menyinkronkan kolom timestamp."""
    try:
        from ..models.api_key import ApiKey
        from ..models.audit_log import AuditLog
        from ..models.spatial_data import VectorLayer, RasterMetadata, WorkspaceMetadata, LayerGroupMetadata
        from ..models.ingest_job import IngestJob
        Base.metadata.create_all(bind=engine)
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE IF EXISTS vector_layers "
                "ADD COLUMN IF NOT EXISTS symbology JSON;"
            ))
            conn.execute(text(
                "ALTER TABLE IF EXISTS vector_layers "
                "ADD COLUMN IF NOT EXISTS file_format VARCHAR(20);"
            ))
            # Buat fungsi trigger untuk auto-update kolom updated_at pada level PostgreSQL
            conn.execute(text("""
                CREATE OR REPLACE FUNCTION update_updated_at_column()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.updated_at = now();
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
            """))

            for table in Base.metadata.sorted_tables:
                if "updated_at" in table.c:
                    for col in ("created_at", "updated_at"):
                        conn.execute(text(
                            f"ALTER TABLE IF EXISTS {table.name} "
                            f"ADD COLUMN IF NOT EXISTS {col} TIMESTAMPTZ NOT NULL DEFAULT now();"
                        ))
                        conn.execute(text(
                            f"ALTER TABLE IF EXISTS {table.name} "
                            f"ALTER COLUMN {col} SET DEFAULT now();"
                        ))
                        conn.execute(text(
                            f"ALTER TABLE IF EXISTS {table.name} "
                            f"ALTER COLUMN {col} SET NOT NULL;"
                        ))
                    trg_name = f"trg_{table.name}_updated_at"
                    conn.execute(text(f"DROP TRIGGER IF EXISTS {trg_name} ON {table.name};"))
                    conn.execute(text(
                        f"CREATE TRIGGER {trg_name} BEFORE UPDATE ON {table.name} "
                        f"FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();"
                    ))
        logger.info("All metadata and security tables verified/created with synced TimestampMixin.")
    except Exception as e:
        logger.warning(f"Could not initialize tables: {e}")
