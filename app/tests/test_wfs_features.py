import uuid
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
import requests

from app.main import app
from app.clients.geoserver_client import geoserver_client
from app.models.api_key import ApiKey, ApiKeyType
from app.models.spatial_data import VectorLayer, RasterMetadata
from app.config.database import get_db
from app.security.auth import require_any_key
from app.services.cql_builder import (
    build_cql,
    build_sort_by,
    build_identify_cql,
    CqlBuildError
)
from app.routes.v1.features import invalidate_schema_cache

client = TestClient(app)

MOCK_SCHEMA = [
    {"name": "gid", "type": "xsd:long", "local_type": "long", "nillable": False, "is_geometry": False, "is_pk": True},
    {"name": "name", "type": "xsd:string", "local_type": "string", "nillable": True, "is_geometry": False, "is_pk": False},
    {"name": "category", "type": "xsd:string", "local_type": "string", "nillable": True, "is_geometry": False, "is_pk": False},
    {"name": "area", "type": "xsd:double", "local_type": "number", "nillable": True, "is_geometry": False, "is_pk": False},
    {"name": "is_active", "type": "xsd:boolean", "local_type": "boolean", "nillable": True, "is_geometry": False, "is_pk": False},
    {"name": "created_date", "type": "xsd:date", "local_type": "date", "nillable": True, "is_geometry": False, "is_pk": False},
    {"name": "geometry", "type": "gml:Geometry", "local_type": "Geometry", "nillable": True, "is_geometry": True, "is_pk": False},
]

# ============================================================================
# 1. UNIT TESTS: CQL BUILDER (INJECTION, OPERATORS, TYPES)
# ============================================================================

def test_cql_builder_simple_equality():
    filters = [{"field": "category", "op": "=", "value": "Agriculture"}]
    cql = build_cql(filters, MOCK_SCHEMA)
    assert cql == "(category = 'Agriculture')"

def test_cql_builder_escapes_single_quotes():
    filters = [{"field": "name", "op": "=", "value": "O'Connor's Farm"}]
    cql = build_cql(filters, MOCK_SCHEMA)
    assert cql == "(name = 'O''Connor''s Farm')"

def test_cql_builder_escapes_single_quotes_and_injection_payloads():
    # Quotes are doubled so injection payloads become harmless string literals
    cql1 = build_cql([{"field": "name", "op": "=", "value": "Agriculture' OR '1'='1"}], MOCK_SCHEMA)
    assert cql1 == "(name = 'Agriculture'' OR ''1''=''1')"

    cql2 = build_cql([{"field": "name", "op": "=", "value": "x'); DROP TABLE users; --"}], MOCK_SCHEMA)
    assert cql2 == "(name = 'x''); DROP TABLE users; --')"

    # Valid values with semicolon or union keyword are preserved
    cql3 = build_cql([{"field": "name", "op": "=", "value": "Union City; Area A"}], MOCK_SCHEMA)
    assert cql3 == "(name = 'Union City; Area A')"

def test_cql_builder_rejects_control_chars_and_null_bytes():
    with pytest.raises(CqlBuildError):
        build_cql([{"field": "name", "op": "=", "value": "Test\0Null"}], MOCK_SCHEMA)

    with pytest.raises(CqlBuildError):
        build_cql([{"field": "name", "op": "=", "value": "Test\x07Bell"}], MOCK_SCHEMA)

def test_cql_builder_rejects_value_over_max_length():
    long_val = "a" * 501
    with pytest.raises(CqlBuildError) as exc:
        build_cql([{"field": "name", "op": "=", "value": long_val}], MOCK_SCHEMA)
    assert "exceeds maximum allowed length" in str(exc.value)

