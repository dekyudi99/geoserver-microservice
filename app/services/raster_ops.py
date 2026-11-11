"""
app/services/raster_ops.py
Modul murni manipulasi koordinat, penentuan window/subsets WCS, dan kalkulasi statistik raster.
Sepenuhnya mematuhi:
- Keputusan 1: Akses nilai raster & statistik HANYA lewat WCS.
- Revisi 1: Nilai non-finite (NaN, Inf, -Inf) selalu di-mask dan tidak masuk response JSON mentah.
- Revisi 2: Indeks pixel deterministik (floor matching rasterio `dataset.index(x, y)`), window 1x1 berpusat di pixel, pre-check extent.
- Revisi 3: Resampling dengan interpolation=nearest OGC URI eksplisit; resolusi dibaca dari transform GeoTIFF hasil.
"""

import math
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import rasterio
from rasterio.io import MemoryFile
import pyproj
from pyproj import Transformer

INTERPOLATION_NEAREST_URI = "http://www.opengis.net/def/interpolation/OGC/1/nearest-neighbor"

class RasterOpsError(ValueError):
    """Custom exception untuk input raster ops yang tidak valid."""
    pass

def validate_bbox(bbox: Any) -> Tuple[float, float, float, float]:
    """
    Memvalidasi format bounding box [minLng, minLat, maxLng, maxLat].
    Memastikan nilai finite, rentang latitude [-90, 90], longitude [-180, 180],
    serta min < max.
    """
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise RasterOpsError("BBox harus berupa array dengan 4 angka [minLng, minLat, maxLng, maxLat].")

    try:
        min_lng, min_lat, max_lng, max_lat = [float(x) for x in bbox]
    except (ValueError, TypeError):
        raise RasterOpsError("Nilai koordinat BBox harus berupa angka valid.")

    for val, name in [(min_lng, "minLng"), (min_lat, "minLat"), (max_lng, "maxLng"), (max_lat, "maxLat")]:
        if not math.isfinite(val):
            raise RasterOpsError(f"Nilai koordinat BBox '{name}' harus finite (bukan NaN atau Infinity).")

    if min_lng >= max_lng:
        raise RasterOpsError(f"minLng ({min_lng}) harus lebih kecil dari maxLng ({max_lng}).")

    if min_lat >= max_lat:
        raise RasterOpsError(f"minLat ({min_lat}) harus lebih kecil dari maxLat ({max_lat}).")

    if not (-180.0 <= min_lng <= 180.0 and -180.0 <= max_lng <= 180.0):
        raise RasterOpsError("Longitude harus berada dalam rentang [-180, 180].")

    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise RasterOpsError("Latitude harus berada dalam rentang [-90, 90].")

    return min_lng, min_lat, max_lng, max_lat

def transform_point_to_native(lat: float, lng: float, native_crs_str: str) -> Tuple[float, float]:
    """
    Mengonversi titik (lat, lng) dalam WGS84 (EPSG:4326) ke koordinat native CRS (x, y).
    """
    clean_crs = (native_crs_str or "").strip().upper()
    if clean_crs in ("EPSG:4326", "WGS 84", "OGC:CRS84", "4326") or not clean_crs:
        return float(lng), float(lat)

    transformer = Transformer.from_crs("EPSG:4326", clean_crs, always_xy=True)
    x_native, y_native = transformer.transform(lng, lat)
    return float(x_native), float(y_native)

def transform_native_to_wgs84(x: float, y: float, native_crs_str: str) -> Tuple[float, float]:
    """
    Mengonversi titik (x, y) dalam koordinat native CRS ke WGS84 (lng, lat).
    """
    clean_crs = (native_crs_str or "").strip().upper()
    if clean_crs in ("EPSG:4326", "WGS 84", "OGC:CRS84", "4326") or not clean_crs:
        return float(x), float(y)

    transformer = Transformer.from_crs(clean_crs, "EPSG:4326", always_xy=True)
    lng, lat = transformer.transform(x, y)
    return float(lng), float(lat)

