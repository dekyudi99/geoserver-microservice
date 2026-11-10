import os
import threading
import logging
from collections import OrderedDict
from typing import Dict, Any, List, Optional, Tuple
import numpy as np
import rasterio

try:
    import jenkspy  # type: ignore
except ImportError:
    jenkspy = None

logger = logging.getLogger("geoserver_service.raster_stats")

class AllMaskedError(ValueError):
    """Exception raised when all pixels in a raster are masked or nodata."""
    pass

class LRUCache:
    """Thread-safe in-memory LRU cache."""
    def __init__(self, maxsize: int = 64):
        self.maxsize = maxsize
        self.cache: OrderedDict[Any, Any] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key: Any) -> Optional[Any]:
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            return None

    def set(self, key: Any, value: Any) -> None:
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = value
            if len(self.cache) > self.maxsize:
                self.cache.popitem(last=False)

    def clear(self) -> None:
        with self.lock:
            self.cache.clear()

_stats_cache = LRUCache(maxsize=64)
_classify_cache = LRUCache(maxsize=64)

def _get_file_stat(path: str) -> Tuple[str, int, int]:
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"File raster tidak ditemukan: '{path}'")
    st = os.stat(path)
    return (os.path.abspath(path), st.st_mtime_ns, st.st_size)

def _pure_jenks_breaks(data: np.ndarray, n_classes: int) -> List[float]:
    """Fallback Fisher-Jenks dynamic programming algorithm if jenkspy is not present."""
    arr = np.sort(np.asarray(data, dtype=np.float64))
    u_vals = np.unique(arr)
    if len(u_vals) <= n_classes:
        return u_vals.tolist()

    if len(arr) > 300:
        arr = np.percentile(arr, np.linspace(0, 100, 300))
    n = len(arr)

    lower_class_limits = np.zeros((n + 1, n_classes + 1), dtype=np.int32)
    variance_combinations = np.full((n + 1, n_classes + 1), float('inf'), dtype=np.float64)

    for i in range(1, n_classes + 1):
        lower_class_limits[1, i] = 1
        variance_combinations[1, i] = 0.0

    for l in range(2, n + 1):
        sum_val = 0.0
        sum_sq = 0.0
        w = 0.0
        for m in range(1, l + 1):
            i3 = l - m + 1
            val = arr[i3 - 1]
            sum_sq += val * val
            sum_val += val
            w += 1.0
            variance = sum_sq - (sum_val * sum_val) / w
            i4 = i3 - 1
            if i4 != 0:
                for j in range(2, n_classes + 1):
                    cand = variance + variance_combinations[i4, j - 1]
                    if variance_combinations[l, j] >= cand:
                        lower_class_limits[l, j] = i3
                        variance_combinations[l, j] = cand
        lower_class_limits[l, 1] = 1
        variance_combinations[l, 1] = variance

    k = n
    kclass = [0.0] * (n_classes + 1)
    kclass[n_classes] = float(arr[n - 1])
    kclass[0] = float(arr[0])
    count_num = n_classes

    while count_num >= 2:
        idx = int(lower_class_limits[k, count_num]) - 1
        kclass[count_num - 1] = float(arr[idx])
        k = int(lower_class_limits[k, count_num] - 1)
        count_num -= 1
    return kclass

def _compute_raw_stats_and_sample(path: str) -> Tuple[Dict[str, Any], np.ndarray]:
    """
    Stream raster blocks to calculate accurate statistics (count, min, max, mean, std)
    without loading the entire raster into memory, and gather a reproducible random sample
    (max ~2M valid values) for classification.
    """
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"File raster tidak ditemukan: '{path}'")

    with rasterio.open(path) as src:
        nodata_val = float(src.nodata) if src.nodata is not None else None
        total_pixels = src.width * src.height
        sample_rate = min(1.0, 2_000_000.0 / total_pixels) if total_pixels > 0 else 1.0
        rng = np.random.default_rng(42)

        count = 0
        min_val = float('inf')
        max_val = float('-inf')
        sum_val = 0.0
        sum_sq = 0.0
        is_integer = True
        unique_counter: Dict[Any, int] = {}
        too_many_uniques = False
        sample_chunks: List[np.ndarray] = []

        for _, win in src.block_windows(1):
            block = src.read(1, window=win, masked=True)
            valid = block.compressed()
            if len(valid) == 0:
                continue

            v_f64 = valid.astype(np.float64)
            count += len(valid)
            b_min = float(np.min(valid))
            b_max = float(np.max(valid))
            if b_min < min_val:
                min_val = b_min
            if b_max > max_val:
                max_val = b_max
            sum_val += float(np.sum(v_f64))
            sum_sq += float(np.sum(v_f64 ** 2))

            # Integer check: verify actual pixel values (handles float32 containing whole integers)
            if is_integer:
                if not np.all(np.equal(np.mod(valid, 1), 0)):
                    is_integer = False
                    unique_counter.clear()
                elif not too_many_uniques:
                    u_vals, u_counts = np.unique(valid, return_counts=True)
                    for v, c in zip(u_vals, u_counts):
                        unique_counter[v] = unique_counter.get(v, 0) + int(c)
                    if len(unique_counter) > 256:
                        too_many_uniques = True
                        unique_counter.clear()

            # Collect proportional sample per block for classification
            if sample_rate < 1.0:
                k = int(len(valid) * sample_rate)
                if k > 0:
                    sample_chunks.append(rng.choice(valid, size=k, replace=False))
            else:
                sample_chunks.append(valid)

        if count == 0:
            raise AllMaskedError("Semua piksel pada raster bernilai nodata atau masked.")

        mean_val = sum_val / count
        variance = max(0.0, (sum_sq / count) - (mean_val ** 2))
        std_val = float(np.sqrt(variance))

        sample = np.concatenate(sample_chunks) if sample_chunks else np.array([], dtype=np.float64)
        if len(sample) > 2_000_000:
            sample = rng.choice(sample, size=2_000_000, replace=False)

        unique_values = None
        if is_integer and not too_many_uniques and len(unique_counter) <= 256:
            unique_values = [
                {"value": int(v), "count": int(c)}
                for v, c in sorted(unique_counter.items(), key=lambda x: x[0])
            ]

        stats = {
            "min": float(min_val),
            "max": float(max_val),
            "mean": float(mean_val),
            "std": float(std_val),
            "nodata": nodata_val,
            "is_integer": bool(is_integer),
            "unique_values": unique_values,
            "pixel_count": int(count),
            "sampled": bool(sample_rate < 1.0),
        }

        return stats, sample

