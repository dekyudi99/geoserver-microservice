# 🗺️ GeoServer Microservice

Microservice mandiri dan terisolasi untuk mengelola **GeoServer REST API**, ingest berkas spasial (**Vector** & **Raster**) ke database **PostGIS**, serta menyediakan endpoint **WMS/WFS**.

Layanan ini dipisahkan dari `fastapi-management-spasial` agar AstraGIS Core tetap ringan dan fokus pada autentikasi, manajemen proyek, dan model Machine Learning.

---

## 🚀 Fitur Utama
1. **DRY & Unified GeoServer Client:** Menghilangkan kode duplikat untuk otentikasi basic auth, endpoint workspaces, datastores, coveragestores, layers, styles, dan WMS capabilities.
2. **Ingest Vektor Otomatis:**
   - Format: Shapefile (.shp / .zip), GeoJSON, GeoPackage (.gpkg), CSV (Point), KML, KMZ.
   - Normalisasi proyeksi otomatis ke EPSG:4326 (WGS84).
   - Topology-preserving simplification (Douglas-Peucker dengan preservasi topologi).
   - Simpan langsung ke PostGIS dan publish otomatis ke GeoServer dengan default SLD style.
3. **Ingest Raster Otomatis:**
   - Format: GeoTIFF (.tif / .tiff).
   - Sanitasi otomatis 4-band Photometric RGB untuk kompatibilitas GeoServer.
   - Pembuatan CoverageStore dan styling SLD Color Ramp otomatis.
4. **Isolasi Database:** Data spasial disimpan di PostGIS spatial database terpisah dari database utama AstraGIS.

---

## 🛠️ Menjalankan Service

### Menggunakan Python (Lokal)
```bash
cd geoserver_microservice
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

### Menggunakan Docker Compose
```bash
cd geoserver_microservice
docker-compose up -d --build
```

---

## 📡 Daftar Endpoint Utama

| Method | Endpoint | Deskripsi |
|---|---|---|
| `GET` | `/health` | Healthcheck koneksi service & GeoServer |
| `GET` | `/workspaces` | Daftar workspace di GeoServer |
| `POST` | `/workspaces` | Buat workspace baru di GeoServer |
| `DELETE` | `/workspaces/{name}` | Hapus workspace GeoServer (rekursif) |
| `GET` | `/stores` | Daftar datastore PostGIS/vector |
| `GET` | `/coverage-stores` | Daftar coveragestore raster |
| `POST` | `/layers/publish-vector` | Upload dan publish layer vektor |
| `POST` | `/layers/publish-raster` | Upload dan publish layer raster |
| `DELETE` | `/layers/{workspace}/{name}` | Hapus layer di GeoServer |
| `POST` | `/styles/apply` | Terapkan SLD style ke layer |
| `GET` | `/wms/capabilities` | GetCapabilities metadata WMS OGC |
