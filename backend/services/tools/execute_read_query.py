"""Tool: execute_read_query — safe read-only SQL + PostGIS analysis for BhumiDrishti.

Supports plain SELECT queries AND any PostGIS spatial functions
(ST_Distance, ST_DWithin, ST_Intersects, ST_Area, ST_Centroid, ST_AsGeoJSON, …).

Safety: mutating SQL keywords are blocked before the query reaches the DB.
Geometry columns are automatically detected and converted to WKB hex strings.
If you want readable / displayable geometry wrap the column with ST_AsGeoJSON().
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

# ── Safety filter ────────────────────────────────────────────────────────────

# INTO is intentionally excluded — it appears in plain sub-queries
# such as  SELECT ... FROM (SELECT ...) sub  and is not mutating by itself.
# We rely on the leading-token check (must start with SELECT/WITH) to block
# INSERT INTO / COPY INTO patterns.
_FORBIDDEN_TOKENS: tuple[str, ...] = (
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE",
    "ALTER", "TRUNCATE", "GRANT", "REVOKE",
    "EXECUTE", "COPY", "\\COPY", "--", "/*",
)

_MAX_ROWS = 500         # spatial queries often return many features
_MAX_SQL_LEN = 8_000    # spatial queries can be verbose


def _is_safe_query(sql: str) -> tuple[bool, str]:
    """Return (safe, reason).  Reject anything that is not a plain SELECT/CTE."""
    stripped = sql.strip()

    if len(stripped) > _MAX_SQL_LEN:
        return False, f"Query exceeds maximum length of {_MAX_SQL_LEN} characters"

    first_token = re.split(r"\s+", stripped, maxsplit=1)[0].upper()
    if first_token not in {"SELECT", "WITH", "EXPLAIN"}:
        return False, f"Only SELECT queries are allowed. Query starts with '{first_token}'"

    upper = stripped.upper()
    for token in _FORBIDDEN_TOKENS:
        # Word-boundary check: avoids blocking "execution", "truncation", etc.
        if re.search(r"(?<![A-Z0-9_])" + re.escape(token) + r"(?![A-Z0-9_])", upper):
            return False, f"Forbidden keyword '{token}' found in query"

    return True, ""


# ── Row serialiser ───────────────────────────────────────────────────────────

def _serialize_value(key: str, value: Any) -> Any:
    """Convert a single cell value to a JSON-serialisable Python object."""
    if value is None:
        return None

    # Timestamp / date
    if hasattr(value, "isoformat"):
        return value.isoformat()

    # Already JSON-safe scalars
    if isinstance(value, (bool, int, float, str)):
        return value

    # Plain list or dict (e.g. JSONB columns)
    if isinstance(value, (list, dict)):
        return value

    # asyncpg returns PostGIS geometry columns as raw bytes (WKB binary).
    # We convert to lowercase hex so callers can recognise and decode it.
    # If the AI wraps the column with ST_AsGeoJSON() it arrives as a string
    # and is handled by the isinstance(str) branch above.
    if isinstance(value, (bytes, bytearray, memoryview)):
        hex_str = bytes(value).hex()
        return {"__type": "geometry_wkb_hex", "hex": hex_str}

    # asyncpg Decimal / other numeric types
    try:
        return float(value)
    except (TypeError, ValueError):
        pass

    # Fallback: stringify
    try:
        json.dumps(str(value))
        return str(value)
    except Exception:
        return repr(value)


def _serialize_row(row: asyncpg.Record) -> dict[str, Any]:
    """Convert one asyncpg Record to a JSON-serialisable dict."""
    return {key: _serialize_value(key, row[key]) for key in row.keys()}


def _has_geometry_columns(rows: list[dict[str, Any]]) -> bool:
    """Check whether any result row contains a raw geometry column."""
    if not rows:
        return False
    return any(
        isinstance(v, dict) and v.get("__type") == "geometry_wkb_hex"
        for row in rows
        for v in row.values()
    )


# ── Public tool function ─────────────────────────────────────────────────────

async def execute_read_query(
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    """Execute a read-only SQL SELECT (including PostGIS spatial queries)."""
    sql = str(tool_args.get("sql") or "").strip()

    if not sql:
        return {"success": False, "error": "sql parameter is required", "rows": [], "row_count": 0}

    if db is None:
        return {"success": False, "error": "Database not available", "rows": [], "row_count": 0}

    safe, reason = _is_safe_query(sql)
    if not safe:
        logger.warning("execute_read_query.blocked reason=%s sql_preview=%.200s", reason, sql)
        return {"success": False, "error": reason, "rows": [], "row_count": 0}

    # Wrap with a LIMIT if the query has none, so runaway spatial scans are capped.
    upper = sql.upper()
    has_limit = bool(re.search(r"\bLIMIT\b", upper))
    if not has_limit:
        limited_sql = f"SELECT * FROM ({sql}) _q LIMIT {_MAX_ROWS}"
    else:
        limited_sql = sql

    logger.info("execute_read_query.running sql_preview=%.300s", limited_sql)
    try:
        rows = await db.fetch(limited_sql)
    except asyncpg.PostgresError as exc:
        logger.warning("execute_read_query.db_error error=%s", exc)
        return {
            "success": False,
            "error": str(exc),
            "hint": (
                "PostgreSQL error. Check column names, table names, and PostGIS function syntax. "
                "Use ST_AsGeoJSON(geom) to return geometry as readable JSON."
            ),
            "rows": [],
            "row_count": 0,
        }
    except Exception as exc:
        logger.exception("execute_read_query.unexpected_error error=%s", exc)
        return {"success": False, "error": str(exc), "rows": [], "row_count": 0}

    serialised = [_serialize_row(row) for row in rows]
    has_geom = _has_geometry_columns(serialised)

    logger.info(
        "execute_read_query.completed row_count=%d has_geometry=%s",
        len(serialised), has_geom,
    )
    result: dict[str, Any] = {
        "success": True,
        "row_count": len(serialised),
        "rows": serialised,
    }
    if not has_limit:
        result["capped_at"] = _MAX_ROWS
    if has_geom:
        result["geometry_note"] = (
            "One or more columns contain raw WKB geometry (hex). "
            "Re-run with ST_AsGeoJSON(geom) to get GeoJSON strings, "
            "or ST_AsText(geom) for WKT, for human-readable output."
        )
    return result
