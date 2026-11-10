import os
import tempfile
import pytest
import numpy as np
import rasterio
from rasterio.transform import from_origin

from app.services.raster_stats_service import (
    compute_raster_stats,
    classify,
    AllMaskedError,
    _stats_cache,
    _classify_cache
)

def create_synthetic_geotiff(
    path: str,
    data: np.ndarray,
    nodata: float = None,
    dtype: str = "float32"
):
    """Helper to create a synthetic GeoTIFF file for unit testing."""
    height, width = data.shape
    transform = from_origin(100.0, 0.0, 1.0, 1.0)
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": dtype,
        "crs": "EPSG:4326",
        "transform": transform,
    }
    if nodata is not None:
        profile["nodata"] = nodata

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype(dtype), 1)

def test_skewed_distribution_quantile():
    """
    Test skewed distribution:
    Quantile classification should result in approximately equal pixel counts per class.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tif_path = os.path.join(tmpdir, "skewed.tif")
        rng = np.random.default_rng(123)
        # Highly skewed exponential distribution
        arr = rng.exponential(scale=2.0, size=(100, 100)).astype(np.float32) + 0.1
        create_synthetic_geotiff(tif_path, arr)

        stats = compute_raster_stats(tif_path)
        assert stats["pixel_count"] == 10000
        assert stats["min"] == float(np.min(arr))
        assert stats["max"] == float(np.max(arr))

        n_classes = 5
        res = classify(tif_path, method="quantile", classes=n_classes)
        breaks = res["breaks"]

        assert len(breaks) == n_classes + 1
        assert breaks[0] == stats["min"]
        assert breaks[-1] == stats["max"]
        # Strictly ascending
        assert all(breaks[i] < breaks[i + 1] for i in range(len(breaks) - 1))
        assert res["effective_classes"] == n_classes

        # Verify pixel distribution across quantile bins (~20% each)
        counts = []
        for i in range(n_classes):
            low = breaks[i]
            high = breaks[i + 1]
            if i == 0:
                mask = (arr >= low) & (arr <= high)
            else:
                mask = (arr > low) & (arr <= high)
            counts.append(int(np.sum(mask)))

        # Each bin in 10,000 pixels should have around 2,000 pixels (+/- 200)
        for c in counts:
            assert abs(c - 2000) < 300, f"Bin count {c} deviated too much from 2000"

def test_integer_data_unique_values_and_jenks_reduction():
    """
    Test integer data 1 to 5:
    is_integer must be True, unique_values has 5 entries,
    and requesting 10 Jenks classes reduces effective_classes to <= 5.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tif_path = os.path.join(tmpdir, "discrete_1_to_5.tif")
        # 100x100 raster with discrete values 1, 2, 3, 4, 5
        arr = np.repeat([1, 2, 3, 4, 5], 2000).reshape((100, 100)).astype(np.int32)
        create_synthetic_geotiff(tif_path, arr, dtype="int32")

        stats = compute_raster_stats(tif_path)
        assert stats["is_integer"] is True
        assert stats["unique_values"] is not None
        assert len(stats["unique_values"]) == 5

        for idx, entry in enumerate(stats["unique_values"], start=1):
            assert entry["value"] == idx
            assert entry["count"] == 2000

        # Request 10 classes with Jenks
        res = classify(tif_path, method="natural_breaks", classes=10)
        assert res["effective_classes"] <= 5
        assert res["notice"] is not None
        assert "disesuaikan" in res["notice"] or "nilai berbeda" in res["notice"]
        breaks = res["breaks"]
        assert breaks[0] == 1.0
        assert breaks[-1] == 5.0
        assert all(breaks[i] < breaks[i + 1] for i in range(len(breaks) - 1))

def test_min_equals_max():
    """
    Test constant raster where all valid pixels have identical value:
    effective_classes must be 1, breaks [val, val].
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tif_path = os.path.join(tmpdir, "constant.tif")
        arr = np.full((50, 50), 42.0, dtype=np.float32)
        create_synthetic_geotiff(tif_path, arr)

        stats = compute_raster_stats(tif_path)
        assert stats["min"] == 42.0
        assert stats["max"] == 42.0
        assert stats["mean"] == 42.0
        assert stats["std"] == 0.0

        res = classify(tif_path, method="equal_interval", classes=5)
        assert res["effective_classes"] == 1
        assert res["breaks"] == [42.0, 42.0]
        assert res["notice"] is not None

def test_nodata_excluded_from_stats():
    """
    Test nodata handling:
    Nodata pixels must be completely excluded from min, max, mean, std, and pixel_count.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tif_path = os.path.join(tmpdir, "with_nodata.tif")
        arr = np.full((100, 100), -9999.0, dtype=np.float32)
        # Only quadrant is valid data (20 to 80)
        valid_part = np.linspace(20.0, 80.0, 2500, dtype=np.float32).reshape((50, 50))
        arr[:50, :50] = valid_part

        create_synthetic_geotiff(tif_path, arr, nodata=-9999.0)

        stats = compute_raster_stats(tif_path)
        assert stats["nodata"] == -9999.0
        assert stats["pixel_count"] == 2500
        assert stats["min"] == 20.0
        assert stats["max"] == 80.0
        assert abs(stats["mean"] - 50.0) < 0.1
        # Nodata -9999.0 must not affect minimum
        assert stats["min"] > 0

def test_float_raster_with_whole_integers():
    """
    Test float32 raster containing whole integers:
    is_integer must evaluate to True based on values, and unique_values populated.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tif_path = os.path.join(tmpdir, "float_integers.tif")
        # float32 with whole numbers 10.0, 20.0, 30.0
        arr = np.repeat([10.0, 20.0, 30.0], 1000).reshape((50, 60)).astype(np.float32)
        create_synthetic_geotiff(tif_path, arr, dtype="float32")

        stats = compute_raster_stats(tif_path)
        assert stats["is_integer"] is True
        assert stats["unique_values"] is not None
        assert len(stats["unique_values"]) == 3
        vals = [entry["value"] for entry in stats["unique_values"]]
        assert vals == [10, 20, 30]

def test_all_nodata_raises_all_masked_error():
    """
    Test raster where all pixels are masked/nodata:
    Must raise AllMaskedError with clear descriptive message.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tif_path = os.path.join(tmpdir, "all_nodata.tif")
        arr = np.full((50, 50), -9999.0, dtype=np.float32)
        create_synthetic_geotiff(tif_path, arr, nodata=-9999.0)

        with pytest.raises(AllMaskedError) as exc_info:
            compute_raster_stats(tif_path)
        assert "Semua piksel pada raster bernilai nodata atau masked" in str(exc_info.value)

def test_missing_file_raises_file_not_found():
    """
    Test missing file raises FileNotFoundError.
    """
    fake_path = "/path/to/definitely/nonexistent_raster_12345.tif"
    with pytest.raises(FileNotFoundError):
        compute_raster_stats(fake_path)

    with pytest.raises(FileNotFoundError):
        classify(fake_path, method="equal_interval", classes=5)

def test_lru_cache_reused():
    """
    Test LRU cache returns the same result and moves item to end.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tif_path = os.path.join(tmpdir, "cached.tif")
        arr = np.linspace(1.0, 100.0, 1000, dtype=np.float32).reshape((20, 50))
        create_synthetic_geotiff(tif_path, arr)

        stats1 = compute_raster_stats(tif_path)
        stats2 = compute_raster_stats(tif_path)
        assert stats1 == stats2

        class1 = classify(tif_path, method="equal_interval", classes=4)
        class2 = classify(tif_path, method="equal_interval", classes=4)
        assert class1 == class2

