"""Tool: get_building_info — query OSM building data at a GPS coordinate."""
from __future__ import annotations

import logging

import asyncpg

from services.gis import query_turkey_building_by_point

logger = logging.getLogger(__name__)


async def get_building_info(lat: float, lon: float, db: asyncpg.Connection | asyncpg.Pool) -> dict:
    logger.info("tool.get_building_info.started lat=%s lon=%s", lat, lon)
    result = await query_turkey_building_by_point(lat=lat, lon=lon, db=db)
    building_data = result.building_data if isinstance(result.building_data, dict) else None

    warnings = list(result.warnings)
    if result.found and building_data is None:
        warnings.append("building_attributes_unavailable")
        logger.warning(
            "tool.get_building_info.sparse_payload lat=%s lon=%s match=%s",
            lat, lon, result.match_strategy,
        )

    payload = {
        "building_type": building_data.get("building") if building_data else None,
        "building_floors": (
            building_data.get("building:levels")
            or building_data.get("building_lev")
            or building_data.get("building_levels")
        ) if building_data else None,
        "building_material": (
            building_data.get("building:materia")
            or building_data.get("building_m")
            or building_data.get("building_material")
            or building_data.get("roof_mater")
        ) if building_data else None,
        "osm_id": building_data.get("osm_id") if building_data else None,
        "name": building_data.get("name") if building_data else None,
        "found": result.found,
        "match_strategy": result.match_strategy,
        "distance_m": result.distance_m,
        "warnings": warnings,
        "building_data": building_data,
    }
    logger.info(
        "tool.get_building_info.completed found=%s match=%s",
        payload["found"], payload["match_strategy"],
    )
    return payload
