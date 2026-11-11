import uuid
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.clients.geoserver_client import geoserver_client
from app.models.api_key import ApiKey, ApiKeyType
from app.models.spatial_data import VectorLayer, RasterMetadata
from app.config.database import get_db
from app.security.auth import require_any_key

client = TestClient(app)

RASTER_LAYER_ID = uuid.uuid4()
VECTOR_LAYER_ID = uuid.uuid4()

mock_primary_key = ApiKey(
    id=uuid.uuid4(),
    key_hash="mock_hash",
    key_prefix="gsvc_pk_",
    name="Test Key",
    key_type=ApiKeyType.PRIMARY,
    is_active=True
)

mock_raster_metadata = RasterMetadata(
    id=RASTER_LAYER_ID,
    api_key_id=mock_primary_key.id,
    workspace_name="test_ws",
    store_name="store_test",
    layer_name="test_raster_layer",
    epsg=4326,
    bbox={"minx": 100.0, "miny": 13.0, "maxx": 101.0, "maxy": 14.0},
    dimensions={"width": 1000, "height": 800, "bands": 1},
    file_path="/data_raster/test_raster.tif"
)

mock_vector_layer = VectorLayer(
    id=VECTOR_LAYER_ID,
    api_key_id=mock_primary_key.id,
    workspace_name="test_ws",
    table_name="vec_test_table",
    layer_name="test_vector_layer"
)

MOCK_DESCRIBE_COVERAGE = {
    "coverage_id": "test_raster_layer",
    "srs_name": "http://www.opengis.net/def/crs/EPSG/0/4326",
    "lower_corner": "13.0 100.0",
    "upper_corner": "14.0 101.0",
    "axis_labels": ["Lat", "Long"],
    "fields": [
        {"name": "Band1", "definition": "Float32", "nil_value": -9999.0}
    ]
}

def test_raster_info_rejects_vector_layer():
    def mock_db_session():
        db = MagicMock()
        # raster is None, vector is found
        db.query.return_value.filter.return_value.first.side_effect = [
            None,
            mock_vector_layer
        ]
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_primary_key

    res = client.get(f"/api/v1/layers/{VECTOR_LAYER_ID}/raster/info")
    assert res.status_code == 400
    assert "hanya mendukung layer raster" in res.json()["detail"]

    app.dependency_overrides.clear()

def test_raster_info_success():
    def mock_db_session():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_raster_metadata
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_primary_key

    with patch.object(geoserver_client, "describe_coverage", return_value=MOCK_DESCRIBE_COVERAGE):
        res = client.get(f"/api/v1/layers/{RASTER_LAYER_ID}/raster/info")
        assert res.status_code == 200
        data = res.json()
        assert data["layer_id"] == str(RASTER_LAYER_ID)
        assert data["layer_name"] == "test_raster_layer"
        assert data["dimensions"]["width"] == 1000
        assert data["dimensions"]["height"] == 800
        assert data["native_crs"] == "EPSG:4326"
        assert len(data["bands"]) == 1
        assert data["bands"][0]["nodata"] == -9999.0

    app.dependency_overrides.clear()

def test_raster_identify_outside_extent_precheck():
    """Point outside bbox should return outside=True immediately without calling WCS GetCoverage."""
    def mock_db_session():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_raster_metadata
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_primary_key

    # Coordinate outside bbox [100.0, 13.0, 101.0, 14.0] -> lat=10.0, lng=50.0
    payload = {"lat": 10.0, "lng": 50.0}
    with patch.object(geoserver_client, "describe_coverage", return_value=MOCK_DESCRIBE_COVERAGE), \
         patch.object(geoserver_client, "get_coverage") as mock_wcs:

        res = client.post(f"/api/v1/layers/{RASTER_LAYER_ID}/raster/identify", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["outside"] is True
        assert data["bands"] == []
        # Assert GeoServer GetCoverage was not called
        assert mock_wcs.call_count == 0

    app.dependency_overrides.clear()

def test_raster_stats_area_too_large_413():
    def mock_db_session():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_raster_metadata
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_primary_key

    # Huge bbox exceeding max pixels
    payload = {"bbox": [0.0, 0.0, 50.0, 50.0]}
    with patch.object(geoserver_client, "describe_coverage", return_value=MOCK_DESCRIBE_COVERAGE), \
         patch("app.routes.v1.raster.build_area_wcs_subsets", side_effect=ValueError("Requested area contains approximately 10000000 pixels (limit is 4000000)")):
        res = client.post(f"/api/v1/layers/{RASTER_LAYER_ID}/raster/stats", json=payload)
        assert res.status_code == 413

    app.dependency_overrides.clear()

def test_raster_stats_inverted_bbox_400():
    def mock_db_session():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_raster_metadata
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_primary_key

    payload = {"bbox": [101.0, 14.0, 100.0, 13.0]}
    res = client.post(f"/api/v1/layers/{RASTER_LAYER_ID}/raster/stats", json=payload)
    assert res.status_code == 400

    app.dependency_overrides.clear()
