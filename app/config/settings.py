import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    PORT: int = int(os.getenv("PORT", "8001"))
    HOST: str = os.getenv("HOST", "0.0.0.0")
    DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

    # API Key & S2S Security
    API_KEY: str = os.getenv("API_KEY", "")
    ASTRAGIS_VERIFY_KEY_URL: str = os.getenv("ASTRAGIS_VERIFY_KEY_URL", "")

    # GeoServer Configuration
    GEOSERVER_URL: str = os.getenv("GEOSERVER_URL", "http://geoserver:8080/geoserver").rstrip("/")
    GEOSERVER_USER: str = os.getenv("GEOSERVER_USER", "admin")
    GEOSERVER_PASS: str = os.getenv("GEOSERVER_PASS", "geoserver")
    GEOSERVER_WMS_URL: str = os.getenv("GEOSERVER_WMS_URL", "http://localhost:8080/geoserver").rstrip("/")

    # PostGIS GeoServer Spatial DB
    POSTGIS_HOST: str = os.getenv("POSTGIS_HOST", "postgis")
    POSTGIS_PORT: int = int(os.getenv("POSTGIS_PORT", "5432"))
    POSTGIS_DB: str = os.getenv("POSTGIS_DB", "geoserver_spatial_db")
    POSTGIS_USER: str = os.getenv("POSTGIS_USER", "postgres")
    POSTGIS_PASSWORD: str = os.getenv("POSTGIS_PASSWORD", "")

    @property
    def DATABASE_URL(self) -> str:
        env_url = os.getenv("DATABASE_URL")
        if env_url:
            return env_url
        return f"postgresql://{self.POSTGIS_USER}:{self.POSTGIS_PASSWORD}@{self.POSTGIS_HOST}:{self.POSTGIS_PORT}/{self.POSTGIS_DB}"

    # Storage Paths
    DATA_RASTER_PATH: str = os.getenv("DATA_RASTER_PATH", "/data_raster")
    DATA_VECTOR_PATH: str = os.getenv("DATA_VECTOR_PATH", "/data_vector")

    # GIS Processing Defaults
    SIMPLIFY_TOLERANCE: float = float(os.getenv("SIMPLIFY_TOLERANCE", "0.0001"))

    # Ingest Staging & Concurrency Settings
    INGEST_STAGING_DIR: str = os.getenv("INGEST_STAGING_DIR", "/tmp/geo_ingest")
    MAX_UPLOAD_SIZE_MB: int = int(os.getenv("MAX_UPLOAD_SIZE_MB", "0"))  # 0 = unlimited
    MAX_CONCURRENT_JOBS_PER_OWNER: int = int(os.getenv("MAX_CONCURRENT_JOBS_PER_OWNER", "10"))
    MAX_TOTAL_CONCURRENT_JOBS: int = int(os.getenv("MAX_TOTAL_CONCURRENT_JOBS", "20"))
    MAX_FILES_PER_BATCH: int = int(os.getenv("MAX_FILES_PER_BATCH", "10"))
    MAX_BATCH_SIZE_MB: int = int(os.getenv("MAX_BATCH_SIZE_MB", "0"))  # 0 = unlimited

settings = Settings()
