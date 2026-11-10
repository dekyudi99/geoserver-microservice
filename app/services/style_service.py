import html
import math
import os
import re
from typing import List, Dict, Any, Optional, Tuple
from ..clients.geoserver_client import geoserver_client
import logging

logger = logging.getLogger("geoserver_service.style")

class ApplyStyleResult(dict):
    """
    Hasil eksekusi apply_style.
    Berfungsi sebagai dict sekaligus boolean (True jika success=True, False jika success=False)
    agar kompatibel dengan pemanggil lama maupun router baru.
    """
    @property
    def success(self) -> bool:
        return bool(self.get("success", False))

    def __bool__(self) -> bool:
        return self.success

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
        style_type: 'values' (kategori diskrit), 'intervals' (rentang kelas), atau 'ramp' (gradien mulus).
        
        Catatan: Tipe 'ramp_exact' tidak didukung oleh spesifikasi OGC SLD 1.0.0 (OGC 02-070)
        maupun implementasi GeoServer ColorMap (GeoServer hanya mendukung 'ramp', 'intervals', 'values'),
        sehingga sengaja ditolak demi integritas SLD dan mencegah runtime error pada GeoServer.

        Semantik Batas Kelas SLD GeoServer vs UI AstraGIS:
        1. Semantik GeoServer 2.28+:
           Pada ColorMap type='intervals', GeoServer mengevaluasi setiap ColorMapEntry sebagai interval [Q_{i-1}, Q_i).
           Artinya: batas bawah adalah INKLUSIF (>= Q_{i-1}) dan batas atas adalah EKSKLUSIF (< Q_i).
        2. Semantik UI AstraGIS dan Legenda:
           UI mendefinisikan kelas i sebagai rentang (B_{i-1}, B_i] di mana batas atas adalah INKLUSIF (<= B_i)
           dan batas bawah adalah EKSKLUSIF (> B_{i-1}), kecuali kelas pertama yang mencakup nilai minimum raster.
        3. Mekanisme Penyesuaian (Infinitesimal Shift):
           Agar semantik WMS GetMap GeoServer cocok persis dengan UI:
           Untuk type='intervals', quantity pada SEMUA ColorMapEntry di XML dinaikkan secara infinitesimal
           menggunakan math.nextafter(q, math.inf).
           Dengan demikian, untuk piksel dengan nilai x == B_i, karena x < math.nextafter(B_i, math.inf),
           piksel x == B_i tepat masuk ke kelas i (bukan melompat ke kelas i+1 atau menjadi transparan pada max).
        4. Transparansi & Round-trip:
           SLD yang dihasilkan diberi penanda <Abstract>astra:upper_inclusive=1</Abstract> di dalam <UserStyle>.
           Parser (parse_raster_sld) mendeteksi penanda ini dan membalikkan penyesuaian dengan
           math.nextafter(q, -math.inf), sehingga nilai yang tersimpan di symbology/API dan dibaca kembali
           selalu merupakan nilai batas asli tanpa pergeseran angka. Jika penanda tidak ada, nilai dikembalikan
           apa adanya dan boundary_semantics ditandai sebagai 'unknown'.
        """
        if not color_entries:
            raise ValueError("color_entries tidak boleh kosong.")
        if len(color_entries) > 256:
            raise ValueError("Style raster maksimal memiliki 256 kelas.")

        if style_type not in {"intervals", "values", "ramp"}:
            raise ValueError(f"style_type raster '{style_type}' tidak didukung. Harus salah satu dari: intervals, values, ramp.")

        entries_xml = ""
        safe_style_name = html.escape(str(style_name), quote=True)

        prev_quantity = None
        seen_values = set()

        for idx, entry in enumerate(color_entries):
            c = entry.get("color")
            if not isinstance(c, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
                raise ValueError(f"Entri ke-{idx} memiliki warna yang tidak valid: '{c}'. Harus berupa HEX #RRGGBB.")

            q = entry.get("quantity")
            if q is None:
                raise ValueError(f"Entri ke-{idx} tidak memiliki atribut 'quantity'.")
            try:
                numeric_quantity = float(q)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Entri ke-{idx} memiliki quantity yang bukan angka: {q}.") from exc

            if not math.isfinite(numeric_quantity):
                raise ValueError(f"Entri ke-{idx} memiliki quantity yang tidak valid (NaN atau inf).")

            op = entry.get("opacity", 1.0)
            try:
                numeric_opacity = float(op)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Entri ke-{idx} memiliki opacity yang bukan angka: {op}.") from exc

            if not math.isfinite(numeric_opacity) or not 0.0 <= numeric_opacity <= 1.0:
                raise ValueError(f"Entri ke-{idx} memiliki opacity di luar rentang 0.0 - 1.0: {op}.")

            if style_type in {"intervals", "ramp"}:
                if prev_quantity is not None and numeric_quantity <= prev_quantity:
                    raise ValueError(
                        f"Entri ke-{idx} memiliki quantity ({numeric_quantity}) yang tidak naik ketat dibanding entri sebelumnya ({prev_quantity})."
                    )
                prev_quantity = numeric_quantity
            elif style_type == "values":
                if numeric_quantity in seen_values:
                    raise ValueError(f"Entri ke-{idx} memiliki quantity duplikat: {numeric_quantity}.")
                seen_values.add(numeric_quantity)

            lbl = html.escape(str(entry.get("label", f"Class {numeric_quantity}")), quote=True)

            # Semantik SLD GeoServer type='intervals':
            # GeoServer mengimplementasikan interval sebagai [Q_{i-1}, Q_i) di mana batas atas adalah eksklusif (< Q_i).
            # Untuk menyesuaikan dengan semantik UI AstraGIS (<= batas atas), quantity pada SEMUA entri intervals
            # dinaikkan secara infinitesimal (math.nextafter(q, math.inf)).
            xml_quantity = numeric_quantity
            if style_type == "intervals":
                xml_quantity = math.nextafter(numeric_quantity, math.inf)

            q_str = repr(xml_quantity)
            entries_xml += f'              <ColorMapEntry color="{c}" quantity="{q_str}" opacity="{numeric_opacity}" label="{lbl}"/>\n'

        abstract_xml = ""
        if style_type == "intervals":
            abstract_xml = "      <Abstract>astra:upper_inclusive=1</Abstract>\n"

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
{abstract_xml}      <FeatureTypeStyle>
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

    def parse_raster_sld(self, sld_xml: str) -> Optional[Dict[str, Any]]:
        """
        Parse SLD XML to extract Raster ColorMap and ColorMapEntry items.
        Returns dict with style_type, entries list, upper_inclusive boolean, and boundary_semantics.
        """
        if not sld_xml:
            return None
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(sld_xml)
            style_type = "intervals"
            raw_entries = []
            upper_inclusive = False

            for elem in root.iter():
                tag = elem.tag.split("}")[-1]
                if tag == "Abstract" and elem.text and "astra:upper_inclusive=1" in elem.text:
                    upper_inclusive = True
                elif tag == "ColorMap":
                    style_type = elem.attrib.get("type", "intervals")
                elif tag == "ColorMapEntry":
                    c = elem.attrib.get("color")
                    if c:
                        try:
                            q = float(elem.attrib.get("quantity", 0))
                        except (ValueError, TypeError):
                            q = 0.0
                        try:
                            op = float(elem.attrib.get("opacity", 1.0))
                        except (ValueError, TypeError):
                            op = 1.0
                        lbl = elem.attrib.get("label", "")
                        raw_entries.append({
                            "color": c,
                            "quantity": q,
                            "opacity": op,
                            "label": lbl
                        })

            if raw_entries:
                if style_type == "intervals" and upper_inclusive:
                    for entry in raw_entries:
                        entry["quantity"] = math.nextafter(entry["quantity"], -math.inf)

                return {
                    "style_type": style_type,
                    "entries": raw_entries,
                    "upper_inclusive": upper_inclusive,
                    "boundary_semantics": "upper_inclusive" if upper_inclusive else "unknown"
                }
        except Exception as e:
            logger.warning(f"Error parsing raster SLD XML: {e}")
        return None

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
        icon_name: Optional[str] = None,
        icon_url: Optional[str] = None,
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

        if geometry_family == "polygon":
            symbolizer_xml = f"""            <PolygonSymbolizer>
              <Fill>
                {fill_xml}
              </Fill>
              <Stroke>
                <CssParameter name="stroke">{stroke_color}</CssParameter>
                <CssParameter name="stroke-width">{stroke_width}</CssParameter>
                <CssParameter name="stroke-opacity">{stroke_opacity}</CssParameter>{dash_xml}
              </Stroke>
            </PolygonSymbolizer>"""
        elif geometry_family == "line":
            symbolizer_xml = f"""            <LineSymbolizer>
              <Stroke>
                <CssParameter name="stroke">{stroke_color}</CssParameter>
                <CssParameter name="stroke-width">{stroke_width}</CssParameter>
                <CssParameter name="stroke-opacity">{stroke_opacity}</CssParameter>{dash_xml}
              </Stroke>
            </LineSymbolizer>"""
        else:
            actual_icon = icon_name
            if not actual_icon and mark and str(mark).startswith("icon:"):
                actual_icon = str(mark)[5:]

            if actual_icon:
                safe_icon = os.path.basename(actual_icon)
                # Di GeoServer, ExternalGraphic harus menggunakan relative path ke styles folder (misal "water-svgrepo-com.svg")
                # Jika menggunakan URL HTTP, Batik SVG engine GeoServer akan memblokir request karena policy keamanan dan fallback ke kotak hitam/abu-abu.
                safe_href = html.escape(safe_icon, quote=True)
                symbolizer_xml = f"""            <PointSymbolizer>
              <Graphic>
                <ExternalGraphic>
                  <OnlineResource xlink:type="simple" xlink:href="{safe_href}"/>
                  <Format>image/svg+xml</Format>
                </ExternalGraphic>
                <Size>{point_size}</Size>
              </Graphic>
            </PointSymbolizer>"""
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
                    <CssParameter name="stroke-opacity">{stroke_opacity}</CssParameter>{dash_xml}
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

    def compute_raster_statistics(self, file_path: str):
        from .raster_stats_service import compute_raster_stats
        try:
            return compute_raster_stats(file_path), None
        except Exception as exc:
            return None, str(exc)

    def classify_raster(self, file_path: str, method: str, classes: int):
        from .raster_stats_service import classify
        res = classify(file_path, method, classes)
        return res["breaks"], res["effective_classes"], res["notice"]

    def apply_style(
        self,
        workspace: str,
        layer_name: str,
        style_name: str,
        sld_xml: Optional[str] = None
    ) -> ApplyStyleResult:
        """
        Menerapkan style ke layer di GeoServer pada scope workspace dengan mekanisme rollback.
        
        Alur:
        1. Cek apakah style sudah ada di workspace layer. Jika ada, simpan SLD lamanya untuk rollback.
        2. Jika sld_xml diberikan, buat atau perbarui style di workspace layer tersebut.
        3. Assign style ke layer. Jika assign gagal, lakukan rollback:
           - Kembalikan ke SLD lama jika style sudah ada sebelumnya.
           - Hapus style jika sebelumnya style belum ada.
        4. Jika sukses, panggil truncate_gwc_layer untuk membersihkan cache GeoWebCache layer tersebut.
        5. Mengembalikan ApplyStyleResult (dict yang juga mengevaluasi truthiness boolean).
        """
        clean_style = os.path.splitext(style_name)[0].replace('.', '_')
        existed_before = False
        old_sld = None

        if sld_xml:
            try:
                existed_before = geoserver_client.style_exists(clean_style, workspace_name=workspace)
                if existed_before:
                    old_sld = geoserver_client.get_style(clean_style, workspace_name=workspace)
            except Exception as e:
                logger.warning(f"Gagal memeriksa status style lama '{clean_style}' di workspace '{workspace}': {e}")

            try:
                sld_ok = geoserver_client.create_or_update_style(
                    clean_style,
                    sld_xml,
                    workspace_name=workspace
                )
                if not sld_ok:
                    return ApplyStyleResult({
                        "success": False,
                        "failed_stage": "CREATE_OR_UPDATE_STYLE",
                        "message": f"GeoServer gagal menyimpan SLD untuk style '{clean_style}' di workspace '{workspace}'."
                    })
            except Exception as exc:
                logger.exception(f"Error saat create_or_update_style '{clean_style}' di workspace '{workspace}': {exc}")
                return ApplyStyleResult({
                    "success": False,
                    "failed_stage": "CREATE_OR_UPDATE_STYLE",
                    "message": f"Gagal membuat atau memperbarui style '{clean_style}': {exc}"
                })

        try:
            assigned = geoserver_client.assign_style_to_layer(workspace, layer_name, clean_style)
        except Exception as exc:
            assigned = False
            assign_err_msg = str(exc)
        else:
            assign_err_msg = "GeoServer mengembalikan status gagal saat assign style ke layer."

        if not assigned:
            logger.warning(f"Assign style '{clean_style}' ke layer '{layer_name}' gagal. Memulai rollback...")
            # Mekanisme Rollback:
            if sld_xml:
                try:
                    if existed_before and old_sld:
                        geoserver_client.create_or_update_style(clean_style, old_sld, workspace_name=workspace)
                        logger.info(f"Rollback berhasil: style '{clean_style}' dikembalikan ke SLD sebelumnya.")
                    elif not existed_before:
                        geoserver_client.delete_style(clean_style, workspace_name=workspace, recurse=False)
                        logger.info(f"Rollback berhasil: style baru '{clean_style}' dihapus dari workspace '{workspace}'.")
                except Exception as rb_exc:
                    logger.exception(f"Rollback style '{clean_style}' gagal: {rb_exc}")

            return ApplyStyleResult({
                "success": False,
                "failed_stage": "ASSIGN_STYLE",
                "message": f"Gagal menetapkan style '{clean_style}' pada layer '{layer_name}': {assign_err_msg}"
            })

        # Bersihkan tile cache GeoWebCache
        try:
            geoserver_client.truncate_gwc_layer(workspace, layer_name)
        except Exception as gwc_err:
            logger.warning(f"Gagal membersihkan cache GeoWebCache layer '{workspace}:{layer_name}': {gwc_err}")

        return ApplyStyleResult({
            "success": True,
            "failed_stage": None,
            "message": f"Style '{clean_style}' berhasil diaplikasikan ke layer '{layer_name}' di workspace '{workspace}'."
        })

style_service = StyleService()