def test_cql_builder_escapes_like_wildcards():
    cql_pct = build_cql([{"field": "name", "op": "contains", "value": "50%"}], MOCK_SCHEMA)
    assert cql_pct == r"(name ILIKE '%50\%%')"

    cql_under = build_cql([{"field": "name", "op": "contains", "value": "Special_Code"}], MOCK_SCHEMA)
    assert cql_under == r"(name ILIKE '%Special\_Code%')"

    cql_backslash = build_cql([{"field": "name", "op": "contains", "value": r"path\to"}], MOCK_SCHEMA)
    assert cql_backslash == r"(name ILIKE '%path\\to%')"

def test_cql_builder_rejects_disallowed_columns():
    with pytest.raises(CqlBuildError) as exc:
        build_cql([{"field": "password_hash", "op": "=", "value": "secret"}], MOCK_SCHEMA)
    assert "not in allowed layer schema" in str(exc.value)

def test_cql_builder_rejects_geometry_column_in_attributes():
    with pytest.raises(CqlBuildError) as exc:
        build_cql([{"field": "geometry", "op": "=", "value": "POINT(0 0)"}], MOCK_SCHEMA)
    assert "Geometry column" in str(exc.value)

def test_cql_builder_rejects_disallowed_operators():
    with pytest.raises(CqlBuildError) as exc:
        build_cql([{"field": "category", "op": "EXEC", "value": "test"}], MOCK_SCHEMA)
    assert "not in allowed operators" in str(exc.value)

def test_cql_builder_numeric_type_coercion_and_validation():
    # Valid number
    cql = build_cql([{"field": "area", "op": ">=", "value": 150.5}], MOCK_SCHEMA)
    assert cql == "(area >= 150.5)"

    # Numeric string
    cql2 = build_cql([{"field": "area", "op": "<", "value": "200"}], MOCK_SCHEMA)
    assert cql2 == "(area < 200)"

    # Invalid numeric string
    with pytest.raises(CqlBuildError):
        build_cql([{"field": "area", "op": ">", "value": "not-a-number"}], MOCK_SCHEMA)

def test_cql_builder_boolean_type_coercion():
    cql1 = build_cql([{"field": "is_active", "op": "=", "value": True}], MOCK_SCHEMA)
    assert cql1 == "(is_active = true)"

    cql2 = build_cql([{"field": "is_active", "op": "=", "value": "0"}], MOCK_SCHEMA)
    assert cql2 == "(is_active = false)"

def test_cql_builder_like_and_contains():
    cql = build_cql([{"field": "name", "op": "contains", "value": "green"}], MOCK_SCHEMA)
    assert cql == "(name ILIKE '%green%')"

def test_cql_builder_in_operator():
    cql = build_cql([{"field": "category", "op": "IN", "value": ["Farm", "Forest"]}], MOCK_SCHEMA)
    assert cql == "(category IN ('Farm', 'Forest'))"

def test_cql_builder_in_operator_caps_at_100_items():
    large_list = [f"item_{i}" for i in range(101)]
    with pytest.raises(CqlBuildError) as exc:
        build_cql([{"field": "category", "op": "IN", "value": large_list}], MOCK_SCHEMA)
    assert "maximum 100 values" in str(exc.value)

def test_cql_builder_null_checks():
    cql_null = build_cql([{"field": "name", "op": "IS NULL"}], MOCK_SCHEMA)
    assert cql_null == "(name IS NULL)"

    cql_not_null = build_cql([{"field": "name", "op": "IS NOT NULL"}], MOCK_SCHEMA)
    assert cql_not_null == "(name IS NOT NULL)"

def test_cql_builder_bbox():
    # BBOX format for EPSG:4326: minLat, minLon, maxLat, maxLon
    bbox = [100.1, 13.5, 100.5, 13.9] # [minLon, minLat, maxLon, maxLat]
    cql = build_cql(bbox=bbox, schema=MOCK_SCHEMA, geom_column="geometry")
    assert cql == "BBOX(geometry, 13.5, 100.1, 13.9, 100.5)"

