import os
import uuid
import pytest
from unittest.mock import MagicMock, patch, call
from fastapi.testclient import TestClient

from app.main import app
from app.models.api_key import ApiKey, ApiKeyType
from app.models.spatial_data import VectorLayer, RasterMetadata
from app.config.database import get_db
from app.security.auth import require_any_key

client = TestClient(app)

OWNER_KEY_ID = uuid.uuid4()
OTHER_KEY_ID = uuid.uuid4()
PRIMARY_KEY_ID = uuid.uuid4()

owner_key = ApiKey(id=OWNER_KEY_ID, key_hash="h1", key_prefix="gsvc_sk_", name="Owner", key_type=ApiKeyType.STANDARD, is_active=True)
other_key = ApiKey(id=OTHER_KEY_ID, key_hash="h2", key_prefix="gsvc_sk_", name="Attacker", key_type=ApiKeyType.STANDARD, is_active=True)
primary_key = ApiKey(id=PRIMARY_KEY_ID, key_hash="h3", key_prefix="gsvc_pk_", name="Admin", key_type=ApiKeyType.PRIMARY, is_active=True)

@pytest.fixture
def mock_db():
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db
    yield db
    app.dependency_overrides.clear()

def test_delete_layer_by_owner_success(mock_db):
    vec_id = uuid.uuid4()
    mock_vec = VectorLayer(
        id=vec_id,
        api_key_id=OWNER_KEY_ID,
        workspace_name="test_ws",
        table_name="vec_layer_1",
        layer_name="vec_layer_1"
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_vec
    app.dependency_overrides[require_any_key] = lambda: owner_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo, \
         patch("app.routes.v1.layer._is_table_referenced_elsewhere", return_value=(False, None)), \
         patch("app.routes.v1.layer.spatial_engine") as mock_eng:
        resp = client.delete("/api/v1/layers/test_ws/vec_layer_1")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert resp.json()["storage_retained"] is False
        mock_db.delete.assert_called_once_with(mock_vec)
        mock_db.commit.assert_called()

def test_delete_layer_by_non_owner_returns_404(mock_db):
    vec_id = uuid.uuid4()
    mock_vec = VectorLayer(
        id=vec_id,
        api_key_id=OWNER_KEY_ID,
        workspace_name="test_ws",
        table_name="vec_layer_1",
        layer_name="vec_layer_1"
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_vec
    app.dependency_overrides[require_any_key] = lambda: other_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo:
        resp = client.delete("/api/v1/layers/test_ws/vec_layer_1")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Layer 'vec_layer_1' tidak ditemukan."
        mock_db.delete.assert_not_called()
        mock_geo.delete_layer.assert_not_called()

def test_delete_layer_by_primary_success(mock_db):
    vec_id = uuid.uuid4()
    mock_vec = VectorLayer(
        id=vec_id,
        api_key_id=OWNER_KEY_ID,
        workspace_name="test_ws",
        table_name="vec_layer_1",
        layer_name="vec_layer_1"
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_vec
    app.dependency_overrides[require_any_key] = lambda: primary_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo, \
         patch("app.routes.v1.layer._is_table_referenced_elsewhere", return_value=(False, None)), \
         patch("app.routes.v1.layer.spatial_engine") as mock_eng:
        resp = client.delete("/api/v1/layers/test_ws/vec_layer_1")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_db.delete.assert_called_once_with(mock_vec)

def test_delete_orphan_without_flag_returns_404(mock_db):
    mock_db.query.return_value.filter.return_value.first.return_value = None
    app.dependency_overrides[require_any_key] = lambda: primary_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo:
        resp = client.delete("/api/v1/layers/test_ws/orphan_layer")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Layer 'orphan_layer' tidak ditemukan."
        mock_geo.delete_layer.assert_not_called()

def test_delete_orphan_with_flag_by_primary_success(mock_db):
    mock_db.query.return_value.filter.return_value.first.return_value = None
    app.dependency_overrides[require_any_key] = lambda: primary_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo, \
         patch("app.routes.v1.layer._is_table_referenced_elsewhere", return_value=(False, None)), \
         patch("app.routes.v1.layer._is_file_referenced_elsewhere", return_value=(False, None)), \
         patch("app.routes.v1.layer.spatial_engine") as mock_eng:
        resp = client.delete("/api/v1/layers/test_ws/orphan_layer?orphan_cleanup=true")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_geo.delete_layer.assert_called()

def test_delete_orphan_by_non_primary_with_flag_returns_404(mock_db):
    mock_db.query.return_value.filter.return_value.first.return_value = None
    app.dependency_overrides[require_any_key] = lambda: other_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo:
        resp = client.delete("/api/v1/layers/test_ws/orphan_layer?orphan_cleanup=true")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Layer 'orphan_layer' tidak ditemukan."
        mock_geo.delete_layer.assert_not_called()

def test_storage_retained_when_referenced_in_catalog(mock_db):
    vec_id = uuid.uuid4()
    mock_vec = VectorLayer(
        id=vec_id,
        api_key_id=OWNER_KEY_ID,
        workspace_name="test_ws",
        table_name="shared_table",
        layer_name="shared_layer"
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_vec
    app.dependency_overrides[require_any_key] = lambda: owner_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo, \
         patch("app.routes.v1.layer._is_table_referenced_elsewhere", return_value=(True, "Direferensikan oleh FeatureType lain")), \
         patch("app.routes.v1.layer.spatial_engine") as mock_eng:
        resp = client.delete("/api/v1/layers/test_ws/shared_layer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["storage_retained"] is True
        assert "Direferensikan oleh FeatureType lain" in data["storage_retention_reason"]
        mock_eng.begin.assert_not_called()
        mock_db.delete.assert_called_once_with(mock_vec)

def test_batch_delete_skips_unauthorized_layers(mock_db):
    vec_id_1 = uuid.uuid4()
    mock_vec_1 = VectorLayer(id=vec_id_1, api_key_id=OWNER_KEY_ID, workspace_name="test_ws", table_name="mine")
    vec_id_2 = uuid.uuid4()
    mock_vec_2 = VectorLayer(id=vec_id_2, api_key_id=OTHER_KEY_ID, workspace_name="test_ws", table_name="theirs")

    def query_filter_side_effect(*args, **kwargs):
        class MockQ:
            def first(self):
                return mock_vec_2
        return MockQ()

    mock_db.query.return_value.filter.side_effect = query_filter_side_effect
    app.dependency_overrides[require_any_key] = lambda: owner_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo:
        resp = client.post("/api/v1/layers/batch-delete", json={"layer_ids": ["theirs"]})
        assert resp.status_code == 200
        assert resp.json()["deleted_count"] == 0
        mock_db.delete.assert_not_called()

# ==================== A4 TAMBAHAN ====================

def test_catalog_check_failure_retains_storage(mock_db):
    """1. Kegagalan pemeriksaan katalog GeoServer -> storage tetap dipertahankan dan storage_retained=true."""
    vec_id = uuid.uuid4()
    mock_vec = VectorLayer(
        id=vec_id,
        api_key_id=OWNER_KEY_ID,
        workspace_name="test_ws",
        table_name="vec_catalog_fail",
        layer_name="vec_catalog_fail"
    )

    def query_dispatcher(model):
        mq = MagicMock()
        if model == VectorLayer:
            mq.filter.return_value.first.side_effect = [mock_vec, None]
            mq.filter.return_value.filter.return_value.first.return_value = None
        else:
            mq.filter.return_value.first.return_value = None
            mq.filter.return_value.all.return_value = []
        return mq

    mock_db.query.side_effect = query_dispatcher
    app.dependency_overrides[require_any_key] = lambda: owner_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo, \
         patch("app.routes.v1.layer.spatial_engine") as mock_eng:
        mock_geo.get_workspaces.side_effect = Exception("GeoServer catalog down")
        
        resp = client.delete("/api/v1/layers/test_ws/vec_catalog_fail")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["storage_retained"] is True
        assert "katalog GeoServer gagal" in data["storage_retention_reason"]
        # PostGIS DROP TABLE tidak boleh dipanggil
        mock_eng.begin.assert_not_called()
        # Baris metadata DB tetap dihapus karena layer dihapus
        mock_db.delete.assert_called_once_with(mock_vec)

def test_deletion_order_gwc_geoserver_storage_metadata(mock_db):
    """2. Urutan eksekusi penghapusan: GWC -> GeoServer -> storage -> metadata."""
    vec_id = uuid.uuid4()
    mock_vec = VectorLayer(
        id=vec_id,
        api_key_id=OWNER_KEY_ID,
        workspace_name="test_ws",
        table_name="vec_order_test",
        layer_name="vec_order_test"
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_vec
    app.dependency_overrides[require_any_key] = lambda: owner_key

    execution_order = []

    def mock_gwc(*args, **kwargs):
        execution_order.append("gwc")

    def mock_del_layer(*args, **kwargs):
        execution_order.append("geoserver_layer")

    def mock_del_ft(*args, **kwargs):
        execution_order.append("geoserver_ft")

    def mock_drop_table(*args, **kwargs):
        execution_order.append("storage")
        mock_cm = MagicMock()
        return mock_cm

    def mock_del_meta(*args, **kwargs):
        execution_order.append("metadata")

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo, \
         patch("app.routes.v1.layer._is_table_referenced_elsewhere", return_value=(False, None)), \
         patch("app.routes.v1.layer.spatial_engine") as mock_eng:
        
        mock_geo.delete_gwc_layer.side_effect = mock_gwc
        mock_geo.delete_layer.side_effect = mock_del_layer
        mock_geo.delete_feature_type.side_effect = mock_del_ft
        mock_eng.begin.side_effect = mock_drop_table
        mock_db.delete.side_effect = mock_del_meta

        resp = client.delete("/api/v1/layers/test_ws/vec_order_test")
        assert resp.status_code == 200
        assert execution_order == ["gwc", "geoserver_layer", "geoserver_ft", "storage", "metadata"]

def test_middle_step_failure_retains_metadata(mock_db):
    """3. Kegagalan di langkah tengah -> metadata DB dipertahankan (tidak dihapus)."""
    vec_id = uuid.uuid4()
    mock_vec = VectorLayer(
        id=vec_id,
        api_key_id=OWNER_KEY_ID,
        workspace_name="test_ws",
        table_name="vec_middle_fail",
        layer_name="vec_middle_fail"
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_vec
    app.dependency_overrides[require_any_key] = lambda: owner_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo, \
         patch("app.routes.v1.layer._is_table_referenced_elsewhere", side_effect=RuntimeError("Middle step unhandled crash")):

        with pytest.raises(RuntimeError, match="Middle step unhandled crash"):
            client.delete("/api/v1/layers/test_ws/vec_middle_fail")

        # Metadata di DB tidak boleh dihapus
        mock_db.delete.assert_not_called()
        mock_db.commit.assert_not_called()

def test_delete_twice_succeeds_idempotently(mock_db):
    """4. Panggilan delete dua kali (termasuk GeoServer 500/404 saat tabel fisik hilang) tetap aman & idempoten."""
    vec_id = uuid.uuid4()
    mock_vec = VectorLayer(
        id=vec_id,
        api_key_id=OWNER_KEY_ID,
        workspace_name="test_ws",
        table_name="vec_double_del",
        layer_name="vec_double_del"
    )
    app.dependency_overrides[require_any_key] = lambda: owner_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo, \
         patch("app.routes.v1.layer._is_table_referenced_elsewhere", return_value=(False, None)), \
         patch("app.routes.v1.layer.spatial_engine") as mock_eng:
        
        # Panggilan pertama: GeoServer merespons 500 karena tabel fisik hilang, tetapi delete_layer toleran
        mock_db.query.return_value.filter.return_value.first.return_value = mock_vec
        mock_geo.delete_layer.return_value = False  # atau 500 ditolerir di internal
        resp1 = client.delete("/api/v1/layers/test_ws/vec_double_del")
        assert resp1.status_code == 200
        assert resp1.json()["success"] is True
        mock_db.delete.assert_called_once_with(mock_vec)

        # Panggilan kedua: Layer sudah terhapus dari DB metadata -> Mengembalikan 404 tanpa merusak sistem
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_db.delete.reset_mock()
        resp2 = client.delete("/api/v1/layers/test_ws/vec_double_del")
        assert resp2.status_code == 404
        assert resp2.json()["detail"] == "Layer 'vec_double_del' tidak ditemukan."
        mock_db.delete.assert_not_called()

def test_batch_delete_with_mixed_owner_and_non_owner(mock_db):
    """5. Batch delete dengan campuran layer milik pemanggil dan milik user lain."""
    my_vec1 = VectorLayer(id=uuid.uuid4(), api_key_id=OWNER_KEY_ID, workspace_name="ws1", table_name="my_vec1", layer_name="my_vec1")
    my_ras2 = RasterMetadata(id=uuid.uuid4(), api_key_id=OWNER_KEY_ID, workspace_name="ws1", store_name="my_ras2", layer_name="my_ras2", file_path="/data_raster/my_ras2.tif")
    other_vec3 = VectorLayer(id=uuid.uuid4(), api_key_id=OTHER_KEY_ID, workspace_name="ws1", table_name="other_vec3", layer_name="other_vec3")
    other_ras4 = RasterMetadata(id=uuid.uuid4(), api_key_id=OTHER_KEY_ID, workspace_name="ws1", store_name="other_ras4", layer_name="other_ras4", file_path="/data_raster/other_ras4.tif")

    mock_db.query.return_value.filter.return_value.first.side_effect = [
        my_vec1,
        other_vec3,
        None,
        my_ras2,
        None,
        other_ras4
    ]
    mock_db.query.return_value.filter.return_value.all.return_value = []
    app.dependency_overrides[require_any_key] = lambda: owner_key

    with patch("app.routes.v1.layer.geoserver_client") as mock_geo, \
         patch("app.routes.v1.layer._is_table_referenced_elsewhere", return_value=(False, None)), \
         patch("app.routes.v1.layer._is_file_referenced_elsewhere", return_value=(False, None)), \
         patch("app.routes.v1.layer.spatial_engine") as mock_eng:

        payload = {"layer_ids": [str(my_vec1.id), str(other_vec3.id), str(my_ras2.id), str(other_ras4.id)]}
        resp = client.post("/api/v1/layers/batch-delete", json=payload)
        
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        # Tepat 2 layer milik owner yang terhapus (1 vector, 1 raster)
        assert data["deleted_count"] == 2

        # mock_db.delete hanya dipanggil untuk layer milik caller
        deleted_objs = [call_args[0][0] for call_args in mock_db.delete.call_args_list]
        assert my_vec1 in deleted_objs
        assert my_ras2 in deleted_objs
        assert other_vec3 not in deleted_objs
        assert other_ras4 not in deleted_objs
