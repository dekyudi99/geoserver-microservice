import os
import re
import zipfile
import tempfile
import xml.etree.ElementTree as ET
from typing import Tuple, Dict, Any, Optional, List
from collections import namedtuple
import geopandas as gpd
import pandas as pd
from shapely.geometry import (
    Point, LineString, Polygon,
    MultiPolygon, MultiLineString, MultiPoint, GeometryCollection
)
from shapely.validation import make_valid
from ..config.database import engine, ensure_postgis_extension
from ..config.settings import settings
from ..clients.geoserver_client import geoserver_client
from .style_service import style_service
import logging

logger = logging.getLogger("geoserver_service.vector")

BoundingBox = namedtuple("BoundingBox", ["left", "bottom", "right", "top"])

def _parse_kml_xml(kml_path_or_content) -> gpd.GeoDataFrame:
    """Fallback parser native untuk berkas KML."""
    if isinstance(kml_path_or_content, str) and os.path.exists(kml_path_or_content):
        tree = ET.parse(kml_path_or_content)
        root = tree.getroot()
    elif isinstance(kml_path_or_content, bytes):
        root = ET.fromstring(kml_path_or_content)
    else:
        root = ET.fromstring(str(kml_path_or_content))

    def _strip_ns(tag):
        return tag.split('}')[-1] if '}' in tag else tag

    def _parse_coords(coord_text):
        if not coord_text:
            return []
        matches = re.findall(r'([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*,\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)', coord_text)
        coords = []
        for x, y in matches:
            try:
                coords.append((float(x), float(y)))
            except (ValueError, TypeError):
                continue
        return coords

    def _parse_geom_node(node):
        tag = _strip_ns(node.tag)
        if tag == 'Point':
            for child in node:
                if _strip_ns(child.tag) == 'coordinates':
                    coords = _parse_coords(child.text)
                    if coords:
                        return Point(coords[0])
        elif tag == 'LineString':
            for child in node:
                if _strip_ns(child.tag) == 'coordinates':
                    coords = _parse_coords(child.text)
                    if len(coords) >= 2:
                        return LineString(coords)
        elif tag == 'Polygon':
            exterior = []
            interiors = []
            for child in node:
                c_tag = _strip_ns(child.tag)
                if c_tag == 'outerBoundaryIs':
                    for sub in child:
                        for s in sub:
                            if _strip_ns(s.tag) == 'coordinates':
                                exterior = _parse_coords(s.text)
                elif c_tag == 'innerBoundaryIs':
                    for sub in child:
                        for s in sub:
                            if _strip_ns(s.tag) == 'coordinates':
                                in_coords = _parse_coords(s.text)
                                if in_coords:
                                    interiors.append(in_coords)
            if exterior and len(exterior) >= 3:
                if exterior[0] != exterior[-1]:
                    exterior.append(exterior[0])
                clean_interiors = []
                for hole in interiors:
                    if len(hole) >= 3:
                        if hole[0] != hole[-1]:
                            hole.append(hole[0])
                        clean_interiors.append(hole)
                try:
                    return Polygon(exterior, clean_interiors)
                except Exception:
                    pass
        elif tag == 'MultiGeometry':
            sub_geoms = []
            for child in node:
                g = _parse_geom_node(child)
                if g and not g.is_empty:
                    sub_geoms.append(g)
            if sub_geoms:
                return GeometryCollection(sub_geoms)
        return None

    features = []
    for elem in root.iter():
        if _strip_ns(elem.tag) == 'Placemark':
            props = {}
            geom = None
            for child in elem:
                c_tag = _strip_ns(child.tag)
                if c_tag in ('Point', 'LineString', 'Polygon', 'MultiGeometry'):
                    geom = _parse_geom_node(child)
                elif c_tag in ('name', 'description'):
                    props[c_tag] = child.text
                elif c_tag == 'ExtendedData':
                    for data_node in child:
                        d_name = data_node.attrib.get('name')
                        val_node = data_node.find('{*}value')
                        if val_node is None:
                            val_node = data_node.find('value')
                        val = val_node.text if val_node is not None else data_node.text
                        if d_name:
                            props[d_name] = val
            if geom and not geom.is_empty:
                props['geometry'] = geom
                features.append(props)

    if not features:
        raise ValueError("Tidak ada geometri valid yang dapat diekstrak dari berkas KML!")

    gdf = gpd.GeoDataFrame(features, crs="EPSG:4326")
    return gdf

def _parse_kmz(kmz_path: str) -> gpd.GeoDataFrame:
    with zipfile.ZipFile(kmz_path, 'r') as z:
        kml_files = [f for f in z.namelist() if f.lower().endswith('.kml')]
        if not kml_files:
            raise ValueError("Berkas KMZ tidak mengandung file .kml di dalamnya!")
        kml_content = z.read(kml_files[0])
        return _parse_kml_xml(kml_content)

