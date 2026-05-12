"""Tool: get_location_info — resolve province and district for a GPS coordinate."""
from __future__ import annotations

import logging

import asyncpg

from services.gis import query_location_info_by_point

logger = logging.getLogger(__name__)


async def get_location_info(lat: float, lon: float, db: asyncpg.Connection | asyncpg.Pool) -> dict:
    logger.info("tool.get_location_info.started lat=%s lon=%s", lat, lon)
    result = await query_location_info_by_point(lat=lat, lon=lon, db=db)
    payload = {
        "found": result.found,
        "province": result.province,
        "district": result.district,
        "province_data": result.province_data,
        "district_data": result.district_data,
        "nearest_point_data": result.nearest_point_data,
        "district_distance_m": result.district_distance_m,
        "nearest_point_distance_m": result.nearest_point_distance_m,
    }
    logger.info(
        "tool.get_location_info.completed found=%s province=%s district=%s",
        payload["found"], payload["province"], payload["district"],
    )
    return payload
