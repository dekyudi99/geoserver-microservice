import uuid
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.models.api_key import ApiKey, ApiKeyType
from app.models.spatial_data import RasterMetadata, WorkspaceMetadata
from app.config.database import get_db
from app.security.auth import get_verified_key

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

# --- Coverage Store Tests ---

def test_coverage_store_delete_no_auth_returns_401():
    app.dependency_overrides.clear()
    resp = client.delete("/api/v1/coverage-stores/ws_test/store_ras_1")
    assert resp.status_code == 401

def test_coverage_store_delete_non_owner_returns_403(mock_db):
    app.dependency_overrides[get_verified_key] = lambda: other_key
    mock_raster = RasterMetadata(
        id=uuid.uuid4(),
        api_key_id=OWNER_KEY_ID,
        workspace_name="ws_test",
        store_name="store_ras_1",
        layer_name="ras_1"
    )
    mock_ws = WorkspaceMetadata(
        id=uuid.uuid4(),
        api_key_id=OWNER_KEY_ID,
        workspace_name="ws_test",
        display_name="Test WS"
    )
    def query_dispatcher(model):
        m = MagicMock()
        if model == RasterMetadata:
            m.filter.return_value.first.return_value = mock_raster
        elif model == WorkspaceMetadata:
            m.filter.return_value.first.return_value = mock_ws
        else:
            m.filter.return_value.first.return_value = None
        return m
    mock_db.query.side_effect = query_dispatcher

    resp = client.delete("/api/v1/coverage-stores/ws_test/store_ras_1")
    assert resp.status_code == 403
    assert "tidak memiliki izin" in resp.json()["detail"]

def test_coverage_store_delete_owner_success(mock_db):
    app.dependency_overrides[get_verified_key] = lambda: owner_key
    mock_raster = RasterMetadata(
        id=uuid.uuid4(),
        api_key_id=OWNER_KEY_ID,
        workspace_name="ws_test",
        store_name="store_ras_1",
        layer_name="ras_1"
    )
    mock_ws = WorkspaceMetadata(
        id=uuid.uuid4(),
        api_key_id=OWNER_KEY_ID,
        workspace_name="ws_test",
        display_name="Test WS"
    )
    def query_dispatcher(model):
        m = MagicMock()
        if model == RasterMetadata:
            m.filter.return_value.first.return_value = mock_raster
        elif model == WorkspaceMetadata:
            m.filter.return_value.first.return_value = mock_ws
        else:
            m.filter.return_value.first.return_value = None
        return m
    mock_db.query.side_effect = query_dispatcher

    with patch("app.routes.v1.coverage_store.geoserver_client.delete_coverage_store", return_value=True):
        resp = client.delete("/api/v1/coverage-stores/ws_test/store_ras_1")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

def test_coverage_store_delete_primary_success(mock_db):
    app.dependency_overrides[get_verified_key] = lambda: primary_key
    with patch("app.routes.v1.coverage_store.geoserver_client.delete_coverage_store", return_value=True):
        resp = client.delete("/api/v1/coverage-stores/ws_test/store_ras_1")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

# --- Workspace RBAC Tests ---

def test_workspace_create_no_auth_returns_401():
    app.dependency_overrides.clear()
    resp = client.post("/api/v1/workspaces", json={"workspace_name": "ws_new", "display_name": "New WS"})
    assert resp.status_code == 401

def test_workspace_create_standard_returns_403(mock_db):
    app.dependency_overrides[get_verified_key] = lambda: other_key
    resp = client.post("/api/v1/workspaces", json={"workspace_name": "ws_new", "display_name": "New WS"})
    assert resp.status_code == 403
    assert "Hanya PRIMARY key" in resp.json()["detail"]

def test_workspace_create_primary_success(mock_db):
    app.dependency_overrides[get_verified_key] = lambda: primary_key
    mock_db.query.return_value.filter.return_value.first.return_value = None
    with patch("app.routes.v1.workspace.geoserver_client.create_workspace", return_value=True):
        resp = client.post("/api/v1/workspaces", json={"workspace_name": "ws_new", "display_name": "New WS"})
        assert resp.status_code == 201
        assert resp.json()["success"] is True

