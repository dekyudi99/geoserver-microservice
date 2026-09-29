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