# ============================================================================
# 2. UNIT TESTS: SAFE SORTING & SORT INJECTION
# ============================================================================

def test_build_sort_by_default_natural_order():
    sort_str = build_sort_by(None, MOCK_SCHEMA, pk_column="gid")
    assert sort_str is None

def test_build_sort_by_with_user_field():
    sort_items = [{"field": "area", "dir": "DESC"}]
    sort_str = build_sort_by(sort_items, MOCK_SCHEMA, pk_column="gid")
    assert sort_str.startswith("area DESC")
    assert "name ASC" in sort_str

def test_build_sort_by_rejects_unknown_columns():
    with pytest.raises(CqlBuildError):
        build_sort_by([{"field": "secret_salary", "dir": "ASC"}], MOCK_SCHEMA)

def test_build_sort_by_rejects_injection_in_field_or_dir():
    with pytest.raises(CqlBuildError):
        build_sort_by([{"field": "area; DROP TABLE--", "dir": "ASC"}], MOCK_SCHEMA)

    with pytest.raises(CqlBuildError):
        build_sort_by([{"field": "area", "dir": "ASC; --"}], MOCK_SCHEMA)

# ============================================================================
# 3. UNIT TESTS: IDENTIFY CQL
# ============================================================================

def test_build_identify_cql_polygon():
    cql_int = build_identify_cql(13.7, 100.1, 50.0, geom_type="Polygon", mode="intersect_first")
    assert cql_int == "INTERSECTS(geometry, POINT(13.7 100.1))"

    cql_dw = build_identify_cql(13.7, 100.1, 50.0, geom_type="Polygon", mode="dwithin")
    assert cql_dw == "DWITHIN(geometry, POINT(13.7 100.1), 50.0, meters)"

def test_build_identify_cql_point():
    cql = build_identify_cql(13.7, 100.1, 25.0, geom_type="Point")
    assert cql == "DWITHIN(geometry, POINT(13.7 100.1), 25.0, meters)"

# ============================================================================
# 4. INTEGRATION TESTS: FEATURES ENDPOINTS
# ============================================================================

USER_UUID = uuid.uuid4()
OTHER_USER_UUID = uuid.uuid4()
VECTOR_LAYER_UUID = uuid.uuid4()
RASTER_LAYER_UUID = uuid.uuid4()

mock_primary_key = ApiKey(id=USER_UUID, key_type=ApiKeyType.PRIMARY, key_hash="test")
mock_standard_key = ApiKey(id=USER_UUID, key_type=ApiKeyType.STANDARD, key_hash="test")
mock_other_key = ApiKey(id=OTHER_USER_UUID, key_type=ApiKeyType.STANDARD, key_hash="test")

mock_vector_layer = VectorLayer(
    id=VECTOR_LAYER_UUID,
    api_key_id=USER_UUID,
    workspace_name="ws_test",
    table_name="vec_test_table",
    layer_name="Test Vector Layer",
    geom_type="MultiPolygon",
    feature_count=100,
    bbox=[100.0, 13.0, 101.0, 14.0],
    srid=4326
)

mock_raster_layer = RasterMetadata(
    id=RASTER_LAYER_UUID,
    api_key_id=USER_UUID,
    workspace_name="ws_test",
    store_name="store_test_raster",
    layer_name="Test Raster Layer"
)

@pytest.fixture(autouse=True)
def cleanup_cache():
    invalidate_schema_cache(str(VECTOR_LAYER_UUID))
    yield
    invalidate_schema_cache(str(VECTOR_LAYER_UUID))

def test_schema_rejects_raster_layer():
    def mock_db_session():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [None, mock_raster_layer]
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_primary_key

    res = client.get(f"/api/v1/layers/{RASTER_LAYER_UUID}/schema")
    assert res.status_code == 400
    assert "hanya mendukung layer vector" in res.json()["detail"]
    app.dependency_overrides.clear()

