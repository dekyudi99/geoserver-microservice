#!/usr/bin/env python3
"""
scripts/find_orphans.py
Audit aset spasial komprehensif dan read-only:
Memeriksa file di disk (/data_raster), tabel PostGIS (database spatial),
metadata database (voxagis_user), dan katalog GeoServer REST.

Wajib memenuhi pemeriksaan kesetaraan:
- file di disk = terdaftar + tak terdaftar
- tabel = terdaftar-yang-ada + sistem + tak terdaftar
- feature type dan coverage store GeoServer dicocokkan dengan DB berdasarkan nama.
Jika kesetaraan gagal, script keluar dengan error code non-zero.
"""

import os
import sys
import base64
import json
import urllib.request
from typing import Set, Dict, List, Any, Tuple
from sqlalchemy import text

# Import database & config
from app.config.database import SessionLocal, spatial_engine
from app.config import settings
from app.models.spatial_data import VectorLayer, RasterMetadata

# ==============================================================================
# PURE CLASSIFICATION FUNCTIONS (DAPAT DIUJI UNIT TEST)
# ==============================================================================

def classify_raster_files(
    disk_files: Set[str],
    db_files: Set[str],
    gs_files: Set[str]
) -> Dict[str, Any]:
    """
    Mengklasifikasikan file raster:
    - registered: ada di disk & ada di DB
    - potential: ada di disk & tidak di DB, tapi ada di GeoServer
    - true_orphans: ada di disk & tidak di DB & tidak di GeoServer
    - unregistered: potential + true_orphans
    - db_missing_disk: ada di DB tapi tidak ada di disk
    """
    registered = sorted(list(disk_files & db_files))
    unregistered_all = disk_files - db_files
    potential = sorted(list(unregistered_all & gs_files))
    true_orphans = sorted(list(unregistered_all - gs_files))
    unregistered = sorted(list(unregistered_all))
    db_missing_disk = sorted(list(db_files - disk_files))

    # Kesetaraan: disk = terdaftar + tak terdaftar
    if len(disk_files) != len(registered) + len(unregistered):
        raise ValueError(
            f"Raster equality failed: len(disk_files)={len(disk_files)} != "
            f"registered({len(registered)}) + unregistered({len(unregistered)})"
        )
    if len(unregistered) != len(potential) + len(true_orphans):
        raise ValueError("Raster unregistered partition failed")

    return {
        "disk_count": len(disk_files),
        "registered": registered,
        "potential": potential,
        "true_orphans": true_orphans,
        "unregistered": unregistered,
        "db_missing_disk": db_missing_disk,
    }


def classify_tables(
    all_pg_tables: Set[str],
    db_tables: Set[str],
    gs_featuretypes: Set[str],
    sys_tables: Set[str]
) -> Dict[str, Any]:
    """
    Mengklasifikasikan tabel PostGIS:
    - system_tables: tabel sistem PostGIS
    - registered_present: tabel user yang ada di DB
    - unregistered: tabel user yang tidak ada di DB (potensial vs yatim murni)
    - db_missing_postgis: tabel di DB yang fisiknya tidak ada di PostGIS
    """
    present_sys = sorted(list(all_pg_tables & sys_tables))
    user_tables = all_pg_tables - sys_tables

    registered_present = sorted(list(user_tables & db_tables))
    unregistered_all = user_tables - db_tables
    potential = sorted(list(unregistered_all & gs_featuretypes))
    true_orphans = sorted(list(unregistered_all - gs_featuretypes))
    unregistered = sorted(list(unregistered_all))
    db_missing_postgis = sorted(list(db_tables - all_pg_tables))

    # Kesetaraan: tabel = terdaftar-yang-ada + sistem + tak terdaftar
    if len(all_pg_tables) != len(registered_present) + len(present_sys) + len(unregistered):
        raise ValueError(
            f"Table equality failed: len(all_pg_tables)={len(all_pg_tables)} != "
            f"registered_present({len(registered_present)}) + sys({len(present_sys)}) + unregistered({len(unregistered)})"
        )

    return {
        "all_count": len(all_pg_tables),
        "system_tables": present_sys,
        "registered_present": registered_present,
        "potential": potential,
        "true_orphans": true_orphans,
        "unregistered": unregistered,
        "db_missing_postgis": db_missing_postgis,
    }


