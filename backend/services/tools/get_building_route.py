"""Tool: get_building_route — get OSRM driving route between two coordinates."""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from services.gis import query_osrm_route
from services.tools._shared import _safe_float

logger = logging.getLogger(__name__)

OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "http://osrm:5000").rstrip("/")


async def _fetch_osrm_steps(
    start_lat: float, start_lon: float,
    end_lat: float, end_lon: float,
) -> list[str]:
    route_url = (
        f"{OSRM_BASE_URL}/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}"
    )
    params = {"overview": "false", "steps": "true", "geometries": "geojson"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(route_url, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return []

    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        return []
    legs = routes[0].get("legs")
    if not isinstance(legs, list) or not legs:
        return []
    steps = legs[0].get("steps")
    if not isinstance(steps, list):
        return []

    results: list[str] = []
    for step in steps[:25]:
        if not isinstance(step, dict):
            continue
        maneuver = step.get("maneuver") if isinstance(step.get("maneuver"), dict) else {}
        instruction = maneuver.get("instruction") or maneuver.get("type") or "Continue"
        distance = step.get("distance")
        dist_str = f"{int(round(float(distance)))}m" if isinstance(distance, (int, float)) else ""
        results.append(f"{instruction} {dist_str}".strip())
    return results


async def get_building_route(
    tool_args: dict[str, Any],
    db: Any,  # not used — kept for uniform dispatch signature
) -> dict[str, Any]:
    from_lat = _safe_float(tool_args.get("from_lat"))
    from_lon = _safe_float(tool_args.get("from_lon"))
    to_lat = _safe_float(tool_args.get("to_lat"))
    to_lon = _safe_float(tool_args.get("to_lon"))
    profile = str(tool_args.get("profile") or "driving")

    if None in {from_lat, from_lon, to_lat, to_lon}:
        return {"success": False, "error": "from/to coordinates required"}

    route = await query_osrm_route(
        start_lat=float(from_lat),  # type: ignore[arg-type]
        start_lon=float(from_lon),  # type: ignore[arg-type]
        end_lat=float(to_lat),  # type: ignore[arg-type]
        end_lon=float(to_lon),  # type: ignore[arg-type]
        profile=profile,
    )
    steps = await _fetch_osrm_steps(
        float(from_lat), float(from_lon),  # type: ignore[arg-type]
        float(to_lat), float(to_lon),  # type: ignore[arg-type]
    )
    return {
        "success": route.found,
        "profile": route.profile,
        "distance_m": route.distance_m,
        "duration_s": route.duration_s,
        "geometry_geojson": route.geometry_geojson,
        "steps": steps,
        "warnings": route.warnings,
        "error": route.error,
    }
