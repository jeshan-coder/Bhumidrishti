"""Tool: get_flood_zone — check whether a coordinate falls inside a flood zone."""
from __future__ import annotations

import logging

import asyncpg

from services.gis import query_flood_zone_by_point

logger = logging.getLogger(__name__)


async def get_flood_zone(lat: float, lon: float, db: asyncpg.Connection | asyncpg.Pool) -> dict:
    logger.info("tool.get_flood_zone.started lat=%s lon=%s", lat, lon)
    result = await query_flood_zone_by_point(lat=lat, lon=lon, db=db)
    flood_zone_data = result.flood_zone_data if isinstance(result.flood_zone_data, dict) else None

    warnings = []
    if result.is_flood_zone and flood_zone_data is None:
        warnings.append("flood_zone_attributes_unavailable")
        logger.warning("tool.get_flood_zone.sparse_payload lat=%s lon=%s", lat, lon)

    payload = {
        "is_flood_zone": result.is_flood_zone,
        "waterway_type": result.waterway_type,
        "waterway_name": result.waterway_name,
        "distance_to_waterway_m": result.distance_to_waterway_m,
        "province": result.province,
        "flood_zone_data": flood_zone_data,
        "warnings": warnings,
    }
    if isinstance(flood_zone_data, dict):
        payload.update(flood_zone_data)

    logger.info("tool.get_flood_zone.completed is_flood_zone=%s", payload["is_flood_zone"])
    return payload