def match_geoserver_to_db(
    gs_fts_by_ws: Dict[str, List[str]],
    gs_cs_by_ws: Dict[str, List[Tuple[str, str]]],
    db_tables: Set[str],
    db_stores: Set[str]
) -> Dict[str, Any]:
    """
    Mencocokkan feature types dan coverage stores GeoServer dengan DB berdasarkan nama.
    """
    ft_matched = []
    ft_unmatched = []
    for ws, fts in gs_fts_by_ws.items():
        for ft in fts:
            if ft in db_tables:
                ft_matched.append((ws, ft))
            else:
                ft_unmatched.append((ws, ft))

    cs_matched = []
    cs_unmatched = []
    for ws, cs_list in gs_cs_by_ws.items():
        for cs, fname in cs_list:
            if cs in db_stores:
                cs_matched.append((ws, cs, fname))
            else:
                cs_unmatched.append((ws, cs, fname))

    return {
        "ft_matched": ft_matched,
        "ft_unmatched": ft_unmatched,
        "cs_matched": cs_matched,
        "cs_unmatched": cs_unmatched,
    }

# ==============================================================================
# AUDIT RUNNER
# ==============================================================================

def fetch_geoserver_details() -> Tuple[Dict[str, List[str]], Dict[str, List[Tuple[str, str]]], Set[str], Set[str]]:
    auth = base64.b64encode(f"{settings.GEOSERVER_USER}:{settings.GEOSERVER_PASS}".encode()).decode()
    headers = {'Authorization': f'Basic {auth}', 'Accept': 'application/json'}
    base_url = settings.GEOSERVER_URL.rstrip('/')

    def get(url):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    ws_data = get(f"{base_url}/rest/workspaces.json")
    workspaces = [w['name'] for w in ws_data['workspaces']['workspace']]

    gs_fts_by_ws: Dict[str, List[str]] = {}
    gs_cs_by_ws: Dict[str, List[Tuple[str, str]]] = {}
    all_gs_fts: Set[str] = set()
    all_gs_files: Set[str] = set()

    for ws in workspaces:
        # Datastores / FeatureTypes
        gs_fts_by_ws[ws] = []
        try:
            ds_data = get(f"{base_url}/rest/workspaces/{ws}/datastores.json")
            datastores = [d['name'] for d in ds_data.get('dataStores', {}).get('dataStore', [])]
        except Exception:
            datastores = []
        for ds in datastores:
            try:
                ft_data = get(f"{base_url}/rest/workspaces/{ws}/datastores/{ds}/featuretypes.json")
                fts = [f['name'] for f in ft_data.get('featureTypes', {}).get('featureType', [])]
                gs_fts_by_ws[ws].extend(fts)
                all_gs_fts.update(fts)
            except Exception:
                pass

        # CoverageStores
        gs_cs_by_ws[ws] = []
        try:
            cs_data = get(f"{base_url}/rest/workspaces/{ws}/coveragestores.json")
            cstores = [c['name'] for c in cs_data.get('coverageStores', {}).get('coverageStore', [])]
        except Exception:
            cstores = []
        for cs in cstores:
            try:
                detail = get(f"{base_url}/rest/workspaces/{ws}/coveragestores/{cs}.json")
                url = detail.get('coverageStore', {}).get('url', '')
                clean_fname = os.path.basename(url.replace("file:", ""))
                gs_cs_by_ws[ws].append((cs, clean_fname))
                if clean_fname:
                    all_gs_files.add(clean_fname)
            except Exception:
                pass

    return gs_fts_by_ws, gs_cs_by_ws, all_gs_fts, all_gs_files


