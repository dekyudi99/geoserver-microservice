"""
Migration Script: add_gid_pk.py
Idempotent script to add 'gid BIGSERIAL PRIMARY KEY' to vector layers in PostGIS.

RUNBOOK FOR PROXMOX / PRODUCTION DEPLOYMENT:
=============================================================================
1. BACKUP (Mandatory before migration):
   Run pg_dump on the PostgreSQL host or container:
     docker exec -t db-central-postgres pg_dump -U geoserver_svc -d spatial -F c -b -v -f /tmp/spatial_backup_before_gid_pk.dump
   (Verify file exists and is non-empty before proceeding).

2. DRY-RUN (Check migration plan without modifications):
   docker exec -e PYTHONPATH=/app geoserver-microservice python scripts/add_gid_pk.py --upgrade --dry-run

3. EXECUTE MIGRATION (Layer-by-layer or all):
   Single layer test:
     docker exec -e PYTHONPATH=/app geoserver-microservice python scripts/add_gid_pk.py --upgrade --table <table_name>
   All layers:
     docker exec -e PYTHONPATH=/app geoserver-microservice python scripts/add_gid_pk.py --upgrade

4. RESET GEOSERVER CATALOG & INVALIDATE SCHEMA CACHE:
   GeoServer reload is invoked automatically by this script via:
     POST /rest/reload
   If manual reset is needed:
     curl -u admin:password -X POST http://<geoserver-host>:8080/geoserver/rest/reload
     curl -u admin:password -X POST http://<geoserver-host>:8080/geoserver/rest/reset

5. ROLLBACK / DOWNGRADE (If needed):
   Single table:
     docker exec -e PYTHONPATH=/app geoserver-microservice python scripts/add_gid_pk.py --downgrade --table <table_name>
   Full DB restore from dump:
     docker exec -t db-central-postgres pg_restore -U geoserver_svc -d spatial -c -v /tmp/spatial_backup_before_gid_pk.dump
=============================================================================
"""
import sys
import argparse
import logging
from typing import List, Optional
from sqlalchemy import text
from app.config.database import SessionLocal, spatial_engine
from app.models.spatial_data import VectorLayer
from app.clients.geoserver_client import GeoServerClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migration.add_gid_pk")

def check_table_has_pk(conn, table_name: str) -> Optional[str]:
    """Mengembalikan nama kolom PK jika sudah ada, atau None."""
    query = text("""
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = :tbl
          AND n.nspname = 'public'
          AND i.indisprimary;
    """)
    res = conn.execute(query, {"tbl": table_name}).fetchone()
    return res[0] if res else None

def check_spatial_index(conn, table_name: str) -> Optional[str]:
    """Mengembalikan nama index GiST jika sudah ada, atau None."""
    query = text("""
        SELECT i.relname
        FROM pg_index x
        JOIN pg_class c ON c.oid = x.indrelid
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_am am ON am.oid = i.relam
        WHERE c.relname = :tbl AND am.amname = 'gist';
    """)
    res = conn.execute(query, {"tbl": table_name}).fetchone()
    return res[0] if res else None

