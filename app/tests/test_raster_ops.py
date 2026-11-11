import math
import json
import pytest
import numpy as np
from fastapi.encoders import jsonable_encoder

from app.services.raster_ops import (
    validate_bbox,
    compute_pixel_index,
    calculate_raster_stats,
    build_1x1_wcs_subset,
    build_area_wcs_subsets,
    RasterOpsError,
)

def test_validate_bbox_success():
    bbox = [100.0, 13.0, 101.0, 14.0]
    result = validate_bbox(bbox)
    assert result == (100.0, 13.0, 101.0, 14.0)

def test_validate_bbox_invalid_length():
    with pytest.raises(RasterOpsError) as exc:
        validate_bbox([100.0, 13.0])
    assert "harus berupa array dengan 4 angka" in str(exc.value)

def test_validate_bbox_inverted_coordinates():
    with pytest.raises(RasterOpsError) as exc:
        validate_bbox([101.0, 14.0, 100.0, 13.0])
    assert "harus lebih kecil" in str(exc.value)

def test_validate_bbox_out_of_range_4326():
    with pytest.raises(RasterOpsError) as exc:
        validate_bbox([100.0, -95.0, 101.0, 14.0])
    assert "Latitude harus berada dalam rentang" in str(exc.value)

def test_compute_pixel_index_matches_rasterio():
    """
    Test pixel index math identically matches rasterio dataset.index(x, y).
    rasterio logic:
        col = math.floor((x - origin_x) / res_x)
        row = math.floor((origin_y - y) / abs(res_y))
    """
    origin_x = 100.0
    origin_y = 15.0
    res_x = 0.001
    res_y = -0.001
    width = 1000
    height = 1000

    # Exactly at top-left origin (col 0, row 0)
    col, row, outside = compute_pixel_index(origin_x, origin_y, origin_x, origin_y, res_x, res_y, width, height)
    assert not outside
    assert col == 0
    assert row == 0

    # Inside pixel (0, 0)
    col, row, outside = compute_pixel_index(origin_x + 0.0005, origin_y - 0.0005, origin_x, origin_y, res_x, res_y, width, height)
    assert not outside
    assert col == 0
    assert row == 0

    # Exactly at right boundary of pixel (0, 0) -> enters pixel (1, 0)
    col, row, outside = compute_pixel_index(origin_x + 0.001, origin_y - 0.0005, origin_x, origin_y, res_x, res_y, width, height)
    assert not outside
    assert col == 1
    assert row == 0

    # Exactly at bottom boundary of pixel (0, 0) -> enters pixel (0, 1)
    col, row, outside = compute_pixel_index(origin_x + 0.0005, origin_y - 0.001, origin_x, origin_y, res_x, res_y, width, height)
    assert not outside
    assert col == 0
    assert row == 1

    # Outside bounds (negative)
    col, row, outside = compute_pixel_index(origin_x - 0.0001, origin_y, origin_x, origin_y, res_x, res_y, width, height)
    assert outside is True

    # Outside bounds (beyond width)
    col, row, outside = compute_pixel_index(origin_x + 2.0, origin_y, origin_x, origin_y, res_x, res_y, width, height)
    assert outside is True

