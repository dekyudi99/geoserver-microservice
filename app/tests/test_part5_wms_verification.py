import os
import io
import math
import uuid
import pytest
import requests
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from PIL import Image

from app.config.settings import settings
from app.clients.geoserver_client import geoserver_client
from app.services.style_service import style_service
from app.services.api_key_service import generate_api_key
from app.models.api_key import ApiKeyType
from app.config.database import SessionLocal
from app.models.spatial_data import RasterMetadata, WorkspaceMetadata

WORKSPACE = "test_part5_ws"
STORE_NAME_FLOAT = "test_part5_store"
LAYER_NAME_FLOAT = "test_part5_layer"
STORE_NAME_BYTE = "test_part5_byte_store"
LAYER_NAME_BYTE = "test_part5_byte_layer"
STORE_NAME_INT16 = "test_part5_int16_store"
LAYER_NAME_INT16 = "test_part5_int16_layer"
STYLE_NAME = "test_part5_style"

GEOTIFF_PATH_FLOAT = "/tmp/test_part5_float.tif"
GEOTIFF_PATH_BYTE = "/tmp/test_part5_byte.tif"
GEOTIFF_PATH_INT16 = "/tmp/test_part5_int16.tif"

BBOX = [106.8, -6.2, 106.9, -6.1] # minx, miny, maxx, maxy (EPSG:4326)

def create_synthetic_float_geotiff(path: str):
    """
    GeoTIFF 10x10 float32:
    Row 0: -9999.0 (NoData)
    Row 1: 10.0 (Min)
    Row 2: 20.0 (Kelas 0 interior)
    Row 3: 30.0 (Batas 1 interior: harus masuk Kelas 0 karena <= 30.0)
    Row 4: 45.0 (Kelas 1 interior)
    Row 5: 60.0 (Batas 2 interior: harus masuk Kelas 1 karena <= 60.0)
    Row 6: 80.0 (Kelas 2 interior)
    Row 7: 100.0 (Batas 3 / Max: harus masuk Kelas 2 karena <= 100.0)
    Row 8: [1.0, 2.0, 3.0, 4.0, 1.0, 2.0, 3.0, 4.0, -9999.0, -9999.0] (Diskrit)
    Row 9: -9999.0 (NoData)
    """
    data = np.zeros((10, 10), dtype=np.float32)
    data[0, :] = -9999.0
    data[1, :] = 10.0
    data[2, :] = 20.0
    data[3, :] = 30.0
    data[4, :] = 45.0
    data[5, :] = 60.0
    data[6, :] = 80.0
    data[7, :] = 100.0
    data[8, :] = [1.0, 2.0, 3.0, 4.0, 1.0, 2.0, 3.0, 4.0, -9999.0, -9999.0]
    data[9, :] = -9999.0

    transform = from_bounds(BBOX[0], BBOX[1], BBOX[2], BBOX[3], 10, 10)

    with rasterio.open(
        path,
        'w',
        driver='GTiff',
        height=10,
        width=10,
        count=1,
        dtype=np.float32,
        crs='EPSG:4326',
        transform=transform,
        nodata=-9999.0
    ) as dst:
        dst.write(data, 1)

def create_synthetic_integer_geotiff(path: str, dtype_str: str):
    """
    GeoTIFF 5x1 dengan nilai bulat 1, 2, 3, 4, 5
    Batas kelas: 2, 3, 4, 5
    """
    dtype = np.uint8 if dtype_str == "uint8" else np.int16
    data = np.array([[1, 2, 3, 4, 5]], dtype=dtype)
    transform = from_bounds(BBOX[0], BBOX[1], BBOX[2], BBOX[3], 5, 1)

    with rasterio.open(
        path,
        'w',
        driver='GTiff',
        height=1,
        width=5,
        count=1,
        dtype=dtype,
        crs='EPSG:4326',
        transform=transform
    ) as dst:
        dst.write(data, 1)