def test_schema_rejects_unauthorized_user():
    def mock_db_session():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_vector_layer
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_other_key

    res = client.get(f"/api/v1/layers/{VECTOR_LAYER_UUID}/schema")
    assert res.status_code == 403
    app.dependency_overrides.clear()

def test_schema_success_and_cache():
    def mock_db_session():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_vector_layer
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_primary_key

    mock_dft = {
        "featureTypes": [{
            "typeName": "vec_test_table",
            "properties": [
                {"name": "gid", "type": "xsd:long", "localType": "long", "nillable": False},
                {"name": "title", "type": "xsd:string", "localType": "string", "nillable": True},
                {"name": "geometry", "type": "gml:Geometry", "localType": "Geometry", "nillable": True}
            ]
        }]
    }

    with patch.object(geoserver_client, "describe_feature_type", return_value=mock_dft) as mock_dft_call, \
         patch("app.routes.v1.features._check_db_pk", return_value="gid"):
        
        # Call 1: Fetches from GeoServer
        res1 = client.get(f"/api/v1/layers/{VECTOR_LAYER_UUID}/schema")
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["has_pk"] is True
        assert data1["pk_column"] == "gid"
        assert len(data1["columns"]) == 3
        # Ensure gid is marked is_pk=True and hidden=True
        gid_col = next(c for c in data1["columns"] if c["name"] == "gid")
        assert gid_col["is_pk"] is True
        assert gid_col["hidden"] is True
        assert mock_dft_call.call_count == 1

        # Call 2: Returns from in-memory cache without calling GeoServer
        res2 = client.get(f"/api/v1/layers/{VECTOR_LAYER_UUID}/schema")
        assert res2.status_code == 200
        assert mock_dft_call.call_count == 1

    app.dependency_overrides.clear()

def test_query_features_rejects_table_without_pk_with_409():
    """Keputusan Desain A.5: Tabel tanpa PK ditolak dengan status 409 Conflict."""
    def mock_db_session():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_vector_layer
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_primary_key

    mock_dft = {
        "featureTypes": [{
            "typeName": "vec_test_table",
            "properties": [
                {"name": "title", "type": "xsd:string", "localType": "string", "nillable": True},
                {"name": "geometry", "type": "gml:Geometry", "localType": "Geometry", "nillable": True}
            ]
        }]
    }

    with patch.object(geoserver_client, "describe_feature_type", return_value=mock_dft), \
         patch("app.routes.v1.features._check_db_pk", return_value=None):

        payload = {"page": 1, "page_size": 10}
        res = client.post(f"/api/v1/layers/{VECTOR_LAYER_UUID}/features/query", json=payload)
        assert res.status_code == 409
        assert "belum memiliki primary key" in res.json()["detail"]

    app.dependency_overrides.clear()

