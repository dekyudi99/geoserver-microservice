"""
E2E Verification Script for WMTS Phase 3 (Steps 1 to 6)
Rule 60 compliance verification.
"""
import sys
import os
import math
import uuid
import json
import requests
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.crs import CRS
from PIL import Image
import io

MICROSERVICE_URL = "http://localhost:8001/api/v1"
GEOSERVER_INTERNAL = "http://geoserver:8080/geoserver"
GEOSERVER_REST_URL = f"{GEOSERVER_INTERNAL}/rest"
GEOSERVER_GWC_URL = f"{GEOSERVER_INTERNAL}/gwc"
GEOSERVER_WMS_URL = GEOSERVER_INTERNAL
GEOSERVER_AUTH = ("admin", "rahasia")
API_KEY = "gsvc_pk_test_verification_key_12345"
HEADERS = {"X-API-Key": API_KEY}

ORIGIN_SHIFT = 20037508.342789244

def tile_bounds_3857(zoom, x, y):
    """Hitung bounding box EPSG:3857 untuk tile x, y pada level zoom tertentu."""
    n = 2.0 ** zoom
    res = (ORIGIN_SHIFT * 2.0) / n
    minx = -ORIGIN_SHIFT + x * res
    maxx = -ORIGIN_SHIFT + (x + 1) * res
    maxy = ORIGIN_SHIFT - y * res
    miny = ORIGIN_SHIFT - (y + 1) * res
    return minx, miny, maxx, maxy

def lat_lon_to_tile(lat, lon, zoom):
    """Konversi koordinat WGS84 ke tile x, y"""
    n = 2.0 ** zoom
    rad = math.radians(lat)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n)
    return x, y

def get_my_layer_id(workspace_name, geoserver_name):
    """Ambil id layer dari GET /api/v1/layers/my-layers"""
    res = requests.get(f"{MICROSERVICE_URL}/layers/my-layers", headers=HEADERS)
    if res.status_code != 200:
        return None
    data = res.json().get("data", [])
    for lyr in data:
        if lyr.get("workspace_name") == workspace_name:
            lyr_gs = lyr.get("geoserver_name") or lyr.get("table_name") or lyr.get("store_name") or lyr.get("layer_name")
            if lyr_gs == geoserver_name:
                return lyr.get("id")
    return None

