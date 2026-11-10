import uuid
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.clients.geoserver_client import geoserver_client
from app.models.api_key import ApiKey, ApiKeyType
from app.models.spatial_data import VectorLayer, RasterMetadata
from app.services.style_service import style_service
from app.config.database import get_db
from app.security.auth import require_any_key

client = TestClient(app)

# ── 1. UNIT TEST GEOSERVER CLIENT GWC METHODS ────────────────────────────────

def test_ensure_gwc_layer_already_exists():
    """Jika layer sudah ada di GWC dengan gridset EPSG:900913 dan format image/png, return True tanpa PUT."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = """<GeoServerLayer>
      <gridSubsets><gridSubset><gridSetName>EPSG:900913</gridSetName></gridSubset></gridSubsets>
      <mimeFormats><string>image/png</string></mimeFormats>
    </GeoServerLayer>"""

    with patch.object(geoserver_client, "_request", return_value=mock_resp) as mock_req:
        res = geoserver_client.ensure_gwc_layer("ws_test", "layer_test", gridset="EPSG:900913", fmt="image/png")
        assert res is True
        mock_req.assert_called_once_with("GET", "/gwc/rest/layers/ws_test:layer_test.xml")

def test_ensure_gwc_layer_creates_via_put():
    """Jika layer belum ada di GWC (404), kirim PUT XML dan return True jika status 201."""
    get_resp = MagicMock()
    get_resp.status_code = 404

    put_resp = MagicMock()
    put_resp.status_code = 201

    with patch.object(geoserver_client, "_request", side_effect=[get_resp, put_resp]) as mock_req:
        res = geoserver_client.ensure_gwc_layer("ws_test", "layer_test", gridset="EPSG:900913", fmt="image/png")
        assert res is True
        assert mock_req.call_count == 2
        assert mock_req.call_args_list[0][0] == ("GET", "/gwc/rest/layers/ws_test:layer_test.xml")
        assert mock_req.call_args_list[1][0][0] == "PUT"
        assert "<gridSetName>EPSG:900913</gridSetName>" in mock_req.call_args_list[1][1]["data"]

def test_ensure_gwc_layer_failure_returns_false():
    """Jika request ke GWC gagal melempar exception atau HTTP 500, tangkap dan return False."""
    with patch.object(geoserver_client, "_request", side_effect=Exception("Network error")):
        res = geoserver_client.ensure_gwc_layer("ws_test", "layer_test")
        assert res is False

def test_delete_gwc_layer_best_effort():
    """delete_gwc_layer mengembalikan status sukses (200/204/404) dan tidak crash jika ada error."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch.object(geoserver_client, "_request", return_value=mock_resp):
        assert geoserver_client.delete_gwc_layer("ws_test", "layer_test") is True

    # Error tidak boleh melempar exception (best effort)
    with patch.object(geoserver_client, "_request", side_effect=Exception("Connection refused")):
        assert geoserver_client.delete_gwc_layer("ws_test", "layer_test") is False


# ── 2. TEST ENDPOINT PATCH /layers/{layer_id}/service-mode ───────────────────

def test_toggle_service_mode_success():
    """Pemilik layer dapat mengubah mode ke wmts bila GWC berhasil disiapkan."""
    owner_id = uuid.uuid4()
    layer_uuid = uuid.uuid4()

    fake_caller = ApiKey(
        id=owner_id,
        name="Test Key",
        key_type=ApiKeyType.STANDARD,
        is_active=True,
        key_hash="hash",
        key_prefix="px"
    )

    fake_raster = RasterMetadata(
        id=layer_uuid,
        api_key_id=owner_id,
        workspace_name="ws_test",
        store_name="ras_test",
        layer_name="Test Raster",
        service_mode="wms",
        gwc_ready=False
    )

    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_db.query.return_value = mock_query
    mock_query.filter.return_value.first.return_value = fake_raster

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[require_any_key] = lambda: fake_caller

    try:
        with patch.object(geoserver_client, "ensure_gwc_layer", return_value=True):
            response = client.patch(
                f"/api/v1/layers/{layer_uuid}/service-mode",
                json={"mode": "wmts", "layer_kind": "raster"},
                headers={"X-API-Key": "dummy"}
            )

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["service_mode"] == "wmts"
            assert data["gwc_ready"] is True
            assert "gwc_url" in data
            assert fake_raster.service_mode == "wmts"
            assert fake_raster.gwc_ready is True
            assert mock_db.commit.called
    finally:
        app.dependency_overrides.clear()