def compute_pixel_index(
    x: float,
    y: float,
    origin_x: float,
    origin_y: float,
    res_x: float,
    res_y: float,
    width: int,
    height: int
) -> Tuple[int, int, bool]:
    """
    Menghitung indeks pixel (col, row) dari koordinat native (x, y) berdasarkan grid DescribeCoverage.
    Mengikuti aturan yang sama persis dengan rasterio.transform.rowcol(transform, x, y, op=math.floor).
    """
    rx = abs(float(res_x))
    ry = abs(float(res_y))

    if rx <= 0 or ry <= 0 or width <= 0 or height <= 0:
        return 0, 0, True

    import affine
    import rasterio.transform

    af = affine.Affine(rx, 0.0, float(origin_x), 0.0, -ry, float(origin_y))
    row, col = rasterio.transform.rowcol(af, float(x), float(y), op=math.floor)
    col = int(col)
    row = int(row)

    is_outside = (col < 0 or col >= width or row < 0 or row >= height)
    return col, row, is_outside

def build_1x1_wcs_subset(
    col: int,
    row: int,
    origin_x: float,
    origin_y: float,
    res_x: float,
    res_y: float,
    native_crs_str: str,
    axis_labels: List[str]
) -> List[str]:
    """
    Membentuk parameter subset WCS 2.0.1 untuk window 1x1 tepat pada pixel (col, row).
    Subset berpusat di tengah pixel dengan batas persis [center - 0.5*res, center + 0.5*res].
    Menyesuaikan nama sumbu dengan axis_labels resmi GeoServer DescribeCoverage:
      - EPSG:4326: ['Lat', 'Long']
      - Proyeksi UTM: ['E', 'N']
    """
    rx = abs(float(res_x))
    ry = abs(float(res_y))

    # Koordinat batas tepat dari pixel ini
    min_x = origin_x + (col * rx)
    max_x = min_x + rx
    max_y = origin_y - (row * ry)
    min_y = max_y - ry

    # Buat rentang sedikit lebih ramping (99% dari lebar pixel) agar GeoServer tidak menarik 2x2
    eps_x = rx * 0.005
    eps_y = ry * 0.005
    sub_min_x = min_x + eps_x
    sub_max_x = max_x - eps_x
    sub_min_y = min_y + eps_y
    sub_max_y = max_y - eps_y

    subsets = []
    labels_upper = [a.upper() for a in axis_labels]

    if "LAT" in labels_upper and "LONG" in labels_upper:
        # Sumbu Lat (Y) dan Long (X)
        for label in axis_labels:
            if label.upper() == "LAT":
                subsets.append(f"subset={label}({sub_min_y:.8f},{sub_max_y:.8f})")
            elif label.upper() == "LONG":
                subsets.append(f"subset={label}({sub_min_x:.8f},{sub_max_x:.8f})")
    elif "E" in labels_upper and "N" in labels_upper:
        # Sumbu Easting (X) dan Northing (Y)
        for label in axis_labels:
            if label.upper() == "E":
                subsets.append(f"subset={label}({sub_min_x:.4f},{sub_max_x:.4f})")
            elif label.upper() == "N":
                subsets.append(f"subset={label}({sub_min_y:.4f},{sub_max_y:.4f})")
    else:
        # Default fallback X dan Y
        subsets.append(f"subset={axis_labels[0]}({sub_min_x:.4f},{sub_max_x:.4f})")
        if len(axis_labels) > 1:
            subsets.append(f"subset={axis_labels[1]}({sub_min_y:.4f},{sub_max_y:.4f})")

    return subsets

