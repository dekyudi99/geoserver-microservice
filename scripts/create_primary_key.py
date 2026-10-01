"""
Script one-time untuk membuat PRIMARY API Key pertama.
Dapat dijalankan langsung di host atau di dalam container:
  python -m scripts.create_primary_key
"""
import sys
import os

# Tambahkan direktori root proyek ke sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config.database import SessionLocal, engine, Base
from app.models.api_key import ApiKey, ApiKeyType
from app.models.audit_log import AuditLog
from app.models.spatial_data import VectorLayer, RasterMetadata
from app.services.api_key_service import generate_api_key

def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    existing = db.query(ApiKey).filter(ApiKey.key_type == ApiKeyType.PRIMARY, ApiKey.is_active == True).first()
    if existing:
        print(f"\n[INFO] PRIMARY key sudah ada sebelumnya:")
        print(f"Name       : {existing.name}")
        print(f"Created At : {existing.created_at}")
        print("\nJika Anda ingin membuat key baru, gunakan endpoint POST /api-keys/ menggunakan PRIMARY key ini.")
        db.close()
        return

    name = "astragis-core"
    if len(sys.argv) > 1:
        name = sys.argv[1]

    plain_key, api_key = generate_api_key(
        key_type=ApiKeyType.PRIMARY,
        db=db,
        name=name,
        owner_info='{"system": "astragis", "description": "Primary key untuk fastapi-management-spasial"}'
    )

    print("\n" + "="*65)
    print("PRIMARY API KEY BERHASIL DIBUAT")
    print("="*65)
    print(f"Name    : {api_key.name}")
    print(f"ID      : {api_key.id}")
    print(f"API Key : {plain_key}")
    print("="*65)
    print("\n[PENTING] Simpan nilai plain key di atas sekarang!")
    print("Masukkan key ini ke file .env di fastapi-management-spasial:")
    print(f"GEOSERVER_MICROSERVICE_API_KEY={plain_key}\n")

    db.close()

if __name__ == "__main__":
    main()
