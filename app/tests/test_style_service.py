import math
import uuid
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.style_service import style_service, ApplyStyleResult
from app.clients.geoserver_client import geoserver_client
from app.models.api_key import ApiKey, ApiKeyType
from app.models.spatial_data import RasterMetadata, VectorLayer
from app.security.auth import require_any_key
from app.config.database import get_db

client = TestClient(app)

# ---------------------------------------------------------
# 1. Tests for generate_raster_sld & parse_raster_sld
# ---------------------------------------------------------

def test_generate_raster_sld_intervals_roundtrip():
    colors = [
        {"quantity": 10.0, "color": "#ff0000", "opacity": 1.0, "label": "Low"},
        {"quantity": 25.5, "color": "#00ff00", "opacity": 0.8, "label": "Med"},
        {"quantity": 50.0, "color": "#0000ff", "opacity": 1.0, "label": "High"},
    ]
    sld = style_service.generate_raster_sld(
        style_name="test_style",
        color_entries=colors,
        style_type="intervals"
    )
    assert '<ColorMap type="intervals">' in sld
    assert '<Abstract>astra:upper_inclusive=1</Abstract>' in sld
    # Pada XML intervals, SEMUA entri dinaikkan infinitesimal (nextafter) agar batas atas inklusif (<=)
    assert 'quantity="10.' in sld
    assert 'quantity="25.5' in sld
    assert 'quantity="50.' in sld

    # Roundtrip parse membalikkan nextafter dengan penanda astra:upper_inclusive=1
    parsed = style_service.parse_raster_sld(sld)
    assert parsed is not None
    assert parsed["style_type"] == "intervals"
    assert parsed["upper_inclusive"] is True
    assert parsed["boundary_semantics"] == "upper_inclusive"
    assert len(parsed["entries"]) == 3
    assert parsed["entries"][0]["quantity"] == 10.0
    assert parsed["entries"][0]["color"].lower() == "#ff0000"
    assert parsed["entries"][1]["quantity"] == 25.5
    assert parsed["entries"][1]["opacity"] == 0.8
    assert parsed["entries"][2]["quantity"] == 50.0
    assert parsed["entries"][2]["label"] == "High"

    # SLD eksternal tanpa penanda: nilai dibiarkan apa adanya dan boundary_semantics unknown
    external_sld = sld.replace("<Abstract>astra:upper_inclusive=1</Abstract>", "")
    parsed_ext = style_service.parse_raster_sld(external_sld)
    assert parsed_ext["upper_inclusive"] is False
    assert parsed_ext["boundary_semantics"] == "unknown"

def test_generate_raster_sld_values_roundtrip():
    colors = [
        {"quantity": 1.0, "color": "#112233", "opacity": 1.0, "label": "Forest"},
        {"quantity": 2.0, "color": "#445566", "opacity": 1.0, "label": "Water"},
        {"quantity": 3.0, "color": "#778899", "opacity": 0.5, "label": "Urban"},
    ]
    sld = style_service.generate_raster_sld(
        style_name="lulc_style",
        color_entries=colors,
        style_type="values"
    )
    assert '<ColorMap type="values">' in sld
    parsed = style_service.parse_raster_sld(sld)
    assert parsed is not None
    assert parsed["style_type"] == "values"
    assert len(parsed["entries"]) == 3
    assert parsed["entries"][0]["quantity"] == 1.0
    assert parsed["entries"][1]["quantity"] == 2.0
    assert parsed["entries"][2]["quantity"] == 3.0

def test_generate_raster_sld_rejects_empty():
    with pytest.raises(ValueError, match="tidak boleh kosong"):
        style_service.generate_raster_sld("empty_style", [], style_type="intervals")

def test_generate_raster_sld_rejects_over_256_entries():
    entries = [{"quantity": float(i), "color": "#112233"} for i in range(257)]
    with pytest.raises(ValueError, match="maksimal memiliki 256 kelas"):
        style_service.generate_raster_sld("large_style", entries, style_type="intervals")

def test_generate_raster_sld_rejects_invalid_hex_color():
    colors = [
        {"quantity": 10.0, "color": "red"},  # Not HEX
    ]
    with pytest.raises(ValueError, match="Entri ke-0 memiliki warna yang tidak valid"):
        style_service.generate_raster_sld("test", colors, style_type="intervals")