def compute_raster_stats(path: str) -> Dict[str, Any]:
    """
    Compute raster statistics adhering to shared contract.
    Cached per process (max 64 entries) using (path, mtime_ns, size).
    """
    file_key = _get_file_stat(path)
    cached = _stats_cache.get(file_key)
    if cached is not None:
        return cached[0]

    stats, sample = _compute_raw_stats_and_sample(path)
    _stats_cache.set(file_key, (stats, sample))
    return stats

def classify(path: str, method: str, classes: int) -> Dict[str, Any]:
    """
    Compute strictly ascending breaks for raster classification.
    Cached per process (max 64 entries) using (path, mtime_ns, size, method, classes).
    """
    norm_method = "manual" if method == "custom" else ("natural_breaks" if method == "jenks" else method)
    valid_methods = {"natural_breaks", "quantile", "equal_interval"}
    if norm_method not in valid_methods:
        raise ValueError(
            f"Metode klasifikasi tidak didukung: '{method}'. Pilih salah satu dari: {', '.join(sorted(valid_methods))}."
        )

    classes_count = int(classes)
    if classes_count < 1 or classes_count > 50:
        raise ValueError("classes harus bernilai antara 1 sampai 50.")

    file_key = _get_file_stat(path)
    classify_key = (*file_key, norm_method, classes_count)
    cached_result = _classify_cache.get(classify_key)
    if cached_result is not None:
        return cached_result

    # Retrieve or compute statistics and sample
    cached_data = _stats_cache.get(file_key)
    if cached_data is not None:
        stats, sample = cached_data
    else:
        stats, sample = _compute_raw_stats_and_sample(path)
        _stats_cache.set(file_key, (stats, sample))

    min_val = float(stats["min"])
    max_val = float(stats["max"])

    if min_val >= max_val:
        res = {
            "breaks": [min_val, max_val],
            "effective_classes": 1,
            "notice": "Semua piksel memiliki nilai yang sama."
        }
        _classify_cache.set(classify_key, res)
        return res

    if norm_method == "equal_interval":
        raw_breaks = np.linspace(min_val, max_val, classes_count + 1).tolist()
    elif norm_method == "quantile":
        probs = np.linspace(0, 1, classes_count + 1)
        raw_breaks = np.quantile(sample, probs).tolist()
    elif norm_method == "natural_breaks":
        rng = np.random.default_rng(42)
        subsample = sample if len(sample) <= 10000 else rng.choice(sample, size=10000, replace=False)
        u_vals = np.unique(subsample)
        if len(u_vals) <= classes_count:
            raw_breaks = u_vals.tolist()
        else:
            if jenkspy is not None:
                try:
                    raw_breaks = jenkspy.jenks_breaks(subsample, n_classes=classes_count)  # type: ignore
                except Exception:
                    raw_breaks = _pure_jenks_breaks(subsample, classes_count)
            else:
                raw_breaks = _pure_jenks_breaks(subsample, classes_count)

    # Enforce strictly ascending breaks with exact min and max bounds
    cleaned = [float(min_val)]
    for b in raw_breaks[1:]:
        bf = float(b)
        if bf > cleaned[-1]:
            cleaned.append(bf)

    cleaned[0] = float(min_val)
    if cleaned[-1] != float(max_val):
        if cleaned[-1] < float(max_val):
            cleaned.append(float(max_val))
        else:
            cleaned[-1] = float(max_val)

    effective_classes = len(cleaned) - 1
    notice = None
    if effective_classes < classes_count:
        notice = f"Data hanya punya {effective_classes} nilai berbeda (jumlah kelas disesuaikan dari {classes_count} menjadi {effective_classes})."

    result = {
        "breaks": cleaned,
        "effective_classes": effective_classes,
        "notice": notice
    }
    _classify_cache.set(classify_key, result)
    return result
