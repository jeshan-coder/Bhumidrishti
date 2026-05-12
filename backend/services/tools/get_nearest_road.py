"""Tool: get_nearest_road — find the nearest named road to a GPS coordinate."""
from __future__ import annotations

import logging

import asyncpg

from services.gis import query_nearest_road_by_point

logger = logging.getLogger(__name__)


async def get_nearest_road(lat: float, lon: float, db: asyncpg.Connection | asyncpg.Pool) -> dict:
    logger.info("tool.get_nearest_road.started lat=%s lon=%s", lat, lon)
    result = await query_nearest_road_by_point(lat=lat, lon=lon, db=db)
    payload = result.model_dump()
    logger.info(
        "tool.get_nearest_road.completed found=%s distance_m=%s",
        payload.get("found"), payload.get("distance_m"),
    )
    return payload