def test_generate_raster_sld_rejects_non_ascending_intervals():
    colors = [
        {"quantity": 10.0, "color": "#ff0000"},
        {"quantity": 5.0, "color": "#00ff00"},  # Decreasing!
    ]
    with pytest.raises(ValueError, match="Entri ke-1 memiliki quantity .* tidak naik ketat"):
        style_service.generate_raster_sld("test", colors, style_type="intervals")

def test_generate_raster_sld_rejects_duplicate_values():
    colors = [
        {"quantity": 10.0, "color": "#ff0000"},
        {"quantity": 10.0, "color": "#00ff00"},  # Duplicate
    ]
    with pytest.raises(ValueError, match="Entri ke-1 memiliki quantity duplikat: 10.0"):
        style_service.generate_raster_sld("test", colors, style_type="values")

def test_generate_raster_sld_rejects_nan_and_inf():
    colors = [{"quantity": float("nan"), "color": "#ff0000"}]
    with pytest.raises(ValueError, match="Entri ke-0 memiliki quantity yang tidak valid"):
        style_service.generate_raster_sld("test", colors, style_type="intervals")

    colors_inf = [{"quantity": float("inf"), "color": "#ff0000"}]
    with pytest.raises(ValueError, match="Entri ke-0 memiliki quantity yang tidak valid"):
        style_service.generate_raster_sld("test", colors_inf, style_type="intervals")

def test_generate_raster_sld_rejects_ramp_exact():
    colors = [{"quantity": 1.0, "color": "#ff0000"}]
    with pytest.raises(ValueError, match="ramp_exact"):
        style_service.generate_raster_sld("test", colors, style_type="ramp_exact")

# ---------------------------------------------------------
# 2. Tests for StyleService.apply_style & Rollback Mechanism
# ---------------------------------------------------------

def test_apply_style_success():
    with patch.object(geoserver_client, "style_exists", return_value=False), \
         patch.object(geoserver_client, "create_or_update_style", return_value=True) as mock_create, \
         patch.object(geoserver_client, "assign_style_to_layer", return_value=True) as mock_assign, \
         patch.object(geoserver_client, "truncate_gwc_layer", return_value=True) as mock_gwc:

        res = style_service.apply_style(
            workspace="ws_test",
            layer_name="lyr_test",
            style_name="style_test",
            sld_xml="<StyledLayerDescriptor>...</StyledLayerDescriptor>"
        )
        assert res.success is True
        assert bool(res) is True
        assert res["failed_stage"] is None
        mock_create.assert_called_with(
            "style_test",
            "<StyledLayerDescriptor>...</StyledLayerDescriptor>",
            workspace_name="ws_test"
        )
        mock_assign.assert_called_with("ws_test", "lyr_test", "style_test")
        mock_gwc.assert_called_with("ws_test", "lyr_test")

def test_apply_style_rollback_restores_old_sld_on_assign_failure():
    with patch.object(geoserver_client, "style_exists", return_value=True), \
         patch.object(geoserver_client, "get_style", return_value="<SLD>OLD_SLD_BODY</SLD>"), \
         patch.object(geoserver_client, "create_or_update_style", return_value=True) as mock_create, \
         patch.object(geoserver_client, "assign_style_to_layer", return_value=False):

        res = style_service.apply_style(
            workspace="ws_test",
            layer_name="lyr_test",
            style_name="style_test",
            sld_xml="<SLD>NEW_SLD_BODY</SLD>"
        )
        assert res.success is False
        assert bool(res) is False
        assert res["failed_stage"] == "ASSIGN_STYLE"

        # Verify rollback called create_or_update_style to restore old SLD
        assert mock_create.call_count == 2
        mock_create.assert_called_with(
            "style_test",
            "<SLD>OLD_SLD_BODY</SLD>",
            workspace_name="ws_test"
        )

def test_apply_style_rollback_deletes_new_style_on_assign_failure():
    with patch.object(geoserver_client, "style_exists", return_value=False), \
         patch.object(geoserver_client, "create_or_update_style", return_value=True), \
         patch.object(geoserver_client, "delete_style", return_value=True) as mock_delete, \
         patch.object(geoserver_client, "assign_style_to_layer", return_value=False):

        res = style_service.apply_style(
            workspace="ws_test",
            layer_name="lyr_test",
            style_name="style_test",
            sld_xml="<SLD>NEW_SLD_BODY</SLD>"
        )
        assert res.success is False
        assert res["failed_stage"] == "ASSIGN_STYLE"

        # Verify rollback called delete_style on newly created style
        mock_delete.assert_called_with("style_test", workspace_name="ws_test", recurse=False)

