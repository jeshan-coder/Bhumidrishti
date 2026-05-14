"""Tool: get_building_info — query OSM building data by point, OSM ID, or geometry."""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

from services.gis import (
    query_turkey_building_by_geometry,
    query_turkey_building_by_osm_id,
    query_turkey_building_by_point,
)

logger = logging.getLogger(__name__)


async def get_building_info(
    lat: float | None = None,
    lon: float | None = None,
    db: asyncpg.Connection | asyncpg.Pool | None = None,
    osm_id: int | str | None = None,
    geometry: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Resolve building attributes from OSM ID, GeoJSON geometry, or GPS coordinates."""
    if db is None:
        return {
            "building_type": None,
            "building_floors": None,
            "building_material": None,
            "osm_id": None,
            "name": None,
            "found": False,
            "match_strategy": "none",
            "distance_m": None,
            "warnings": ["database_unavailable"],
            "building_data": None,
        }

    logger.info(
        "tool.get_building_info.started lat=%s lon=%s osm_id=%s has_geometry=%s",
        lat,
        lon,
        osm_id,
        geometry is not None,
    )

    if osm_id is not None:
        try:
            result = await query_turkey_building_by_osm_id(osm_id=int(osm_id), db=db)
        except (TypeError, ValueError):
            result = await query_turkey_building_by_geometry(geometry=geometry, db=db) if geometry is not None else None
            if result is None:
                return {
                    "building_type": None,
                    "building_floors": None,
                    "building_material": None,
                    "osm_id": None,
                    "name": None,
                    "found": False,
                    "match_strategy": "none",
                    "distance_m": None,
                    "warnings": ["invalid_osm_id"],
                    "building_data": None,
                }
    elif geometry is not None:
        result = await query_turkey_building_by_geometry(geometry=geometry, db=db)
    elif lat is not None and lon is not None:
        result = await query_turkey_building_by_point(lat=lat, lon=lon, db=db)
    else:
        return {
            "building_type": None,
            "building_floors": None,
            "building_material": None,
            "osm_id": None,
            "name": None,
            "found": False,
            "match_strategy": "none",
            "distance_m": None,
            "warnings": ["missing_lookup_input"],
            "building_data": None,
        }

    building_data = result.building_data if isinstance(result.building_data, dict) else None

    warnings = list(result.warnings)
    if result.found and building_data is None:
        warnings.append("building_attributes_unavailable")
        logger.warning(
            "tool.get_building_info.sparse_payload lat=%s lon=%s osm_id=%s match=%s",
            lat, lon, osm_id, result.match_strategy,
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
