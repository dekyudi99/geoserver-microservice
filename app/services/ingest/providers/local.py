import os
import shutil
import re
from pathlib import Path
from typing import Optional
from fastapi import UploadFile
from .base import SourceProvider, DownloadedFile
from app.config.settings import settings

ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
TIFF_MAGIC = (b"II*\x00", b"MM\x00*")

def sanitize_filename(filename: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "_", filename)
    return cleaned[:100]

class LocalUploadProvider(SourceProvider):
    def __init__(self, upload_file: UploadFile):
        self.upload_file = upload_file
        self.downloaded_path: Optional[Path] = None

    def download(self, target_dir: Path) -> DownloadedFile:
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(self.upload_file.filename or "uploaded_spatial_file")
        dest_path = target_dir / safe_name

        max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        total_bytes = 0

        # Stream chunked 64KB directly to disk
        CHUNK_SIZE = 64 * 1024
        header_bytes = b""

        with open(dest_path, "wb") as f_out:
            while True:
                chunk = self.upload_file.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                if len(header_bytes) < 16:
                    header_bytes += chunk[:16 - len(header_bytes)]
                total_bytes += len(chunk)
                if max_bytes > 0 and total_bytes > max_bytes:
                    f_out.close()
                    if dest_path.exists():
                        dest_path.unlink()
                    raise ValueError(f"Ukuran file melampaui batas maksimum {settings.MAX_UPLOAD_SIZE_MB} MB.")
                f_out.write(chunk)

        # Magic Bytes & Format Validation
        lower_ext = Path(safe_name).suffix.lower()
        stripped_header = header_bytes.lstrip(b"\xef\xbb\xbf \t\r\n")

        is_zip = any(header_bytes.startswith(m) for m in ZIP_MAGIC)
        is_tiff = any(header_bytes.startswith(m) for m in TIFF_MAGIC)
        is_geojson = (lower_ext in [".geojson", ".json"]) and (
            stripped_header.startswith(b"{") or stripped_header.startswith(b"[")
        )
        is_kml = (lower_ext == ".kml") and (
            b"<kml" in header_bytes.lower() or b"<?xml" in header_bytes.lower() or b"<document" in header_bytes.lower() or stripped_header.startswith(b"<")
        )
        is_kmz = (lower_ext == ".kmz") and is_zip
        is_csv = (lower_ext == ".csv")

        if not (is_zip or is_tiff or is_geojson or is_kml or is_kmz or is_csv):
            if dest_path.exists():
                dest_path.unlink()
            raise ValueError(
                "Tipe file tidak valid. Format yang didukung: GeoTIFF (.tif/.tiff), Shapefile (.zip), GeoJSON (.geojson/.json), KML (.kml), KMZ (.kmz), dan CSV (.csv)."
            )

        self.downloaded_path = dest_path
        if is_kmz or (is_zip and lower_ext == ".kmz"):
            content_type = "application/vnd.google-earth.kmz"
        elif is_kml:
            content_type = "application/vnd.google-earth.kml+xml"
        elif is_csv:
            content_type = "text/csv"
        elif is_zip:
            content_type = "application/zip"
        elif is_tiff:
            content_type = "image/tiff"
        else:
            content_type = "application/geo+json"

        return DownloadedFile(
            local_path=dest_path,
            original_filename=self.upload_file.filename,
            file_size=total_bytes,
            content_type=content_type
        )

    def cleanup(self) -> None:
        if self.downloaded_path and self.downloaded_path.exists():
            try:
                self.downloaded_path.unlink()
            except Exception:
                pass
