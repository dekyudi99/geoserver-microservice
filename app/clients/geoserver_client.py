import os
import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry
from typing import Dict, Any, List, Optional
import xmltodict
import logging
from ..config.settings import settings

logger = logging.getLogger("geoserver_service.client")

class GeoServerClient:
    """
    Unified client for GeoServer REST API & OGC Services.
    Follows DRY principles to eliminate repeated HTTP requests and headers.
    """

    def __init__(self, base_url: Optional[str] = None, user: Optional[str] = None, password: Optional[str] = None):
        self.base_url = (base_url or settings.GEOSERVER_URL).rstrip("/")
        self.user = user or settings.GEOSERVER_USER
        self.password = password or settings.GEOSERVER_PASS
        self.auth = HTTPBasicAuth(self.user, self.password)
        self.session = requests.Session()
        self.session.auth = self.auth
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=Retry(total=2, backoff_factor=0.3, allowed_methods=frozenset(["GET"]), status_forcelist=(502, 503, 504)),
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = self._url(path)
        kwargs.setdefault("timeout", 30)
        try:
            resp = self.session.request(method, url, **kwargs)
            return resp
        except requests.RequestException as e:
            logger.error(f"GeoServer request failed [{method} {url}]: {e}")
            raise

    @staticmethod
    def _extract_list(data: Any, parent_key: str, child_key: str) -> List[Dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        parent = data.get(parent_key)
        if not isinstance(parent, dict):
            return []
        items = parent.get(child_key, [])
        if isinstance(items, dict):
            return [items]
        if isinstance(items, list):
            return items
        return []

    # โ”€โ”€ WORKSPACE OPERATIONS โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def get_workspaces(self) -> List[Dict[str, Any]]:
        res = self._request("GET", "/rest/workspaces.json")
        if res.status_code == 200:
            try:
                return self._extract_list(res.json(), "workspaces", "workspace")
            except Exception as e:
                logger.warning(f"Error parsing workspaces json: {e}")
                return []
        return []

    def get_workspace(self, workspace_name: str) -> Optional[Dict[str, Any]]:
        res = self._request("GET", f"/rest/workspaces/{workspace_name}.json")
        if res.status_code == 200:
            return res.json().get("workspace")
        return None

    def create_workspace(self, workspace_name: str) -> bool:
        if self.get_workspace(workspace_name):
            return True
        payload = f"<workspace><name>{workspace_name}</name></workspace>"
        res = self._request(
            "POST",
            "/rest/workspaces",
            headers={"Content-Type": "text/xml"},
            data=payload
        )
        return res.status_code in (200, 201)

    def delete_workspace(self, workspace_name: str, recurse: bool = True) -> bool:
        res = self._request(
            "DELETE",
            f"/rest/workspaces/{workspace_name}?recurse={str(recurse).lower()}"
        )
        return res.status_code in (200, 204, 404)

    # โ”€โ”€ DATASTORE OPERATIONS (VECTOR) โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def get_datastores(self, workspace_name: Optional[str] = None) -> List[Dict[str, Any]]:
        path = f"/rest/workspaces/{workspace_name}/datastores.json" if workspace_name else "/rest/datastores.json"
        res = self._request("GET", path)
        if res.status_code == 200:
            try:
                return self._extract_list(res.json(), "dataStores", "dataStore")
            except Exception as e:
                logger.warning(f"Error parsing datastores json: {e}")
                return []
        return []

    def get_datastore(self, workspace_name: str, store_name: str) -> Optional[Dict[str, Any]]:
        res = self._request("GET", f"/rest/workspaces/{workspace_name}/datastores/{store_name}.json")
        if res.status_code == 200:
            return res.json().get("dataStore")
        return None

    def ensure_postgis_datastore(
        self,
        workspace_name: str,
        store_name: str = "postgis_store",
        host: Optional[str] = None,
        port: Optional[int] = None,
        db_name: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        schema: str = "public"
    ) -> bool:
        """Membuat datastore PostGIS di workspace GeoServer jika belum ada."""
        if self.get_datastore(workspace_name, store_name):
            return True

        h = host or settings.POSTGIS_HOST
        p = port or settings.POSTGIS_PORT
        d = db_name or settings.POSTGIS_DB
        u = user or settings.POSTGIS_USER
        pwd = password or settings.POSTGIS_PASSWORD

        xml_payload = f"""<dataStore>
  <name>{store_name}</name>
  <connectionParameters>
    <entry key="host">{h}</entry>
    <entry key="port">{p}</entry>
    <entry key="database">{d}</entry>
    <entry key="user">{u}</entry>
    <entry key="passwd">{pwd}</entry>
    <entry key="dbtype">postgis</entry>
    <entry key="schema">{schema}</entry>
    <entry key="Estimated extents">true</entry>
    <entry key="Loose bbox">true</entry>
    <entry key="preparedStatements">true</entry>
    <entry key="fetch size">1000</entry>
    <entry key="min connections">2</entry>
    <entry key="max connections">20</entry>
  </connectionParameters>
</dataStore>"""
        res = self._request(
            "POST",
            f"/rest/workspaces/{workspace_name}/datastores",
            headers={"Content-Type": "text/xml"},
            data=xml_payload
        )
        return res.status_code in (200, 201)

    # โ”€โ”€ COVERAGE STORE OPERATIONS (RASTER) โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def get_coverage_stores(self, workspace_name: Optional[str] = None) -> List[Dict[str, Any]]:
        path = f"/rest/workspaces/{workspace_name}/coveragestores.json" if workspace_name else "/rest/coveragestores.json"
        res = self._request("GET", path)
        if res.status_code == 200:
            try:
                return self._extract_list(res.json(), "coverageStores", "coverageStore")
            except Exception as e:
                logger.warning(f"Error parsing coveragestores json: {e}")
                return []
        return []

    def get_coverage_store(self, workspace_name: str, store_name: str) -> Optional[Dict[str, Any]]:
        res = self._request("GET", f"/rest/workspaces/{workspace_name}/coveragestores/{store_name}.json")
        if res.status_code == 200:
            return res.json().get("coverageStore")
        return None

    def create_coverage_store(self, workspace_name: str, store_name: str, file_path: str) -> bool:
        """Membuat coverage store GeoTIFF dan mempublikasikannya."""
        # GeoServer REST external.geotiff expects absolute filesystem path in body
        clean_path = file_path.replace("file://", "").replace("file:", "")
        url = f"/rest/workspaces/{workspace_name}/coveragestores/{store_name}/external.geotiff?configure=first&coverageName={store_name}"
        res = self._request(
            "PUT",
            url,
            headers={"Content-Type": "text/plain"},
            data=clean_path
        )
        return res.status_code in (200, 201)

    def delete_coverage_store(self, workspace_name: str, store_name: str, recurse: bool = True) -> bool:
        res = self._request(
            "DELETE",
            f"/rest/workspaces/{workspace_name}/coveragestores/{store_name}?recurse={str(recurse).lower()}"
        )
        return res.status_code in (200, 204, 404)

    # โ”€โ”€ FEATURE TYPES & PUBLISH โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def publish_postgis_feature_type(
        self,
        workspace_name: str,
        store_name: str,
        table_name: str,
        title: Optional[str] = None,
        srid: int = 4326,
        bbox: Optional[List[float]] = None
    ) -> bool:
        """Mempublikasikan tabel PostGIS sebagai FeatureType Layer di GeoServer.

        bbox ([minx, miny, maxx, maxy], sama dengan srid) opsional; bila diberikan,
        GeoServer tidak perlu menghitung extent sendiri.
        """
        bbox_xml = ""
        if bbox:
            minx, miny, maxx, maxy = bbox
            box = f"<minx>{minx}</minx><maxx>{maxx}</maxx><miny>{miny}</miny><maxy>{maxy}</maxy><crs>EPSG:{srid}</crs>"
            bbox_xml = f"\n  <nativeBoundingBox>{box}</nativeBoundingBox>\n  <latLonBoundingBox>{box}</latLonBoundingBox>"
        xml_payload = f"""<featureType>
  <name>{table_name}</name>
  <nativeName>{table_name}</nativeName>
  <title>{title or table_name}</title>
  <srs>EPSG:{srid}</srs>
  <nativeCRS>EPSG:{srid}</nativeCRS>
  <enabled>true</enabled>{bbox_xml}
</featureType>"""
        res = self._request(
            "POST",
            f"/rest/workspaces/{workspace_name}/datastores/{store_name}/featuretypes",
            headers={"Content-Type": "text/xml"},
            data=xml_payload
        )
        return res.status_code in (200, 201)

    def delete_layer(self, workspace_name: str, layer_name: str, recurse: bool = True) -> bool:
        res = self._request(
            "DELETE",
            f"/rest/workspaces/{workspace_name}/layers/{layer_name}?recurse={str(recurse).lower()}"
        )
        return res.status_code in (200, 204, 404)

    def delete_feature_type(self, workspace_name: str, store_name: str, feature_type_name: str, recurse: bool = True) -> bool:
        """Menghapus FeatureType dari DataStore GeoServer (vector layer resource)."""
        res = self._request(
            "DELETE",
            f"/rest/workspaces/{workspace_name}/datastores/{store_name}/featuretypes/{feature_type_name}?recurse={str(recurse).lower()}"
        )
        return res.status_code in (200, 204, 404)

    # โ”€โ”€ STYLES & SLD โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def style_exists(self, style_name: str, workspace_name: Optional[str] = None) -> bool:
        clean_name = os.path.splitext(style_name)[0].replace('.', '_')
        path = f"/rest/workspaces/{workspace_name}/styles/{clean_name}.json" if workspace_name else f"/rest/styles/{clean_name}.json"
        res = self._request("GET", path)
        return res.status_code == 200

    def get_style(self, style_name: str, workspace_name: Optional[str] = None) -> Optional[str]:
        """Mengambil SLD XML untuk style tertentu (workspace-level atau global)."""
        clean_name = os.path.splitext(style_name)[0].replace('.', '_')
        if workspace_name:
            path = f"/rest/workspaces/{workspace_name}/styles/{clean_name}.sld"
            res = self._request("GET", path)
            if res.status_code == 200:
                return res.text
        path_global = f"/rest/styles/{clean_name}.sld"
        res_global = self._request("GET", path_global)
        if res_global.status_code == 200:
            return res_global.text
        return None

    def create_or_update_style(self, style_name: str, sld_xml: str, workspace_name: Optional[str] = None) -> bool:
        clean_name = os.path.splitext(style_name)[0].replace('.', '_')
        prefix = f"/rest/workspaces/{workspace_name}" if workspace_name else "/rest"

        if not self.style_exists(clean_name, workspace_name=workspace_name):
            create_res = self._request(
                "POST",
                f"{prefix}/styles",
                headers={"Content-Type": "text/xml"},
                data=f"<style><name>{clean_name}</name><filename>{clean_name}.sld</filename></style>"
            )
            if create_res.status_code not in (200, 201):
                raise RuntimeError(f"Gagal membuat stub style '{clean_name}': {create_res.text}")

        res = self._request(
            "PUT",
            f"{prefix}/styles/{clean_name}",
            headers={"Content-Type": "application/vnd.ogc.sld+xml"},
            data=sld_xml.encode("utf-8")
        )
        if res.status_code not in (200, 201):
            logger.error(f"GeoServer failed to save SLD body for style '{clean_name}': HTTP {res.status_code} - {res.text}")
            return False
        return True

    def assign_style_to_layer(self, workspace_name: str, layer_name: str, style_name: str) -> bool:
        clean_style = os.path.splitext(style_name)[0].replace('.', '_')
        if not self.style_exists(clean_style, workspace_name=workspace_name) and not self.style_exists(clean_style):
            raise ValueError(f"Style '{clean_style}' tidak ditemukan di GeoServer.")

        url = f"/rest/workspaces/{workspace_name}/layers/{layer_name}.json"
        body_ws = {
            "layer": {
                "defaultStyle": {
                    "name": clean_style,
                    "workspace": workspace_name
                }
            }
        }
        res_ws = self._request("PUT", url, json=body_ws)
        if res_ws.status_code in (200, 201):
            return True

        body_simple = {
            "layer": {
                "defaultStyle": {"name": clean_style}
            }
        }
        res = self._request("PUT", url, json=body_simple)
        if res.status_code in (200, 201):
            return True

        url_global = f"/rest/layers/{layer_name}.json"
        res_global = self._request("PUT", url_global, json=body_simple)
        return res_global.status_code in (200, 201)

    def truncate_gwc_layer(self, workspace_name: str, layer_name: str) -> bool:
        """Membersihkan tile cache GeoWebCache (GWC) untuk layer tertentu bila ter-cache."""
        layer_qualified = f"{workspace_name}:{layer_name}"
        try:
            check_res = self._request("GET", f"/gwc/rest/layers/{layer_qualified}.xml")
            if check_res.status_code != 200:
                return True
            payload = f"<truncateLayer><layerName>{layer_qualified}</layerName></truncateLayer>"
            res = self._request(
                "POST",
                "/gwc/rest/masstruncate",
                headers={"Content-Type": "text/xml"},
                data=payload
            )
            return res.status_code in (200, 204)
        except Exception as exc:
            logger.warning(f"Gagal truncate GWC layer '{layer_qualified}': {exc}")
            return False

    def ensure_gwc_layer(
        self,
        workspace_name: str,
        layer_name: str,
        gridset: str = "EPSG:900913",
        fmt: str = "image/png"
    ) -> bool:
        """
        Memastikan layer terdaftar dan aktif di GeoWebCache dengan gridset dan format yang ditentukan.
        1. GET /gwc/rest/layers/{layer_qualified}.xml.
        2. Jika 200, pastikan gridset dan format ada. Jika sudah ada, return True.
        3. Jika belum ada atau gridset belum lengkap, PUT /gwc/rest/layers/{layer_qualified}.xml dengan XML GeoServerLayer.
        4. Return True jika 200/201. Tangkap exception, log warning, return False.
        """
        layer_qualified = f"{workspace_name}:{layer_name}"
        try:
            check_res = self._request("GET", f"/gwc/rest/layers/{layer_qualified}.xml")
            if check_res.status_code == 200:
                text_xml = check_res.text or ""
                if f"<gridSetName>{gridset}</gridSetName>" in text_xml and f"<string>{fmt}</string>" in text_xml:
                    return True

            put_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<GeoServerLayer>
  <enabled>true</enabled>
  <inMemoryCached>true</inMemoryCached>
  <name>{layer_qualified}</name>
  <mimeFormats>
    <string>{fmt}</string>
    <string>image/jpeg</string>
  </mimeFormats>
  <gridSubsets>
    <gridSubset>
      <gridSetName>{gridset}</gridSetName>
    </gridSubset>
  </gridSubsets>
  <metaWidthHeight>
    <int>4</int>
    <int>4</int>
  </metaWidthHeight>
  <expireCache>0</expireCache>
  <expireClients>0</expireClients>