# ---------------------------------------------------------
# 3. Tests for Security & Router Validation
# ---------------------------------------------------------

def test_delete_style_requires_auth():
    response = client.delete("/api/v1/styles/any_style")
    assert response.status_code in (401, 403)

def test_delete_style_forbidden_for_unowned_layer():
    user_key = ApiKey(id=uuid.uuid4(), key_type=ApiKeyType.STANDARD, is_active=True)
    other_key_id = uuid.uuid4()

    mock_db = MagicMock()
    other_raster = RasterMetadata(
        id=uuid.uuid4(),
        api_key_id=other_key_id,
        workspace_name="ws",
        store_name="protected_layer",
        layer_name="protected_layer",
        symbology={"style_name": "protected_style"}
    )
    mock_db.query().filter().all.side_effect = [[other_raster], []]

    app.dependency_overrides[require_any_key] = lambda: user_key
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        response = client.delete("/api/v1/styles/protected_style")
        assert response.status_code == 403
        assert "tidak memiliki izin" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()

def test_delete_style_allowed_for_primary_key():
    primary_key = ApiKey(id=uuid.uuid4(), key_type=ApiKeyType.PRIMARY, is_active=True)

    with patch.object(geoserver_client, "delete_style", return_value=True) as mock_delete:
        app.dependency_overrides[require_any_key] = lambda: primary_key
        try:
            response = client.delete("/api/v1/styles/some_style?recurse=false")
            assert response.status_code == 200
            assert response.json()["success"] is True
            mock_delete.assert_called_with(style_name="some_style", workspace_name=None, recurse=False)
        finally:
            app.dependency_overrides.clear()

def test_apply_style_rejects_raw_sld_for_non_primary():
    user_key = ApiKey(id=uuid.uuid4(), key_type=ApiKeyType.STANDARD, is_active=True)
    layer_id = uuid.uuid4()

    mock_db = MagicMock()
    raster_obj = RasterMetadata(
        id=layer_id,
        api_key_id=user_key.id,
        workspace_name="ws",
        store_name="my_raster",
        layer_name="my_raster"
    )
    mock_db.query().filter().first.return_value = raster_obj

    app.dependency_overrides[require_any_key] = lambda: user_key
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        payload = {
            "layer_id": str(layer_id),
            "layer_kind": "raster",
            "sld_xml": "<StyledLayerDescriptor>...</StyledLayerDescriptor>"
        }
        response = client.post("/api/v1/styles/apply", json=payload)
        assert response.status_code == 403
        assert "PRIMARY key" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()

def test_apply_style_normalizes_legacy_method_names():
    primary_key = ApiKey(id=uuid.uuid4(), key_type=ApiKeyType.PRIMARY, is_active=True)
    layer_id = uuid.uuid4()

    mock_db = MagicMock()
    raster_obj = RasterMetadata(
        id=layer_id,
        api_key_id=primary_key.id,
        workspace_name="ws",
        store_name="my_raster",
        layer_name="my_raster"
    )
    mock_db.query().filter().first.return_value = raster_obj

    app.dependency_overrides[require_any_key] = lambda: primary_key
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch.object(geoserver_client, "style_exists", return_value=False), \
         patch.object(geoserver_client, "create_or_update_style", return_value=True), \
         patch.object(geoserver_client, "assign_style_to_layer", return_value=True), \
         patch.object(geoserver_client, "truncate_gwc_layer", return_value=True):
        try:
            payload = {
                "layer_id": str(layer_id),
                "layer_kind": "raster",
                "classification_method": "custom",  # Legacy name
                "last_auto_method": "jenks",        # Legacy name
                "colors": [
                    {"quantity": 10.0, "color": "#ff0000", "label": "Low"},
                    {"quantity": 20.0, "color": "#00ff00", "label": "High"},
                ]
            }
            response = client.post("/api/v1/styles/apply", json=payload)
            assert response.status_code == 200
            data = response.json()
            symbology = data["symbology"]
            assert symbology["version"] == 2
            assert symbology["classification_method"] == "manual"
            assert symbology["last_auto_method"] == "natural_breaks"
            assert len(symbology["classes"]) == 2
            assert symbology["classes"][0]["min"] == 10.0
            assert symbology["classes"][0]["max"] == 10.0
            assert symbology["classes"][1]["min"] == 10.0
            assert symbology["classes"][1]["max"] == 20.0
        finally:
            app.dependency_overrides.clear()
