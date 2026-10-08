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

    def style_exists(self, style_name: str) -> bool:
        clean_name = os.path.splitext(style_name)[0].replace('.', '_')
        res = self._request("GET", f"/rest/styles/{clean_name}.json")
        return res.status_code == 200

    def create_or_update_style(self, style_name: str, sld_xml: str) -> bool:
        clean_name = os.path.splitext(style_name)[0].replace('.', '_')
        if not self.style_exists(clean_name):
            # Create style stub
            create_res = self._request(
                "POST",
                "/rest/styles",
                headers={"Content-Type": "text/xml"},
                data=f"<style><name>{clean_name}</name><filename>{clean_name}.sld</filename></style>"
            )
            if create_res.status_code not in (200, 201):
                raise RuntimeError(f"Gagal membuat stub style: {create_res.text}")

        # Upload SLD body
        res = self._request(
            "PUT",
            f"/rest/styles/{clean_name}",
            headers={"Content-Type": "application/vnd.ogc.sld+xml"},
            data=sld_xml.encode("utf-8")
        )
        return res.status_code in (200, 201)

    def assign_style_to_layer(self, workspace_name: str, layer_name: str, style_name: str) -> bool:
        clean_style = os.path.splitext(style_name)[0].replace('.', '_')
        if not self.style_exists(clean_style):
            raise ValueError(f"Style '{clean_style}' tidak ditemukan di GeoServer.")

        # Workspace layer endpoint
        url = f"/rest/workspaces/{workspace_name}/layers/{layer_name}.json"
        body = {
            "layer": {
                "defaultStyle": {"name": clean_style}
            }
        }
        res = self._request("PUT", url, json=body)
        if res.status_code in (200, 201):
            return True

        # Fallback to global layer endpoint
        url_global = f"/rest/layers/{layer_name}.json"
        res_global = self._request("PUT", url_global, json=body)
        return res_global.status_code in (200, 201)

    # โ”€โ”€ WMS / WFS CAPABILITIES โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def get_wms_capabilities(self, workspace_name: Optional[str] = None) -> Dict[str, Any]:
        prefix = f"/{workspace_name}" if workspace_name else ""
        url = f"{prefix}/ows?service=WMS&version=1.3.0&request=GetCapabilities"
        res = self._request("GET", url)
        if res.status_code == 200:
            return xmltodict.parse(res.text)
        raise RuntimeError(f"Gagal mengambil WMS capabilities: {res.text}")

    def delete_style(self, style_name: str, workspace_name: Optional[str] = None, recurse: bool = True) -> bool:
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

geoserver_client = GeoServerClient()