def main():
    results = {}
    print("=" * 70)
    print("STARTING WMTS PHASE 3 VERIFICATION")
    print("=" * 70)

    # 0. Setup Workspace
    ws_req_name = f"ws_fase3_{uuid.uuid4().hex[:6]}"
    print(f"\n[STEP 0] Creating test workspace: {ws_req_name}")
    resp_ws = requests.post(
        f"{MICROSERVICE_URL}/workspaces",
        json={"name": ws_req_name, "display_name": f"Workspace {ws_req_name}", "visibility": "public"},
        headers=HEADERS
    )
    if resp_ws.status_code not in (200, 201):
        print(f"FAILED to create workspace: {resp_ws.status_code} {resp_ws.text}")
        return
    ws_data = resp_ws.json()
    actual_ws = ws_data.get("workspace_name", ws_req_name)
    print(f"Workspace created: {actual_ws}")

    raster_file = "test_fase3_ras.tif"
    vec_file = "test_fase3_vec.geojson"

    try:
        # ======================================================================
        # STEP 1: Publish Raster (EPSG:32647) & Vector BARU
        # ======================================================================
        print("\n" + "=" * 70)
        print("[STEP 1] Publishing Raster (EPSG:32647) and Vector (GeoJSON)")
        print("=" * 70)

        # 1a. Buat synthetic GeoTIFF EPSG:32647
        data_ras = np.arange(1, 101, dtype=np.float32).reshape(10, 10)
        transform = from_bounds(600000, 1520000, 605000, 1525000, 10, 10)
        with rasterio.open(
            raster_file,
            "w",
            driver="GTiff",
            height=10,
            width=10,
            count=1,
            dtype=rasterio.float32,
            crs=CRS.from_epsg(32647),
            transform=transform,
            nodata=-9999.0
        ) as dst:
            dst.write(data_ras, 1)

        ras_title = f"ras_title_{uuid.uuid4().hex[:6]}"
        with open(raster_file, "rb") as f:
            resp_ras = requests.post(
                f"{MICROSERVICE_URL}/layers/publish-raster",
                data={"workspace_name": actual_ws, "layer_name": ras_title},
                files={"file": ("test_ras.tif", f, "image/tiff")},
                headers=HEADERS
            )
        print(f"Publish Raster Status: {resp_ras.status_code}")
        ras_json = resp_ras.json()
        ras_gs_name = ras_json.get("store_name")
        print(f"Raster published -> store_name/geoserver_name: {ras_gs_name}")

        # 1b. Buat synthetic GeoJSON Vector
        vector_geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"id": 1, "name": "Zone A", "val": 42},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[100.0, 13.7], [100.1, 13.7], [100.1, 13.8], [100.0, 13.8], [100.0, 13.7]]]
                    }
                }
            ]
        }
        with open(vec_file, "w") as f:
            json.dump(vector_geojson, f)

        vec_title = f"vec_title_{uuid.uuid4().hex[:6]}"
        with open(vec_file, "rb") as f:
            resp_vec = requests.post(
                f"{MICROSERVICE_URL}/layers/publish-vector",
                data={"workspace_name": actual_ws, "layer_name": vec_title},
                files={"file": ("test_vec.geojson", f, "application/geo+json")},
                headers=HEADERS
            )
        print(f"Publish Vector Status: {resp_vec.status_code}")
        vec_json = resp_vec.json()
        vec_gs_name = vec_json.get("table_name")
        print(f"Vector published -> table_name/geoserver_name: {vec_gs_name}")

        # Ambil layer_id via /layers/my-layers
        ras_layer_id = get_my_layer_id(actual_ws, ras_gs_name)
        vec_layer_id = get_my_layer_id(actual_ws, vec_gs_name)
        print(f"Retrieved IDs -> Raster: {ras_layer_id}, Vector: {vec_layer_id}")

        # 1c. Cek GWC XML untuk Raster & Vector
        ras_gwc_res = requests.get(
            f"{GEOSERVER_GWC_URL}/rest/layers/{actual_ws}:{ras_gs_name}.xml",
            auth=GEOSERVER_AUTH
        )
        print(f"\n[GWC Layer Check Raster ({actual_ws}:{ras_gs_name})] HTTP {ras_gwc_res.status_code}")
        has_900913_ras = "EPSG:900913" in ras_gwc_res.text
        has_png_ras = "image/png" in ras_gwc_res.text
        print(f"Raster GWC automatic EPSG:900913: {has_900913_ras}, image/png: {has_png_ras}")

        vec_gwc_res = requests.get(
            f"{GEOSERVER_GWC_URL}/rest/layers/{actual_ws}:{vec_gs_name}.xml",
            auth=GEOSERVER_AUTH
        )
        print(f"\n[GWC Layer Check Vector ({actual_ws}:{vec_gs_name})] HTTP {vec_gwc_res.status_code}")
        has_900913_vec = "EPSG:900913" in vec_gwc_res.text
        has_png_vec = "image/png" in vec_gwc_res.text
        print(f"Vector GWC automatic EPSG:900913: {has_900913_vec}, image/png: {has_png_vec}")

        results["step1"] = {
            "raster_status": resp_ras.status_code,
            "vector_status": resp_vec.status_code,
            "raster_gs_name": ras_gs_name,
            "vector_gs_name": vec_gs_name,
            "raster_gwc_auto": has_900913_ras and has_png_ras,
            "vector_gwc_auto": has_900913_vec and has_png_vec,
        }

        # ======================================================================
        # STEP 2: Toggle keduanya ke WMTS via PATCH & Ambil tile (MISS lalu HIT)
        # ======================================================================
        print("\n" + "=" * 70)
        print("[STEP 2] Toggle ke WMTS via PATCH & Check Cache MISS -> HIT")
        print("=" * 70)

        # Toggle Raster
        resp_toggle_ras = requests.patch(
            f"{MICROSERVICE_URL}/layers/{ras_layer_id}/service-mode",
            json={"mode": "wmts"},
            headers=HEADERS
        )
        print(f"Toggle Raster WMTS: HTTP {resp_toggle_ras.status_code}, body: {resp_toggle_ras.text}")

        # Toggle Vector
        resp_toggle_vec = requests.patch(
            f"{MICROSERVICE_URL}/layers/{vec_layer_id}/service-mode",
            json={"mode": "wmts"},
            headers=HEADERS
        )
        print(f"Toggle Vector WMTS: HTTP {resp_toggle_vec.status_code}, body: {resp_toggle_vec.text}")

        # Ambil tile untuk Vector di zoom 11 (Lat 13.75, Lon 100.05)
        z = 11
        tx, ty = lat_lon_to_tile(13.75, 100.05, z)
        print(f"\nTile koordinat untuk Vector: z={z}, x={tx}, y={ty}")

        tile_url_vec = (
            f"{GEOSERVER_GWC_URL}/service/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
            f"&LAYER={actual_ws}:{vec_gs_name}&STYLE=&TILEMATRIXSET=EPSG:900913&FORMAT=image/png"
            f"&TILEMATRIX=EPSG:900913:{z}&TILEROW={ty}&TILECOL={tx}"
        )
        print(f"Fetching Vector tile via: {tile_url_vec}")

        # Request 1 (Curl #1)
        r1_vec = requests.get(tile_url_vec)
        cache_r1_vec = r1_vec.headers.get("geowebcache-cache-result", "UNKNOWN")
        print(f"Request 1 Vector: HTTP {r1_vec.status_code}, cache-result: {cache_r1_vec}, length: {len(r1_vec.content)}")

        # Request 2 (Curl #2)
        r2_vec = requests.get(tile_url_vec)
        cache_r2_vec = r2_vec.headers.get("geowebcache-cache-result", "UNKNOWN")
        print(f"Request 2 Vector: HTTP {r2_vec.status_code}, cache-result: {cache_r2_vec}, length: {len(r2_vec.content)}")

        results["step2"] = {
            "raster_toggle_status": resp_toggle_ras.status_code,
            "vector_toggle_status": resp_toggle_vec.status_code,
            "vec_r1_cache": cache_r1_vec,
            "vec_r2_cache": cache_r2_vec,
            "cache_miss_hit_verified": (cache_r1_vec == "MISS" and cache_r2_vec == "HIT")
        }

        # ======================================================================
        # STEP 3: Bandingkan WMS GetMap dan WMTS GetTile pada BBOX yang sama
        # ======================================================================
        print("\n" + "=" * 70)
        print("[STEP 3] Compare WMS vs WMTS for identical Tile BBOX")
        print("=" * 70)

        minx, miny, maxx, maxy = tile_bounds_3857(z, tx, ty)
        print(f"Tile BBOX (EPSG:3857): [{minx:.2f}, {miny:.2f}, {maxx:.2f}, {maxy:.2f}]")

        wms_url = (
            f"{GEOSERVER_WMS_URL}/{actual_ws}/wms?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap"
            f"&LAYERS={actual_ws}:{vec_gs_name}&STYLES=&SRS=EPSG:3857"
            f"&BBOX={minx},{miny},{maxx},{maxy}&WIDTH=256&HEIGHT=256&FORMAT=image/png&TRANSPARENT=TRUE"
        )
        wms_res = requests.get(wms_url)
        print(f"WMS GetMap: HTTP {wms_res.status_code}, length: {len(wms_res.content)}")

        img_wmts = Image.open(io.BytesIO(r2_vec.content))
        img_wms = Image.open(io.BytesIO(wms_res.content))

        print(f"WMTS Image Size: {img_wmts.size}, mode: {img_wmts.mode}")
        print(f"WMS Image Size:  {img_wms.size}, mode: {img_wms.mode}")

        arr_wmts = np.array(img_wmts)
        arr_wms = np.array(img_wms)

        # Hitung perbedaan pixel
        diff = np.abs(arr_wmts.astype(float) - arr_wms.astype(float))
        max_diff = np.max(diff)
        mean_diff = np.mean(diff)
        print(f"Pixel Diff: max={max_diff}, mean={mean_diff:.4f}")
        is_consistent = (img_wmts.size == img_wms.size == (256, 256)) and (mean_diff < 1.0)
        print(f"Consistency Check Passed (no shift / inversion): {is_consistent}")
        results["step3"] = {
            "wmts_size": list(img_wmts.size),
            "wms_size": list(img_wms.size),
            "mean_pixel_diff": float(mean_diff),
            "consistent": bool(is_consistent)
        }

        # ======================================================================
        # STEP 4: Ambil tile di luar bbox layer
        # ======================================================================
        print("\n" + "=" * 70)
        print("[STEP 4] Fetch Tile Outside Layer BBOX")
        print("=" * 70)

        # Tile di London (Lat 51.5, Lon -0.1) pada zoom 11
        out_tx, out_ty = lat_lon_to_tile(51.5, -0.1, z)
        out_tile_url = (
            f"{GEOSERVER_GWC_URL}/service/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
            f"&LAYER={actual_ws}:{vec_gs_name}&STYLE=&TILEMATRIXSET=EPSG:900913&FORMAT=image/png"
            f"&TILEMATRIX=EPSG:900913:{z}&TILEROW={out_ty}&TILECOL={out_tx}"
        )
        resp_out = requests.get(out_tile_url)
        print(f"Outside Tile: HTTP {resp_out.status_code}")
        print(f"Content-Type: {resp_out.headers.get('content-type')}")
        print(f"Content-Length: {len(resp_out.content)} bytes")
        is_blank = False
        if resp_out.status_code == 200 and "image/png" in resp_out.headers.get("content-type", ""):
            img_out = Image.open(io.BytesIO(resp_out.content))
            arr_out = np.array(img_out)
            alpha_max = np.max(arr_out[:, :, 3]) if arr_out.shape[2] == 4 else np.max(arr_out)
            is_blank = (alpha_max == 0)
            print(f"Image is completely blank transparent: {is_blank}")

        results["step4"] = {
            "http_status": resp_out.status_code,
            "content_type": resp_out.headers.get("content-type"),
            "content_length": len(resp_out.content),
            "is_blank": is_blank
        }

        # ======================================================================
        # STEP 5: Apply Style Baru & Verify Cache Bust (Bukan HIT dari cache lama)
        # ======================================================================
        print("\n" + "=" * 70)
        print("[STEP 5] Apply New Style via POST /styles/apply & Check Cache Truncate")
        print("=" * 70)

        # Terapkan style baru untuk Vector
        style_payload = {
            "layer_kind": "vector",
            "layer_id": str(vec_layer_id),
            "symbol": {
                "fill_color": "#FF0000",
                "stroke_color": "#880000",
                "fill_opacity": 0.8,
                "stroke_width": 2.0
            }
        }
        resp_style = requests.post(
            f"{MICROSERVICE_URL}/styles/apply",
            json=style_payload,
            headers=HEADERS
        )
        print(f"Apply Style Status: HTTP {resp_style.status_code}")
        print("Apply Style Body:", resp_style.text)

        # Ambil tile yang sama lagi
        r_after_style = requests.get(tile_url_vec)
        cache_after_style = r_after_style.headers.get("geowebcache-cache-result", "UNKNOWN")
        print(f"Tile Fetch after Style Apply: HTTP {r_after_style.status_code}, cache-result: {cache_after_style}")

        # Bandingkan gambar baru dengan gambar sebelum apply style
        img_after = Image.open(io.BytesIO(r_after_style.content))
        arr_after = np.array(img_after)
        style_diff = np.mean(np.abs(arr_after.astype(float) - arr_wmts.astype(float)))
        print(f"Difference between old tile and restyled tile: {style_diff:.4f}")

        results["step5"] = {
            "style_apply_status": resp_style.status_code,
            "cache_result_after_style": cache_after_style,
            "is_cache_invalidated": cache_after_style == "MISS",
            "content_changed": bool(style_diff > 0)
        }

        # ======================================================================
        # STEP 6: Delete Layer & Verify GWC Cleanup + GetCapabilities 200
        # ======================================================================
        print("\n" + "=" * 70)
        print("[STEP 6] Delete Layer via API & Verify GWC Cleanup")
        print("=" * 70)

        # Hapus vector layer
        resp_del_vec = requests.delete(
            f"{MICROSERVICE_URL}/layers/{actual_ws}/{vec_gs_name}",
            headers=HEADERS
        )
        print(f"Delete Vector Layer ({vec_gs_name}): HTTP {resp_del_vec.status_code}")

        # Hapus raster layer
        resp_del_ras = requests.delete(
            f"{MICROSERVICE_URL}/layers/{actual_ws}/{ras_gs_name}",
            headers=HEADERS
        )
        print(f"Delete Raster Layer ({ras_gs_name}): HTTP {resp_del_ras.status_code}")

        # Cek apakah GWC layer ikut hilang
        gwc_check_vec = requests.get(
            f"{GEOSERVER_GWC_URL}/rest/layers/{actual_ws}:{vec_gs_name}.xml",
            auth=GEOSERVER_AUTH
        )
        print(f"Check GWC Vector Layer after delete: HTTP {gwc_check_vec.status_code} (Expected: 404)")

        gwc_check_ras = requests.get(
            f"{GEOSERVER_GWC_URL}/rest/layers/{actual_ws}:{ras_gs_name}.xml",
            auth=GEOSERVER_AUTH
        )
        print(f"Check GWC Raster Layer after delete: HTTP {gwc_check_ras.status_code} (Expected: 404)")

        # Cek GetCapabilities global
        gwc_caps = requests.get(f"{GEOSERVER_GWC_URL}/service/wmts?REQUEST=GetCapabilities")
        print(f"Global WMTS GetCapabilities: HTTP {gwc_caps.status_code}")

        results["step6"] = {
            "delete_vec_status": resp_del_vec.status_code,
            "delete_ras_status": resp_del_ras.status_code,
            "gwc_vec_deleted": gwc_check_vec.status_code == 404,
            "gwc_ras_deleted": gwc_check_ras.status_code == 404,
            "get_capabilities_200": gwc_caps.status_code == 200
        }

    finally:
        # Cleanup Workspace
        print("\n[CLEANUP] Deleting test workspace...")
        requests.delete(f"{MICROSERVICE_URL}/workspaces/{actual_ws}", headers=HEADERS)
        if os.path.exists(raster_file):
            try: os.remove(raster_file)
            except: pass
        if os.path.exists(vec_file):
            try: os.remove(vec_file)
            except: pass

    print("\n" + "=" * 70)
    print("VERIFICATION SUMMARY:")
    print("=" * 70)
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
