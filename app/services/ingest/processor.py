import os
import shutil
import zipfile
import re
import uuid
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session
import rasterio
import geopandas as gpd
from shapely.geometry import MultiPolygon, MultiLineString, MultiPoint, Polygon, LineString, Point

from app.config.settings import settings
from app.config.database import engine
from app.clients.geoserver_client import geoserver_client
from app.models.ingest_job import IngestJob
from app.models.spatial_data import VectorLayer, RasterMetadata
from app.services.hash_id import resolve_workspace

logger = logging.getLogger("geoserver_service.ingest_processor")

def slugify_layer_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower().strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:30] or "layer"

def force_multi_geometry(geom):
    if geom is None or geom.is_empty:
        return geom
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    elif isinstance(geom, LineString):
        return MultiLineString([geom])
    elif isinstance(geom, Point):
        return MultiPoint([geom])
    return geom

class IngestProcessor:
    @staticmethod
    def process(job_id: str, db: Session) -> Dict[str, Any]:
        """Proses ingest berkas spasial tunggal dari staging disk."""
        job: IngestJob = db.query(IngestJob).filter(IngestJob.id == job_id).first()
        if not job:
            raise ValueError(f"Job '{job_id}' tidak ditemukan.")

        staging_path = Path(job.staging_file_path) if job.staging_file_path else None
        if not staging_path or not staging_path.exists():
            raise FileNotFoundError(f"Berkas staging '{staging_path}' tidak ditemukan.")

        # Resolve workspace name jika berupa hashed ID
        ws_meta = resolve_workspace(db, job.workspace_name)
        actual_ws_name = ws_meta.workspace_name if ws_meta else job.workspace_name

        try:
            geoserver_client.create_workspace(actual_ws_name)
        except Exception:
            pass

        job.progress = 20
        db.commit()

        return IngestProcessor.process_file_path(job, staging_path, actual_ws_name, db)

    @staticmethod
    def process_file_path(
        job: IngestJob,
        file_path: Path,
        workspace_name: str,
        db: Session,
        display_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Memproses berkas spasial sesuai ekstensi (Raster GeoTIFF, Vektor ZIP Shapefile, atau Vektor GeoJSON)."""
        ext = file_path.suffix.lower()
        if ext in (".tif", ".tiff"):
            return IngestProcessor._process_raster(job, file_path, workspace_name, db, display_name=display_name)
        elif ext == ".zip":
            if IngestProcessor._is_kmz(file_path):
                return IngestProcessor._process_vector_kmz(job, file_path, workspace_name, db, display_name=display_name)
            return IngestProcessor._process_vector_zip(job, file_path, workspace_name, db, display_name=display_name)
        elif ext == ".kmz":
            return IngestProcessor._process_vector_kmz(job, file_path, workspace_name, db, display_name=display_name)
        elif ext == ".kml":
            return IngestProcessor._process_vector_kml(job, file_path, workspace_name, db, display_name=display_name)
        elif ext in (".geojson", ".json"):
            return IngestProcessor._process_vector_geojson(job, file_path, workspace_name, db, display_name=display_name)
        elif ext == ".csv":
            return IngestProcessor._process_vector_csv(job, file_path, workspace_name, db, display_name=display_name)
        else:
            raise ValueError(f"Ekstensi berkas '{ext}' tidak didukung. Format yang didukung: GeoTIFF (.tif/.tiff), Shapefile (.zip), GeoJSON (.geojson/.json), KML (.kml), KMZ (.kmz), dan CSV (.csv).")

    @staticmethod
    def _process_raster(
        job: IngestJob,
        path: Path,
        workspace_name: str,
        db: Session,
        display_name: Optional[str] = None
    ) -> Dict[str, Any]:
        target_display_name = display_name or job.layer_name
        clean_slug = slugify_layer_name(target_display_name)
        unique_suffix = uuid.uuid4().hex[:6]
        # geoserver_name: nama teknis unik di GeoServer & sistem berkas
        geoserver_name = f"ras_{clean_slug}_{unique_suffix}"

        logger.info(f"Processing Raster Job {job.id} for '{target_display_name}' -> geoserver_name='{geoserver_name}'")
        job.progress = min(job.progress + 15, 85)
        db.commit()

        # 1. Validasi CRS menggunakan rasterio
        with rasterio.open(path) as src:
            if not src.crs:
                raise ValueError("Berkas GeoTIFF tidak memiliki CRS / Spatial Reference System yang valid.")
            epsg = src.crs.to_epsg() or 4326
            bounds = src.bounds
            width, height = src.width, src.height
            count = src.count

        # 2. Simpan fisik ke direktori data_raster bersama dengan geoserver_name unik
        dest_filename = f"{geoserver_name}.tif"
        os.makedirs(settings.DATA_RASTER_PATH, exist_ok=True)
        dest_file = Path(settings.DATA_RASTER_PATH) / dest_filename
        shutil.copy2(path, dest_file)

        # 3. Publish ke GeoServer CoverageStore
        store_name = f"store_{geoserver_name}"
        geoserver_path = f"/data_raster/{dest_filename}"
        success = geoserver_client.create_coverage_store(
            workspace_name=workspace_name,
            store_name=store_name,
            file_path=geoserver_path
        )
        if not success:
            raise RuntimeError(f"Gagal mempublikasikan CoverageStore '{store_name}' ke GeoServer.")

        # 4. Catat Metadata Raster dengan Bbox WGS84 (EPSG:4326)
        try:
            from rasterio.warp import transform_bounds
            if src.crs and str(src.crs).upper() not in ("EPSG:4326", "WGS 84", "OGC:CRS84"):
                wgs_b = transform_bounds(src.crs, "EPSG:4326", bounds.left, bounds.bottom, bounds.right, bounds.top)
                bbox_dict = {
                    "minx": float(wgs_b[0]),
                    "miny": float(wgs_b[1]),
                    "maxx": float(wgs_b[2]),
                    "maxy": float(wgs_b[3])
                }
            else:
                bbox_dict = {
                    "minx": float(bounds.left),
                    "miny": float(bounds.bottom),
                    "maxx": float(bounds.right),
                    "maxy": float(bounds.top)
                }
        except Exception as warp_err:
            logger.warning(f"Could not transform bounds to WGS84: {warp_err}")
            bbox_dict = {
                "minx": float(bounds.left),
                "miny": float(bounds.bottom),
                "maxx": float(bounds.right),
                "maxy": float(bounds.top)
            }

        wms_url = f"{settings.GEOSERVER_WMS_URL}/{workspace_name}/wms"

        raster_meta = RasterMetadata(
            api_key_id=job.owner_id,
            workspace_name=workspace_name,
            store_name=store_name,
            layer_name=target_display_name,
            file_path=str(dest_file),
            epsg=epsg,
            bbox=bbox_dict,
            dimensions={"width": width, "height": height, "bands": count},
            wms_url=wms_url
        )
        db.add(raster_meta)
        db.commit()

        result_data = {
            "type": "RASTER",
            "workspace_name": workspace_name,
            "store_name": store_name,
            "layer_name": target_display_name,
            "geoserver_name": store_name,
            "display_name": target_display_name,
            "title": target_display_name,
            "epsg": epsg,
            "bbox": bbox_dict,
            "dimensions": {"width": width, "height": height, "bands": count},
            "wms_url": wms_url
        }

        # Bersihkan berkas staging
        try:
            path.unlink()
        except Exception:
            pass

        return result_data

    @staticmethod
    def _process_vector_zip(
        job: IngestJob,
        path: Path,
        workspace_name: str,
        db: Session,
        display_name: Optional[str] = None
    ) -> Dict[str, Any]:
        target_display_name = display_name or job.layer_name
        owner_prefix = str(job.owner_id).replace("-", "")[:8]
        clean_slug = slugify_layer_name(target_display_name)
        unique_suffix = uuid.uuid4().hex[:6]
        # geoserver_name: nama teknis unik di PostGIS & GeoServer
        geoserver_name = f"vec_{owner_prefix}_{clean_slug}_{unique_suffix}"

        logger.info(f"Processing Vector ZIP Job {job.id} for '{target_display_name}' -> geoserver_name='{geoserver_name}'")
        job.progress = min(job.progress + 15, 85)
        db.commit()

        extract_dir = path.parent / f"extracted_{job.id}_{unique_suffix}"
        extract_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 1. Validasi Keamanan Ekstraksi ZIP (Anti Path-Traversal & Anti Zip-Bomb)
            with zipfile.ZipFile(path, "r") as zf:
                total_uncompressed = 0
                for info in zf.infolist():
                    if ".." in info.filename or info.filename.startswith(("/", "\\")):
                        raise ValueError(f"Berkas zip berbahaya: mengandung path traversal '{info.filename}'.")
                    total_uncompressed += info.file_size
                    if total_uncompressed > 2 * 1024 * 1024 * 1024:
                        raise ValueError("Berkas zip melebihi batas rasio kompresi aman (potensi zip bomb).")
                zf.extractall(extract_dir)

            # 2. Temukan berkas Shapefile dan komponen pendukungnya
            shp_files = list(extract_dir.rglob("*.shp"))
            if not shp_files:
                raise ValueError("Berkas Shapefile (.shp) tidak ditemukan di dalam arsip ZIP.")

            shp_path = shp_files[0]
            stem = shp_path.stem
            parent = shp_path.parent

            missing = []
            for ext in (".shx", ".dbf", ".prj"):
                expected = parent / f"{stem}{ext}"
                if not expected.exists():
                    found = any(f.name.lower() == f"{stem.lower()}{ext}" for f in parent.iterdir())
                    if not found:
                        missing.append(ext)
            if missing:
                raise ValueError(f"Shapefile tidak lengkap. Berkas pendukung wajib hilang: {', '.join(missing)}.")

            # 3. Baca dengan GeoPandas (engine pyogrio dengan fallback)
            try:
                gdf = gpd.read_file(shp_path, engine="pyogrio")
            except Exception:
                gdf = gpd.read_file(shp_path)

            if gdf.empty:
                raise ValueError("Shapefile kosong (tidak ada baris fitur).")
            if gdf.crs is None:
                raise ValueError("Berkas .prj tidak dapat dibaca atau CRS tidak valid.")

            native_srid = gdf.crs.to_epsg() or 4326

            # 4. Standardisasi Geometri & Kolom
            if native_srid != 4326:
                gdf = gdf.to_crs(epsg=4326)

            gdf["geometry"] = gdf["geometry"].make_valid()
            gdf["geometry"] = gdf["geometry"].apply(force_multi_geometry)

            new_cols = {}
            for col in gdf.columns:
                if col != "geometry":
                    clean_col = re.sub(r"[^a-zA-Z0-9_]", "_", col.lower().strip()).strip("_")
                    if clean_col in ("table", "user", "order", "group", "select", "from", "where"):
                        clean_col = f"col_{clean_col}"
                    new_cols[col] = clean_col[:50]
            gdf.rename(columns=new_cols, inplace=True)

            # 5. Staging PostGIS & Atomic Table Swap
            staging_table_name = f"staging_{uuid.uuid4().hex[:10]}"

            try:
                gdf.to_postgis(name=staging_table_name, con=engine, if_exists="replace", index=False)

                with engine.begin() as conn:
                    conn.execute(text(f"""
                        DROP TABLE IF EXISTS {geoserver_name} CASCADE;
                        ALTER TABLE {staging_table_name} RENAME TO {geoserver_name};
                        CREATE INDEX idx_{geoserver_name}_geom ON {geoserver_name} USING GIST (geometry);
                    """))
            except Exception as db_err:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(f"DROP TABLE IF EXISTS {staging_table_name} CASCADE;"))
                except Exception:
                    pass
                raise db_err

            # 6. Publish PostGIS FeatureType ke GeoServer
            postgis_store = "postgis_default"
            geoserver_client.ensure_postgis_datastore(
                workspace_name=workspace_name,
                store_name=postgis_store
            )

            pub_ok = geoserver_client.publish_postgis_feature_type(
                workspace_name=workspace_name,
                store_name=postgis_store,
                table_name=geoserver_name,
                title=target_display_name,
                srid=4326
            )
            if not pub_ok:
                raise RuntimeError(f"Gagal mempublikasikan FeatureType '{geoserver_name}' ke GeoServer.")

            # 7. Simpan Metadata Layer Vektor
            total_bounds = gdf.total_bounds
            bbox_list = [float(b) for b in total_bounds]
            geom_type = gdf.geometry.geom_type.iloc[0] if not gdf.empty else "Geometry"
            wms_url = f"{settings.GEOSERVER_WMS_URL}/{workspace_name}/wms"

            vec_layer = VectorLayer(
                api_key_id=job.owner_id,
                workspace_name=workspace_name,
                table_name=geoserver_name,
                layer_name=target_display_name,
                geom_type=geom_type,
                feature_count=len(gdf),
                bbox=bbox_list,
                srid=4326,
                wms_url=wms_url
            )
            db.add(vec_layer)
            db.commit()

            result_data = {
                "type": "VECTOR",
                "workspace_name": workspace_name,
                "table_name": geoserver_name,
                "layer_name": target_display_name,
                "geoserver_name": geoserver_name,
                "display_name": target_display_name,
                "title": target_display_name,
                "geom_type": geom_type,
                "feature_count": len(gdf),
                "native_srid": native_srid,
                "bbox": bbox_list,
                "wms_url": wms_url
            }

            return result_data

        finally:
            try:
                shutil.rmtree(extract_dir, ignore_errors=True)
                if path.exists():
                    path.unlink()
            except Exception:
                pass

    @staticmethod
    def _process_vector_geojson(
        job: IngestJob,
        path: Path,
        workspace_name: str,
        db: Session,
        display_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Memproses berkas GeoJSON (.geojson / .json) ke PostGIS dan GeoServer."""
        target_display_name = display_name or job.layer_name
        owner_prefix = str(job.owner_id).replace("-", "")[:8]
        clean_slug = slugify_layer_name(target_display_name)
        unique_suffix = uuid.uuid4().hex[:6]
        # geoserver_name unik
        geoserver_name = f"vec_{owner_prefix}_{clean_slug}_{unique_suffix}"

        logger.info(f"Processing GeoJSON Job {job.id} for '{target_display_name}' -> geoserver_name='{geoserver_name}'")
        job.progress = min(job.progress + 15, 85)
        db.commit()

        try:
            # 1. Baca GeoJSON via pyogrio/geopandas
            try:
                gdf = gpd.read_file(path, engine="pyogrio")
            except Exception as read_err:
                try:
                    gdf = gpd.read_file(path)
                except Exception:
                    raise ValueError(f"Berkas GeoJSON tidak valid atau rusak: {read_err}")

            if gdf.empty:
                raise ValueError("Berkas GeoJSON kosong (tidak ada baris fitur).")

            # 2. CRS Handling: Standar GeoJSON RFC 7946 adalah EPSG:4326
            if gdf.crs is None:
                gdf.set_crs(epsg=4326, inplace=True)
                native_srid = 4326
            else:
                native_srid = gdf.crs.to_epsg() or 4326
                if native_srid != 4326:
                    gdf = gdf.to_crs(epsg=4326)

            # 3. Validasi Geometri & Multi-Geometry
            gdf["geometry"] = gdf["geometry"].make_valid()
            gdf["geometry"] = gdf["geometry"].apply(force_multi_geometry)

            # 4. Sanitasi Kolom
            new_cols = {}
            for col in gdf.columns:
                if col != "geometry":
                    clean_col = re.sub(r"[^a-zA-Z0-9_]", "_", col.lower().strip()).strip("_")
                    if clean_col in ("table", "user", "order", "group", "select", "from", "where"):
                        clean_col = f"col_{clean_col}"
                    new_cols[col] = clean_col[:50]
            gdf.rename(columns=new_cols, inplace=True)

            # 5. Staging PostGIS & Atomic Swap
            staging_table_name = f"staging_{uuid.uuid4().hex[:10]}"

            try:
                gdf.to_postgis(name=staging_table_name, con=engine, if_exists="replace", index=False)

                with engine.begin() as conn:
                    conn.execute(text(f"""
                        DROP TABLE IF EXISTS {geoserver_name} CASCADE;
                        ALTER TABLE {staging_table_name} RENAME TO {geoserver_name};
                        CREATE INDEX idx_{geoserver_name}_geom ON {geoserver_name} USING GIST (geometry);
                    """))
            except Exception as db_err:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(f"DROP TABLE IF EXISTS {staging_table_name} CASCADE;"))
                except Exception:
                    pass
                raise db_err

            # 6. Publish FeatureType ke GeoServer
            postgis_store = "postgis_default"
            geoserver_client.ensure_postgis_datastore(
                workspace_name=workspace_name,
                store_name=postgis_store
            )

            pub_ok = geoserver_client.publish_postgis_feature_type(
                workspace_name=workspace_name,
                store_name=postgis_store,
                table_name=geoserver_name,
                title=target_display_name,
                srid=4326
            )
            if not pub_ok:
                raise RuntimeError(f"Gagal mempublikasikan FeatureType '{geoserver_name}' ke GeoServer.")

            # 7. Simpan Metadata Layer Vektor
            total_bounds = gdf.total_bounds
            bbox_list = [float(b) for b in total_bounds]
            geom_type = gdf.geometry.geom_type.iloc[0] if not gdf.empty else "Geometry"
            wms_url = f"{settings.GEOSERVER_WMS_URL}/{workspace_name}/wms"

            vec_layer = VectorLayer(
                api_key_id=job.owner_id,
                workspace_name=workspace_name,
                table_name=geoserver_name,
                layer_name=target_display_name,
                geom_type=geom_type,
                feature_count=len(gdf),
                bbox=bbox_list,
                srid=4326,
                wms_url=wms_url
            )
            db.add(vec_layer)
            db.commit()

            result_data = {
                "type": "VECTOR",
                "format": "GEOJSON",
                "workspace_name": workspace_name,
                "table_name": geoserver_name,
                "layer_name": target_display_name,
                "geoserver_name": geoserver_name,
                "display_name": target_display_name,
                "title": target_display_name,
                "geom_type": geom_type,
                "feature_count": len(gdf),
                "native_srid": native_srid,
                "bbox": bbox_list,
                "wms_url": wms_url
            }

            return result_data

        finally:
            if path.exists():
                try:
                    path.unlink()
                except Exception:
                    pass


    @staticmethod
    def _is_kmz(path: Path) -> bool:
        """Mengecek apakah berkas zip adalah arsip KMZ yang berisi file .kml."""
        try:
            with zipfile.ZipFile(path, "r") as zf:
                return any(n.lower().endswith(".kml") for n in zf.namelist())
        except Exception:
            return False

    @staticmethod
    def _save_gdf_to_postgis_and_geoserver(
        gdf: gpd.GeoDataFrame,
        job: IngestJob,
        workspace_name: str,
        target_display_name: str,
        file_type_format: str,
        db: Session
    ) -> Dict[str, Any]:
        """Metode utilitas terpadu untuk menyimpan GeoDataFrame ke PostGIS dan mempublikasikannya ke GeoServer."""
        owner_prefix = str(job.owner_id).replace("-", "")[:8]
        clean_slug = slugify_layer_name(target_display_name)
        unique_suffix = uuid.uuid4().hex[:6]
        geoserver_name = f"vec_{owner_prefix}_{clean_slug}_{unique_suffix}"

        logger.info(f"Publishing {file_type_format} layer '{target_display_name}' -> geoserver_name='{geoserver_name}'")
        job.progress = min(job.progress + 15, 85)
        db.commit()

        # CRS Standard: WGS84 EPSG:4326
        if gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)
            native_srid = 4326
        else:
            native_srid = gdf.crs.to_epsg() or 4326
            if native_srid != 4326:
                gdf = gdf.to_crs(epsg=4326)

        # Validasi geometri & Multi-geometry
        gdf["geometry"] = gdf["geometry"].make_valid()
        gdf["geometry"] = gdf["geometry"].apply(force_multi_geometry)

        # Sanitasi nama kolom
        new_cols = {}
        for col in gdf.columns:
            if col != "geometry":
                clean_col = re.sub(r"[^a-zA-Z0-9_]", "_", str(col).lower().strip()).strip("_")
                if clean_col in ("table", "user", "order", "group", "select", "from", "where"):
                    clean_col = f"col_{clean_col}"
                new_cols[col] = clean_col[:50]
        gdf.rename(columns=new_cols, inplace=True)

        staging_table_name = f"staging_{uuid.uuid4().hex[:10]}"
        try:
            gdf.to_postgis(name=staging_table_name, con=engine, if_exists="replace", index=False)
            with engine.begin() as conn:
                conn.execute(text(f"""
                    DROP TABLE IF EXISTS "{geoserver_name}" CASCADE;
                    ALTER TABLE "{staging_table_name}" RENAME TO "{geoserver_name}";
                    CREATE INDEX idx_{geoserver_name}_geom ON "{geoserver_name}" USING GIST (geometry);
                """))
        except Exception as db_err:
            try:
                with engine.begin() as conn:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{staging_table_name}" CASCADE;'))
            except Exception:
                pass
            raise db_err

        # Publish PostGIS FeatureType ke GeoServer
        postgis_store = "postgis_default"
        geoserver_client.ensure_postgis_datastore(
            workspace_name=workspace_name,
            store_name=postgis_store
        )

        pub_ok = geoserver_client.publish_postgis_feature_type(
            workspace_name=workspace_name,
            store_name=postgis_store,
            table_name=geoserver_name,
            title=target_display_name,
            srid=4326
        )
        if not pub_ok:
            raise RuntimeError(f"Gagal mempublikasikan FeatureType '{geoserver_name}' ke GeoServer.")

        total_bounds = gdf.total_bounds
        bbox_list = [float(b) for b in total_bounds]
        geom_type = gdf.geometry.geom_type.iloc[0] if not gdf.empty else "Geometry"
        wms_url = f"{settings.GEOSERVER_WMS_URL}/{workspace_name}/wms"

        vec_layer = VectorLayer(
            api_key_id=job.owner_id,
            workspace_name=workspace_name,
            table_name=geoserver_name,
            layer_name=target_display_name,
            geom_type=geom_type,
            feature_count=len(gdf),
            bbox=bbox_list,
            srid=4326,
            wms_url=wms_url
        )
        db.add(vec_layer)
        db.commit()

        return {
            "type": "VECTOR",
            "format": file_type_format,
            "workspace_name": workspace_name,
            "table_name": geoserver_name,
            "layer_name": target_display_name,
            "geoserver_name": geoserver_name,
            "display_name": target_display_name,
            "title": target_display_name,
            "geom_type": geom_type,
            "feature_count": len(gdf),
            "native_srid": native_srid,
            "bbox": bbox_list,
            "wms_url": wms_url
        }

    @staticmethod
    def _process_vector_kml(
        job: IngestJob,
        path: Path,
        workspace_name: str,
        db: Session,
        display_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Memproses berkas KML (.kml) ke PostGIS dan GeoServer."""
        target_display_name = display_name or job.layer_name
        try:
            gdf = gpd.read_file(path, engine="pyogrio")
        except Exception:
            try:
                gdf = gpd.read_file(path)
            except Exception as e:
                raise ValueError(f"Gagal membaca berkas KML: {e}")

        if gdf.empty:
            raise ValueError("Berkas KML kosong (tidak ada baris fitur).")

        return IngestProcessor._save_gdf_to_postgis_and_geoserver(
            gdf=gdf,
            job=job,
            workspace_name=workspace_name,
            target_display_name=target_display_name,
            file_type_format="KML",
            db=db
        )

    @staticmethod
    def _process_vector_kmz(
        job: IngestJob,
        path: Path,
        workspace_name: str,
        db: Session,
        display_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Memproses berkas KMZ (.kmz) dengan mengekstrak file KML didalamnya."""
        target_display_name = display_name or job.layer_name
        extract_dir = Path(tempfile.mkdtemp(prefix="kmz_extract_"))
        try:
            with zipfile.ZipFile(path, "r") as zf:
                kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
                if not kml_names:
                    raise ValueError("Berkas KMZ tidak berisi file .kml yang valid.")
                # Ekstrak kml utama (biasanya doc.kml)
                main_kml = kml_names[0]
                extracted_path = zf.extract(main_kml, extract_dir)

            try:
                gdf = gpd.read_file(extracted_path, engine="pyogrio")
            except Exception:
                gdf = gpd.read_file(extracted_path)

            if gdf.empty:
                raise ValueError("Berkas KMZ kosong (tidak ada baris fitur).")

            return IngestProcessor._save_gdf_to_postgis_and_geoserver(
                gdf=gdf,
                job=job,
                workspace_name=workspace_name,
                target_display_name=target_display_name,
                file_type_format="KMZ",
                db=db
            )
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)

    @staticmethod
    def _process_vector_csv(
        job: IngestJob,
        path: Path,
        workspace_name: str,
        db: Session,
        display_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Memproses berkas CSV bergeometri (koordinat lat/lon atau WKT kolom)."""
        target_display_name = display_name or job.layer_name
        import pandas as pd
        from shapely import wkt

        # Coba beberapa delimiter umum
        for sep in [",", ";", "\t"]:
            try:
                df = pd.read_csv(path, sep=sep)
                if len(df.columns) > 1:
                    break
            except Exception:
                continue

        if df.empty:
            raise ValueError("Berkas CSV kosong.")

        # Deteksi kolom geometri WKT atau pasangan Lat/Lon
        cols_lower = {str(c).lower().strip(): c for c in df.columns}
        
        # 1. Cek kolom WKT
        wkt_col = None
        for cand in ["wkt", "geometry", "geom", "the_geom"]:
            if cand in cols_lower:
                wkt_col = cols_lower[cand]
                break

        if wkt_col:
            def parse_wkt_safe(val):
                try:
                    return wkt.loads(str(val))
                except Exception:
                    return None
            geoms = df[wkt_col].apply(parse_wkt_safe)
            df = df[geoms.notnull()].copy()
            if df.empty:
                raise ValueError("Semua baris WKT pada CSV tidak valid.")
            gdf = gpd.GeoDataFrame(df.drop(columns=[wkt_col]), geometry=geoms[geoms.notnull()], crs="EPSG:4326")
        else:
            # 2. Cek kolom latitude dan longitude
            lat_col = None
            lon_col = None
            for cand in ["lat", "latitude", "y", "lintang", "lat_deg"]:
                if cand in cols_lower:
                    lat_col = cols_lower[cand]
                    break
            for cand in ["lon", "lng", "longitude", "x", "bujur", "lon_deg", "long"]:
                if cand in cols_lower:
                    lon_col = cols_lower[cand]
                    break

            if not (lat_col and lon_col):
                raise ValueError(
                    f"Kolom koordinat spasial tidak ditemukan pada CSV. Harap sediakan kolom 'latitude' & 'longitude' (atau 'lat' & 'lon'), atau kolom WKT 'geometry'."
                )

            # Konversi kolom ke numerik
            df[lat_col] = pd.to_numeric(df[lat_col], errors="coerce")
            df[lon_col] = pd.to_numeric(df[lon_col], errors="coerce")
            valid_rows = df[df[lat_col].notnull() & df[lon_col].notnull()].copy()
            if valid_rows.empty:
                raise ValueError("Tidak ada nilai koordinat latitude/longitude valid pada CSV.")

            gdf = gpd.GeoDataFrame(
                valid_rows,
                geometry=gpd.points_from_xy(valid_rows[lon_col], valid_rows[lat_col]),
                crs="EPSG:4326"
            )

        return IngestProcessor._save_gdf_to_postgis_and_geoserver(
            gdf=gdf,
            job=job,
            workspace_name=workspace_name,
            target_display_name=target_display_name,
            file_type_format="CSV",
            db=db
        )
