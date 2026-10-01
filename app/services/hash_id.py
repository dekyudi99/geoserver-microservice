import os
import uuid
from typing import Optional
from sqids import Sqids
from sqlalchemy.orm import Session
from app.models.spatial_data import WorkspaceMetadata

SECRET_ALPHABET = os.getenv("SECRET_ALPHABET", "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

sqids = Sqids(min_length=16, alphabet=SECRET_ALPHABET)

def encode_id(num: int) -> str:
    if num is None:
        return ""
    try:
        return sqids.encode([int(num)])
    except Exception:
        return ""

def decode_id(code: str) -> Optional[int]:
    if not code:
        return None
    try:
        numbers = sqids.decode(str(code).strip())
        return numbers[0] if numbers else None
    except Exception:
        return None

def resolve_workspace(db: Session, identifier: str) -> Optional[WorkspaceMetadata]:
    """
    Resolve workspace using:
    1. Hashed ID (e.g. 'UkLWZg9DAJQ7Xlrz')
    2. Raw numeric ID (e.g. '1', '2')
    3. Technical name (e.g. 'ws_...')
    4. UUID
    5. Display Name
    """
    if not identifier:
        return None
    ident = str(identifier).strip()

    # 1. Try decode as Hashed ID
    num = decode_id(ident)
    if num is not None:
        meta = db.query(WorkspaceMetadata).filter(WorkspaceMetadata.raw_id == num).first()
        if meta:
            return meta

    # 2. Try match raw_id if numeric
    if ident.isdigit():
        meta = db.query(WorkspaceMetadata).filter(WorkspaceMetadata.raw_id == int(ident)).first()
        if meta:
            return meta

    # 3. Try match technical GeoServer workspace_name
    meta = db.query(WorkspaceMetadata).filter(WorkspaceMetadata.workspace_name == ident).first()
    if meta:
        return meta

    # 4. Try match UUID
    try:
        u = uuid.UUID(ident)
        meta = db.query(WorkspaceMetadata).filter(WorkspaceMetadata.id == u).first()
        if meta:
            return meta
    except Exception:
        pass

    # 5. Try match display_name
    meta = db.query(WorkspaceMetadata).filter(WorkspaceMetadata.display_name == ident).first()
    if meta:
        return meta

    return None
