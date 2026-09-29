import os
import rasterio
from rasterio.enums import ColorInterp
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from ..clients.geoserver_client import geoserver_client
from ..config.settings import settings
from .style_service import style_service
import logging

logger = logging.getLogger("geoserver_service.raster")

COLOR_RAMPS = {
    "cyan_blue_magenta": ["#00e5ff", "#0044ff", "#ff00ee"],
    "blues": ["#eff3ff", "#bdd7e7", "#6baed6", "#3182bd", "#08519c"],
    "viridis": ["#440154", "#3b528b", "#21908c", "#5dc863", "#fde725"],
    "spectral": ["#2b83ba", "#abdda4", "#ffffbf", "#fdae61", "#d7191c"],
    "traffic_light": ["#1a9641", "#a6d96a", "#ffffbf", "#fdae61", "#d7191c"],
    "magma": ["#000004", "#51127c", "#b73779", "#fb8861", "#fcfdbf"],
    "greens": ["#edf8fb", "#b2e2e2", "#66c2a4", "#2ca25f", "#006d2c"],
    "reds": ["#fee5d9", "#fcae91", "#fb6a4a", "#de2d26", "#a50f15"],
    "terrain": ["#33a02c", "#b2df8a", "#ffff99", "#fdbf6f", "#ff7f00", "#e31a1c"],
}

def hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    return '#{:02x}{:02x}{:02x}'.format(
        max(0, min(255, int(round(rgb[0])))),
        max(0, min(255, int(round(rgb[1])))),
        max(0, min(255, int(round(rgb[2]))))
    )

def interpolate_palette(stops: list, n: int) -> list:
    if n <= 1:
        return [stops[0]]
    if n == len(stops):
        return stops
    rgb_stops = [hex_to_rgb(s) for s in stops]
    result = []
    for i in range(n):
        t = i / (n - 1)
        scaled_t = t * (len(rgb_stops) - 1)
        idx = int(scaled_t)
        frac = scaled_t - idx
        if idx >= len(rgb_stops) - 1:
            result.append(rgb_to_hex(rgb_stops[-1]))
        else:
            c1 = rgb_stops[idx]
            c2 = rgb_stops[idx + 1]
            interp = (
                c1[0] + frac * (c2[0] - c1[0]),
                c1[1] + frac * (c2[1] - c1[1]),
                c1[2] + frac * (c2[2] - c1[2]),
            )
            result.append(rgb_to_hex(interp))
    return result

class RasterService:
    def get_tiff_metadata(self, file_path: str) -> Dict[str, Any]:
        with rasterio.open(file_path) as dataset:
            epsg = dataset.crs.to_epsg() if dataset.crs else 4326
            bbox = dataset.bounds
            width = dataset.width
            height = dataset.height
            return {
                "epsg": epsg or 4326,
                "bbox": {
                    "left": bbox.left,
                    "bottom": bbox.bottom,
                    "right": bbox.right,
                    "top": bbox.top
                },
                "dimensions": {
                    "width": width,
                    "height": height
                }
            }

    def validate_single_band(self, file_path: str) -> None:
        with rasterio.open(file_path) as dataset:
            if dataset.count != 1:
                raise ValueError(
                    f"File GeoTIFF harus 1 band (single-band). File ini memiliki {dataset.count} band."
                )

    def sanitize_tiff_for_geoserver(self, file_path: str) -> None:
        with rasterio.open(file_path) as src:
            if src.count == 4 and src.colorinterp[0] == ColorInterp.gray:
                profile = src.profile.copy()
                profile.update(photometric='RGB', nodata=0)
                data = src.read()
                with rasterio.open(file_path, 'w', **profile) as dst:
                    dst.write(data)
                    dst.colorinterp = [
                        ColorInterp.red, 
                        ColorInterp.green, 
                        ColorInterp.blue, 
                        ColorInterp.alpha
                    ]

    def get_raster_statistics(self, file_path: str, band_index: int = 1) -> Dict[str, Any]:
        with rasterio.open(file_path) as src:
            data = src.read(band_index)
            nodata = src.nodata
            if nodata is not None:
                valid = data[data != nodata]
            else:
                valid = data[~np.isnan(data)]

            if valid.size == 0:
                raise ValueError("Tidak ada data valid di dalam raster.")

            return {
                "min": float(np.nanmin(valid)),
                "max": float(np.nanmax(valid)),
                "mean": float(np.nanmean(valid)),
                "std": float(np.nanstd(valid)),
                "total_valid_pixels": int(valid.size)
            }

    def publish_raster_to_geoserver(
        self,
        file_path: str,
        workspace_name: str,
        store_name: str,
        title: str,
        style_config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        # 1. Pastikan valid untuk GeoServer
        self.sanitize_tiff_for_geoserver(file_path)
        meta = self.get_tiff_metadata(file_path)

        # 2. Publish CoverageStore ke GeoServer
        success = geoserver_client.create_coverage_store(
            workspace_name=workspace_name,
            store_name=store_name,
            file_path=file_path
        )
        if not success:
            raise RuntimeError(f"Gagal membuat coveragestore '{store_name}' di GeoServer.")

        # 3. Handle default / custom style jika dikonfigurasi
        symbology = None
        if style_config and style_config.get("colors"):
            try:
                layer_style_name = f"style_{store_name}"
                sld_xml = style_service.generate_raster_sld(
                    style_name=layer_style_name,
                    color_entries=style_config.get("colors", []),
                    style_type=style_config.get("style_type", "intervals")
                )
                style_service.apply_style(
                    workspace=workspace_name,
                    layer_name=store_name,
                    style_name=layer_style_name,
                    sld_xml=sld_xml
                )
                symbology = {
                    "style_name": layer_style_name,
                    "style_type": style_config.get("style_type", "intervals"),
                    "classes_count": len(style_config.get("colors", []))
                }
            except Exception as e:
                logger.warning(f"Gagal menerapkan style raster: {e}")

        wms_base = settings.GEOSERVER_WMS_URL
        return {
            "store_name": store_name,
            "title": title,
            "epsg": meta["epsg"],
            "bbox": meta["bbox"],
            "dimensions": meta["dimensions"],
            "wms_url": f"{wms_base}/{workspace_name}/wms",
            "symbology": symbology
        }

raster_service = RasterService()
