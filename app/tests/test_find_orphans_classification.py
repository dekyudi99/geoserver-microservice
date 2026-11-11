import pytest
from scripts.find_orphans import classify_raster_files, classify_tables, match_geoserver_to_db

def test_classify_raster_files_synthetic():
    # 5 files on disk
    disk = {"r1.tif", "r2.tif", "r3.tif", "r4.tif", "r5.tif"}
    # 3 in DB: r1, r2, and r_missing (missing from disk)
    db = {"r1.tif", "r2.tif", "r_missing.tif"}
    # GeoServer has r1, r2, r3, r_gs_only
    gs = {"r1.tif", "r2.tif", "r3.tif", "r_gs_only.tif"}

    res = classify_raster_files(disk, db, gs)

    assert res["disk_count"] == 5
    assert set(res["registered"]) == {"r1.tif", "r2.tif"}
    assert set(res["potential"]) == {"r3.tif"} # on disk, not in DB, in GS
    assert set(res["true_orphans"]) == {"r4.tif", "r5.tif"} # on disk, not in DB, not in GS
    assert set(res["unregistered"]) == {"r3.tif", "r4.tif", "r5.tif"}
    assert set(res["db_missing_disk"]) == {"r_missing.tif"}

    # Equality check
    assert res["disk_count"] == len(res["registered"]) + len(res["unregistered"])

def test_classify_tables_synthetic():
    # 6 tables in PostGIS
    all_pg = {"t_user1", "t_user2", "t_orphan1", "t_orphan2", "spatial_ref_sys", "geometry_columns"}
    sys_tables = {"spatial_ref_sys", "geometry_columns", "geography_columns"}
    # DB has t_user1, t_user2, and t_missing (not in PostGIS)
    db = {"t_user1", "t_user2", "t_missing"}
    # GeoServer has t_user1, t_user2, t_orphan1
    gs_fts = {"t_user1", "t_user2", "t_orphan1"}

    res = classify_tables(all_pg, db, gs_fts, sys_tables)

    assert res["all_count"] == 6
    assert set(res["system_tables"]) == {"spatial_ref_sys", "geometry_columns"}
    assert set(res["registered_present"]) == {"t_user1", "t_user2"}
    assert set(res["potential"]) == {"t_orphan1"} # in GS, not in DB
    assert set(res["true_orphans"]) == {"t_orphan2"} # neither
    assert set(res["unregistered"]) == {"t_orphan1", "t_orphan2"}
    assert set(res["db_missing_postgis"]) == {"t_missing"}

    # Equality check
    assert res["all_count"] == len(res["registered_present"]) + len(res["system_tables"]) + len(res["unregistered"])

def test_match_geoserver_to_db_synthetic():
    gs_fts = {
        "ws_a": ["t1", "t2"],
        "ws_b": ["t3", "t4"]
    }
    gs_cs = {
        "ws_a": [("cs1", "r1.tif"), ("cs2", "r2.tif")],
        "ws_b": [("cs3", "r3.tif")]
    }
    db_tables = {"t1", "t3"}
    db_stores = {"cs1", "cs3"}

    res = match_geoserver_to_db(gs_fts, gs_cs, db_tables, db_stores)

    assert set(res["ft_matched"]) == {("ws_a", "t1"), ("ws_b", "t3")}
    assert set(res["ft_unmatched"]) == {("ws_a", "t2"), ("ws_b", "t4")}
    assert set(res["cs_matched"]) == {("ws_a", "cs1", "r1.tif"), ("ws_b", "cs3", "r3.tif")}
    assert set(res["cs_unmatched"]) == {("ws_a", "cs2", "r2.tif")}

