"""Tool: get_nearest_shelter — find the nearest shelter with OSRM route data."""
from __future__ import annotations

import logging
import re

import asyncpg

from services.gis import query_osrm_route

logger = logging.getLogger(__name__)

_SHELTER_AMENITIES = (
    "hospital", "clinic", "school", "townhall",
    "place_of_worship", "police", "pharmacy",
)
_SHELTER_DESCRIPTIONS = {
    "hospital": "Hospital — full medical facility",
    "clinic": "Clinic — medical treatment",
    "school": "School — large shelter space",
    "townhall": "Town hall — coordination center",
    "place_of_worship": "Mosque — community gathering point",
    "police": "Police station — security and coordination",
    "pharmacy": "Pharmacy — medical supplies",
}
_PRIORITY_MAP = {
    "hospital": 1, "clinic": 1,
    "school": 2, "townhall": 2,
    "place_of_worship": 3, "police": 3,
    "pharmacy": 4,
}

_SHELTER_SQL = """
    WITH candidates AS (
        SELECT
            to_jsonb(turkey_points) - 'geom' AS shelter_data,
            amenity AS shelter_type,
            ST_X(ST_Centroid(geom)) AS shelter_lon,
            ST_Y(ST_Centroid(geom)) AS shelter_lat,
            ST_Distance(
                geom::geography,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
            ) AS distance_m,
            CASE
                WHEN amenity IN ('hospital', 'clinic') THEN 1
                WHEN amenity IN ('school', 'townhall') THEN 2
                WHEN amenity IN ('place_of_worship', 'police') THEN 3
                WHEN amenity = 'pharmacy' THEN 4
                ELSE 5
            END AS priority_rank
        FROM turkey_points
        WHERE amenity = ANY($3::text[])
    ),
    per_priority AS (
        SELECT
            shelter_data, shelter_type, shelter_lon, shelter_lat,
            distance_m, priority_rank,
            ROW_NUMBER() OVER (PARTITION BY priority_rank ORDER BY distance_m ASC) AS rank_in_priority
        FROM candidates
    )
    SELECT shelter_data, shelter_type, shelter_lon, shelter_lat, distance_m, priority_rank
    FROM per_priority
    WHERE rank_in_priority = 1
    ORDER BY distance_m ASC
    LIMIT 1
"""

_ROAD_SQL = """
    SELECT name, highway,
        ST_Distance(
            geom::geography,
            ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
        ) AS distance_m
    FROM turkey_lines
    WHERE highway IS NOT NULL AND name IS NOT NULL
    ORDER BY distance_m ASC
    LIMIT 1
"""


