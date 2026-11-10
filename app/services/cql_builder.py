"""
Pure function module to safely construct CQL filters and sortBy parameters for WFS 2.0.0.
Protects against SQL/CQL injection, validates column names against layer schema,
and strictly coerces types according to schema definitions.
"""
import re
import datetime
from typing import List, Dict, Any, Optional, Set

class CqlBuildError(ValueError):
    """Exception raised when filter, column, or operator validation fails."""
    pass

ALLOWED_OPERATORS = {
    "=", "!=", "<>", "<", "<=", ">", ">=",
    "LIKE", "ILIKE", "contains",
    "IN", "IS NULL", "IS NOT NULL"
}

ALLOWED_SORT_DIRS = {"ASC", "DESC"}

MAX_STR_LEN = 500

def _validate_str_chars_and_length(val: str) -> str:
    s = str(val) if not isinstance(val, str) else val
    if len(s) > MAX_STR_LEN:
        raise CqlBuildError(f"Value exceeds maximum allowed length of {MAX_STR_LEN} characters.")
    for ch in s:
        if ch == '\0':
            raise CqlBuildError("Value contains forbidden null byte.")
        if ord(ch) < 32 and ch not in ('\t', '\n', '\r'):
            raise CqlBuildError("Value contains forbidden control character.")
    return s

def _clean_str_literal(val: str) -> str:
    s = _validate_str_chars_and_length(val)
    # Escape single quotes by doubling them
    return s.replace("'", "''")

def _clean_like_pattern(val: str) -> str:
    s = _validate_str_chars_and_length(val)
    # Escape backslash, wildcards % and _, and single quotes
    escaped = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")
    return escaped

def _coerce_numeric(val: Any) -> str:
    try:
        # Check if float or int
        num = float(val)
        if int(num) == num and "." not in str(val):
            return str(int(num))
        return str(num)
    except (ValueError, TypeError):
        raise CqlBuildError(f"Expected numeric value, got '{val}'")

def _coerce_bool(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    s = str(val).strip().lower()
    if s in ("true", "1", "t", "yes"):
        return "true"
    if s in ("false", "0", "f", "no"):
        return "false"
    raise CqlBuildError(f"Expected boolean value, got '{val}'")

def _coerce_date(val: Any) -> str:
    s = str(val).strip()
    # Check ISO formats YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS
    iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?)?Z?$")
    if not iso_pattern.match(s):
        raise CqlBuildError(f"Expected ISO date/datetime format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS), got '{val}'")
    return f"'{s}'"

def build_single_condition(
    field: str,
    op: str,
    value: Any,
    schema_map: Dict[str, Dict[str, Any]]
) -> str:
    """Membangun 1 klausa kondisi CQL terisolasi dengan validasi tipe."""
    field_lower = field.strip().lower()
    matched_col = None
    for col_name in schema_map:
        if col_name.lower() == field_lower:
            matched_col = col_name
            break

    if not matched_col:
        raise CqlBuildError(f"Column '{field}' is not in allowed layer schema.")

    col_info = schema_map[matched_col]
    if col_info.get("is_geometry"):
        raise CqlBuildError(f"Geometry column '{field}' cannot be used in attribute filters.")

    norm_op = op.strip().upper()
    if norm_op == "CONTAINS":
        norm_op = "ILIKE"
    elif norm_op == "<>":
        norm_op = "!="

    if norm_op not in ALLOWED_OPERATORS:
        raise CqlBuildError(f"Operator '{op}' is not in allowed operators.")

    # Null checks
    if norm_op == "IS NULL":
        return f"({matched_col} IS NULL)"
    if norm_op == "IS NOT NULL":
        return f"({matched_col} IS NOT NULL)"

    col_type = col_info.get("local_type", "string").lower()

    # IN operator
    if norm_op == "IN":
        if not isinstance(value, (list, tuple, set)):
            raise CqlBuildError(f"Operator IN requires an array of values, got {type(value).__name__}.")
        if len(value) == 0:
            raise CqlBuildError("Operator IN array cannot be empty.")
        if len(value) > 100:
            raise CqlBuildError(f"Operator IN supports maximum 100 values, got {len(value)}.")

        formatted_items = []
        for item in value:
            if col_type in ("int", "integer", "long", "short", "byte", "number", "float", "double", "decimal"):
                formatted_items.append(_coerce_numeric(item))
            elif col_type in ("bool", "boolean"):
                formatted_items.append(_coerce_bool(item))
            elif col_type in ("date", "datetime", "timestamp"):
                formatted_items.append(_coerce_date(item))
            else:
                formatted_items.append(f"'{_clean_str_literal(str(item))}'")

        return f"({matched_col} IN ({', '.join(formatted_items)}))"

    # LIKE / ILIKE operator
    if norm_op in ("LIKE", "ILIKE"):
        clean_val = _clean_like_pattern(str(value))
        # Format string containment
        return f"({matched_col} ILIKE '%{clean_val}%')"

    # Comparison operators (=, !=, <, <=, >, >=)
    op_sql = "<>" if norm_op == "!=" else norm_op

    if col_type in ("int", "integer", "long", "short", "byte", "number", "float", "double", "decimal"):
        val_str = _coerce_numeric(value)
        return f"({matched_col} {op_sql} {val_str})"
    elif col_type in ("bool", "boolean"):
        val_str = _coerce_bool(value)
        return f"({matched_col} = {val_str})"
    elif col_type in ("date", "datetime", "timestamp"):
        val_str = _coerce_date(value)
        return f"({matched_col} {op_sql} {val_str})"
    else:
        # String / text
        clean_val = _clean_str_literal(str(value))
        return f"({matched_col} {op_sql} '{clean_val}')"

