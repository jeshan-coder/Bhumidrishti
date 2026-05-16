"""Tool: get_centroid — resolve any named entity or geometry to a lat/lon centroid."""
from __future__ import annotations

import json
import logging
import re

import asyncpg

logger = logging.getLogger(__name__)


async def get_centroid(
    tool_args: dict,
    db: asyncpg.Connection | asyncpg.Pool,
) -> dict:
    """Resolve a site, building, or raw GeoJSON geometry to its centroid lat/lon.

    Priority order:
      1. site_id / site_name   → centroid of site boundary polygon
      2. osm_id                → centroid of OSM building polygon
      3. geometry              → centroid of provided GeoJSON geometry object
    """
    site_id_raw  = tool_args.get("site_id")
    site_name_raw = str(tool_args.get("site_name") or "").strip()
    osm_id_raw   = tool_args.get("osm_id")
    geometry_raw = tool_args.get("geometry")

    # ── 1. Site by ID or name ──────────────────────────────────────────────
    if site_id_raw is not None or site_name_raw:
        # Accept "site 3" shorthand
        parsed_site_id = None
        if site_id_raw is None and site_name_raw:
            m = re.match(r"^\s*site\s+(\d+)\s*$", site_name_raw, re.IGNORECASE)
            if m:
                parsed_site_id = int(m.group(1))

        site_id = int(site_id_raw) if site_id_raw is not None else parsed_site_id

        try:
            if site_id is not None:
                row = await db.fetchrow(
                    """
                    SELECT id, name,
                        ST_Y(ST_PointOnSurface(boundary)) AS lat,
                        ST_X(ST_PointOnSurface(boundary)) AS lon,
                        ST_Area(boundary::geography)       AS area_m2
                    FROM sites WHERE id = $1 LIMIT 1
                    """,
                    site_id,
                )
            else:
                row = await db.fetchrow(
                    """
                    SELECT id, name,
                        ST_Y(ST_PointOnSurface(boundary)) AS lat,
                        ST_X(ST_PointOnSurface(boundary)) AS lon,
                        ST_Area(boundary::geography)       AS area_m2
                    FROM sites
                    WHERE LOWER(name) = LOWER($1) OR name ILIKE $2
                    ORDER BY CASE WHEN LOWER(name) = LOWER($1) THEN 0 ELSE 1 END,
                             updated_at DESC
                    LIMIT 1
                    """,
                    site_name_raw, f"%{site_name_raw}%",
                )
        except asyncpg.exceptions.UndefinedTableError:
            return {"found": False, "error": "Sites table not available.", "lat": None, "lon": None}

        if row is None:
            ref = f"site_id={site_id}" if site_id is not None else f"site_name='{site_name_raw}'"
            return {"found": False, "error": f"Site not found: {ref}", "lat": None, "lon": None}

        lat, lon = row["lat"], row["lon"]
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return {
                "found": False,
                "error": f"Site '{row['name']}' has no boundary geometry.",
                "lat": None, "lon": None,
                "site_id": int(row["id"]), "site_name": str(row["name"]),
            }

        logger.info("tool.get_centroid site_id=%s name=%s lat=%s lon=%s", row["id"], row["name"], lat, lon)
        return {
            "found": True,
            "lat": float(lat), "lon": float(lon),
            "label": str(row["name"]),
            "entity_type": "site",
            "site_id": int(row["id"]),
            "site_name": str(row["name"]),
            "area_m2": float(row["area_m2"]) if isinstance(row.get("area_m2"), (int, float)) else None,
        }

    # ── 2. OSM building by osm_id ──────────────────────────────────────────
    if osm_id_raw is not None:
        osm_id = int(osm_id_raw)
        try:
            row = await db.fetchrow(
                """
                SELECT osm_id,
                    ST_Y(ST_Centroid(geom)) AS lat,
                    ST_X(ST_Centroid(geom)) AS lon
                FROM turkey_polygons
                WHERE osm_id = $1 LIMIT 1
                """,
                osm_id,
            )
        except asyncpg.exceptions.UndefinedTableError:
            return {"found": False, "error": "Building table not available.", "lat": None, "lon": None}

        if row is None:
            return {"found": False, "error": f"Building osm_id={osm_id} not found.", "lat": None, "lon": None}

        lat, lon = row["lat"], row["lon"]
        logger.info("tool.get_centroid osm_id=%s lat=%s lon=%s", osm_id, lat, lon)
        return {
            "found": True,
            "lat": float(lat), "lon": float(lon),
            "label": f"Building {osm_id}",
            "entity_type": "building",
            "osm_id": osm_id,
        }

    # ── 3. Raw GeoJSON geometry ────────────────────────────────────────────
    if geometry_raw is not None:
        geom_str = geometry_raw if isinstance(geometry_raw, str) else json.dumps(geometry_raw)
        try:
            row = await db.fetchrow(
                """
                SELECT
                    ST_Y(ST_Centroid(ST_GeomFromGeoJSON($1))) AS lat,
                    ST_X(ST_Centroid(ST_GeomFromGeoJSON($1))) AS lon
                """,
                geom_str,
            )
        except Exception as exc:
            return {"found": False, "error": f"Invalid geometry: {exc}", "lat": None, "lon": None}

        if row is None or row["lat"] is None:
            return {"found": False, "error": "Could not compute centroid from geometry.", "lat": None, "lon": None}

        lat, lon = float(row["lat"]), float(row["lon"])
        logger.info("tool.get_centroid geometry centroid lat=%s lon=%s", lat, lon)
        return {
            "found": True,
            "lat": lat, "lon": lon,
            "label": "geometry centroid",
            "entity_type": "geometry",
        }

    return {
        "found": False,
        "error": "Provide site_id, site_name, osm_id, or geometry.",
        "lat": None, "lon": None,
    }