def setup_geoserver_layers():
    create_synthetic_float_geotiff(GEOTIFF_PATH_FLOAT)
    create_synthetic_integer_geotiff(GEOTIFF_PATH_BYTE, "uint8")
    create_synthetic_integer_geotiff(GEOTIFF_PATH_INT16, "int16")

    auth = (settings.GEOSERVER_USER, settings.GEOSERVER_PASS)
    base = settings.GEOSERVER_URL.rstrip('/')

    # Workspace
    requests.post(f"{base}/rest/workspaces", headers={"Content-Type": "application/json"}, json={"workspace": {"name": WORKSPACE}}, auth=auth)
    
    # Stores
    with open(GEOTIFF_PATH_FLOAT, "rb") as f:
        requests.put(f"{base}/rest/workspaces/{WORKSPACE}/coveragestores/{STORE_NAME_FLOAT}/file.geotiff?coverageName={LAYER_NAME_FLOAT}", headers={"Content-Type": "image/tiff"}, data=f.read(), auth=auth)
    
    with open(GEOTIFF_PATH_BYTE, "rb") as f:
        requests.put(f"{base}/rest/workspaces/{WORKSPACE}/coveragestores/{STORE_NAME_BYTE}/file.geotiff?coverageName={LAYER_NAME_BYTE}", headers={"Content-Type": "image/tiff"}, data=f.read(), auth=auth)

    with open(GEOTIFF_PATH_INT16, "rb") as f:
        requests.put(f"{base}/rest/workspaces/{WORKSPACE}/coveragestores/{STORE_NAME_INT16}/file.geotiff?coverageName={LAYER_NAME_INT16}", headers={"Content-Type": "image/tiff"}, data=f.read(), auth=auth)

def teardown_geoserver_layers():
    auth = (settings.GEOSERVER_USER, settings.GEOSERVER_PASS)
    base = settings.GEOSERVER_URL.rstrip("/")
    requests.delete(f"{base}/rest/workspaces/{WORKSPACE}/coveragestores/{STORE_NAME_FLOAT}?recurse=true", auth=auth)
    requests.delete(f"{base}/rest/workspaces/{WORKSPACE}/coveragestores/{STORE_NAME_BYTE}?recurse=true", auth=auth)
    requests.delete(f"{base}/rest/workspaces/{WORKSPACE}/coveragestores/{STORE_NAME_INT16}?recurse=true", auth=auth)
    requests.delete(f"{base}/rest/workspaces/{WORKSPACE}/styles/{STYLE_NAME}?recurse=true", auth=auth)
    requests.delete(f"{base}/rest/workspaces/{WORKSPACE}/styles/style_int?recurse=true", auth=auth)
    requests.delete(f"{base}/rest/workspaces/{WORKSPACE}?recurse=true", auth=auth)

    for p in [GEOTIFF_PATH_FLOAT, GEOTIFF_PATH_BYTE, GEOTIFF_PATH_INT16]:
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

def fetch_wms_map(workspace, layer, style=None, width=10, height=10):
    auth = (settings.GEOSERVER_USER, settings.GEOSERVER_PASS)
    wms_url = f"{settings.GEOSERVER_URL.rstrip('/')}/{workspace}/wms"
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetMap",
        "LAYERS": f"{workspace}:{layer}",
        "STYLES": style or "",
        "BBOX": f"{BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]}",
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FORMAT": "image/png",
        "TRANSPARENT": "TRUE",
        "SRS": "EPSG:4326"
    }
    resp = requests.get(wms_url, params=params, auth=auth, timeout=10)
    assert resp.status_code == 200, f"GetMap error {resp.status_code}: {resp.text}"
    img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    return img