def build_area_wcs_subsets(
    bbox_wgs84: Tuple[float, float, float, float],
    origin_x: float,
    origin_y: float,
    res_x: float,
    res_y: float,
    native_crs_str: str,
    axis_labels: List[str],
    max_pixels: int = 4_000_000
) -> Tuple[List[str], Optional[float], bool, Tuple[float, float, float, float]]:
    """
    Membentuk parameter subset WCS 2.0.1 untuk area persegi panjang (BBox).
    Mengembalikan (subsets, scalefactor, approximate, native_bbox).
    Jika estimasi pixel melebihi max_pixels, scalefactor dihitung otomatis dan approximate = True.
    """
    min_lng, min_lat, max_lng, max_lat = bbox_wgs84
    clean_crs = (native_crs_str or "").strip().upper()

    if clean_crs in ("EPSG:4326", "WGS 84", "OGC:CRS84", "4326") or not clean_crs:
        min_x, min_y = min_lng, min_lat
        max_x, max_y = max_lng, max_lat
    else:
        transformer = Transformer.from_crs("EPSG:4326", clean_crs, always_xy=True)
        # Transform 4 sudut untuk mencakup rotasi/distorsi proyeksi
        xs, ys = transformer.transform(
            [min_lng, min_lng, max_lng, max_lng],
            [min_lat, max_lat, min_lat, max_lat]
        )
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

    rx = abs(float(res_x))
    ry = abs(float(res_y))
    est_w = max(1, int(math.ceil((max_x - min_x) / rx)))
    est_h = max(1, int(math.ceil((max_y - min_y) / ry)))
    total_px = est_w * est_h

    scalefactor: Optional[float] = None
    approximate = False

    if total_px > max_pixels:
        scalefactor = math.sqrt(max_pixels / total_px)
        # Batasi scalefactor minimum agar tidak terlalu mikro
        scalefactor = max(0.01, round(scalefactor, 4))
        approximate = True

    subsets = []
    labels_upper = [a.upper() for a in axis_labels]

    if "LAT" in labels_upper and "LONG" in labels_upper:
        for label in axis_labels:
            if label.upper() == "LAT":
                subsets.append(f"subset={label}({min_y:.8f},{max_y:.8f})")
            elif label.upper() == "LONG":
                subsets.append(f"subset={label}({min_x:.8f},{max_x:.8f})")
    elif "E" in labels_upper and "N" in labels_upper:
        for label in axis_labels:
            if label.upper() == "E":
                subsets.append(f"subset={label}({min_x:.4f},{max_x:.4f})")
            elif label.upper() == "N":
                subsets.append(f"subset={label}({min_y:.4f},{max_y:.4f})")
    else:
        subsets.append(f"subset={axis_labels[0]}({min_x:.4f},{max_x:.4f})")
        if len(axis_labels) > 1:
            subsets.append(f"subset={axis_labels[1]}({min_y:.4f},{max_y:.4f})")

    native_bbox = (float(min_x), float(min_y), float(max_x), float(max_y))
    return subsets, scalefactor, approximate, native_bbox

def sanitize_float_value(val: Any) -> Optional[float]:
    """
    Memastikan float yang diserialisasi ke JSON benar-benar finite.
    Jika None, NaN, Inf, atau -Inf, kembalikan None (null di JSON).
    """
    if val is None:
        return None
    try:
        f_val = float(val)
        if math.isfinite(f_val):
            return f_val
        return None
    except (TypeError, ValueError):
        return None

