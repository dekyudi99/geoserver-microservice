import sys
import os

# Tambahkan direktori root proyek ke sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.config.database import engine

def upgrade():
    """Menambahkan kolom service_mode dan gwc_ready ke vector_layers dan raster_metadata secara idempotent."""
    print("[MIGRATION] Menambahkan kolom service_mode dan gwc_ready...")
    queries = [
        "ALTER TABLE vector_layers ADD COLUMN IF NOT EXISTS service_mode VARCHAR(10) NOT NULL DEFAULT 'wms';",
        "ALTER TABLE vector_layers ADD COLUMN IF NOT EXISTS gwc_ready BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE raster_metadata ADD COLUMN IF NOT EXISTS service_mode VARCHAR(10) NOT NULL DEFAULT 'wms';",
        "ALTER TABLE raster_metadata ADD COLUMN IF NOT EXISTS gwc_ready BOOLEAN NOT NULL DEFAULT FALSE;",
    ]
    with engine.begin() as conn:
        for q in queries:
            print(f"  Executing: {q}")
            conn.execute(text(q))
    print("[MIGRATION] Upgrade selesai.")

def downgrade():
    """Menghapus kolom service_mode dan gwc_ready dari vector_layers dan raster_metadata."""
    print("[MIGRATION] Me-rollback kolom service_mode dan gwc_ready...")
    queries = [
        "ALTER TABLE vector_layers DROP COLUMN IF EXISTS service_mode;",
        "ALTER TABLE vector_layers DROP COLUMN IF EXISTS gwc_ready;",
        "ALTER TABLE raster_metadata DROP COLUMN IF EXISTS service_mode;",
        "ALTER TABLE raster_metadata DROP COLUMN IF EXISTS gwc_ready;",
    ]
    with engine.begin() as conn:
        for q in queries:
            print(f"  Executing: {q}")
            conn.execute(text(q))
    print("[MIGRATION] Downgrade selesai.")

if __name__ == "__main__":
    action = sys.argv[1].lower() if len(sys.argv) > 1 else "upgrade"
    if action == "downgrade":
        downgrade()
    else:
        upgrade()

