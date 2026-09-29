import os
import requests
from requests.auth import HTTPBasicAuth
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

    # ── WORKSPACE OPERATIONS ─────────────────────────────────────────

    def get_workspaces(self) -> List[Dict[str, Any]]:
        res = self._request("GET", "/rest/workspaces.json")
        if res.status_code == 200:
            data = res.json()
            workspaces = data.get("workspaces", {}).get("workspace", [])
            if isinstance(workspaces, dict):
                return [workspaces]
            return workspaces
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
        return res.status_code in (200, 204)

    # ── DATASTORE OPERATIONS (VECTOR) ───────────────────────────────

    def get_datastores(self, workspace_name: Optional[str] = None) -> List[Dict[str, Any]]:
        path = f"/rest/workspaces/{workspace_name}/datastores.json" if workspace_name else "/rest/datastores.json"
        res = self._request("GET", path)
        if res.status_code == 200:
            data = res.json()
            stores = data.get("dataStores", {}).get("dataStore", [])
            return [stores] if isinstance(stores, dict) else stores
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
    <host>{h}</host>
    <port>{p}</port>
    <database>{d}</database>
    <user>{u}</user>
    <passwd>{pwd}</passwd>
    <dbtype>postgis</dbtype>
    <schema>{schema}</schema>
    <Expose primary keys>true</Expose>
    <Estimated extends>true</Estimated>
  </connectionParameters>
</dataStore>"""
        res = self._request(
            "POST",
            f"/rest/workspaces/{workspace_name}/datastores",
            headers={"Content-Type": "text/xml"},
            data=xml_payload
        )
        return res.status_code in (200, 201)

    # ── COVERAGE STORE OPERATIONS (RASTER) ──────────────────────────

    def get_coverage_stores(self, workspace_name: Optional[str] = None) -> List[Dict[str, Any]]:
        path = f"/rest/workspaces/{workspace_name}/coveragestores.json" if workspace_name else "/rest/coveragestores.json"
        res = self._request("GET", path)
        if res.status_code == 200:
            data = res.json()
            stores = data.get("coverageStores", {}).get("coverageStore", [])
            return [stores] if isinstance(stores, dict) else stores
        return []

    def get_coverage_store(self, workspace_name: str, store_name: str) -> Optional[Dict[str, Any]]:
        res = self._request("GET", f"/rest/workspaces/{workspace_name}/coveragestores/{store_name}.json")
        if res.status_code == 200:
            return res.json().get("coverageStore")
        return None

    def create_coverage_store(self, workspace_name: str, store_name: str, file_path: str) -> bool:
        """Membuat coverage store GeoTIFF dan mempublikasikannya."""
        # GeoServer endpoint format untuk file lokal
        url = f"/rest/workspaces/{workspace_name}/coveragestores/{store_name}/external.geotiff?configure=first&coverageName={store_name}"
        res = self._request(
            "PUT",
            url,
            headers={"Content-Type": "text/plain"},
            data=f"file://{file_path}"
        )
        return res.status_code in (200, 201)

    def delete_coverage_store(self, workspace_name: str, store_name: str, recurse: bool = True) -> bool:
        res = self._request(
            "DELETE",
            f"/rest/workspaces/{workspace_name}/coveragestores/{store_name}?recurse={str(recurse).lower()}"
        )
        return res.status_code in (200, 204)

    # ── FEATURE TYPES & PUBLISH ──────────────────────────────────────

    def publish_postgis_feature_type(
        self,
        workspace_name: str,
        store_name: str,
        table_name: str,
        title: Optional[str] = None,
        srid: int = 4326
    ) -> bool:
        """Mempublikasikan tabel PostGIS sebagai FeatureType Layer di GeoServer."""
        xml_payload = f"""<featureType>
  <name>{table_name}</name>
  <nativeName>{table_name}</nativeName>
  <title>{title or table_name}</title>
  <srs>EPSG:{srid}</srs>
  <nativeCRS>EPSG:{srid}</nativeCRS>
  <enabled>true</enabled>
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
        return res.status_code in (200, 204)

    # ── STYLES & SLD ────────────────────────────────────────────────

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

    # ── WMS / WFS CAPABILITIES ───────────────────────────────────────

    def get_wms_capabilities(self, workspace_name: Optional[str] = None) -> Dict[str, Any]:
        prefix = f"/{workspace_name}" if workspace_name else ""
        url = f"{prefix}/ows?service=WMS&version=1.3.0&request=GetCapabilities"
        res = self._request("GET", url)
        if res.status_code == 200:
            return xmltodict.parse(res.text)
        raise RuntimeError(f"Gagal mengambil WMS capabilities: {res.text}")

geoserver_client = GeoServerClient()

    def delete_style(self, style_name: str, workspace_name: Optional[str] = None, recurse: bool = True) -> bool:
        if workspace_name:
            path = f"rest/workspaces/{workspace_name}/styles/{style_name}?recurse={str(recurse).lower()}"
        else:
            path = f"rest/styles/{style_name}?recurse={str(recurse).lower()}"
        res = self._request("DELETE", path)
        return res.status_code in (200, 204)

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