def test_compute_pixel_index_grid_lines_and_epsilon():
    """
    Uji batas garis kisi (grid line x = origin + k * res) dan epsilon +- 1e-9.
    Memastikan konsistensi 100% dengan rasterio.transform.rowcol(..., op=math.floor).
    """
    import affine
    import rasterio.transform
    import math

    ox, oy = 500000.0, 1500000.0
    rx, ry = 30.0, -30.0
    w, h = 500, 500
    af = affine.Affine(rx, 0.0, ox, 0.0, ry, oy)

    for k in [1, 2, 5, 10, 50]:
        x_grid = ox + k * rx
        y_grid = oy - k * abs(ry)

        # 1. Tepat pada garis kisi (harus masuk ke pixel k)
        c, r, out = compute_pixel_index(x_grid, y_grid, ox, oy, rx, ry, w, h)
        exp_r, exp_c = rasterio.transform.rowcol(af, x_grid, y_grid, op=math.floor)
        assert c == exp_c == k
        assert r == exp_r == k
        assert not out

        # 2. Garis kisi - 1e-9 (harus masuk ke pixel k-1)
        c_minus, r_minus, out_minus = compute_pixel_index(x_grid - 1e-9, y_grid + 1e-9, ox, oy, rx, ry, w, h)
        exp_r_min, exp_c_min = rasterio.transform.rowcol(af, x_grid - 1e-9, y_grid + 1e-9, op=math.floor)
        assert c_minus == exp_c_min == k - 1
        assert r_minus == exp_r_min == k - 1
        assert not out_minus

        # 3. Garis kisi + 1e-9 (harus masuk ke pixel k)
        c_plus, r_plus, out_plus = compute_pixel_index(x_grid + 1e-9, y_grid - 1e-9, ox, oy, rx, ry, w, h)
        exp_r_plus, exp_c_plus = rasterio.transform.rowcol(af, x_grid + 1e-9, y_grid - 1e-9, op=math.floor)
        assert c_plus == exp_c_plus == k
        assert r_plus == exp_r_plus == k
        assert not out_plus

def test_build_1x1_wcs_subset():
    # EPSG:4326 uses Lat, Long
    subsets_4326 = build_1x1_wcs_subset(10, 20, 100.0, 15.0, 0.001, -0.001, "EPSG:4326", ["Lat", "Long"])
    assert len(subsets_4326) == 2
    assert subsets_4326[0].startswith("subset=Lat(")
    assert subsets_4326[1].startswith("subset=Long(")

    # EPSG:32647 uses E, N
    subsets_utm = build_1x1_wcs_subset(5, 5, 700000.0, 1500000.0, 30.0, -30.0, "EPSG:32647", ["E", "N"])
    assert len(subsets_utm) == 2
    assert subsets_utm[0].startswith("subset=E(")
    assert subsets_utm[1].startswith("subset=N(")

def test_calculate_raster_stats_with_nan_and_infinities():
    """
    Revision 1 requirement:
    All nodata/NaN/inf (including -inf in float32) are masked.
    Non-finite nodata and min/max must be JSON-serializable (serialized as None/null).
    Test with array float32 containing -inf and NaN, and pass through jsonable_encoder.
    """
    arr = np.array([
        [-np.inf, np.nan, 10.0, 20.0],
        [np.inf, 30.0, 40.0, 50.0],
        [-9999.0, 60.0, 70.0, 80.0]
    ], dtype=np.float32)

    stats = calculate_raster_stats(arr, nodata_val=-9999.0, bins=10)
    assert stats["valid_count"] == 8
    assert stats["nodata_count"] == 4
    assert stats["min"] == 10.0
    assert stats["max"] == 80.0
    assert stats["mean"] == 45.0
    assert stats["nodata"] == -9999.0
    assert stats["all_nodata"] is False

    # Check JSON serializability via FastAPI's jsonable_encoder and json.dumps
    encoded = jsonable_encoder(stats)
    json_str = json.dumps(encoded)
    assert json_str is not None
    assert "NaN" not in json_str
    assert "Infinity" not in json_str

def test_calculate_raster_stats_all_nodata_and_nonfinite():
    """
    Test edge case where all values are NaN / Inf or nodata.
    Must return all_nodata=True, stats min/max/mean as None, and be fully JSON-serializable.
    """
    arr = np.array([np.nan, np.inf, -np.inf, -9999.0], dtype=np.float32)
    stats = calculate_raster_stats(arr, nodata_val=-9999.0, bins=5)
    assert stats["all_nodata"] is True
    assert stats["min"] is None
    assert stats["max"] is None
    assert stats["mean"] is None
    assert stats["nodata"] == -9999.0

    # Also test when nodata_val is float("-inf")
    arr2 = np.array([np.nan, np.inf, -np.inf, np.nan], dtype=np.float32)
    stats2 = calculate_raster_stats(arr2, nodata_val=float("-inf"), bins=5)
    assert stats2["all_nodata"] is True
    assert stats2["nodata"] is None  # -inf serialized as None

    encoded = jsonable_encoder(stats2)
    json_str = json.dumps(encoded)
    assert "NaN" not in json_str
    assert "null" in json_str