def test_wms_rendering_intervals_values_nodata_gwc():
    try:
        setup_geoserver_layers()

        # =========================================================================
        # KASUS 1: Intervals (Float32) dengan SEMUA entri disesuaikan math.nextafter
        # Breaks UI: [10.0, 30.0, 60.0, 100.0]
        # Kelas 0: <= 30.0 -> Red (#FF0000)
        # Kelas 1: (30.0, 60.0] -> Green (#00FF00)
        # Kelas 2: (60.0, 100.0] -> Blue (#0000FF)
        # =========================================================================
        entries_intervals = [
            {"quantity": 30.0, "color": "#FF0000", "opacity": 1.0, "label": "10 - 30"},
            {"quantity": 60.0, "color": "#00FF00", "opacity": 1.0, "label": "30 - 60"},
            {"quantity": 100.0, "color": "#0000FF", "opacity": 1.0, "label": "60 - 100"}
        ]
        sld_intervals = style_service.generate_raster_sld(
            style_name=STYLE_NAME,
            color_entries=entries_intervals,
            style_type="intervals"
        )
        assert "<Abstract>astra:upper_inclusive=1</Abstract>" in sld_intervals

        # Roundtrip parse mengembalikan breaks asli yang sama persis
        parsed_intervals = style_service.parse_raster_sld(sld_intervals)
        assert parsed_intervals["upper_inclusive"] is True
        assert parsed_intervals["entries"][0]["quantity"] == 30.0
        assert parsed_intervals["entries"][1]["quantity"] == 60.0
        assert parsed_intervals["entries"][2]["quantity"] == 100.0

        res1 = style_service.apply_style(
            workspace=WORKSPACE,
            layer_name=LAYER_NAME_FLOAT,
            style_name=STYLE_NAME,
            sld_xml=sld_intervals
        )
        assert res1.success, f"Apply style intervals failed: {res1}"

        img_intervals = fetch_wms_map(WORKSPACE, LAYER_NAME_FLOAT, STYLE_NAME, width=10, height=10)

        pix_min = img_intervals.getpixel((5, 1))     # Row 1: val 10.0 (Min) -> Kelas 0 (Red)
        pix_c0 = img_intervals.getpixel((5, 2))      # Row 2: val 20.0 (Interior 0) -> Kelas 0 (Red)
        pix_b1 = img_intervals.getpixel((5, 3))      # Row 3: val 30.0 (Batas 1) -> Kelas 0 (Red) KARENA <= 30.0
        pix_c1 = img_intervals.getpixel((5, 4))      # Row 4: val 45.0 (Interior 1) -> Kelas 1 (Green)
        pix_b2 = img_intervals.getpixel((5, 5))      # Row 5: val 60.0 (Batas 2) -> Kelas 1 (Green) KARENA <= 60.0
        pix_c2 = img_intervals.getpixel((5, 6))      # Row 6: val 80.0 (Interior 2) -> Kelas 2 (Blue)
        pix_max = img_intervals.getpixel((5, 7))     # Row 7: val 100.0 (Batas 3 / Max) -> Kelas 2 (Blue) KARENA <= 100.0
        pix_nodata = img_intervals.getpixel((5, 0))  # Row 0: val -9999.0 (NoData) -> Transparan

        print("\n--- HASIL DIAGNOSTIK KASUS 1 (FLOAT32 INTERVALS DENGAN NEXTAFTER SEMUA ENTRI) ---")
        print(f"Row 1 (Min 10.0): RGBA={pix_min}")
        print(f"Row 2 (Interior 20.0): RGBA={pix_c0}")
        print(f"Row 3 (Batas 1 30.0 - harapan Merah): RGBA={pix_b1}")
        print(f"Row 4 (Interior 45.0): RGBA={pix_c1}")
        print(f"Row 5 (Batas 2 60.0 - harapan Hijau): RGBA={pix_b2}")
        print(f"Row 6 (Interior 80.0): RGBA={pix_c2}")
        print(f"Row 7 (Max 100.0 - harapan Biru): RGBA={pix_max}")
        print(f"Row 0 (NoData -9999.0): RGBA={pix_nodata}")

        # Verifikasi batas atas inklusif (<=) pada setiap batas:
        assert pix_min[:3] == (255, 0, 0), f"Min 10.0 bukan Merah: {pix_min}"
        assert pix_c0[:3] == (255, 0, 0), f"Interior 20.0 bukan Merah: {pix_c0}"
        assert pix_b1[:3] == (255, 0, 0), f"Batas 30.0 HARUS Merah (masuk kelas <= 30.0): {pix_b1}"
        assert pix_c1[:3] == (0, 255, 0), f"Interior 45.0 bukan Hijau: {pix_c1}"
        assert pix_b2[:3] == (0, 255, 0), f"Batas 60.0 HARUS Hijau (masuk kelas <= 60.0): {pix_b2}"
        assert pix_c2[:3] == (0, 0, 255), f"Interior 80.0 bukan Biru: {pix_c2}"
        assert pix_max[:3] == (0, 0, 255), f"Max 100.0 HARUS Biru (masuk kelas <= 100.0): {pix_max}"
        assert pix_max[3] == 255, f"Max 100.0 harus opak (alpha 255): {pix_max}"
        assert pix_nodata[3] == 0, f"NoData piksel harus transparan (alpha 0): {pix_nodata}"

        # =========================================================================
        # KASUS 2: Byte (uint8) dan Int16 dengan Nilai Bulat 1-5 dan Breaks 2, 3, 4, 5
        # Data: [1, 2, 3, 4, 5]
        # Kelas 0: <= 2 -> Merah (#FF0000)
        # Kelas 1: (2, 3] -> Hijau (#00FF00)
        # Kelas 2: (3, 4] -> Biru (#0000FF)
        # Kelas 3: (4, 5] -> Kuning (#FFFF00)
        # =========================================================================
        entries_int = [
            {"quantity": 2.0, "color": "#FF0000", "opacity": 1.0, "label": "1 - 2"},
            {"quantity": 3.0, "color": "#00FF00", "opacity": 1.0, "label": "2 - 3"},
            {"quantity": 4.0, "color": "#0000FF", "opacity": 1.0, "label": "3 - 4"},
            {"quantity": 5.0, "color": "#FFFF00", "opacity": 1.0, "label": "4 - 5"}
        ]
        sld_int = style_service.generate_raster_sld(
            style_name="style_int",
            color_entries=entries_int,
            style_type="intervals"
        )
        parsed_int = style_service.parse_raster_sld(sld_int)
        assert parsed_int["entries"][0]["quantity"] == 2.0
        assert parsed_int["entries"][1]["quantity"] == 3.0
        assert parsed_int["entries"][2]["quantity"] == 4.0
        assert parsed_int["entries"][3]["quantity"] == 5.0

        for layer_int, dtype_label in [(LAYER_NAME_BYTE, "Byte (uint8)"), (LAYER_NAME_INT16, "Int16")]:
            res_int = style_service.apply_style(
                workspace=WORKSPACE,
                layer_name=layer_int,
                style_name="style_int",
                sld_xml=sld_int
            )
            assert res_int.success, f"Apply style {dtype_label} failed: {res_int}"

            img_int = fetch_wms_map(WORKSPACE, layer_int, "style_int", width=5, height=1)
            p1 = img_int.getpixel((0, 0)) # Val 1 -> Kelas 0 (Merah)
            p2 = img_int.getpixel((1, 0)) # Val 2 -> Kelas 0 (Merah) KARENA <= 2
            p3 = img_int.getpixel((2, 0)) # Val 3 -> Kelas 1 (Hijau) KARENA <= 3
            p4 = img_int.getpixel((3, 0)) # Val 4 -> Kelas 2 (Biru) KARENA <= 4
            p5 = img_int.getpixel((4, 0)) # Val 5 -> Kelas 3 (Kuning) KARENA <= 5

            print(f"\n--- HASIL DIAGNOSTIK {dtype_label} (INTERVALS 1-5) ---")
            print(f"Col 0 (Val 1): RGBA={p1}")
            print(f"Col 1 (Val 2 - Batas Kelas 0): RGBA={p2}")
            print(f"Col 2 (Val 3 - Batas Kelas 1): RGBA={p3}")
            print(f"Col 3 (Val 4 - Batas Kelas 2): RGBA={p4}")
            print(f"Col 4 (Val 5 - Batas Kelas 3): RGBA={p5}")

            assert p1[:3] == (255, 0, 0), f"{dtype_label} Val 1 bukan Merah: {p1}"
            assert p2[:3] == (255, 0, 0), f"{dtype_label} Val 2 HARUS Merah (masuk kelas <= 2): {p2}"
            assert p3[:3] == (0, 255, 0), f"{dtype_label} Val 3 HARUS Hijau (masuk kelas <= 3): {p3}"
            assert p4[:3] == (0, 0, 255), f"{dtype_label} Val 4 HARUS Biru (masuk kelas <= 4): {p4}"
            assert p5[:3] == (255, 255, 0), f"{dtype_label} Val 5 HARUS Kuning (masuk kelas <= 5): {p5}"

        # =========================================================================
        # KASUS 3: Preset dengan NoData Entry eksplisit (opacity=0)
        # Entry 0: NoData (-9999.0, opacity=0) -> dinaikkan nextafter agar piksel == -9999.0 tepat transparan
        # Entry 1: 50.0 (Merah, opacity=1.0)
        # Entry 2: 100.0 (Biru, opacity=1.0)
        # =========================================================================
        entries_nodata = [
            {"quantity": -9999.0, "color": "#000000", "opacity": 0.0, "label": "No Data"},
            {"quantity": 50.0, "color": "#FF0000", "opacity": 1.0, "label": "Low"},
            {"quantity": 100.0, "color": "#0000FF", "opacity": 1.0, "label": "High"}
        ]
        sld_nodata = style_service.generate_raster_sld(
            style_name=STYLE_NAME,
            color_entries=entries_nodata,
            style_type="intervals"
        )
        parsed_nd = style_service.parse_raster_sld(sld_nodata)
        assert parsed_nd["entries"][0]["quantity"] == -9999.0
        assert parsed_nd["entries"][1]["quantity"] == 50.0
        assert parsed_nd["entries"][2]["quantity"] == 100.0

        res3 = style_service.apply_style(
            workspace=WORKSPACE,
            layer_name=LAYER_NAME_FLOAT,
            style_name=STYLE_NAME,
            sld_xml=sld_nodata
        )
        assert res3.success, f"Apply style nodata failed: {res3}"

        img_nodata = fetch_wms_map(WORKSPACE, LAYER_NAME_FLOAT, STYLE_NAME, width=10, height=10)
        p_nd_row0 = img_nodata.getpixel((5, 0)) # tepat -9999.0 -> harus transparan (alpha 0)
        p_c_low = img_nodata.getpixel((5, 3))   # 30.0 -> Low (Merah)
        p_c_high = img_nodata.getpixel((5, 7))  # 100.0 -> High (Biru)

        print("\n--- HASIL DIAGNOSTIK KASUS 3 (PRESET + NODATA ENTRY) ---")
        print(f"Row 0 (Tepat -9999.0 NoData): RGBA={p_nd_row0}")
        print(f"Row 3 (30.0 Low): RGBA={p_c_low}")
        print(f"Row 7 (100.0 High): RGBA={p_c_high}")

        assert p_nd_row0[3] == 0, f"NoData piksel (-9999.0) harus transparan (alpha 0): {p_nd_row0}"
        assert p_c_low[:3] == (255, 0, 0), f"3.0 Low bukan Merah: {p_c_low}"
        assert p_c_high[:3] == (0, 0, 255), f"100.0 High bukan Biru: {p_c_high}"

        # =========================================================================
        # KASUS 4: Values (Data Diskrit Bulat)
        # =========================================================================
        entries_values = [
            {"quantity": 1.0, "color": "#FF0000", "opacity": 1.0, "label": "Class 1"},
            {"quantity": 2.0, "color": "#00FF00", "opacity": 1.0, "label": "Class 2"},
            {"quantity": 3.0, "color": "#0000FF", "opacity": 1.0, "label": "Class 3"},
            {"quantity": 4.0, "color": "#FFFF00", "opacity": 1.0, "label": "Class 4"}
        ]
        sld_values = style_service.generate_raster_sld(
            style_name=STYLE_NAME,
            color_entries=entries_values,
            style_type="values"
        )
        res_val = style_service.apply_style(
            workspace=WORKSPACE,
            layer_name=LAYER_NAME_FLOAT,
            style_name=STYLE_NAME,
            sld_xml=sld_values
        )
        assert res_val.success

        img_values = fetch_wms_map(WORKSPACE, LAYER_NAME_FLOAT, STYLE_NAME, width=10, height=10)
        p_v1 = img_values.getpixel((0, 8))
        p_v2 = img_values.getpixel((1, 8))
        p_v3 = img_values.getpixel((2, 8))
        p_v4 = img_values.getpixel((3, 8))
        p_unmatched = img_values.getpixel((8, 8))

        assert p_v1[:3] == (255, 0, 0)
        assert p_v2[:3] == (0, 255, 0)
        assert p_v3[:3] == (0, 0, 255)
        assert p_v4[:3] == (255, 255, 0)
        assert p_unmatched[3] == 0

        # =========================================================================
        # KASUS 5: GWC Masstruncate
        # =========================================================================
        gwc_res = geoserver_client.truncate_gwc_layer(WORKSPACE, LAYER_NAME_FLOAT)
        assert gwc_res is True

        print("\nSEMUA UJI INTEGRASI WMS DAN SEMANTIK BATAS SUKSES!")

    finally:
        teardown_geoserver_layers()

if __name__ == "__main__":
    test_wms_rendering_intervals_values_nodata_gwc()