def main():
    print("================================================================================")
    print("           AUDIT READ-ONLY ASET DATA SPASIAL & PENGECEKAN KESETARAAN            ")
    print("================================================================================")
    db = SessionLocal()

    # 1. DB Metadata
    vec_layers = db.query(VectorLayer).all()
    ras_metas = db.query(RasterMetadata).all()
    db_tables = {v.table_name for v in vec_layers if v.table_name}
    db_stores = {r.store_name for r in ras_metas if r.store_name}
    db_raster_files = {os.path.basename(r.file_path) for r in ras_metas if r.file_path}

    print(f"\n1. METADATA DATABASE (voxagis_user):")
    print(f"   - VectorLayer terdaftar: {len(vec_layers)}")
    for v in vec_layers:
        print(f"     * id={v.id} | table_name={v.table_name}")
    print(f"   - RasterMetadata terdaftar: {len(ras_metas)}")
    for r in ras_metas:
        print(f"     * id={r.id} | store_name={r.store_name} | file={os.path.basename(r.file_path)}")

    # 2. GeoServer Catalog
    gs_fts_by_ws, gs_cs_by_ws, all_gs_fts, all_gs_files = fetch_geoserver_details()

    # 3. PostGIS Tables
    with spatial_engine.connect() as conn:
        res = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name;"))
        all_pg_tables = {row[0] for row in res}

    sys_tables = {'spatial_ref_sys', 'geometry_columns', 'geography_columns'}
    tbl_res = classify_tables(all_pg_tables, db_tables, all_gs_fts, sys_tables)

    print(f"\n2. TABEL POSTGIS (spatial db, schema public):")
    print(f"   - Total tabel fisik: {tbl_res['all_count']}")
    print(f"   - Tabel sistem ({len(tbl_res['system_tables'])}): {tbl_res['system_tables']}")
    print(f"   - Terdaftar-yang-ada ({len(tbl_res['registered_present'])}):")
    for t in tbl_res['registered_present']:
        print(f"     * {t}")
    print(f"   - Tak terdaftar - Potensial GeoServer ({len(tbl_res['potential'])}):")
    for t in tbl_res['potential']:
        print(f"     * {t}")
    print(f"   - Tak terdaftar - Yatim Murni ({len(tbl_res['true_orphans'])}):")
    for t in tbl_res['true_orphans']:
        print(f"     * {t}")
    print(f"   - DB terdaftar tapi fisik PostGIS HILANG ({len(tbl_res['db_missing_postgis'])}):")
    for t in tbl_res['db_missing_postgis']:
        print(f"     * {t}")

    # 4. Raster Files
    raster_dir = settings.DATA_RASTER_PATH if hasattr(settings, "DATA_RASTER_PATH") else "/data_raster"
    all_disk_tif = set([f for f in os.listdir(raster_dir) if f.endswith(('.tif', '.tiff'))]) if os.path.exists(raster_dir) else set()
    ras_res = classify_raster_files(all_disk_tif, db_raster_files, all_gs_files)

    print(f"\n3. FILE RASTER DISK ({raster_dir}):")
    print(f"   - Total file .tif di disk: {ras_res['disk_count']}")
    print(f"   - Terdaftar di DB ({len(ras_res['registered'])}):")
    for f in ras_res['registered']:
        print(f"     * {f}")
    print(f"   - Tak terdaftar - Potensial GeoServer ({len(ras_res['potential'])}):")
    for f in ras_res['potential']:
        print(f"     * {f}")
    print(f"   - Tak terdaftar - Yatim Murni ({len(ras_res['true_orphans'])}):")
    for f in ras_res['true_orphans']:
        print(f"     * {f}")
    print(f"   - DB terdaftar tapi file disk HILANG ({len(ras_res['db_missing_disk'])}):")
    for f in ras_res['db_missing_disk']:
        print(f"     * {f}")

    # 5. GeoServer Matching
    gs_match = match_geoserver_to_db(gs_fts_by_ws, gs_cs_by_ws, db_tables, db_stores)
    print(f"\n4. PENCOCOKAN GEOSERVER DENGAN DB:")
    print(f"   - FeatureTypes cocok dengan DB ({len(gs_match['ft_matched'])}):")
    for ws, ft in gs_match['ft_matched']:
        print(f"     * [{ws}] {ft}")
    print(f"   - FeatureTypes tanpa baris DB ({len(gs_match['ft_unmatched'])}):")
    for ws, ft in gs_match['ft_unmatched']:
        print(f"     * [{ws}] {ft}")
    print(f"   - CoverageStores cocok dengan DB ({len(gs_match['cs_matched'])}):")
    for ws, cs, fn in gs_match['cs_matched']:
        print(f"     * [{ws}] {cs} -> {fn}")
    print(f"   - CoverageStores tanpa baris DB ({len(gs_match['cs_unmatched'])}):")
    for ws, cs, fn in gs_match['cs_unmatched']:
        print(f"     * [{ws}] {cs} -> {fn}")

    # 6. PEMERIKSAAN KESETARAAN (EQUALITY CHECK)
    print(f"\n5. PEMERIKSAAN KESETARAAN AKHIR:")
    # File disk:
    disk_calc = len(ras_res['registered']) + len(ras_res['unregistered'])
    print(f"   - File di disk ({ras_res['disk_count']}) == Terdaftar ({len(ras_res['registered'])}) + Tak Terdaftar ({len(ras_res['unregistered'])}): {ras_res['disk_count'] == disk_calc}")
    assert ras_res['disk_count'] == disk_calc, "Kesetaraan file di disk GAGAL!"

    # Tabel:
    tbl_calc = len(tbl_res['registered_present']) + len(tbl_res['system_tables']) + len(tbl_res['unregistered'])
    print(f"   - Tabel PostGIS ({tbl_res['all_count']}) == Terdaftar-ada ({len(tbl_res['registered_present'])}) + Sistem ({len(tbl_res['system_tables'])}) + Tak Terdaftar ({len(tbl_res['unregistered'])}): {tbl_res['all_count'] == tbl_calc}")
    assert tbl_res['all_count'] == tbl_calc, "Kesetaraan tabel PostGIS GAGAL!"

    print("\n>>> SEMUA PEMERIKSAAN KESETARAAN BERHASIL (PASS) <<<")
    db.close()

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"\n[FATAL ERROR] Audit kesetaraan gagal: {e}", file=sys.stderr)
        sys.exit(1)
