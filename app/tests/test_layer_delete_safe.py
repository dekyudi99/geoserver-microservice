import uuid
import pytest
from unittest.mock import MagicMock, patch

from app.models.spatial_data import VectorLayer, RasterMetadata
from app.routes.v1.layer import (
    _is_table_referenced_elsewhere,
    _is_file_referenced_elsewhere
)

def test_is_table_referenced_elsewhere_returns_true_when_other_exists():
    db = MagicMock()
    vec_id_1 = uuid.uuid4()
    vec_id_2 = uuid.uuid4()
    
    other_vec = VectorLayer(id=vec_id_2, table_name="shared_table", workspace_name="ws")
    q = MagicMock()
    db.query.return_value = q
    q.filter.return_value = q
    q.first.return_value = other_vec

    is_ref, reason = _is_table_referenced_elsewhere(db, "shared_table", exclude_vec_id=vec_id_1)
    assert is_ref is True
    assert "Direferensikan oleh VectorLayer lain" in reason

def test_is_table_referenced_elsewhere_returns_false_when_no_other_exists():
    db = MagicMock()
    vec_id_1 = uuid.uuid4()
    q = MagicMock()
    db.query.return_value = q
    q.filter.return_value = q
    q.first.return_value = None

    with patch("app.routes.v1.layer.geoserver_client.get_workspaces", return_value=[]):
        is_ref, reason = _is_table_referenced_elsewhere(db, "unique_table", exclude_vec_id=vec_id_1)
        assert is_ref is False
        assert reason is None

def test_is_file_referenced_elsewhere_returns_true_when_other_exists():
    db = MagicMock()
    ras_id_1 = uuid.uuid4()
    ras_id_2 = uuid.uuid4()

    other_ras = RasterMetadata(id=ras_id_2, file_path="/data_raster/shared.tif")
    q = MagicMock()
    db.query.return_value = q
    q.filter.return_value = q
    q.all.return_value = [other_ras]

    is_ref, reason = _is_file_referenced_elsewhere(db, "/data_raster/shared.tif", exclude_ras_id=ras_id_1)
    assert is_ref is True
    assert "Direferensikan oleh RasterMetadata lain" in reason

def test_is_file_referenced_elsewhere_returns_false_when_no_other_exists():
    db = MagicMock()
    ras_id_1 = uuid.uuid4()
    q = MagicMock()
    db.query.return_value = q
    q.filter.return_value = q
    q.all.return_value = []

    with patch("app.routes.v1.layer.geoserver_client.get_workspaces", return_value=[]):
        is_ref, reason = _is_file_referenced_elsewhere(db, "/data_raster/unique.tif", exclude_ras_id=ras_id_1)
        assert is_ref is False
        assert reason is None

