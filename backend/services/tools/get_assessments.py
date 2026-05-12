"""Tool: get_assessments — query assessments with filters."""
from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


def _normalize_order_by(raw: Any) -> str:
    val = str(raw or "created_at").lower()
    return val if val in {"severity", "created_at", "action_priority"} else "created_at"


def _normalize_order_dir(raw: Any) -> str:
    return "ASC" if str(raw or "desc").lower() == "asc" else "DESC"


async def get_assessments(
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    """Return assessments filtered by site/building/severity/status context."""
    if db is None:
        return {"success": False, "error": "Database not available", "items": []}

    limit = max(1, min(int(tool_args.get("limit") or 10), 200))
    order_by = _normalize_order_by(tool_args.get("order_by"))
    order_dir = _normalize_order_dir(tool_args.get("order_dir"))
    occupant_status_raw = str(tool_args.get("occupant_status") or "").strip().lower()

    filters: list[str] = []
    args: list[Any] = []
    arg_index = 1

    def add_filter(clause: str, value: Any) -> None:
        nonlocal arg_index
        filters.append(clause.replace("?", f"${arg_index}", 1))
        args.append(value)
        arg_index += 1

    assessment_id = tool_args.get("assessment_id")
    if assessment_id:
        add_filter("a.id = ?", str(assessment_id))

    site_id = tool_args.get("site_id")
    if site_id is not None:
        add_filter("a.site_id = ?", int(site_id))

    site_name = str(tool_args.get("site_name") or "").strip()
    if site_name:
        add_filter("COALESCE(s.name, b.site_name, '') ILIKE ?", f"%{site_name}%")

    building_id = tool_args.get("building_id")
    if building_id is not None:
        add_filter("a.osm_building_id = ?", int(building_id))

    batch_id = tool_args.get("batch_id")
    if batch_id:
        add_filter("a.batch_id = ?", str(batch_id))

    severity_min = tool_args.get("severity_min")
    if severity_min is not None:
        add_filter("COALESCE(a.severity, 0) >= ?", int(severity_min))

    severity_max = tool_args.get("severity_max")
    if severity_max is not None:
        add_filter("COALESCE(a.severity, 0) <= ?", int(severity_max))

    status_value = str(tool_args.get("status") or "").strip().lower()
    if status_value:
        add_filter("LOWER(a.status) = ?", status_value)

    if occupant_status_raw:
        if occupant_status_raw in {"signs_of_life", "potentially_trapped"}:
            add_filter(
                "LOWER(COALESCE(a.occupant_status, '')) = ANY(?::text[])",
                ["trapped", "signs_of_life", "potentially_trapped"],
            )
        else:
            add_filter("LOWER(COALESCE(a.occupant_status, '')) = ?", occupant_status_raw)

    flood_zone = tool_args.get("flood_zone")
    if isinstance(flood_zone, bool):
        add_filter("COALESCE(a.flood_zone, false) = ?", flood_zone)

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    order_sql = f"ORDER BY a.{order_by} {order_dir}, a.created_at DESC"
    limit_ph = f"${arg_index}"

    query = f"""
        SELECT
            a.*,
            COALESCE(s.name, b.site_name) AS site_name,
            ST_AsGeoJSON(a.geom)::text AS geom_geojson
        FROM assessments a
        LEFT JOIN sites s ON a.site_id = s.id
        LEFT JOIN batches b ON a.batch_id = b.id
        {where_sql}
        {order_sql}
        LIMIT {limit_ph}
    """
    args.append(limit)

    try:
        rows = await db.fetch(query, *args)
    except asyncpg.exceptions.UndefinedTableError:
        return {"success": False, "error": "assessments table not available", "items": []}
    except asyncpg.exceptions.UndefinedColumnError:
        legacy_filters = [f for f in filters if "a.site_id" not in f and "s.name" not in f]
        legacy_where = f"WHERE {' AND '.join(legacy_filters)}" if legacy_filters else ""
        legacy_query = f"""
            SELECT a.*, b.site_name AS site_name, ST_AsGeoJSON(a.geom)::text AS geom_geojson
            FROM assessments a
            LEFT JOIN batches b ON a.batch_id = b.id
            {legacy_where}
            {order_sql}
            LIMIT {limit_ph}
        """
        rows = await db.fetch(legacy_query, *args)

    items: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        geom_text = payload.pop("geom_geojson", None)
        payload["geom_geojson"] = json.loads(geom_text) if geom_text else None
        for ts_col in ("created_at", "updated_at", "responded_at"):
            if payload.get(ts_col) is not None:
                payload[ts_col] = payload[ts_col].isoformat()
        items.append(payload)

    return {
        "success": True,
        "count": len(items),
        "filters_applied": {
            "site_id": site_id, "site_name": site_name or None,
            "severity_min": severity_min, "severity_max": severity_max,
            "status": status_value or None, "occupant_status": occupant_status_raw or None,
            "flood_zone": flood_zone if isinstance(flood_zone, bool) else None,
            "building_id": building_id, "batch_id": batch_id,
            "limit": limit, "order_by": order_by, "order_dir": order_dir.lower(),
        },
        "items": items,
    }