class VectorService:
    def read_vector_file_to_gdf(self, file_path: str) -> Tuple[gpd.GeoDataFrame, str]:
        ext = os.path.splitext(file_path)[1].lower()

        if ext == '.kmz':
            gdf = _parse_kmz(file_path)
            format_name = "KMZ"
        elif ext == '.kml':
            try:
                # pyrefly: ignore [missing-import]
                import fiona
                fiona.drvsupport.supported_drivers['KML'] = 'rw'
                gdf = gpd.read_file(file_path, driver='KML')
            except Exception:
                gdf = _parse_kml_xml(file_path)
            format_name = "KML"
        elif ext in ('.geojson', '.json'):
            gdf = gpd.read_file(file_path)
            format_name = "GeoJSON"
        elif ext == '.gpkg':
            gdf = gpd.read_file(file_path)
            format_name = "GeoPackage"
        elif ext == '.csv':
            df = pd.read_csv(file_path)
            lat_col = next((c for c in df.columns if c.lower() in ('latitude', 'lat', 'y')), None)
            lon_col = next((c for c in df.columns if c.lower() in ('longitude', 'lon', 'long', 'lng', 'x')), None)
            if not lat_col or not lon_col:
                raise ValueError("Berkas CSV spasial wajib memiliki kolom latitude (lat) dan longitude (lon)!")
            df[lat_col] = pd.to_numeric(df[lat_col], errors='coerce')
            df[lon_col] = pd.to_numeric(df[lon_col], errors='coerce')
            df = df.dropna(subset=[lat_col, lon_col])
            geometry = [Point(xy) for xy in zip(df[lon_col], df[lat_col])]
            gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
            format_name = "CSV Point"
        elif ext == '.zip':
            with tempfile.TemporaryDirectory() as tmpdir:
                with zipfile.ZipFile(file_path, 'r') as zip_ref:
                    zip_ref.extractall(tmpdir)
                shp_files = []
                for root, _, files in os.walk(tmpdir):
                    for f in files:
                        if f.lower().endswith('.shp'):
                            shp_files.append(os.path.join(root, f))
                if not shp_files:
                    raise ValueError("Arsip .zip tidak memuat berkas Shapefile (.shp)!")
                gdf = gpd.read_file(shp_files[0])
            format_name = "Shapefile"
        elif ext == '.shp':
            gdf = gpd.read_file(file_path)
            format_name = "Shapefile"
        else:
            raise ValueError(f"Ekstensi {ext} tidak didukung!")

        # Normalisasi ke WGS84 EPSG:4326
        if gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)

        # Bersihkan geometri invalid
        gdf['geometry'] = gdf['geometry'].apply(lambda g: make_valid(g) if g is not None else None)
        gdf = gdf[gdf.geometry.notnull() & ~gdf.geometry.is_empty]

        return gdf, format_name

    def simplify_vector_gdf(
        self,
        gdf: gpd.GeoDataFrame,
        tolerance: float = 0.0001,
        preserve_topology: bool = True
    ) -> Tuple[gpd.GeoDataFrame, Dict[str, Any]]:
        initial_features = len(gdf)
        simplified_gdf = gdf.copy()
        simplified_gdf['geometry'] = simplified_gdf['geometry'].simplify(
            tolerance=tolerance,
            preserve_topology=preserve_topology
        )
        simplified_gdf['geometry'] = simplified_gdf['geometry'].apply(lambda g: make_valid(g) if g is not None else None)
        simplified_gdf = simplified_gdf[simplified_gdf.geometry.notnull() & ~simplified_gdf.geometry.is_empty]

        geom_types = simplified_gdf.geom_type.value_counts().to_dict()
        dominant_type = max(geom_types, key=geom_types.get) if geom_types else "Polygon"

        stats = {
            "initial_feature_count": initial_features,
            "simplified_feature_count": len(simplified_gdf),
            "geometry_type": dominant_type,
            "tolerance": tolerance
        }
        return simplified_gdf, stats

    def publish_vector_to_geoserver(
        self,
        gdf: gpd.GeoDataFrame,
        workspace_name: str,
        table_name: str,
        title: str,
        geom_type: str = "Polygon"
    ) -> Dict[str, Any]:
        ensure_postgis_extension()

        # 1. Pastikan PostGIS Datastore tersedia di GeoServer
        geoserver_client.ensure_postgis_datastore(
            workspace_name=workspace_name,
            store_name="postgis_store"
        )

        # 2. Simpan GeoDataFrame ke PostGIS DB
        clean_gdf = gdf.copy()
        # Normalisasi kolom yang bertipe object tidak serializable
        for col in clean_gdf.columns:
            if col != 'geometry' and clean_gdf[col].dtype == 'object':
                clean_gdf[col] = clean_gdf[col].astype(str)

        clean_gdf.to_postgis(
            name=table_name,
            con=engine,
            if_exists="replace",
            index=False,
            schema="public"
        )

        # 3. Publish FeatureType ke GeoServer
        geoserver_client.publish_postgis_feature_type(
            workspace_name=workspace_name,
            store_name="postgis_store",
            table_name=table_name,
            title=title,
            srid=4326
        )

        # 4. Generate & Assign Default Style
        style_name = f"style_{table_name}"
        sld_xml = style_service.generate_vector_sld(
            style_name=style_name,
            geom_type=geom_type
        )
        try:
            style_service.apply_style(
                workspace=workspace_name,
                layer_name=table_name,
                style_name=style_name,
                sld_xml=sld_xml
            )
        except Exception as e:
            logger.warning(f"Could not apply auto style: {e}")

        bounds = gdf.total_bounds.tolist()
        wms_base = settings.GEOSERVER_WMS_URL
        return {
            "table_name": table_name,
            "feature_count": len(gdf),
            "geom_type": geom_type,
            "bbox": bounds,
            "srid": 4326,
            "wms_url": f"{wms_base}/{workspace_name}/wms"
        }

vector_service = VectorService()
