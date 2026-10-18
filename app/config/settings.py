import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    PORT: int = int(os.getenv("PORT", "8001"))
    HOST: str = os.getenv("HOST", "0.0.0.0")
    DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")


    # GeoServer Configuration
    GEOSERVER_URL: str = os.getenv("GEOSERVER_URL", "http://geoserver:8080/geoserver").rstrip("/")
    GEOSERVER_USER: str = os.getenv("GEOSERVER_USER", "admin")
    GEOSERVER_PASS: str = os.getenv("GEOSERVER_PASS", "geoserver")
    GEOSERVER_WMS_URL: str = os.getenv("GEOSERVER_WMS_URL", "http://localhost:8080/geoserver").rstrip("/")

    # Central Database 1: Auth & User Metadata DB (voxagis_user)
    DB_HOST: str = os.getenv("DB_HOST", "db-central-postgres")
    DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
    DB_NAME: str = os.getenv("DB_NAME", "voxagis_user")
    DB_USER: str = os.getenv("DB_USER", "voxagis_app")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")

    @property
    def DATABASE_URL(self) -> str:
        env_url = os.getenv("DATABASE_URL")
        if env_url:
            return env_url
        return f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    # Central Database 2: Spatial PostGIS & GeoServer Datastore (spatial)
    SPATIAL_DB_HOST: str = os.getenv("SPATIAL_DB_HOST", os.getenv("POSTGIS_HOST", "db-central-postgres"))
    SPATIAL_DB_PORT: int = int(os.getenv("SPATIAL_DB_PORT", os.getenv("POSTGIS_PORT", "5432")))
    SPATIAL_DB_NAME: str = os.getenv("SPATIAL_DB_NAME", os.getenv("POSTGIS_DB", "spatial"))
    SPATIAL_DB_USER: str = os.getenv("SPATIAL_DB_USER", os.getenv("POSTGIS_USER", "geoserver_svc"))
    SPATIAL_DB_PASSWORD: str = os.getenv("SPATIAL_DB_PASSWORD", os.getenv("POSTGIS_PASSWORD", ""))

    @property
    def POSTGIS_HOST(self) -> str:
        return self.SPATIAL_DB_HOST

    @property
    def POSTGIS_PORT(self) -> int:
        return self.SPATIAL_DB_PORT

    @property
    def POSTGIS_DB(self) -> str:
        return self.SPATIAL_DB_NAME

    @property
    def POSTGIS_USER(self) -> str:
        return self.SPATIAL_DB_USER

    @property
    def POSTGIS_PASSWORD(self) -> str:
        return self.SPATIAL_DB_PASSWORD

    @property
    def SPATIAL_DATABASE_URL(self) -> str:
        env_url = os.getenv("SPATIAL_DATABASE_URL")
        if env_url:
            return env_url
        return f"postgresql://{self.SPATIAL_DB_USER}:{self.SPATIAL_DB_PASSWORD}@{self.SPATIAL_DB_HOST}:{self.SPATIAL_DB_PORT}/{self.SPATIAL_DB_NAME}"

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

    # Rate Limiting Configuration (mudah diatur via .env)
    RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "true").lower() in ("true", "1", "yes")
    RATE_LIMIT_UPLOAD_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_UPLOAD_PER_MINUTE", "10"))   # Operasi upload/publish/ingest berat
    RATE_LIMIT_READ_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_READ_PER_MINUTE", "120"))      # Operasi baca/query metadata
    RATE_LIMIT_GLOBAL_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_GLOBAL_PER_MINUTE", "300"))  # Kumulatif safety-net per API Key/IP

    # S2S Internal Security & Docs
    INTERNAL_SERVICE_SECRET: str = os.getenv("INTERNAL_SERVICE_SECRET", "voxagis_s2s_internal_secret_key_2026")
    ENABLE_DOCS: bool = os.getenv("ENABLE_DOCS", "true").lower() in ("true", "1", "yes")
    DEFAULT_QUOTA_MB: int = int(os.getenv("DEFAULT_QUOTA_MB", "500"))

settings = Settings()
