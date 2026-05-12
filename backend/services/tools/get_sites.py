"""Tool: get_sites — query sites with summary assessment counts."""
from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


async def get_sites(
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    """Return sites with summary counts and optional spatial filters."""
    if db is None:
        return {"success": False, "error": "Database not available", "items": []}

    limit = max(1, min(int(tool_args.get("limit") or 20), 200))
    filters: list[str] = []
    args: list[Any] = []
    arg_index = 1

    def add_filter(clause: str, value: Any) -> None:
        nonlocal arg_index
        filters.append(clause.replace("?", f"${arg_index}", 1))
        args.append(value)
        arg_index += 1

    site_id = tool_args.get("site_id")
    if site_id is not None:
        add_filter("s.id = ?", int(site_id))

    site_name = str(tool_args.get("site_name") or "").strip()
    if site_name:
        add_filter("s.name ILIKE ?", f"%{site_name}%")

    status_value = str(tool_args.get("status") or "").strip().lower()
    if status_value:
        add_filter("LOWER(s.status) = ?", status_value)

    contains_lat = tool_args.get("contains_lat")
    contains_lon = tool_args.get("contains_lon")
    if contains_lat is not None and contains_lon is not None:
        filters.append(
            f"s.boundary IS NOT NULL AND ST_Contains(s.boundary, "
            f"ST_SetSRID(ST_Point(${arg_index}, ${arg_index + 1}), 4326))"
        )
        args.extend([float(contains_lon), float(contains_lat)])
        arg_index += 2

    building_id = tool_args.get("building_id")
    if building_id is not None:
        add_filter(
            """
            EXISTS (
              SELECT 1 FROM turkey_buildings tb
              WHERE tb.osm_id = ?
                AND s.boundary IS NOT NULL
                AND ST_Intersects(tb.geom, s.boundary)
            )
            """,
            int(building_id),
        )

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    limit_ph = f"${arg_index}"
    args.append(limit)

    query = f"""
        SELECT
            s.id, s.name, s.status, s.total_buildings, s.created_at, s.updated_at,
            ST_AsGeoJSON(s.boundary)::text AS boundary_geojson,
            COALESCE(COUNT(a.id), 0)::int AS assessment_count,
            COALESCE(SUM(CASE WHEN a.status = 'pending' THEN 1 ELSE 0 END), 0)::int AS pending_count,
            COALESCE(SUM(CASE WHEN a.status IN ('responded', 'closed') THEN 1 ELSE 0 END), 0)::int AS responded_count,
            COALESCE(SUM(CASE WHEN COALESCE(a.severity, 0) >= 4 THEN 1 ELSE 0 END), 0)::int AS critical_count
        FROM sites s
        LEFT JOIN assessments a ON a.site_id = s.id
        {where_sql}
        GROUP BY s.id, s.name, s.status, s.total_buildings, s.created_at, s.updated_at, s.boundary
        ORDER BY s.updated_at DESC
        LIMIT {limit_ph}
    """

    try:
        rows = await db.fetch(query, *args)
    except asyncpg.exceptions.UndefinedTableError:
        return {"success": False, "error": "sites table not available", "items": []}
    except asyncpg.exceptions.UndefinedColumnError:
        return {"success": False, "error": "sites schema missing required columns", "items": []}

    items: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        boundary_text = payload.pop("boundary_geojson", None)
        payload["boundary_geojson"] = json.loads(boundary_text) if boundary_text else None
        for ts_col in ("created_at", "updated_at"):
            if payload.get(ts_col) is not None:
                payload[ts_col] = payload[ts_col].isoformat()
        items.append(payload)

    return {
        "success": True,
        "count": len(items),
        "filters_applied": {
            "site_id": site_id, "site_name": site_name or None,
            "status": status_value or None,
            "contains_lat": contains_lat, "contains_lon": contains_lon,
            "building_id": building_id, "limit": limit,
        },
        "items": items,
    }