def parse_1x1_coverage_tiff(
    tiff_bytes: bytes,
    nodata_val: Optional[float] = None
) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Mengekstrak nilai pixel per band dari respon GeoTIFF WCS 1x1.
    Memastikan nilai non-finite atau nodata dikirim sebagai null dengan flag nodata=True.
    Mengembalikan (is_nodata_pixel, bands_result).
    """
    bands_res = []
    overall_nodata = True

    with MemoryFile(tiff_bytes) as mem:
        with mem.open() as src:
            for b_idx in range(1, src.count + 1):
                arr = src.read(b_idx)
                b_name = f"Band {b_idx}"
                # Ambil pixel tengah jika GeoServer mengembalikan > 1x1
                mid_r = arr.shape[0] // 2
                mid_c = arr.shape[1] // 2
                raw_val = arr[mid_r, mid_c]

                band_nodata = src.nodata if src.nodata is not None else nodata_val

                is_nd = False
                if not math.isfinite(raw_val):
                    is_nd = True
                elif band_nodata is not None and math.isclose(float(raw_val), float(band_nodata), rel_tol=1e-5):
                    is_nd = True

                clean_val = None if is_nd else float(raw_val)
                if not is_nd:
                    overall_nodata = False

                bands_res.append({
                    "index": b_idx,
                    "name": b_name,
                    "value": clean_val,
                    "nodata": is_nd
                })

    return overall_nodata, bands_res

def calculate_raster_stats(
    arr: np.ndarray,
    nodata_val: Optional[float] = None,
    bins: int = 20
) -> Dict[str, Any]:
    """
    Menghitung statistik array 2D/1D dengan masking ketat terhadap nodata dan nilai non-finite.
    Revisi 1: Semua nilai output finite; jika tidak ada data valid, kembalikan None dengan valid_count=0.
    """
    flat = np.asarray(arr, dtype=np.float64).flatten()
    total_count = int(flat.size)

    # Masking nilai non-finite (NaN, Inf, -Inf)
    finite_mask = np.isfinite(flat)

    # Masking nodata
    if nodata_val is not None and math.isfinite(nodata_val):
        valid_mask = finite_mask & (~np.isclose(flat, nodata_val, rtol=1e-5))
    else:
        valid_mask = finite_mask

    valid_data = flat[valid_mask]
    valid_count = int(len(valid_data))
    nodata_count = total_count - valid_count

    if valid_count == 0:
        return {
            "count": total_count,
            "valid_count": 0,
            "nodata_count": nodata_count,
            "all_nodata": True,
            "nodata": sanitize_float_value(nodata_val),
            "min": None,
            "max": None,
            "mean": None,
            "std": None,
            "percentiles": None,
            "histogram": None,
        }

    min_val = float(np.min(valid_data))
    max_val = float(np.max(valid_data))
    mean_val = float(np.mean(valid_data))
    std_val = float(np.std(valid_data))

    p5, p25, p50, p75, p95 = [
        float(x) for x in np.percentile(valid_data, [5, 25, 50, 75, 95])
    ]

    # Histogram
    num_bins = min(max(5, int(bins)), 100)
    if min_val == max_val:
        counts = [valid_count]
        bin_edges = [min_val, max_val]
    else:
        hist_counts, hist_edges = np.histogram(valid_data, bins=num_bins)
        counts = [int(c) for c in hist_counts]
        bin_edges = [float(e) for e in hist_edges]

    return {
        "count": total_count,
        "valid_count": valid_count,
        "nodata_count": nodata_count,
        "all_nodata": False,
        "nodata": sanitize_float_value(nodata_val),
        "min": sanitize_float_value(min_val),
        "max": sanitize_float_value(max_val),
        "mean": sanitize_float_value(mean_val),
        "std": sanitize_float_value(std_val),
        "percentiles": {
            "p5": sanitize_float_value(p5),
            "p25": sanitize_float_value(p25),
            "p50": sanitize_float_value(p50),
            "p75": sanitize_float_value(p75),
            "p95": sanitize_float_value(p95),
        },
        "histogram": {
            "bins": len(counts),
            "counts": counts,
            "bin_edges": bin_edges,
        }
    }

def parse_coverage_stats(
    tiff_bytes: bytes,
    nodata_val: Optional[float] = None,
    band_idx: int = 1,
    bins: int = 20
) -> Tuple[Dict[str, Any], Tuple[float, float]]:
    """
    Membaca GeoTIFF WCS hasil download, mengekstrak resolusi asli GeoTIFF,
    dan menghitung statistik nilai pada band_idx yang dipilih.
    Mengembalikan (stats_dict, (res_x, res_y)).
    """
    with MemoryFile(tiff_bytes) as mem:
        with mem.open() as src:
            actual_res = (float(src.res[0]), float(src.res[1]))
            actual_band = min(max(1, band_idx), src.count)
            arr = src.read(actual_band)
            band_nodata = src.nodata if src.nodata is not None else nodata_val

            stats = calculate_raster_stats(arr, nodata_val=band_nodata, bins=bins)
            return stats, actual_res


def mask_geotiff_by_polygon(
    tiff_bytes: bytes,
    polygon_coords: List[List[float]],
    nodata_value: Optional[float] = None
) -> bytes:
    """
    Memotong (mask) GeoTIFF menggunakan poligon GeoJSON [[lng, lat], ...].
    Piksel di luar poligon disetel ke nodata_value (untuk raw) atau alpha=0 (untuk RGBA).
    """
    from shapely.geometry import Polygon
    from rasterio.features import rasterize

    if not polygon_coords or len(polygon_coords) < 3:
        return tiff_bytes

    poly = Polygon(polygon_coords)

    with MemoryFile(tiff_bytes) as in_mem:
        with in_mem.open() as src:
            if src.crs and not src.crs.is_geographic:
                from pyproj import Transformer
                from shapely.ops import transform as shp_transform
                t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
                poly = shp_transform(t.transform, poly)

            poly_mask = rasterize([poly], out_shape=(src.height, src.width), transform=src.transform, fill=0, default_value=1)
            arr = src.read()
            out_meta = src.meta.copy()

            if src.count == 4:
                arr[3][poly_mask == 0] = 0
            else:
                nd = src.nodata if src.nodata is not None else (nodata_value if nodata_value is not None else 0)
                for b in range(src.count):
                    arr[b][poly_mask == 0] = nd
                out_meta["nodata"] = nd

            out_mem = MemoryFile()
            with out_mem.open(**out_meta) as dst:
                dst.write(arr)
            return bytes(out_mem.getbuffer())


def convert_geotiff_to_styled_rgb(
    tiff_bytes: bytes,
    mask_polygon: Optional[List[List[float]]] = None,
    symbology: Optional[Dict[str, Any]] = None
) -> bytes:
    """
    Mengonversi GeoTIFF menjadi 3-band True Color RGB GeoTIFF (photometric='RGB').
    Format 3-band RGB ini kompatibel 100% dengan Windows Photo Viewer, Microsoft Photos,
    Paint, QGIS, ArcGIS, dan viewer lainnya tanpa masalah stripping alpha.
    """
    from shapely.geometry import Polygon
    from rasterio.features import rasterize

    with MemoryFile(tiff_bytes) as in_mem:
        with in_mem.open() as src:
            data = src.read(1)
            try:
                cm = src.colormap(1)
            except Exception:
                cm = None

            height, width = src.height, src.width
            # Default background putih
            rgb = np.full((3, height, width), 255, dtype=np.uint8)

            if cm:
                for val, (r, g, b, a) in cm.items():
                    if a > 0:
                        m = (data == val)
                        rgb[0][m] = r
                        rgb[1][m] = g
                        rgb[2][m] = b
            elif symbology and isinstance(symbology, dict) and symbology.get("classes"):
                # Gunakan kelas klasifikasi yang tersimpan di database
                classes = symbology.get("classes") or []
                for cls in classes:
                    c_hex = cls.get("color")
                    if not c_hex or not c_hex.startswith("#") or len(c_hex) < 7:
                        continue
                    try:
                        cr = int(c_hex[1:3], 16)
                        cg = int(c_hex[3:5], 16)
                        cb = int(c_hex[5:7], 16)
                    except ValueError:
                        continue

                    c_min = cls.get("min")
                    c_max = cls.get("max")
                    if c_min is not None and c_max is not None:
                        m = (data >= float(c_min)) & (data <= float(c_max))
                        rgb[0][m] = cr
                        rgb[1][m] = cg
                        rgb[2][m] = cb
                    elif "value" in cls:
                        m = (data == float(cls["value"]))
                        rgb[0][m] = cr
                        rgb[1][m] = cg
                        rgb[2][m] = cb
            else:
                # Fallback color ramp jika tidak ada colormap atau symbology
                valid = np.isfinite(data)
                if src.nodata is not None:
                    valid = valid & (data != src.nodata)
                if np.any(valid):
                    d_min, d_max = float(data[valid].min()), float(data[valid].max())
                    denom = max(1e-5, d_max - d_min)
                    norm = np.clip((data - d_min) / denom, 0.0, 1.0)
                    rgb[0][valid] = (norm[valid] * 255).astype(np.uint8)
                    rgb[1][valid] = ((1.0 - np.abs(norm[valid] - 0.5) * 2) * 255).astype(np.uint8)
                    rgb[2][valid] = ((1.0 - norm[valid]) * 255).astype(np.uint8)

            if mask_polygon and len(mask_polygon) >= 3:
                poly = Polygon(mask_polygon)
                if src.crs and not src.crs.is_geographic:
                    from pyproj import Transformer
                    from shapely.ops import transform as shp_transform
                    t = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
                    poly = shp_transform(t.transform, poly)
                poly_mask = rasterize([poly], out_shape=(height, width), transform=src.transform, fill=0, default_value=1)
                rgb[0][poly_mask == 0] = 255
                rgb[1][poly_mask == 0] = 255
                rgb[2][poly_mask == 0] = 255

            out_meta = src.meta.copy()
            out_meta.update({
                "count": 3,
                "dtype": "uint8",
                "photometric": "RGB",
                "nodata": None
            })

            out_mem = MemoryFile()
            with out_mem.open(**out_meta) as dst:
                dst.write(rgb)
            return bytes(out_mem.getbuffer())


# Alias untuk kompatibilitas
convert_indexed_geotiff_to_rgba = convert_geotiff_to_styled_rgb


