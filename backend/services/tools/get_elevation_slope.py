"""Tool: get_elevation_slope — retrieve DEM elevation and slope for a GPS coordinate."""
from __future__ import annotations

import logging

from services.gis import query_dem_elevation_by_point

logger = logging.getLogger(__name__)


def get_elevation_slope(lat: float, lon: float, dem_path: str | None = None) -> dict:
    """Synchronous — run in executor for async contexts."""
    logger.info("tool.get_elevation_slope.started lat=%s lon=%s", lat, lon)
    result = query_dem_elevation_by_point(lat=lat, lon=lon)
    payload = result.model_dump()
    logger.info(
        "tool.get_elevation_slope.completed found=%s elevation_m=%s slope_degrees=%s",
        payload.get("found"), payload.get("elevation_m"), payload.get("slope_degrees"),
    )
    return payload
