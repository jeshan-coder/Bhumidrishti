"""Tool: get_building_report_data — fetch full assessment data for a single building report."""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

_QUERY = """
    SELECT
        a.id, a.site_name, a.province, a.district, a.lat, a.lon,
        a.severity, a.damage_type, a.damage_description, a.structural_risk,
        a.building_type, a.building_floors, a.building_material,
        a.estimated_occupants, a.occupant_status, a.recommended_action,
        a.action_priority, a.flood_zone, a.elevation_m, a.slope_degrees,
        a.slope_risk, a.nearest_road, a.road_distance_m, a.road_access,
        a.nearest_shelter, a.shelter_type, a.shelter_distance_m,
        a.confidence, a.reasoning, a.warnings, a.turkish_summary,
        a.model_used, a.photo_path, a.chip_path, a.pre_chip_path, a.drone_frames,
        COALESCE(NULLIF(b.site_name, ''), NULLIF(a.site_name, ''), 'Unknown') AS resolved_site_name
    FROM assessments a
    LEFT JOIN batches b ON a.batch_id = b.id
    WHERE a.id = $1
    LIMIT 1
"""


async def _fetch_by_id(
    db: asyncpg.Connection | asyncpg.Pool,
    assessment_id: str,
) -> asyncpg.Record | None:
    if isinstance(db, asyncpg.Pool):
        async with db.acquire() as conn:
            return await conn.fetchrow(_QUERY, assessment_id)
    return await db.fetchrow(_QUERY, assessment_id)


async def get_building_report_data(
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    if db is None:
        return {"success": False, "error": "Database unavailable"}
    assessment_id = str(tool_args.get("assessment_id") or "").strip()
    if not assessment_id:
        return {"success": False, "error": "assessment_id is required"}
    row = await _fetch_by_id(db, assessment_id)
    if row is None:
        return {"success": False, "error": f"assessment not found: {assessment_id}"}
    return {"success": True, "building": dict(row)}
