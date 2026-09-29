import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

class Settings:
    PORT: int = int(os.getenv("PORT", "8001"))
    HOST: str = os.getenv("HOST", "0.0.0.0")
    DEBUG: bool = os.getenv("DEBUG", "True").lower() in ("true", "1", "yes")
    
    # API Key & S2S Security
    API_KEY: str = os.getenv("API_KEY", "rahasia_s2s_geoserver_key_2026")
    ASTRAGIS_VERIFY_KEY_URL: str = os.getenv("ASTRAGIS_VERIFY_KEY_URL", "http://fastapi_backend:8000/api-key/verify")

    # GeoServer Configuration
    GEOSERVER_URL: str = os.getenv("GEOSERVER_URL", "http://geoserver:8080/geoserver").rstrip("/")
    GEOSERVER_USER: str = os.getenv("GEOSERVER_USER", "admin")
    GEOSERVER_PASS: str = os.getenv("GEOSERVER_PASS", "rahasia")
    GEOSERVER_WMS_URL: str = os.getenv("GEOSERVER_WMS_URL", "http://localhost:8080/geoserver").rstrip("/")

    # PostGIS GeoServer Spatial DB
    POSTGIS_HOST: str = os.getenv("POSTGIS_HOST", "postgis_geoserver")
    POSTGIS_PORT: int = int(os.getenv("POSTGIS_PORT", "5432"))
    POSTGIS_DB: str = os.getenv("POSTGIS_DB", "geoserver_spatial_db")
    POSTGIS_USER: str = os.getenv("POSTGIS_USER", "postgres")
    POSTGIS_PASSWORD: str = os.getenv("POSTGIS_PASSWORD", "rahasia")
    
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

settings = Settings()