async def _resolve_origin(
    tool_args: dict,
    db: asyncpg.Connection | asyncpg.Pool,
) -> dict:
    """Resolve query origin from either direct lat/lon or a site reference."""
    lat_raw = tool_args.get("lat")
    lon_raw = tool_args.get("lon")
    if isinstance(lat_raw, (int, float)) and isinstance(lon_raw, (int, float)):
        return {
            "found": True, "lat": float(lat_raw), "lon": float(lon_raw),
            "origin_type": "coordinates",
            "origin_label": f"{float(lat_raw):.6f}, {float(lon_raw):.6f}",
            "site_id": None, "site_name": None,
            "site_boundary_geojson": None, "site_area_m2": None, "message": None,
        }

    site_id_raw = tool_args.get("site_id")
    site_name_raw = str(tool_args.get("site_name") or "").strip()
    parsed_site_id = None
    if site_id_raw is None and site_name_raw:
        m = re.match(r"^\s*site\s+(\d+)\s*$", site_name_raw, flags=re.IGNORECASE)
        if m:
            parsed_site_id = int(m.group(1))

    site_id = int(site_id_raw) if site_id_raw is not None else parsed_site_id
    if site_id is None and not site_name_raw:
        return {
            "found": False, "lat": None, "lon": None,
            "origin_type": "unknown", "origin_label": None,
            "site_id": None, "site_name": None,
            "site_boundary_geojson": None, "site_area_m2": None,
            "message": "Provide either lat/lon or site_id/site_name.",
        }

    try:
        if site_id is not None:
            site_row = await db.fetchrow(
                """
                SELECT id, name,
                    ST_Y(ST_PointOnSurface(boundary)) AS centroid_lat,
                    ST_X(ST_PointOnSurface(boundary)) AS centroid_lon,
                    ST_AsGeoJSON(boundary)::jsonb AS boundary_geojson,
                    ST_Area(boundary::geography) AS area_m2
                FROM sites WHERE id = $1 LIMIT 1
                """,
                site_id,
            )
        else:
            site_row = await db.fetchrow(
                """
                SELECT id, name,
                    ST_Y(ST_PointOnSurface(boundary)) AS centroid_lat,
                    ST_X(ST_PointOnSurface(boundary)) AS centroid_lon,
                    ST_AsGeoJSON(boundary)::jsonb AS boundary_geojson,
                    ST_Area(boundary::geography) AS area_m2
                FROM sites
                WHERE LOWER(name) = LOWER($1) OR name ILIKE $2
                ORDER BY CASE WHEN LOWER(name) = LOWER($1) THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                site_name_raw, f"%{site_name_raw}%",
            )
    except asyncpg.exceptions.UndefinedTableError:
        ref = f"site_id={site_id}" if site_id is not None else f"site_name='{site_name_raw}'"
        return {
            "found": False, "lat": None, "lon": None,
            "origin_type": "site", "origin_label": ref,
            "site_id": site_id, "site_name": site_name_raw or None,
            "site_boundary_geojson": None, "site_area_m2": None,
            "message": "Sites table is not available.",
        }

    if site_row is None:
        ref = f"site_id={site_id}" if site_id is not None else f"site_name='{site_name_raw}'"
        return {
            "found": False, "lat": None, "lon": None,
            "origin_type": "site", "origin_label": ref,
            "site_id": site_id, "site_name": site_name_raw or None,
            "site_boundary_geojson": None, "site_area_m2": None,
            "message": f"Site not found for {ref}.",
        }

    clat = site_row.get("centroid_lat")
    clon = site_row.get("centroid_lon")
    resolved_name = site_row.get("name")
    resolved_id = site_row.get("id")

    if not isinstance(clat, (int, float)) or not isinstance(clon, (int, float)):
        return {
            "found": False, "lat": None, "lon": None,
            "origin_type": "site",
            "origin_label": str(resolved_name or site_name_raw or site_id),
            "site_id": int(resolved_id) if resolved_id is not None else site_id,
            "site_name": str(resolved_name) if isinstance(resolved_name, str) else (site_name_raw or None),
            "site_boundary_geojson": site_row.get("boundary_geojson"),
            "site_area_m2": float(site_row.get("area_m2")) if isinstance(site_row.get("area_m2"), (int, float)) else None,
            "message": "Site exists but has no boundary geometry centroid.",
        }

    return {
        "found": True, "lat": float(clat), "lon": float(clon),
        "origin_type": "site",
        "origin_label": str(resolved_name or f"site {resolved_id}"),
        "site_id": int(resolved_id) if resolved_id is not None else None,
        "site_name": str(resolved_name) if isinstance(resolved_name, str) else (site_name_raw or None),
        "site_boundary_geojson": site_row.get("boundary_geojson"),
        "site_area_m2": float(site_row.get("area_m2")) if isinstance(site_row.get("area_m2"), (int, float)) else None,
        "message": None,
    }


async def get_nearest_shelter(
    tool_args: dict,
    db: asyncpg.Connection | asyncpg.Pool,
) -> dict:
    origin = await _resolve_origin(tool_args, db)
    lat = origin.get("lat")
    lon = origin.get("lon")
    logger.info(
        "tool.get_nearest_shelter.started lat=%s lon=%s origin_type=%s",
        lat, lon, origin.get("origin_type"),
    )

    result = {
        "name": None, "name_en": None,
        "shelter_type": None, "shelter_description": None, "shelter_priority": None,
        "distance_m": None, "street": None, "house_number": None,
        "operator": None, "beds": None, "province": None,
        "nearest_road": None, "road_distance_m": None,
        "route_distance_m": None, "route_duration_s": None,
        "route_profile": "driving", "route_found": False,
        "route_geometry_geojson": None, "route_name": None, "route_warnings": [],
        "query_origin_type": origin.get("origin_type"),
        "query_origin_label": origin.get("origin_label"),
        "query_origin_lat": lat,
        "query_origin_lon": lon,
        "site_id": origin.get("site_id"),
        "site_name": origin.get("site_name"),
        "site_boundary_geojson": origin.get("site_boundary_geojson"),
        "site_area_m2": origin.get("site_area_m2"),
        "site_exists": bool(origin.get("found")) if origin.get("origin_type") == "site" else None,
        "message": origin.get("message"),
        "found": False,
    }

    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        logger.info(
            "tool.get_nearest_shelter.completed found=%s message=%s",
            result.get("found"), result.get("message"),
        )
        return result

    shelter_row = await db.fetchrow(_SHELTER_SQL, lon, lat, list(_SHELTER_AMENITIES))
    road_row = await db.fetchrow(_ROAD_SQL, lon, lat)

    if shelter_row:
        shelter_data = shelter_row["shelter_data"] if isinstance(shelter_row["shelter_data"], dict) else {}
        shelter_type = shelter_row["shelter_type"] if isinstance(shelter_row["shelter_type"], str) else None
        shelter_lon = shelter_row["shelter_lon"]
        shelter_lat = shelter_row["shelter_lat"]

        route_result = await query_osrm_route(
            start_lat=lat, start_lon=lon,
            end_lat=float(shelter_lat), end_lon=float(shelter_lon),
            profile="driving",
        )
        result.update({
            "name": shelter_data.get("name") if isinstance(shelter_data.get("name"), str) else None,
            "name_en": shelter_data.get("name_en") if isinstance(shelter_data.get("name_en"), str) else None,
            "shelter_type": shelter_type,
            "shelter_description": _SHELTER_DESCRIPTIONS.get(
                shelter_type, f"{shelter_type} — emergency facility" if shelter_type else None,
            ),
            "shelter_priority": _PRIORITY_MAP.get(shelter_type) if shelter_type else None,
            "distance_m": round(float(shelter_row["distance_m"]), 1),
            "street": shelter_data.get("addr_stree") if isinstance(shelter_data.get("addr_stree"), str) else None,
            "house_number": shelter_data.get("addr_house") if isinstance(shelter_data.get("addr_house"), str) else None,
            "operator": shelter_data.get("operator") if isinstance(shelter_data.get("operator"), str) else None,
            "beds": shelter_data.get("beds"),
            "province": shelter_data.get("province") if isinstance(shelter_data.get("province"), str) else None,
            "route_distance_m": route_result.distance_m,
            "route_duration_s": route_result.duration_s,
            "route_profile": route_result.profile,
            "route_found": route_result.found,
            "route_geometry_geojson": route_result.geometry_geojson,
            "route_name": (
                f"{origin.get('origin_label') or 'origin'} → "
                f"{shelter_data.get('name') if isinstance(shelter_data.get('name'), str) else (shelter_type or 'nearest shelter')}"
            ),
            "route_warnings": route_result.warnings,
            "found": True,
        })

    if road_row:
        result.update({
            "nearest_road": road_row["name"],
            "road_distance_m": round(float(road_row["distance_m"]), 1),
        })

    logger.info(
        "tool.get_nearest_shelter.completed found=%s shelter_type=%s distance_m=%s route_found=%s",
        result.get("found"), result.get("shelter_type"),
        result.get("distance_m"), result.get("route_found"),
    )
    return result
