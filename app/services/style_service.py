import html
import math
import re
from typing import List, Dict, Any, Optional
from ..clients.geoserver_client import geoserver_client
import logging

logger = logging.getLogger("geoserver_service.style")

class StyleService:
    """
    Service for SLD styling, color ramp manipulation, and GeoServer style publishing.
    """

    def generate_raster_sld(
        self,
        style_name: str,
        color_entries: List[Dict[str, Any]],
        style_type: str = "intervals"
    ) -> str:
        """
        Generate SLD 1.0.0 XML untuk Raster Layer.
        style_type: 'values' (kategori diskrit), 'intervals' (rentang kelas), atau 'ramp' (gradien mulus)
        """
        if style_type not in {"intervals", "values", "ramp", "ramp_exact"}:
            raise ValueError("style_type raster tidak didukung.")
        entries_xml = ""
        safe_style_name = html.escape(str(style_name), quote=True)
        for entry in color_entries:
            q = entry.get("quantity", 0)
            c = entry.get("color", "#000000")
            op = entry.get("opacity", 1.0)
            lbl = html.escape(str(entry.get("label", f"Class {q}")), quote=True)
            if not isinstance(c, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
                raise ValueError("Warna raster harus berupa HEX #RRGGBB.")
            try:
                numeric_quantity = float(q)
                numeric_opacity = float(op)
            except (TypeError, ValueError) as exc:
                raise ValueError("Nilai raster atau opacity harus berupa angka.") from exc
            if not math.isfinite(numeric_quantity) or not math.isfinite(numeric_opacity) or not 0 <= numeric_opacity <= 1:
                raise ValueError("Nilai raster atau opacity tidak valid.")
            entries_xml += f'              <ColorMapEntry color="{c}" quantity="{numeric_quantity}" opacity="{numeric_opacity}" label="{lbl}"/>\n'

        sld_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" 
    xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc" 
    xmlns:xlink="http://www.w3.org/1999/xlink" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <NamedLayer>
    <Name>{safe_style_name}</Name>
    <UserStyle>
      <Title>{safe_style_name}</Title>
      <FeatureTypeStyle>
        <Rule>
          <RasterSymbolizer>
            <ColorMap type="{style_type}">
{entries_xml}            </ColorMap>
          </RasterSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
"""
        return sld_xml

    def generate_vector_sld(
        self,
        style_name: str,
        geom_type: str = "polygon",
        fill_color: str = "#0d9488",
        stroke_color: str = "#0f766e",
        stroke_width: float = 1.5,
        fill_opacity: float = 0.45,
        stroke_opacity: float = 1.0,
        point_size: float = 8.0,
        mark: str = "circle",
        stroke_dasharray: Optional[str] = None,
    ) -> str:
        """
        Generate SLD 1.0.0 XML untuk Vector Layer (Polygon, Line, Point).
        """
        def validate_color(value: str, field_name: str) -> str:
            if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                raise ValueError(f"{field_name} harus berupa warna HEX #RRGGBB.")
            return value

        def validate_number(value: Any, field_name: str, minimum: float, maximum: float) -> float:
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field_name} harus berupa angka.") from exc
            if not math.isfinite(number) or not minimum <= number <= maximum:
                raise ValueError(f"{field_name} harus berada pada rentang {minimum} sampai {maximum}.")
            return number

        normalized_geometry = (geom_type or "").strip().lower().replace(" ", "")
        if normalized_geometry in {"polygon", "multipolygon"}:
            geometry_family = "polygon"
        elif normalized_geometry in {"line", "linestring", "multilinestring", "multiline"}:
            geometry_family = "line"
        elif normalized_geometry in {"point", "multipoint"}:
            geometry_family = "point"
        else:
            raise ValueError(f"Tipe geometri vector tidak didukung: {geom_type}.")

        fill_color = validate_color(fill_color, "fill_color")
        stroke_color = validate_color(stroke_color, "stroke_color")
        stroke_width = validate_number(stroke_width, "stroke_width", 0, 20)
        fill_opacity = validate_number(fill_opacity, "fill_opacity", 0, 1)
        stroke_opacity = validate_number(stroke_opacity, "stroke_opacity", 0, 1)
        point_size = validate_number(point_size, "point_size", 1, 80)
        safe_style_name = html.escape(str(style_name), quote=True)
        fill_xml = (
            f'<CssParameter name="fill">{fill_color}</CssParameter>\n'
            f'                    <CssParameter name="fill-opacity">{fill_opacity}</CssParameter>'
        )

        if geometry_family == "polygon":
            symbolizer_xml = f"""            <PolygonSymbolizer>
              <Fill>
                {fill_xml}
              </Fill>
              <Stroke>
                <CssParameter name="stroke">{stroke_color}</CssParameter>
                <CssParameter name="stroke-width">{stroke_width}</CssParameter>
                <CssParameter name="stroke-opacity">{stroke_opacity}</CssParameter>
              </Stroke>
            </PolygonSymbolizer>"""
        elif geometry_family == "line":
            dash_xml = ""
            if stroke_dasharray:
                parts = str(stroke_dasharray).split(",")
                if len(parts) > 8:
                    raise ValueError("stroke_dasharray maksimal memiliki 8 nilai.")
                values = [validate_number(part.strip(), "stroke_dasharray", 0, 100) for part in parts]
                dash_xml = (
                    "\n                <CssParameter name=\"stroke-dasharray\">"
                    + " ".join(str(value) for value in values)
                    + "</CssParameter>"
                )
            symbolizer_xml = f"""            <LineSymbolizer>
              <Stroke>
                <CssParameter name="stroke">{stroke_color}</CssParameter>
                <CssParameter name="stroke-width">{stroke_width}</CssParameter>
                <CssParameter name="stroke-opacity">{stroke_opacity}</CssParameter>{dash_xml}
              </Stroke>
            </LineSymbolizer>"""
        else:
            supported_marks = {"circle", "square", "triangle", "star", "cross", "x"}
            mark = str(mark or "circle").lower()
            if mark not in supported_marks:
                raise ValueError(f"Bentuk marker tidak didukung: {mark}.")
            if mark == "x":
                mark = "cross"
            symbolizer_xml = f"""            <PointSymbolizer>
              <Graphic>
                <Mark>
                  <WellKnownName>{mark}</WellKnownName>
                  <Fill>
                    {fill_xml}
                  </Fill>
                  <Stroke>
                    <CssParameter name="stroke">{stroke_color}</CssParameter>
                    <CssParameter name="stroke-width">{stroke_width}</CssParameter>
                    <CssParameter name="stroke-opacity">{stroke_opacity}</CssParameter>
                  </Stroke>
                </Mark>
                <Size>{point_size}</Size>
              </Graphic>
            </PointSymbolizer>"""

        sld_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" 
    xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc" 
    xmlns:xlink="http://www.w3.org/1999/xlink" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <NamedLayer>
    <Name>{safe_style_name}</Name>
    <UserStyle>
      <Title>{safe_style_name}</Title>
      <FeatureTypeStyle>
        <Rule>
{symbolizer_xml}
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
"""
        return sld_xml

    def apply_style(
        self,
        workspace: str,
        layer_name: str,
        style_name: str,
        sld_xml: Optional[str] = None
    ) -> bool:
        if sld_xml:
            geoserver_client.create_or_update_style(style_name, sld_xml)
        return geoserver_client.assign_style_to_layer(workspace, layer_name, style_name)

style_service = StyleService()
