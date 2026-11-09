import sys
import os

# Tambahkan direktori root proyek ke sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.config.database import engine

def drop_wms_url_columns():
    print("[MIGRATION] Menghapus kolom wms_url dari database...")
    queries = [
        "ALTER TABLE IF EXISTS vector_layers DROP COLUMN IF EXISTS wms_url;",
        "ALTER TABLE IF EXISTS raster_metadata DROP COLUMN IF EXISTS wms_url;",
        "ALTER TABLE IF EXISTS layer_groups DROP COLUMN IF EXISTS wms_url;",
    ]
    with engine.begin() as conn:
        for q in queries:
            print(f"  Executing: {q}")
            conn.execute(text(q))
    print("[MIGRATION] Selesai. Kolom wms_url berhasil dihapus dari vector_layers, raster_metadata, dan layer_groups.")

if __name__ == "__main__":
    drop_wms_url_columns()