def test_toggle_service_mode_gwc_failure_returns_502_and_rolls_back():
    """Jika ensure_gwc_layer gagal, return 502 dan database TIDAK diubah."""
    owner_id = uuid.uuid4()
    layer_uuid = uuid.uuid4()

    fake_caller = ApiKey(
        id=owner_id,
        name="Test Key",
        key_type=ApiKeyType.STANDARD,
        is_active=True,
        key_hash="hash",
        key_prefix="px"
    )

    fake_vector = VectorLayer(
        id=layer_uuid,
        api_key_id=owner_id,
        workspace_name="ws_test",
        table_name="vec_test",
        layer_name="Test Vector",
        service_mode="wms",
        gwc_ready=False
    )

    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_db.query.return_value = mock_query
    mock_query.filter.return_value.first.return_value = fake_vector

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[require_any_key] = lambda: fake_caller

    try:
        with patch.object(geoserver_client, "ensure_gwc_layer", return_value=False):
            response = client.patch(
                f"/api/v1/layers/{layer_uuid}/service-mode",
                json={"mode": "wmts", "layer_kind": "vector"},
                headers={"X-API-Key": "dummy"}
            )

            assert response.status_code == 502
            assert "GeoWebCache gagal" in response.json()["detail"]
            # Database tidak boleh di-commit
            assert fake_vector.service_mode == "wms"
            assert fake_vector.gwc_ready is False
            assert not mock_db.commit.called
    finally:
        app.dependency_overrides.clear()

def test_toggle_service_mode_ownership_rejected():
    """User lain (bukan owner dan bukan PRIMARY) ditolak 403."""
    owner_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    layer_uuid = uuid.uuid4()

    intruder_caller = ApiKey(
        id=other_user_id,
        name="Other Key",
        key_type=ApiKeyType.STANDARD,
        is_active=True,
        key_hash="hash",
        key_prefix="px"
    )

    fake_raster = RasterMetadata(
        id=layer_uuid,
        api_key_id=owner_id,
        workspace_name="ws_test",
        store_name="ras_test",
        layer_name="Test Raster",
        service_mode="wms",
        gwc_ready=False
    )

    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_db.query.return_value = mock_query
    mock_query.filter.return_value.first.return_value = fake_raster

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[require_any_key] = lambda: intruder_caller

    try:
        response = client.patch(
            f"/api/v1/layers/{layer_uuid}/service-mode",
            json={"mode": "wmts"},
            headers={"X-API-Key": "dummy"}
        )

        assert response.status_code == 403
        assert "tidak memiliki izin" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


# ── 3. TEST KONSISTENSI TRUNCATE PADA APPLY_STYLE ─────────────────────────────

def test_apply_style_calls_truncate_after_assign_style():
    """apply_style harus memanggil truncate_gwc_layer SETELAH assign_style_to_layer berhasil."""
    call_order = []

    def mock_create_or_update(*args, **kwargs):
        call_order.append("create_or_update_style")
        return True

    def mock_assign(*args, **kwargs):
        call_order.append("assign_style_to_layer")
        return True

    def mock_truncate(*args, **kwargs):
        call_order.append("truncate_gwc_layer")
        return True

    with patch.object(geoserver_client, "style_exists", return_value=False), \
         patch.object(geoserver_client, "create_or_update_style", side_effect=mock_create_or_update), \
         patch.object(geoserver_client, "assign_style_to_layer", side_effect=mock_assign), \
         patch.object(geoserver_client, "truncate_gwc_layer", side_effect=mock_truncate):

        res = style_service.apply_style(
            workspace="ws_test",
            layer_name="lyr_test",
            style_name="style_lyr_test",
            sld_xml="<StyledLayerDescriptor></StyledLayerDescriptor>"
        )

        assert res["success"] is True
        assert call_order == [
            "create_or_update_style",
            "assign_style_to_layer",
            "truncate_gwc_layer"
        ]