def test_query_features_success():
    def mock_db_session():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_vector_layer
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_primary_key

    mock_dft = {
        "featureTypes": [{
            "typeName": "vec_test_table",
            "properties": [
                {"name": "gid", "type": "xsd:long", "localType": "long", "nillable": False},
                {"name": "title", "type": "xsd:string", "localType": "string", "nillable": True},
                {"name": "geometry", "type": "gml:Geometry", "localType": "Geometry", "nillable": True}
            ]
        }]
    }

    mock_hits = {"numberMatched": 42}
    mock_data = {
        "features": [
            {"id": "vec_test_table.1", "properties": {"gid": 1, "title": "Area Alpha"}},
            {"id": "vec_test_table.2", "properties": {"gid": 2, "title": "Area Beta"}}
        ]
    }

    with patch.object(geoserver_client, "describe_feature_type", return_value=mock_dft), \
         patch("app.routes.v1.features._check_db_pk", return_value="gid"), \
         patch.object(geoserver_client, "get_features", side_effect=[mock_hits, mock_data]) as mock_wfs:

        payload = {
            "page": 1,
            "page_size": 10,
            "sort": [{"field": "title", "dir": "ASC"}],
            "filters": [{"field": "title", "op": "contains", "value": "Area"}]
        }

        res = client.post(f"/api/v1/layers/{VECTOR_LAYER_UUID}/features/query", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 42
        assert data["page"] == 1
        assert data["page_size"] == 10
        assert len(data["rows"]) == 2
        assert data["rows"][0]["fid"] == "vec_test_table.1"

        # Verify sortBy had PK tie-breaker
        assert mock_wfs.call_count == 2
        second_call_kwargs = mock_wfs.call_args_list[1][1]
        assert second_call_kwargs["sortBy"] == "title ASC"
        assert second_call_kwargs["propertyName"] == "title"

    app.dependency_overrides.clear()

def test_ingest_renames_source_gid_column():
    """Memastikan bahwa kolom sumber bernama 'gid' atau 'GID' di-rename menjadi 'gid_src'."""
    import geopandas as gpd
    from shapely.geometry import Point
    gdf = gpd.GeoDataFrame({
        "GID": [101, 102],
        "name": ["Alpha", "Beta"],
        "geometry": [Point(100.1, 13.7), Point(100.2, 13.8)]
    })
    rename_map = {}
    for col in gdf.columns:
        if str(col).strip().lower() == "gid":
            rename_map[col] = "gid_src"
    gdf.rename(columns=rename_map, inplace=True)
    assert "gid_src" in gdf.columns
    assert "GID" not in gdf.columns
    assert list(gdf["gid_src"]) == [101, 102]

def test_identify_polygon_feature_success():
    def mock_db_session():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_vector_layer
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_primary_key

    mock_dft = {
        "featureTypes": [{
            "typeName": "vec_test_table",
            "properties": [
                {"name": "gid", "type": "xsd:long", "localType": "long", "nillable": False},
                {"name": "title", "type": "xsd:string", "localType": "string", "nillable": True},
                {"name": "geometry", "type": "gml:Geometry", "localType": "Geometry", "nillable": True}
            ]
        }]
    }

    mock_features = {
        "features": [
            {
                "id": "vec_test_table.1",
                "properties": {"gid": 1, "title": "Identified Polygon"},
                "geometry": {"type": "Polygon", "coordinates": [[[100.1, 13.7], [100.2, 13.7], [100.1, 13.7]]]}
            }
        ]
    }

    with patch.object(geoserver_client, "describe_feature_type", return_value=mock_dft), \
         patch("app.routes.v1.features._check_db_pk", return_value="gid"), \
         patch.object(geoserver_client, "get_features", return_value=mock_features):

        payload = {"lat": 13.7, "lng": 100.1, "zoom": 12.0}
        res = client.post(f"/api/v1/layers/{VECTOR_LAYER_UUID}/features/identify", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["count"] == 1
        assert data["features"][0]["id"] == "vec_test_table.1"

        # Fractional zoom test
        frac_payload = {"lat": 13.7, "lng": 100.1, "zoom": 10.5, "tolerance_px": 8.0}
        res_frac = client.post(f"/api/v1/layers/{VECTOR_LAYER_UUID}/features/identify", json=frac_payload)
        assert res_frac.status_code == 200
        assert res_frac.json()["count"] == 1

    app.dependency_overrides.clear()

def test_geoserver_timeout_maps_to_504():
    def mock_db_session():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_vector_layer
        yield db

    app.dependency_overrides[get_db] = mock_db_session
    app.dependency_overrides[require_any_key] = lambda: mock_primary_key

    with patch.object(geoserver_client, "describe_feature_type", side_effect=requests.Timeout("Timeout")):
        res = client.get(f"/api/v1/layers/{VECTOR_LAYER_UUID}/schema")
        assert res.status_code == 504
        assert "timeout" in res.json()["detail"].lower()

    app.dependency_overrides.clear()
