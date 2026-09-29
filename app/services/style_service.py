import os
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
        entries_xml = ""
        for entry in color_entries:
            q = entry.get("quantity", 0)
            c = entry.get("color", "#000000")
            op = entry.get("opacity", 1.0)
            lbl = entry.get("label", f"Class {q}")
            entries_xml += f'              <ColorMapEntry color="{c}" quantity="{q}" opacity="{op}" label="{lbl}"/>\n'

        sld_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" 
    xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc" 
    xmlns:xlink="http://www.w3.org/1999/xlink" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <NamedLayer>
    <Name>{style_name}</Name>
    <UserStyle>
      <Title>{style_name}</Title>
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
        fill_opacity: float = 0.45
    ) -> str:
        """
        Generate SLD 1.0.0 XML untuk Vector Layer (Polygon, Line, Point).
        """
        geom_type_lower = (geom_type or "polygon").lower()
        if "polygon" in geom_type_lower:
            symbolizer_xml = f"""            <PolygonSymbolizer>
              <Fill>
                <CssParameter name="fill">{fill_color}</CssParameter>
                <CssParameter name="fill-opacity">{fill_opacity}</CssParameter>
              </Fill>
              <Stroke>
                <CssParameter name="stroke">{stroke_color}</CssParameter>
                <CssParameter name="stroke-width">{stroke_width}</CssParameter>
              </Stroke>
            </PolygonSymbolizer>"""
        elif "line" in geom_type_lower or "string" in geom_type_lower:
            symbolizer_xml = f"""            <LineSymbolizer>
              <Stroke>
                <CssParameter name="stroke">{stroke_color}</CssParameter>
                <CssParameter name="stroke-width">{stroke_width}</CssParameter>
              </Stroke>
            </LineSymbolizer>"""
        else:
            symbolizer_xml = f"""            <PointSymbolizer>
              <Graphic>
                <Mark>
                  <WellKnownName>circle</WellKnownName>
                  <Fill>
                    <CssParameter name="fill">{fill_color}</CssParameter>
                  </Fill>
                  <Stroke>
                    <CssParameter name="stroke">{stroke_color}</CssParameter>
                    <CssParameter name="stroke-width">{stroke_width}</CssParameter>
                  </Stroke>
                </Mark>
                <Size>8</Size>
              </Graphic>
            </PointSymbolizer>"""

        sld_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" 
    xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc" 
    xmlns:xlink="http://www.w3.org/1999/xlink" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <NamedLayer>
    <Name>{style_name}</Name>
    <UserStyle>
      <Title>{style_name}</Title>
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