def check_column_exists(conn, table_name: str, column_name: str) -> bool:
    query = text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :tbl AND column_name = :col;
    """)
    return bool(conn.execute(query, {"tbl": table_name, "col": column_name}).fetchone())

def upgrade(dry_run: bool = False, target_tables: Optional[List[str]] = None):
    logger.info("=== STARTING MIGRATION: UPGRADE (Add gid PK) ===")
    db = SessionLocal()
    try:
        layers = db.query(VectorLayer).all()
        table_names = [l.table_name for l in layers if l.table_name]
        if target_tables:
            table_names = [t for t in table_names if t in target_tables]
            # Also allow testing tables not yet in VectorLayer
            for tt in target_tables:
                if tt not in table_names:
                    table_names.append(tt)

        logger.info(f"Target vector tables to evaluate: {table_names}")
        modified_count = 0

        with spatial_engine.begin() as conn:
            for tbl in table_names:
                # Cek apakah tabel fisik ada di database
                tbl_exists = conn.execute(
                    text("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=:tbl"),
                    {"tbl": tbl}
                ).fetchone()
                if not tbl_exists:
                    logger.warning(f"Table '{tbl}' does not exist in spatial DB public schema. Skipping.")
                    continue

                existing_pk = check_table_has_pk(conn, tbl)
                existing_gist = check_spatial_index(conn, tbl)

                if existing_pk:
                    logger.info(f"Table '{tbl}' ALREADY has primary key '{existing_pk}'.")
                else:
                    logger.info(f"Table '{tbl}' has NO primary key. Plan: ADD 'gid BIGSERIAL PRIMARY KEY'.")
                    if not dry_run:
                        if check_column_exists(conn, tbl, "gid"):
                            conn.execute(text(f'ALTER TABLE "{tbl}" ADD PRIMARY KEY ("gid");'))
                        else:
                            conn.execute(text(f'ALTER TABLE "{tbl}" ADD COLUMN "gid" BIGSERIAL PRIMARY KEY;'))
                        modified_count += 1
                    else:
                        logger.info(f"[DRY-RUN] Would execute: ALTER TABLE \"{tbl}\" ADD COLUMN \"gid\" BIGSERIAL PRIMARY KEY;")
                        modified_count += 1

                if existing_gist:
                    logger.info(f"Table '{tbl}' ALREADY has spatial index '{existing_gist}'. Skipping spatial index creation.")
                else:
                    logger.info(f"Table '{tbl}' has NO spatial index. Plan: CREATE INDEX \"idx_{tbl}_geom\" USING GIST (geometry).")
                    if not dry_run:
                        conn.execute(text(f'CREATE INDEX IF NOT EXISTS "idx_{tbl}_geom" ON "{tbl}" USING GIST (geometry);'))
                    else:
                        logger.info(f"[DRY-RUN] Would execute: CREATE INDEX IF NOT EXISTS \"idx_{tbl}_geom\" ON \"{tbl}\" USING GIST (geometry);")

        if not dry_run and modified_count > 0:
            logger.info("Reloading GeoServer catalog to ensure GeoServer recognizes the new PKs...")
            try:
                gc = GeoServerClient()
                resp = gc._request("POST", "rest/reload")
                logger.info(f"GeoServer catalog reload: HTTP {resp.status_code}")
                # Reset catalog to flush featureType caches
                resp_reset = gc._request("POST", "rest/reset")
                logger.info(f"GeoServer catalog reset: HTTP {resp_reset.status_code}")
            except Exception as e:
                logger.warning(f"Failed to reload/reset GeoServer catalog: {e}")

        logger.info(f"Upgrade completed. Modified {modified_count} table(s).")
    finally:
        db.close()

def downgrade(dry_run: bool = False, target_tables: Optional[List[str]] = None):
    logger.info("=== STARTING MIGRATION: DOWNGRADE (Remove gid PK) ===")
    db = SessionLocal()
    try:
        layers = db.query(VectorLayer).all()
        table_names = [l.table_name for l in layers if l.table_name]
        if target_tables:
            table_names = [t for t in table_names if t in target_tables]
            for tt in target_tables:
                if tt not in table_names:
                    table_names.append(tt)

        logger.info(f"Target vector tables to evaluate: {table_names}")
        modified_count = 0

        with spatial_engine.begin() as conn:
            for tbl in table_names:
                tbl_exists = conn.execute(
                    text("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=:tbl"),
                    {"tbl": tbl}
                ).fetchone()
                if not tbl_exists:
                    continue

                if check_column_exists(conn, tbl, "gid"):
                    logger.info(f"Table '{tbl}' has 'gid' column. Dropping...")
                    if not dry_run:
                        # Drop constraint and column
                        conn.execute(text(f'ALTER TABLE "{tbl}" DROP COLUMN IF EXISTS "gid" CASCADE;'))
                        modified_count += 1
                    else:
                        logger.info(f"[DRY-RUN] Would execute: ALTER TABLE \"{tbl}\" DROP COLUMN IF EXISTS \"gid\" CASCADE;")
                        modified_count += 1
                else:
                    logger.info(f"Table '{tbl}' has no 'gid' column. Skipping.")

        if not dry_run and modified_count > 0:
            logger.info("Reloading GeoServer catalog...")
            try:
                gc = GeoServerClient()
                resp = gc._request("POST", "rest/reload")
                logger.info(f"GeoServer catalog reload: HTTP {resp.status_code}")
            except Exception as e:
                logger.warning(f"Failed to reload GeoServer catalog: {e}")

        logger.info(f"Downgrade completed. Modified {modified_count} table(s).")
    finally:
        db.close()

def main():
    parser = argparse.ArgumentParser(description="Migration script to add gid PK to vector layers.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--upgrade", action="store_true", help="Add gid BIGSERIAL PRIMARY KEY to vector tables")
    group.add_argument("--downgrade", action="store_true", help="Remove gid column and PK from vector tables")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without modifying database")
    parser.add_argument("--table", type=str, default=None, help="Specific table name to migrate (for testing)")

    args = parser.parse_args()
    target_tables = [args.table] if args.table else None

    if args.upgrade:
        upgrade(dry_run=args.dry_run, target_tables=target_tables)
    elif args.downgrade:
        downgrade(dry_run=args.dry_run, target_tables=target_tables)

if __name__ == "__main__":
    main()