def build_cql(
    filters: Optional[List[Dict[str, Any]]] = None,
    schema: Optional[List[Dict[str, Any]]] = None,
    bbox: Optional[List[float]] = None,
    geom_column: str = "geometry"
) -> Optional[str]:
    """
    Menyusun ekspresi CQL lengkap dengan operator AND untuk daftar filter
    dan optional bounding box.
    """
    clauses = []
    schema_map = {}
    if schema:
        for c in schema:
            schema_map[c["name"]] = c

    if filters:
        for f in filters:
            field = f.get("field")
            op = f.get("op")
            val = f.get("value")
            if not field or not op:
                continue
            cond = build_single_condition(field, op, val, schema_map)
            clauses.append(cond)

    if bbox and len(bbox) == 4:
        # Format BBOX GeoServer WFS 2.0.0 EPSG:4326: minLat, minLon, maxLat, maxLon
        min_lon, min_lat, max_lon, max_lat = [float(b) for b in bbox]
        clauses.append(f"BBOX({geom_column}, {min_lat}, {min_lon}, {max_lat}, {max_lon})")

    if not clauses:
        return None

    return " AND ".join(clauses)

def build_sort_by(
    sort_items: Optional[List[Dict[str, Any]]],
    schema: List[Dict[str, Any]],
    pk_column: str = "gid"
) -> Optional[str]:
    """
    Menyusun sortBy parameter yang aman dan deterministik untuk WFS 2.0.0.
    - Kolom sort harus ada dalam allow-list skema layer.
    - Arah harus 'ASC' atau 'DESC'.
    - Jika user tidak memberikan sort, return None agar GeoServer memakai natural order PK.
    - Jika user memberikan sort pada kolom non-unik, kolom skema lainnya ditambahkan sebagai
      tie-breaker sekunder agar paginasi selalu 100% deterministik dan bebas duplikasi antar-halaman.
    """
    allowed_cols = {c["name"].lower(): c["name"] for c in schema if not c.get("is_geometry") and not c.get("is_pk")}
    sort_clauses = []
    used_cols = set()

    if sort_items:
        for item in sort_items:
            field = str(item.get("field", "")).strip()
            direction = str(item.get("dir", "ASC")).strip().upper()
            if not field:
                continue
            if field.lower() not in allowed_cols:
                raise CqlBuildError(f"Sort column '{field}' is not in allowed layer schema.")
            if direction not in ALLOWED_SORT_DIRS:
                raise CqlBuildError(f"Sort direction '{direction}' must be 'ASC' or 'DESC'.")

            exact_col = allowed_cols[field.lower()]
            sort_clauses.append(f"{exact_col} {direction}")
            used_cols.add(exact_col.lower())

        # Secondary deterministic tie-breaker dari kolom skema tersisa
        for c in schema:
            c_name = c.get("name", "")
            if not c.get("is_geometry") and not c.get("is_pk") and c_name.lower() not in used_cols:
                sort_clauses.append(f"{c_name} ASC")
                used_cols.add(c_name.lower())

    return ",".join(sort_clauses) if sort_clauses else None

def build_identify_cql(
    lat: float,
    lng: float,
    tolerance_meters: float,
    geom_type: str = "Polygon",
    geom_column: str = "geometry",
    mode: str = "intersect_first"
) -> str:
    """
    Menyusun filter spasial untuk identify.
    WFS 2.0.0 EPSG:4326: POINT(lat lon).
    - Poligon: INTERSECTS(geometry, POINT(lat lon)) dulu; jika kosong baru DWITHIN dengan toleransi piksel.
    - Point / Line: DWITHIN langsung.
    """
    lat_val = float(lat)
    lng_val = float(lng)
    tol_val = float(tolerance_meters)

    is_polygon = "polygon" in (geom_type or "").lower()

    if is_polygon and mode == "intersect_first":
        return f"INTERSECTS({geom_column}, POINT({lat_val} {lng_val}))"
    else:
        return f"DWITHIN({geom_column}, POINT({lat_val} {lng_val}), {tol_val}, meters)"