</GeoServerLayer>"""
            put_res = self._request(
                "PUT",
                f"/gwc/rest/layers/{layer_qualified}.xml",
                headers={"Content-Type": "text/xml"},
                data=put_payload
            )
            return put_res.status_code in (200, 201)
        except Exception as exc:
            logger.warning(f"Gagal memastikan GWC layer '{layer_qualified}': {exc}")
            return False

    def delete_gwc_layer(self, workspace_name: str, layer_name: str) -> bool:
        """
        Menghapus konfigurasi layer dari GeoWebCache (best effort).
        DELETE /gwc/rest/layers/{layer_qualified}.xml
        Tidak boleh melempar error agar penghapusan layer utama tidak gagal.
        """
        layer_qualified = f"{workspace_name}:{layer_name}"
        try:
            res = self._request("DELETE", f"/gwc/rest/layers/{layer_qualified}.xml")
            return res.status_code in (200, 204, 404)
        except Exception as exc:
            logger.warning(f"Gagal menghapus GWC layer '{layer_qualified}': {exc}")
            return False

    def probe_gwc_tile(
        self,
        workspace_name: str,
        layer_name: str,
        z: int = 10,
        x: int = 0,
        y: int = 0,
        gridset: str = "EPSG:900913",
        fmt: str = "image/png"
    ) -> bool:
        """
        Melakukan satu probe GetTile ke GeoWebCache untuk memverifikasi kesiapan cache.
        Mengembalikan True hanya jika respon HTTP 200 dan Content-Type berupa image/*.
        """
        layer_qualified = f"{workspace_name}:{layer_name}"
        path = (
            f"/gwc/service/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
            f"&LAYER={layer_qualified}&STYLE=&TILEMATRIXSET={gridset}&FORMAT={fmt}"
            f"&TILEMATRIX={gridset}:{z}&TILEROW={y}&TILECOL={x}"
        )
        try:
            res = self._request("GET", path)
            content_type = res.headers.get("Content-Type", "")
            return res.status_code == 200 and "image" in content_type
        except Exception as exc:
            logger.warning(f"Probe GWC tile gagal untuk '{layer_qualified}': {exc}")
            return False

    # โ”€โ”€ WMS / WFS CAPABILITIES โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def get_wms_capabilities(self, workspace_name: Optional[str] = None) -> Dict[str, Any]:
        prefix = f"/{workspace_name}" if workspace_name else ""
        url = f"{prefix}/ows?service=WMS&version=1.3.0&request=GetCapabilities"
        res = self._request("GET", url)
        if res.status_code == 200:
            return xmltodict.parse(res.text)
        raise RuntimeError(f"Gagal mengambil WMS capabilities: {res.text}")

    def delete_style(self, style_name: str, workspace_name: Optional[str] = None, recurse: bool = False) -> bool:
        if workspace_name:
            path = f"rest/workspaces/{workspace_name}/styles/{style_name}?recurse={str(recurse).lower()}"
        else:
            path = f"rest/styles/{style_name}?recurse={str(recurse).lower()}"
        res = self._request("DELETE", path)
        return res.status_code in (200, 204, 404)

    def get_layer(self, layer_name: str, workspace_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if workspace_name:
            path = f"rest/workspaces/{workspace_name}/layers/{layer_name}.json"
        else:
            path = f"rest/layers/{layer_name}.json"
        res = self._request("GET", path)
        if res.status_code == 200:
            return res.json().get("layer", {})
        return None

    def get_layers(self, workspace_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Mengambil daftar layer dari GeoServer REST API.
        Jika workspace_name diisi, mengambil layer khusus untuk workspace tersebut:
        GET /rest/workspaces/{workspace_name}/layers.json
        Jika tidak diisi, mengambil seluruh layer global:
        GET /rest/layers.json
        """
        path = f"/rest/workspaces/{workspace_name}/layers.json" if workspace_name else "/rest/layers.json"
        res = self._request("GET", path)
        if res.status_code == 200:
            try:
                return self._extract_list(res.json(), "layers", "layer")
            except Exception as e:
                logger.warning(f"Error parsing layers json for workspace '{workspace_name}': {e}")
                return []
        return []


    def get_version(self) -> Dict[str, Any]:
        res = self._request("GET", "rest/about/version.json")
        if res.status_code == 200:
            return res.json()
        return {"version": "2.28.2"}

    def get_status(self) -> Dict[str, Any]:
        res = self._request("GET", "rest/about/status.json")
        if res.status_code == 200:
            return res.json()
        return {"status": "available"}

    def create_or_update_layer_group(
        self,
        workspace_name: str,
        group_name: str,
        title: str,
        mode: str,
        layer_names: list
    ) -> bool:
        """Membuat atau memperbarui layer group di GeoServer."""
        clean_name = group_name.replace(" ", "_")
        check_res = self._request("GET", f"/rest/workspaces/{workspace_name}/layergroups/{clean_name}.json")
        exists = check_res.status_code == 200

        payload = {
            "layerGroup": {
                "name": clean_name,
                "mode": mode.upper() if mode else "SINGLE",
                "title": title or clean_name,
                "publishables": {
                    "published": [{"@type": "layer", "name": str(l)} for l in layer_names]
                }
            }
        }

        if exists:
            res = self._request(
                "PUT",
                f"/rest/workspaces/{workspace_name}/layergroups/{clean_name}",
                json=payload
            )
        else:
            res = self._request(
                "POST",
                f"/rest/workspaces/{workspace_name}/layergroups",
                json=payload
            )
        return res.status_code in (200, 201)

    def delete_layer_group(self, workspace_name: str, group_name: str) -> bool:
        """Menghapus layer group dari GeoServer."""
        clean_name = group_name.replace(" ", "_")
        res = self._request(
            "DELETE",
            f"/rest/workspaces/{workspace_name}/layergroups/{clean_name}"
        )
        return res.status_code in (200, 204, 404)

    @staticmethod
    def _parse_ows_exception(xml_text: str) -> str:
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)
            for elem in root.iter():
                if "ExceptionText" in elem.tag or "ServiceException" in elem.tag:
                    if elem.text:
                        return elem.text.strip()
            return xml_text[:300].strip()
        except Exception:
            return xml_text[:300].strip()

    @staticmethod
    def _parse_hits_number(xml_text: str) -> int:
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)
            num = root.attrib.get("numberMatched")
            if num is not None and num != "unknown":
                return int(num)
            num_ret = root.attrib.get("numberReturned")
            if num_ret is not None and num_ret != "unknown":
                return int(num_ret)
            return 0
        except Exception:
            return 0

    def describe_feature_type(self, workspace_name: str, layer_name: str) -> Dict[str, Any]:
        """Mengambil skema DescribeFeatureType WFS 2.0.0 dalam format JSON."""
        path = f"{workspace_name}/wfs"
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "DescribeFeatureType",
            "typeName": f"{workspace_name}:{layer_name}",
            "outputFormat": "application/json"
        }
        res = self._request("GET", path, params=params)
        if res.status_code != 200:
            err = self._parse_ows_exception(res.text)
            raise ValueError(f"GeoServer DescribeFeatureType error ({res.status_code}): {err}")
        return res.json()

    def get_features(self, workspace_name: str, layer_name: str, **params) -> Dict[str, Any]:
        """Memanggil WFS 2.0.0 GetFeature untuk layer tertentu."""
        path = f"{workspace_name}/wfs"
        query_params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": f"{workspace_name}:{layer_name}",
            "outputFormat": "application/json"
        }
        query_params.update(params)
        res = self._request("GET", path, params=query_params)
        if res.status_code != 200:
            err = self._parse_ows_exception(res.text)
            raise ValueError(f"GeoServer GetFeature error ({res.status_code}): {err}")

        if params.get("resultType") == "hits":
            return {"xml": res.text, "numberMatched": self._parse_hits_number(res.text)}

        try:
            return res.json()
        except Exception:
            return {"raw": res.text}

geoserver_client = GeoServerClient()

