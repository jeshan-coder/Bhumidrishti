"""Gemma-4 analysis pipeline for ground photos and video frames."""

import asyncio
import base64
import json
import logging
import time
from typing import Any, Awaitable, Callable

import asyncpg
import ollama
from prompts.gemma_system_prompt import (
    ORTHOPHOTO_ASSESSMENT_SYSTEM_PROMPT,
    PHOTO_ASSESSMENT_SYSTEM_PROMPT,
)
from services.ai_runtime import ACTIVE_GEMMA_MODEL
from services.gis import (
    query_dem_elevation_by_point,
    query_flood_zone_by_point,
    query_location_info_by_point,
    query_nearest_road_by_point,
    query_osrm_route,
    query_turkey_building_by_point,
)

# =============================================================
# SYSTEM PROMPT
# =============================================================

# This variable switches the active prompt to the centralized photo assessment prompt file.
SYSTEM_PROMPT = PHOTO_ASSESSMENT_SYSTEM_PROMPT

# This variable stores the orthophoto-specific prompt with aerial assessment addendum.
ORTHOPHOTO_SYSTEM_PROMPT = ORTHOPHOTO_ASSESSMENT_SYSTEM_PROMPT

# This variable stores the module logger used for pipeline debugging.
logger = logging.getLogger(__name__)


def _safe_json(data: Any) -> str:
    """Serialize any payload for logs without crashing logger calls."""
    try:
        return json.dumps(data, default=str, ensure_ascii=False)
    except Exception:
        return str(data)


def _log_agent_event(stage: str, payload: dict[str, Any]) -> None:
    """Write one structured agent log line with stage and payload."""
    logger.info("agent_stage=%s payload=%s", stage, _safe_json(payload))


def request_assessment_json_repair(
    model: str,
    system_prompt: str,
    raw_assistant_output: str,
) -> str:
    """Request a strict JSON-only rewrite when the model returns prose instead of JSON."""
    # This variable stores the strict repair instruction used to coerce valid JSON output.
    repair_instruction = (
        "The previous assistant output was not valid JSON. "
        "Rewrite it as ONE valid JSON object only, with no markdown and no extra text.\n\n"
        "Required keys: severity, damage_type, damage_description, structural_risk, "
        "building_type, building_floors, building_material, estimated_occupants, occupant_status, "
        "recommended_action, action_priority, flood_zone, elevation_m, slope_degrees, slope_risk, "
        "nearest_shelter, shelter_distance_m, shelter_type, road_access, nearest_road, road_distance_m, "
        "reasoning, warnings, confidence, turkish_summary.\n\n"
        "Rules: severity must be 1-5, action_priority 1-5, confidence 0-1, warnings must be an array.\n\n"
        "Previous output:\n"
        f"{raw_assistant_output}"
    )

    repair_response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": repair_instruction},
        ],
        options={"temperature": 0.0, "num_ctx": 8192},
    )

    return repair_response.message.content or ""

# =============================================================
# TOOLS
# =============================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_building_info",
            "description": (
                "Get building information from the local PostGIS database "
                "for the building at the given GPS coordinates. "
                "Returns building type, number of floors, construction material, "
                "and OSM building ID if a matching footprint exists. "
                "Call this first before any other tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {
                        "type": "number",
                        "description": "Latitude of the building in decimal degrees WGS84"
                    },
                    "lon": {
                        "type": "number",
                        "description": "Longitude of the building in decimal degrees WGS84"
                    }
                },
                "required": ["lat", "lon"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_flood_zone",
            "description": (
                "Check if the given GPS coordinates fall within a flood risk zone. "
                "Flood zones are derived from a 300 metre buffer around all waterways "
                "in the local PostGIS database. "
                "Returns whether the location is in a flood zone and the return period. "
                "This affects rescue priority — buildings in flood zones face additional risk."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {
                        "type": "number",
                        "description": "Latitude in decimal degrees WGS84"
                    },
                    "lon": {
                        "type": "number",
                        "description": "Longitude in decimal degrees WGS84"
                    }
                },
                "required": ["lat", "lon"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_location_info",
            "description": (
                "Get location context for the given GPS coordinates from local GIS layers. "
                "Returns exact province from turkey_provinces polygon containment, "
                "district approximation from nearest turkey_districts_pts centroid, "
                "and nearest turkey_points feature as fallback locality context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {
                        "type": "number",
                        "description": "Latitude in decimal degrees WGS84"
                    },
                    "lon": {
                        "type": "number",
                        "description": "Longitude in decimal degrees WGS84"
                    }
                },
                "required": ["lat", "lon"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_nearest_road",
            "description": (
                "Find the nearest road to the given GPS coordinates from local turkey_lines data. "
                "Only highway features are queried. "
                "Returns road name, highway type, surface, bridge/tunnel flags, "
                "distance in metres, and road access category."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {
                        "type": "number",
                        "description": "Latitude in decimal degrees WGS84"
                    },
                    "lon": {
                        "type": "number",
                        "description": "Longitude in decimal degrees WGS84"
                    }
                },
                "required": ["lat", "lon"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_elevation_slope",
            "description": (
                "Get terrain elevation and slope at the given GPS coordinates "
                "from the local Digital Elevation Model (GLO-30 DEM). "
                "Returns elevation in metres above sea level, slope in degrees, "
                "and slope risk level. "
                "High slope increases collapse risk and complicates rescue access. "
                "Call this after get_building_info and get_flood_zone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {
                        "type": "number",
                        "description": "Latitude in decimal degrees WGS84"
                    },
                    "lon": {
                        "type": "number",
                        "description": "Longitude in decimal degrees WGS84"
                    }
                },
                "required": ["lat", "lon"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_nearest_shelter",
            "description": (
                "Find the nearest shelter or safe facility to the given GPS coordinates "
                "from the local PostGIS database. "
                "Searches for hospitals, clinics, schools, town halls, "
                "places of worship, police stations, and pharmacies. "
                "Returns the facility name, straight-line distance in metres, "
                "facility type, and nearest road name. "
                "Call this after all road, flood, and building context tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {
                        "type": "number",
                        "description": "Latitude in decimal degrees WGS84"
                    },
                    "lon": {
                        "type": "number",
                        "description": "Longitude in decimal degrees WGS84"
                    }
                },
                "required": ["lat", "lon"]
            }
        }
    }
]

# This variable stores chat-specific tools that include coordinator data access.
CHAT_TOOLS = [
    *TOOLS,
    {
        "type": "function",
        "function": {
            "name": "get_assessments",
            "description": (
                "Query assessments with optional filters for site, severity, status, "
                "occupant status, flood risk, building, and sorting. "
                "Returns full assessment fields for response coordination."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "integer", "description": "Filter by sites.id"},
                    "site_name": {"type": "string", "description": "Partial match on site name"},
                    "assessment_id": {"type": "string", "description": "Exact assessment id (e.g. ASS-2847)"},
                    "building_id": {"type": "integer", "description": "Filter by assessments.osm_building_id"},
                    "batch_id": {"type": "string", "description": "Filter by assessments.batch_id"},
                    "severity_min": {"type": "integer", "description": "Minimum severity 1-5"},
                    "severity_max": {"type": "integer", "description": "Maximum severity 1-5"},
                    "status": {"type": "string", "description": "pending, in_review, responded, closed, false_positive"},
                    "occupant_status": {
                        "type": "string",
                        "description": "occupant_status filter (trapped, signs_of_life, potentially_trapped, evacuated, unknown)"
                    },
                    "flood_zone": {"type": "boolean", "description": "Filter by flood zone true/false"},
                    "limit": {"type": "integer", "description": "Rows to return (default 10, max 200)"},
                    "order_by": {
                        "type": "string",
                        "description": "Sort field: severity, created_at, action_priority",
                    },
                    "order_dir": {
                        "type": "string",
                        "description": "Sort direction: asc or desc (default desc)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sites",
            "description": (
                "Query sites with optional filters and summary counts. "
                "Supports lookup by id/name, geometry containment by lat/lon, "
                "or by building OSM id lying inside site boundary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "integer", "description": "Exact sites.id"},
                    "site_name": {"type": "string", "description": "Partial site name match"},
                    "status": {"type": "string", "description": "active, processing, completed"},
                    "contains_lat": {"type": "number", "description": "Latitude for point-in-site filter"},
                    "contains_lon": {"type": "number", "description": "Longitude for point-in-site filter"},
                    "building_id": {
                        "type": "integer",
                        "description": "Return sites containing this turkey_buildings.osm_id geometry",
                    },
                    "limit": {"type": "integer", "description": "Rows to return (default 20, max 200)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_field_workers",
            "description": (
                "List field workers and their assignment status. "
                "Use this before dispatch to select an available worker. "
                "Workers with status 'busy' cannot receive new assignments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Optional filter: available or busy",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Rows to return (default 50, max 200)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_assessments",
            "description": (
                "Dispatch one or more assessments to a field worker. "
                "Sets assessment status to 'responded' and assigns worker name. "
                "Supports direct assessment_ids or filtered bulk dispatch by site/severity/status. "
                "If worker is busy, dispatch is rejected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "assessment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Explicit assessment ids to dispatch",
                    },
                    "assessment_id": {
                        "type": "string",
                        "description": "Single assessment id shortcut",
                    },
                    "site_name": {
                        "type": "string",
                        "description": "Optional site_name filter for bulk dispatch",
                    },
                    "severity_min": {
                        "type": "integer",
                        "description": "Optional minimum severity filter",
                    },
                    "severity_max": {
                        "type": "integer",
                        "description": "Optional maximum severity filter",
                    },
                    "status": {
                        "type": "string",
                        "description": "Optional current status filter (default pending)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Bulk dispatch row limit (default 50, max 200)",
                    },
                    "worker_name": {
                        "type": "string",
                        "description": "Worker name to assign",
                    },
                    "create_worker_if_missing": {
                        "type": "boolean",
                        "description": "Create worker if it does not exist (default true)",
                    },
                },
                "required": ["worker_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_assessment_status",
            "description": (
                "Update status for one or more assessments. "
                "Supported target statuses: responded, closed. "
                "When status becomes closed, assigned worker is released (available)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "assessment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Assessment ids to update",
                    },
                    "assessment_id": {
                        "type": "string",
                        "description": "Single assessment id shortcut",
                    },
                    "status": {
                        "type": "string",
                        "description": "Target status: responded or closed",
                    },
                    "response_notes": {
                        "type": "string",
                        "description": "Optional response notes to store",
                    },
                },
                "required": ["status"],
            },
        },
    },
]

# =============================================================
# PROMPT BUILDER
# =============================================================

def build_user_prompt(
    lat: float,
    lon: float,
    input_type: str,
    image_count: int,
    building_type: str | None = None,
    building_floors: str | None = None,
    field_note: str | None = None,
    pre_image_available: bool = False
) -> str:
    """Build the user message prompt for each assessment."""
    input_descriptions = {
        "ground_photo": f"{image_count} ground-level photo(s) taken by field worker",
        "drone_images": f"{image_count} drone oblique image(s) from aerial survey",
        "video": f"{image_count} frame(s) extracted from field worker video"
    }
    input_desc = input_descriptions.get(input_type, f"{image_count} image(s)")
 
    osm_context = ""
    if building_type and building_type != "yes":
        osm_context += f"\n- Building type from OSM: {building_type}"
    if building_floors:
        osm_context += f"\n- Floors from OSM: {building_floors}"
    if not osm_context:
        osm_context = "\n- No OSM building data available — estimate from image"
 
    note_section = ""
    if field_note:
        note_section = f"""
FIELD WORKER NOTE (ground truth — prioritize this):
{field_note}
"""
    pre_image_section = ""
    if pre_image_available:
        pre_image_section = """
PRE-EARTHQUAKE REFERENCE IMAGE:
The first image in the list is a pre-earthquake satellite reference image
showing this building before the February 2023 earthquake.
The remaining images show the current post-earthquake state.
Use the pre-earthquake image to understand the original building structure
and compare against current damage.
"""
 
    prompt = f"""You are assessing earthquake damage at:
Latitude: {lat}
Longitude: {lon}
Province: {'Hatay' if lon < 37.5 else 'Adiyaman'}, Turkey
Input type: {input_desc}
 
OSM building data:{osm_context}
{note_section}{pre_image_section}
INSTRUCTIONS:
1. Call get_building_info({lat}, {lon}) first
2. Call get_flood_zone({lat}, {lon})
3. Call get_location_info({lat}, {lon})
4. Call get_nearest_road({lat}, {lon})
5. Call get_elevation_slope({lat}, {lon})
6. Call get_nearest_shelter({lat}, {lon})
7. Analyze all {image_count} provided image(s) for structural damage
8. Combine visual analysis with all tool results
9. Return your assessment as a single JSON object
 
Remember: Call ALL SIX tools before returning JSON.
Return ONLY the JSON object. No other text."""
 
    return prompt


def build_orthophoto_user_prompt(
    lat: float,
    lon: float,
    osm_id: int | str,
    batch_id: str,
    site_name: str,
    building_index: int,
    total_buildings: int,
    width_m: float,
    height_m: float,
    area_m2: float,
    pre_available: bool,
    is_dark: bool,
) -> str:
    """Build the user message prompt for an orthophoto (aerial) building assessment."""
    if pre_available:
        pre_section = (
            "IMAGE 1: PRE-EARTHQUAKE REFERENCE\n"
            "This shows the building BEFORE the earthquake.\n"
            "Use this to understand the original structure and compare against current damage."
        )
        post_index = 2
    else:
        pre_section = (
            "No pre-earthquake reference image available.\n"
            "Assess the post-earthquake image only."
        )
        post_index = 1

    dark_warning = ""
    if is_dark:
        dark_warning = (
            "\nWARNING: Parts of this image contain no data (dark areas).\n"
            "Base your assessment only on the visible portions.\n"
            "Note limited visibility in your reasoning.\n"
        )

    prompt = f"""Assessment type: AERIAL ORTHOPHOTO
OSM Building ID: {osm_id}
Approximate dimensions: {width_m:.0f}m × {height_m:.0f}m
Approximate area: {area_m2:.0f}m²
Site name: {site_name}
Batch: {batch_id} — Building {building_index} of {total_buildings}

Latitude: {lat}
Longitude: {lon}

{pre_section}

IMAGE {post_index}: POST-EARTHQUAKE
This is the current state. Assess this image for damage.

IMPORTANT: These two images show the SAME location at different times.
The building outlines may look different because the building was
damaged or destroyed between the two images.
The green box marks the same geographic location in both images.
Compare what was there before versus what is there now.

The GREEN polygon outline marks your target building.
ALL other buildings visible are context only.
DO NOT assess any building other than the green-outlined one.
{dark_warning}
INSTRUCTIONS:
1. Call get_building_info({lat}, {lon}) first
2. Call get_flood_zone({lat}, {lon})
3. Call get_location_info({lat}, {lon})
4. Call get_nearest_road({lat}, {lon})
5. Call get_elevation_slope({lat}, {lon})
6. Call get_nearest_shelter({lat}, {lon})
7. Analyze the provided aerial chip image(s) for structural damage
8. Combine visual analysis with all tool results
9. Return your assessment as a single JSON object

Remember: Call ALL SIX tools before returning JSON.
Return ONLY the JSON object. No other text."""

    return prompt


# =============================================================
# TOOL IMPLEMENTATIONS (GIS QUERIES)
# =============================================================

async def get_building_info(lat: float, lon: float, db) -> dict:
    logger.info("pipeline.tool.get_building_info.started lat=%s lon=%s", lat, lon)
    result = await query_turkey_building_by_point(lat=lat, lon=lon, db=db)
    # This variable stores normalized building attributes when present.
    building_data = result.building_data if isinstance(result.building_data, dict) else None

    # This variable tracks response warnings with optional enrichment for sparse rows.
    warnings = list(result.warnings)
    if result.found and building_data is None:
        warnings.append("building_attributes_unavailable")
        logger.warning(
            "pipeline.tool.get_building_info.sparse_payload lat=%s lon=%s match=%s",
            lat,
            lon,
            result.match_strategy,
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
    logger.info("pipeline.tool.get_building_info.completed found=%s match=%s", payload["found"], payload["match_strategy"])
    return payload


async def get_flood_zone(lat: float, lon: float, db) -> dict:
    logger.info("pipeline.tool.get_flood_zone.started lat=%s lon=%s", lat, lon)
    result = await query_flood_zone_by_point(lat=lat, lon=lon, db=db)
    # This variable stores normalized flood zone attributes when present.
    flood_zone_data = result.flood_zone_data if isinstance(result.flood_zone_data, dict) else None

    # This variable tracks response warnings with optional enrichment for sparse rows.
    warnings = []
    if result.is_flood_zone and flood_zone_data is None:
        warnings.append("flood_zone_attributes_unavailable")
        logger.warning(
            "pipeline.tool.get_flood_zone.sparse_payload lat=%s lon=%s",
            lat,
            lon,
        )

    payload = {
        "is_flood_zone": result.is_flood_zone,
        "waterway_type": result.waterway_type,
        "waterway_name": result.waterway_name,
        "distance_to_waterway_m": result.distance_to_waterway_m,
        "province": result.province,
        "flood_zone_data": flood_zone_data,
        "warnings": warnings,
    }
    # Merge flood_zone_data keys at top level if available for backwards compatibility.
    if isinstance(flood_zone_data, dict):
        payload.update(flood_zone_data)
    logger.info("pipeline.tool.get_flood_zone.completed is_flood_zone=%s", payload["is_flood_zone"])
    return payload


async def get_nearest_road(lat: float, lon: float, db) -> dict:
    logger.info("pipeline.tool.get_nearest_road.started lat=%s lon=%s", lat, lon)
    result = await query_nearest_road_by_point(lat=lat, lon=lon, db=db)
    payload = result.model_dump()
    logger.info("pipeline.tool.get_nearest_road.completed found=%s distance_m=%s", payload.get("found"), payload.get("distance_m"))
    return payload


async def get_location_info(lat: float, lon: float, db: asyncpg.Connection | asyncpg.Pool) -> dict:
    """Get province and district context with full source records for a GPS coordinate."""
    logger.info("pipeline.tool.get_location_info.started lat=%s lon=%s", lat, lon)
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
    logger.info("pipeline.tool.get_location_info.completed found=%s province=%s district=%s", payload["found"], payload["province"], payload["district"])
    return payload


def get_elevation_slope(lat: float, lon: float, dem_path: str | None = None) -> dict:
    """Get elevation and slope from local DEM rasters for a WGS84 coordinate."""
    logger.info("pipeline.tool.get_elevation_slope.started lat=%s lon=%s", lat, lon)
    result = query_dem_elevation_by_point(lat=lat, lon=lon)
    payload = result.model_dump()
    logger.info(
        "pipeline.tool.get_elevation_slope.completed found=%s elevation_m=%s slope_degrees=%s",
        payload.get("found"),
        payload.get("elevation_m"),
        payload.get("slope_degrees"),
    )
    return payload


async def get_nearest_shelter(lat: float, lon: float, db: asyncpg.Connection | asyncpg.Pool) -> dict:
    """Find nearest shelter candidate with priority-aware ranking and route context."""
    logger.info("pipeline.tool.get_nearest_shelter.started lat=%s lon=%s", lat, lon)
    # This variable lists shelter amenity types ordered by disaster-response usefulness.
    SHELTER_AMENITIES = (
        "hospital",
        "clinic",
        "school",
        "townhall",
        "place_of_worship",
        "police",
        "pharmacy",
    )

    # This variable maps shelter amenity types to plain-language descriptions.
    SHELTER_DESCRIPTIONS = {
        "hospital": "Hospital — full medical facility",
        "clinic": "Clinic — medical treatment",
        "school": "School — large shelter space",
        "townhall": "Town hall — coordination center",
        "place_of_worship": "Mosque — community gathering point",
        "police": "Police station — security and coordination",
        "pharmacy": "Pharmacy — medical supplies",
    }

    # This variable maps amenity values to shelter priority groups.
    PRIORITY_MAP = {
        "hospital": 1,
        "clinic": 1,
        "school": 2,
        "townhall": 2,
        "place_of_worship": 3,
        "police": 3,
        "pharmacy": 4,
    }

    shelter_query = """
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
                shelter_data,
                shelter_type,
                shelter_lon,
                shelter_lat,
                distance_m,
                priority_rank,
                ROW_NUMBER() OVER (PARTITION BY priority_rank ORDER BY distance_m ASC) AS rank_in_priority
            FROM candidates
        )
        SELECT
            shelter_data,
            shelter_type,
            shelter_lon,
            shelter_lat,
            distance_m,
            priority_rank
        FROM per_priority
        WHERE rank_in_priority = 1
        ORDER BY distance_m ASC
        LIMIT 1
    """
    road_query = """
        SELECT
            name,
            highway,
            ST_Distance(
                geom::geography,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
            ) AS distance_m
        FROM turkey_lines
        WHERE highway IS NOT NULL AND name IS NOT NULL
        ORDER BY distance_m ASC
        LIMIT 1
    """
    shelter_row = await db.fetchrow(shelter_query, lon, lat, list(SHELTER_AMENITIES))
    road_row = await db.fetchrow(road_query, lon, lat)

    result = {
        "name": None,
        "name_en": None,
        "shelter_type": None,
        "shelter_description": None,
        "shelter_priority": None,
        "distance_m": None,
        "street": None,
        "house_number": None,
        "operator": None,
        "beds": None,
        "province": None,
        "nearest_road": None, "road_distance_m": None,
        "route_distance_m": None, "route_duration_s": None,
        "route_profile": "driving", "route_found": False,
        "route_warnings": [], "found": False
    }
    if shelter_row:
        shelter_data = shelter_row["shelter_data"] if isinstance(shelter_row["shelter_data"], dict) else {}
        shelter_type = shelter_row["shelter_type"] if isinstance(shelter_row["shelter_type"], str) else None
        shelter_lon = shelter_row["shelter_lon"]
        shelter_lat = shelter_row["shelter_lat"]

        route_result = await query_osrm_route(
            start_lat=lat,
            start_lon=lon,
            end_lat=float(shelter_lat),
            end_lon=float(shelter_lon),
            profile="driving",
        )

        result.update({
            "name": shelter_data.get("name") if isinstance(shelter_data.get("name"), str) else None,
            "name_en": shelter_data.get("name_en") if isinstance(shelter_data.get("name_en"), str) else None,
            "shelter_type": shelter_type,
            "shelter_description": SHELTER_DESCRIPTIONS.get(
                shelter_type,
                f"{shelter_type} — emergency facility" if shelter_type else None,
            ),
            "shelter_priority": PRIORITY_MAP.get(shelter_type) if shelter_type else None,
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
            "route_warnings": route_result.warnings,
            "found": True
        })
    if road_row:
        result.update({
            "nearest_road": road_row["name"],
            "road_distance_m": round(float(road_row["distance_m"]), 1)
        })
    logger.info(
        "pipeline.tool.get_nearest_shelter.completed found=%s shelter_type=%s distance_m=%s route_found=%s",
        result.get("found"),
        result.get("shelter_type"),
        result.get("distance_m"),
        result.get("route_found"),
    )
    return result


def _normalize_order_by(raw_order_by: Any) -> str:
    order_by = str(raw_order_by or "created_at").lower()
    allowed = {"severity", "created_at", "action_priority"}
    if order_by not in allowed:
        return "created_at"
    return order_by


def _normalize_order_dir(raw_order_dir: Any) -> str:
    order_dir = str(raw_order_dir or "desc").lower()
    return "ASC" if order_dir == "asc" else "DESC"


async def get_assessments(tool_args: dict[str, Any], db: asyncpg.Connection | asyncpg.Pool | None) -> dict[str, Any]:
    """Return assessments filtered by site/building/severity/status context."""
    if db is None:
        return {"success": False, "error": "Database not available", "items": []}

    limit = int(tool_args.get("limit") or 10)
    limit = max(1, min(limit, 200))
    order_by = _normalize_order_by(tool_args.get("order_by"))
    order_dir = _normalize_order_dir(tool_args.get("order_dir"))
    occupant_status_raw = str(tool_args.get("occupant_status") or "").strip().lower()

    filters: list[str] = []
    args: list[Any] = []
    arg_index = 1

    def add_filter(clause: str, value: Any) -> None:
        nonlocal arg_index
        filters.append(clause.replace("?", f"${arg_index}", 1))
        args.append(value)
        arg_index += 1

    assessment_id = tool_args.get("assessment_id")
    if assessment_id:
        add_filter("a.id = ?", str(assessment_id))

    site_id = tool_args.get("site_id")
    if site_id is not None:
        add_filter("a.site_id = ?", int(site_id))

    site_name = str(tool_args.get("site_name") or "").strip()
    if site_name:
        add_filter("COALESCE(s.name, b.site_name, '') ILIKE ?", f"%{site_name}%")

    building_id = tool_args.get("building_id")
    if building_id is not None:
        add_filter("a.osm_building_id = ?", int(building_id))

    batch_id = tool_args.get("batch_id")
    if batch_id:
        add_filter("a.batch_id = ?", str(batch_id))

    severity_min = tool_args.get("severity_min")
    if severity_min is not None:
        add_filter("COALESCE(a.severity, 0) >= ?", int(severity_min))

    severity_max = tool_args.get("severity_max")
    if severity_max is not None:
        add_filter("COALESCE(a.severity, 0) <= ?", int(severity_max))

    status_value = str(tool_args.get("status") or "").strip().lower()
    if status_value:
        add_filter("LOWER(a.status) = ?", status_value)

    if occupant_status_raw:
        if occupant_status_raw in {"signs_of_life", "potentially_trapped"}:
            add_filter(
                "LOWER(COALESCE(a.occupant_status, '')) = ANY(?::text[])",
                ["trapped", "signs_of_life", "potentially_trapped"],
            )
        else:
            add_filter("LOWER(COALESCE(a.occupant_status, '')) = ?", occupant_status_raw)

    flood_zone = tool_args.get("flood_zone")
    if isinstance(flood_zone, bool):
        add_filter("COALESCE(a.flood_zone, false) = ?", flood_zone)

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    order_sql = f"ORDER BY a.{order_by} {order_dir}, a.created_at DESC"
    limit_placeholder = f"${arg_index}"

    query = f"""
        SELECT
            a.*,
            COALESCE(s.name, b.site_name) AS site_name,
            ST_AsGeoJSON(a.geom)::text AS geom_geojson
        FROM assessments a
        LEFT JOIN sites s
          ON a.site_id = s.id
        LEFT JOIN batches b
          ON a.batch_id = b.id
        {where_sql}
        {order_sql}
        LIMIT {limit_placeholder}
    """
    args.append(limit)

    try:
        rows = await db.fetch(query, *args)
    except asyncpg.exceptions.UndefinedTableError:
        return {
            "success": False,
            "error": "assessments table not available",
            "items": [],
        }
    except asyncpg.exceptions.UndefinedColumnError:
        # Backward-compatible fallback when sites/site_id schema is not applied.
        legacy_filters = [f for f in filters if "a.site_id" not in f and "s.name" not in f]
        legacy_where = f"WHERE {' AND '.join(legacy_filters)}" if legacy_filters else ""
        legacy_query = f"""
            SELECT
                a.*,
                b.site_name AS site_name,
                ST_AsGeoJSON(a.geom)::text AS geom_geojson
            FROM assessments a
            LEFT JOIN batches b
              ON a.batch_id = b.id
            {legacy_where}
            {order_sql}
            LIMIT {limit_placeholder}
        """
        rows = await db.fetch(legacy_query, *args)

    items: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        geom_text = payload.pop("geom_geojson", None)
        payload["geom_geojson"] = json.loads(geom_text) if geom_text else None
        if payload.get("created_at") is not None:
            payload["created_at"] = payload["created_at"].isoformat()
        if payload.get("updated_at") is not None:
            payload["updated_at"] = payload["updated_at"].isoformat()
        if payload.get("responded_at") is not None:
            payload["responded_at"] = payload["responded_at"].isoformat()
        items.append(payload)

    return {
        "success": True,
        "count": len(items),
        "filters_applied": {
            "site_id": site_id,
            "site_name": site_name or None,
            "severity_min": severity_min,
            "severity_max": severity_max,
            "status": status_value or None,
            "occupant_status": occupant_status_raw or None,
            "flood_zone": flood_zone if isinstance(flood_zone, bool) else None,
            "building_id": building_id,
            "batch_id": batch_id,
            "limit": limit,
            "order_by": order_by,
            "order_dir": order_dir.lower(),
        },
        "items": items,
    }


async def get_sites(tool_args: dict[str, Any], db: asyncpg.Connection | asyncpg.Pool | None) -> dict[str, Any]:
    """Return sites with summary counts and optional spatial filters."""
    if db is None:
        return {"success": False, "error": "Database not available", "items": []}

    limit = int(tool_args.get("limit") or 20)
    limit = max(1, min(limit, 200))
    filters: list[str] = []
    args: list[Any] = []
    arg_index = 1

    def add_filter(clause: str, value: Any) -> None:
        nonlocal arg_index
        filters.append(clause.replace("?", f"${arg_index}", 1))
        args.append(value)
        arg_index += 1

    site_id = tool_args.get("site_id")
    if site_id is not None:
        add_filter("s.id = ?", int(site_id))

    site_name = str(tool_args.get("site_name") or "").strip()
    if site_name:
        add_filter("s.name ILIKE ?", f"%{site_name}%")

    status_value = str(tool_args.get("status") or "").strip().lower()
    if status_value:
        add_filter("LOWER(s.status) = ?", status_value)

    contains_lat = tool_args.get("contains_lat")
    contains_lon = tool_args.get("contains_lon")
    if contains_lat is not None and contains_lon is not None:
        filters.append(
            f"s.boundary IS NOT NULL AND ST_Contains(s.boundary, ST_SetSRID(ST_Point(${arg_index}, ${arg_index + 1}), 4326))"
        )
        args.extend([float(contains_lon), float(contains_lat)])
        arg_index += 2

    building_id = tool_args.get("building_id")
    if building_id is not None:
        add_filter(
            """
            EXISTS (
              SELECT 1
              FROM turkey_buildings tb
              WHERE tb.osm_id = ?
                AND s.boundary IS NOT NULL
                AND ST_Intersects(tb.geom, s.boundary)
            )
            """,
            int(building_id),
        )

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    limit_placeholder = f"${arg_index}"

    query = f"""
        SELECT
            s.id,
            s.name,
            s.status,
            s.total_buildings,
            s.created_at,
            s.updated_at,
            ST_AsGeoJSON(s.boundary)::text AS boundary_geojson,
            COALESCE(COUNT(a.id), 0)::int AS assessment_count,
            COALESCE(SUM(CASE WHEN a.status = 'pending' THEN 1 ELSE 0 END), 0)::int AS pending_count,
            COALESCE(SUM(CASE WHEN a.status IN ('responded', 'closed') THEN 1 ELSE 0 END), 0)::int AS responded_count,
            COALESCE(SUM(CASE WHEN COALESCE(a.severity, 0) >= 4 THEN 1 ELSE 0 END), 0)::int AS critical_count
        FROM sites s
        LEFT JOIN assessments a
          ON a.site_id = s.id
        {where_sql}
        GROUP BY s.id, s.name, s.status, s.total_buildings, s.created_at, s.updated_at, s.boundary
        ORDER BY s.updated_at DESC
        LIMIT {limit_placeholder}
    """
    args.append(limit)

    try:
        rows = await db.fetch(query, *args)
    except asyncpg.exceptions.UndefinedTableError:
        return {"success": False, "error": "sites table not available", "items": []}
    except asyncpg.exceptions.UndefinedColumnError:
        return {"success": False, "error": "sites schema missing required columns", "items": []}

    items: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        boundary_text = payload.pop("boundary_geojson", None)
        payload["boundary_geojson"] = json.loads(boundary_text) if boundary_text else None
        if payload.get("created_at") is not None:
            payload["created_at"] = payload["created_at"].isoformat()
        if payload.get("updated_at") is not None:
            payload["updated_at"] = payload["updated_at"].isoformat()
        items.append(payload)

    return {
        "success": True,
        "count": len(items),
        "filters_applied": {
            "site_id": site_id,
            "site_name": site_name or None,
            "status": status_value or None,
            "contains_lat": contains_lat,
            "contains_lon": contains_lon,
            "building_id": building_id,
            "limit": limit,
        },
        "items": items,
    }


async def get_field_workers(tool_args: dict[str, Any], db: asyncpg.Connection | asyncpg.Pool | None) -> dict[str, Any]:
    """Return field workers with availability status."""
    if db is None:
        return {"success": False, "error": "Database not available", "items": []}

    limit = int(tool_args.get("limit") or 50)
    limit = max(1, min(limit, 200))
    status_value = str(tool_args.get("status") or "").strip().lower()
    status_filter = status_value if status_value in {"available", "busy"} else ""

    query = """
        SELECT
            id,
            name,
            status,
            current_assessment_id,
            current_site_name,
            created_at,
            updated_at
        FROM field_workers
        WHERE ($1 = '' OR status = $1)
        ORDER BY
            CASE WHEN status = 'available' THEN 0 ELSE 1 END,
            LOWER(name) ASC
        LIMIT $2
    """
    try:
        rows = await db.fetch(query, status_filter, limit)
    except asyncpg.exceptions.UndefinedTableError:
        return {"success": False, "error": "field_workers table not available", "items": []}

    items: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        if payload.get("created_at") is not None:
            payload["created_at"] = payload["created_at"].isoformat()
        if payload.get("updated_at") is not None:
            payload["updated_at"] = payload["updated_at"].isoformat()
        items.append(payload)

    return {
        "success": True,
        "count": len(items),
        "filters_applied": {"status": status_filter or None, "limit": limit},
        "items": items,
    }


async def dispatch_assessments(tool_args: dict[str, Any], db: asyncpg.Connection | asyncpg.Pool | None) -> dict[str, Any]:
    """Assign assessments to a field worker and mark them responded."""
    if db is None:
        return {"success": False, "error": "Database not available", "updated_count": 0, "assessment_ids": []}

    worker_name = str(tool_args.get("worker_name") or "").strip()
    if not worker_name:
        return {"success": False, "error": "worker_name is required", "updated_count": 0, "assessment_ids": []}

    create_worker_if_missing = bool(tool_args.get("create_worker_if_missing", True))

    explicit_ids: list[str] = []
    single_id = str(tool_args.get("assessment_id") or "").strip()
    if single_id:
        explicit_ids.append(single_id)
    raw_ids = tool_args.get("assessment_ids")
    if isinstance(raw_ids, list):
        explicit_ids.extend([str(item).strip() for item in raw_ids if str(item).strip()])
    explicit_ids = list(dict.fromkeys(explicit_ids))

    limit = int(tool_args.get("limit") or 50)
    limit = max(1, min(limit, 200))
    site_name = str(tool_args.get("site_name") or "").strip()
    status_filter = str(tool_args.get("status") or "pending").strip().lower() or "pending"
    severity_min = tool_args.get("severity_min")
    severity_max = tool_args.get("severity_max")

    try:
        async with db.acquire() if hasattr(db, "acquire") else _AsyncNullContext(db) as conn:  # type: ignore[arg-type]
            worker_row = await conn.fetchrow(
                """
                SELECT id, name, status, current_assessment_id
                FROM field_workers
                WHERE LOWER(name) = LOWER($1)
                LIMIT 1
                """,
                worker_name,
            )

            if worker_row is None:
                if not create_worker_if_missing:
                    return {
                        "success": False,
                        "error": "worker_not_found",
                        "updated_count": 0,
                        "assessment_ids": [],
                    }
                worker_row = await conn.fetchrow(
                    """
                    INSERT INTO field_workers (name, status)
                    VALUES ($1, 'available')
                    RETURNING id, name, status, current_assessment_id
                    """,
                    worker_name,
                )

            if str(worker_row["status"] or "").lower() == "busy":
                return {
                    "success": False,
                    "error": "worker_busy",
                    "worker_name": worker_row["name"],
                    "current_assessment_id": worker_row["current_assessment_id"],
                    "updated_count": 0,
                    "assessment_ids": [],
                }

            selected_ids: list[str] = explicit_ids
            if not selected_ids:
                filters: list[str] = ["LOWER(COALESCE(a.status, '')) = $1", "LOWER(COALESCE(a.status, '')) <> 'closed'"]
                args: list[Any] = [status_filter]
                arg_idx = 2
                if site_name:
                    filters.append("LOWER(COALESCE(a.site_name, b.site_name, '')) LIKE LOWER($" + str(arg_idx) + ")")
                    args.append(f"%{site_name}%")
                    arg_idx += 1
                if severity_min is not None:
                    filters.append("COALESCE(a.severity, 0) >= $" + str(arg_idx))
                    args.append(int(severity_min))
                    arg_idx += 1
                if severity_max is not None:
                    filters.append("COALESCE(a.severity, 0) <= $" + str(arg_idx))
                    args.append(int(severity_max))
                    arg_idx += 1
                args.append(limit)
                where_sql = " AND ".join(filters)
                rows = await conn.fetch(
                    f"""
                    SELECT a.id
                    FROM assessments a
                    LEFT JOIN batches b ON a.batch_id = b.id
                    WHERE {where_sql}
                    ORDER BY COALESCE(a.severity, 0) DESC, a.created_at DESC
                    LIMIT ${arg_idx}
                    """
                    ,
                    *args,
                )
                selected_ids = [str(row["id"]) for row in rows]

            if not selected_ids:
                return {"success": False, "error": "no_assessments_matched", "updated_count": 0, "assessment_ids": []}

            await conn.execute(
                """
                UPDATE assessments
                SET
                    status = 'responded',
                    response_team = $1,
                    worker_name = $1,
                    updated_at = NOW(),
                    responded_at = COALESCE(responded_at, NOW())
                WHERE id = ANY($2::text[])
                  AND LOWER(COALESCE(status, '')) <> 'closed'
                """,
                worker_name,
                selected_ids,
            )

            first_site = await conn.fetchval(
                """
                SELECT COALESCE(a.site_name, b.site_name)
                FROM assessments a
                LEFT JOIN batches b ON a.batch_id = b.id
                WHERE a.id = ANY($1::text[])
                ORDER BY a.created_at DESC
                LIMIT 1
                """,
                selected_ids,
            )

            await conn.execute(
                """
                UPDATE field_workers
                SET
                    status = 'busy',
                    current_assessment_id = $2,
                    current_site_name = $3,
                    updated_at = NOW()
                WHERE LOWER(name) = LOWER($1)
                """,
                worker_name,
                selected_ids[0],
                first_site,
            )

        return {
            "success": True,
            "worker_name": worker_name,
            "updated_count": len(selected_ids),
            "assessment_ids": selected_ids,
            "status_set": "responded",
        }
    except asyncpg.exceptions.UndefinedTableError as exc:
        return {"success": False, "error": str(exc), "updated_count": 0, "assessment_ids": []}


async def update_assessment_status(tool_args: dict[str, Any], db: asyncpg.Connection | asyncpg.Pool | None) -> dict[str, Any]:
    """Update assessment status and release worker if closed."""
    if db is None:
        return {"success": False, "error": "Database not available", "updated_count": 0, "assessment_ids": []}

    status = str(tool_args.get("status") or "").strip().lower()
    if status not in {"responded", "closed"}:
        return {"success": False, "error": "status must be responded or closed", "updated_count": 0, "assessment_ids": []}

    explicit_ids: list[str] = []
    single_id = str(tool_args.get("assessment_id") or "").strip()
    if single_id:
        explicit_ids.append(single_id)
    raw_ids = tool_args.get("assessment_ids")
    if isinstance(raw_ids, list):
        explicit_ids.extend([str(item).strip() for item in raw_ids if str(item).strip()])
    assessment_ids = list(dict.fromkeys(explicit_ids))
    if not assessment_ids:
        return {"success": False, "error": "assessment_id or assessment_ids required", "updated_count": 0, "assessment_ids": []}

    response_notes = str(tool_args.get("response_notes") or "").strip() or None

    try:
        async with db.acquire() if hasattr(db, "acquire") else _AsyncNullContext(db) as conn:  # type: ignore[arg-type]
            rows = await conn.fetch(
                """
                SELECT id, COALESCE(response_team, worker_name, '') AS assigned_worker
                FROM assessments
                WHERE id = ANY($1::text[])
                """,
                assessment_ids,
            )
            found_ids = [str(row["id"]) for row in rows]
            if not found_ids:
                return {"success": False, "error": "assessments_not_found", "updated_count": 0, "assessment_ids": []}

            if status == "closed":
                await conn.execute(
                    """
                    UPDATE assessments
                    SET
                        status = 'closed',
                        response_notes = COALESCE($2, response_notes),
                        updated_at = NOW(),
                        responded_at = COALESCE(responded_at, NOW())
                    WHERE id = ANY($1::text[])
                    """,
                    found_ids,
                    response_notes,
                )
                worker_names = sorted(
                    {
                        str(row["assigned_worker"]).strip()
                        for row in rows
                        if str(row["assigned_worker"]).strip()
                    }
                )
                if worker_names:
                    await conn.execute(
                        """
                        UPDATE field_workers
                        SET
                            status = 'available',
                            current_assessment_id = NULL,
                            current_site_name = NULL,
                            updated_at = NOW()
                        WHERE LOWER(name) = ANY($1::text[])
                        """,
                        [name.lower() for name in worker_names],
                    )
            else:
                await conn.execute(
                    """
                    UPDATE assessments
                    SET
                        status = 'responded',
                        response_notes = COALESCE($2, response_notes),
                        updated_at = NOW(),
                        responded_at = COALESCE(responded_at, NOW())
                    WHERE id = ANY($1::text[])
                    """,
                    found_ids,
                    response_notes,
                )

        return {"success": True, "status_set": status, "updated_count": len(found_ids), "assessment_ids": found_ids}
    except asyncpg.exceptions.UndefinedTableError as exc:
        return {"success": False, "error": str(exc), "updated_count": 0, "assessment_ids": []}


class _AsyncNullContext:
    """Async context wrapper for an existing asyncpg connection."""

    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


async def dispatch_tool(tool_name: str, tool_args: dict, db) -> dict:
    _log_agent_event(
        "dispatch_tool_started",
        {
            "tool_name": tool_name,
            "tool_args": tool_args,
        },
    )
    lat = tool_args.get("lat")
    lon = tool_args.get("lon")
    # This variable stores monotonic start time for tool timing logs.
    tool_started_at = time.perf_counter()

    try:
        if tool_name == "get_building_info":
            result = await get_building_info(lat, lon, db)
        elif tool_name == "get_flood_zone":
            result = await get_flood_zone(lat, lon, db)
        elif tool_name == "get_location_info":
            result = await get_location_info(lat, lon, db)
        elif tool_name == "get_nearest_road":
            result = await get_nearest_road(lat, lon, db)
        elif tool_name == "get_elevation_slope":
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, get_elevation_slope, lat, lon, None)
        elif tool_name == "get_nearest_shelter":
            result = await get_nearest_shelter(lat, lon, db)
        elif tool_name == "get_assessments":
            result = await get_assessments(tool_args, db)
        elif tool_name == "get_sites":
            result = await get_sites(tool_args, db)
        elif tool_name == "get_field_workers":
            result = await get_field_workers(tool_args, db)
        elif tool_name == "dispatch_assessments":
            result = await dispatch_assessments(tool_args, db)
        elif tool_name == "update_assessment_status":
            result = await update_assessment_status(tool_args, db)
        else:
            result = {"error": f"Unknown tool: {tool_name}"}
    except Exception as exc:
        _log_agent_event(
            "dispatch_tool_failed",
            {
                "tool_name": tool_name,
                "tool_args": tool_args,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "elapsed_ms": round((time.perf_counter() - tool_started_at) * 1000, 2),
            },
        )
        raise

    _log_agent_event(
        "dispatch_tool_completed",
        {
            "tool_name": tool_name,
            "tool_args": tool_args,
            "result": result,
            "elapsed_ms": round((time.perf_counter() - tool_started_at) * 1000, 2),
        },
    )
    return result


# =============================================================
# AGENT LOOP
# =============================================================

def encode_image(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logging.error(f"Failed to read image at {path}: {e}")
        return ""


async def run_assessment_agent(
    image_paths: list[str],
    lat: float,
    lon: float,
    input_type: str,
    db,
    field_note: str | None = None,
    pre_image_path: str | None = None,
    building_type: str | None = None,
    building_floors: str | None = None,
    model: str = ACTIVE_GEMMA_MODEL,
    progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    orthophoto_context: dict[str, Any] | None = None,
) -> dict:
    _log_agent_event(
        "assessment_started",
        {
            "lat": lat,
            "lon": lon,
            "input_type": input_type,
            "image_path_count": len(image_paths),
            "pre_image_available": pre_image_path is not None,
            "field_note_present": bool(field_note),
            "model": model,
        },
    )

    # This variable stores monotonic start time for full assessment timing.
    assessment_started_at = time.perf_counter()

    all_image_paths = []
    if pre_image_path:
        all_image_paths.append(pre_image_path)
    all_image_paths.extend(image_paths[:5])

    _log_agent_event(
        "assessment_image_paths_prepared",
        {
            "all_image_paths": all_image_paths,
            "all_image_count": len(all_image_paths),
            "truncated_to_max_images": len(image_paths) > 5,
        },
    )

    images_b64 = [encode_image(p) for p in all_image_paths if p]
    images_b64 = [img for img in images_b64 if img] # remove empty strings on failure

    _log_agent_event(
        "assessment_images_encoded",
        {
            "requested_count": len(all_image_paths),
            "encoded_count": len(images_b64),
            "encoding_failed_count": max(0, len(all_image_paths) - len(images_b64)),
        },
    )
    
    is_orthophoto = input_type == "orthophoto" and orthophoto_context is not None
    if is_orthophoto:
        ctx = orthophoto_context or {}
        user_prompt = build_orthophoto_user_prompt(
            lat=lat,
            lon=lon,
            osm_id=ctx.get("osm_id", "unknown"),
            batch_id=ctx.get("batch_id", ""),
            site_name=ctx.get("site_name", ""),
            building_index=int(ctx.get("building_index", 1)),
            total_buildings=int(ctx.get("total_buildings", 1)),
            width_m=float(ctx.get("width_m", 0)),
            height_m=float(ctx.get("height_m", 0)),
            area_m2=float(ctx.get("area_m2", 0)),
            pre_available=bool(ctx.get("pre_available", False)),
            is_dark=bool(ctx.get("is_dark", False)),
        )
        active_system_prompt = ORTHOPHOTO_SYSTEM_PROMPT
    else:
        user_prompt = build_user_prompt(
            lat=lat, lon=lon, input_type=input_type, image_count=len(images_b64),
            building_type=building_type, building_floors=building_floors,
            field_note=field_note, pre_image_available=pre_image_path is not None,
        )
        active_system_prompt = SYSTEM_PROMPT

    messages = [
        {"role": "system", "content": active_system_prompt},
        {"role": "user", "content": user_prompt, "images": images_b64}
    ]

    _log_agent_event(
        "assessment_prompt_built",
        {
            "user_prompt_preview": user_prompt[:500],
            "message_count": len(messages),
        },
    )

    iteration = 0
    max_iterations = 10
    
    while iteration < max_iterations:
        iteration += 1
        _log_agent_event(
            "assessment_iteration_started",
            {
                "iteration": iteration,
                "max_iterations": max_iterations,
                "message_count": len(messages),
            },
        )

        # This variable stores monotonic iteration start time for debugging slow turns.
        iteration_started_at = time.perf_counter()

        if progress_callback:
            await progress_callback(
                {
                    "stage": "ai_reasoning",
                    "progress_percent": min(78, 30 + (iteration * 7)),
                    "thought": "Gemma is reviewing visual evidence and context.",
                }
            )

        try:
            response_stream = ollama.chat(
                model=model,
                messages=messages,
                tools=TOOLS,
                options={"temperature": 0.1, "num_ctx": 8192},
                stream=True,
            )
        except Exception as exc:
            _log_agent_event(
                "assessment_chat_failed",
                {
                    "iteration": iteration,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "elapsed_ms": round((time.perf_counter() - iteration_started_at) * 1000, 2),
                },
            )
            raise

        assistant_thinking_parts: list[str] = []
        assistant_content_parts: list[str] = []
        assistant_tool_calls: list[dict[str, Any]] = []
        emitted_thinking_chars = 0
        emitted_response_chars = 0

        for chunk in response_stream:
            if isinstance(chunk, dict):
                message_block = chunk.get("message")
            else:
                message_block = getattr(chunk, "message", None)

            if message_block is None:
                continue

            if isinstance(message_block, dict):
                thinking_chunk = message_block.get("thinking")
                content_chunk = message_block.get("content")
                tool_calls_chunk = message_block.get("tool_calls")
            else:
                thinking_chunk = getattr(message_block, "thinking", None)
                content_chunk = getattr(message_block, "content", None)
                tool_calls_chunk = getattr(message_block, "tool_calls", None)

            if isinstance(thinking_chunk, str) and thinking_chunk:
                assistant_thinking_parts.append(thinking_chunk)
                if progress_callback:
                    full_thinking = "".join(assistant_thinking_parts).strip()
                    if len(full_thinking) - emitted_thinking_chars >= 24:
                        emitted_thinking_chars = len(full_thinking)
                        await progress_callback(
                            {
                                "stage": "ai_reasoning_stream",
                                "progress_percent": min(82, 32 + (iteration * 7)),
                                "thought": full_thinking[-480:],
                                "thinking_text": full_thinking,
                            }
                        )

            if isinstance(content_chunk, str) and content_chunk:
                assistant_content_parts.append(content_chunk)
                if progress_callback:
                    full_response = "".join(assistant_content_parts).strip()
                    if len(full_response) - emitted_response_chars >= 24:
                        emitted_response_chars = len(full_response)
                        await progress_callback(
                            {
                                "stage": "ai_response_stream",
                                "progress_percent": min(86, 36 + (iteration * 7)),
                                "thought": full_response[-480:],
                                "response_text": full_response,
                            }
                        )

            if isinstance(tool_calls_chunk, list) and tool_calls_chunk:
                normalized_tool_calls: list[dict[str, Any]] = []
                for raw_call in tool_calls_chunk:
                    if isinstance(raw_call, dict):
                        function_block = raw_call.get("function", {})
                    else:
                        function_block = getattr(raw_call, "function", None)

                    if isinstance(function_block, dict):
                        tool_name = function_block.get("name")
                        raw_args = function_block.get("arguments")
                    elif function_block is not None:
                        tool_name = getattr(function_block, "name", None)
                        raw_args = getattr(function_block, "arguments", None)
                    else:
                        tool_name = None
                        raw_args = None

                    if not isinstance(tool_name, str) or not tool_name:
                        continue

                    parsed_args: dict[str, Any]
                    if isinstance(raw_args, dict):
                        parsed_args = raw_args
                    elif isinstance(raw_args, str):
                        try:
                            loaded_args = json.loads(raw_args)
                            parsed_args = loaded_args if isinstance(loaded_args, dict) else {}
                        except json.JSONDecodeError:
                            parsed_args = {}
                    else:
                        parsed_args = {}

                    normalized_tool_calls.append(
                        {
                            "function": {
                                "name": tool_name,
                                "arguments": parsed_args,
                            }
                        }
                    )

                if normalized_tool_calls:
                    assistant_tool_calls = normalized_tool_calls

        assistant_content = "".join(assistant_content_parts)
        assistant_thinking = "".join(assistant_thinking_parts).strip()

        if progress_callback and assistant_thinking and len(assistant_thinking) > emitted_thinking_chars:
            await progress_callback(
                {
                    "stage": "ai_reasoning_stream",
                    "progress_percent": min(82, 32 + (iteration * 7)),
                    "thought": assistant_thinking[-480:],
                    "thinking_text": assistant_thinking,
                }
            )
        if progress_callback and assistant_content and len(assistant_content) > emitted_response_chars:
            await progress_callback(
                {
                    "stage": "ai_response_stream",
                    "progress_percent": min(86, 36 + (iteration * 7)),
                    "thought": assistant_content[-480:],
                    "response_text": assistant_content,
                }
            )

        _log_agent_event(
            "assessment_chat_received",
            {
                "iteration": iteration,
                "assistant_content_preview": (assistant_content or "")[:500],
                "assistant_thinking_preview": assistant_thinking[:500],
                "tool_call_count": len(assistant_tool_calls),
                "elapsed_ms": round((time.perf_counter() - iteration_started_at) * 1000, 2),
            },
        )
        
        messages.append({
            "role": "assistant",
            "content": assistant_content or "",
            "tool_calls": assistant_tool_calls or []
        })

        if assistant_tool_calls:
            for tool_call in assistant_tool_calls:
                function_block = tool_call.get("function", {})
                tool_name = function_block.get("name")
                tool_args = function_block.get("arguments", {})
                if not isinstance(tool_name, str):
                    continue
                if not isinstance(tool_args, dict):
                    tool_args = {}
                _log_agent_event(
                    "assessment_tool_call",
                    {
                        "iteration": iteration,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                    },
                )

                if progress_callback:
                    live_stream_text = (assistant_content or assistant_thinking or "").strip()
                    await progress_callback(
                        {
                            "stage": "tool_call",
                            "progress_percent": min(82, 35 + (iteration * 6)),
                            "thought": (live_stream_text[-480:] if live_stream_text else f"Calling tool: {tool_name}"),
                            "thinking_text": assistant_thinking or None,
                            "response_text": assistant_content or None,
                        }
                    )

                tool_result = await dispatch_tool(tool_name, tool_args, db)
                _log_agent_event(
                    "assessment_tool_result",
                    {
                        "iteration": iteration,
                        "tool_name": tool_name,
                        "tool_result": tool_result,
                    },
                )
                messages.append({
                    "role": "tool",
                    "content": json.dumps(tool_result, ensure_ascii=False)
                })

            _log_agent_event(
                "assessment_iteration_continue",
                {
                    "iteration": iteration,
                    "reason": "tool_calls_present",
                    "elapsed_ms": round((time.perf_counter() - iteration_started_at) * 1000, 2),
                },
            )
            continue

        if progress_callback:
            await progress_callback(
                {
                    "stage": "finalize_output",
                    "progress_percent": 84,
                    "thought": (assistant_content or assistant_thinking or "AI is finalizing structured assessment output.")[-480:],
                }
            )

        raw_assistant_content = assistant_content or ""
        try:
            parsed_assessment = parse_assessment_json(raw_assistant_content)
        except ValueError as parse_exc:
            _log_agent_event(
                "assessment_parse_retry_started",
                {
                    "iteration": iteration,
                    "error": str(parse_exc),
                    "assistant_content_preview": raw_assistant_content[:500],
                },
            )

            if progress_callback:
                await progress_callback(
                    {
                        "stage": "finalize_output",
                        "progress_percent": 86,
                        "thought": "AI is converting response into strict JSON output.",
                    }
                )

            try:
                repaired_output = request_assessment_json_repair(
                    model=model,
                    system_prompt=active_system_prompt,
                    raw_assistant_output=raw_assistant_content,
                )
                parsed_assessment = parse_assessment_json(repaired_output)
                _log_agent_event(
                    "assessment_parse_retry_succeeded",
                    {
                        "iteration": iteration,
                        "repaired_output_preview": repaired_output[:500],
                    },
                )
            except Exception as repair_exc:
                _log_agent_event(
                    "assessment_parse_retry_failed",
                    {
                        "iteration": iteration,
                        "parse_error": str(parse_exc),
                        "repair_error": str(repair_exc),
                    },
                )
                raise parse_exc

        _log_agent_event(
            "assessment_completed",
            {
                "iteration": iteration,
                "severity": parsed_assessment.get("severity"),
                "damage_type": parsed_assessment.get("damage_type"),
                "recommended_action": parsed_assessment.get("recommended_action"),
                "confidence": parsed_assessment.get("confidence"),
                "elapsed_ms_total": round((time.perf_counter() - assessment_started_at) * 1000, 2),
            },
        )
        return parsed_assessment
    
    _log_agent_event(
        "assessment_failed_max_iterations",
        {
            "max_iterations": max_iterations,
            "elapsed_ms_total": round((time.perf_counter() - assessment_started_at) * 1000, 2),
        },
    )
    raise ValueError(f"Agent loop exceeded {max_iterations} iterations without final response")


def parse_assessment_json(raw: str) -> dict:
    import re
    _log_agent_event(
        "parse_assessment_started",
        {
            "raw_preview": raw[:500],
            "raw_length": len(raw),
        },
    )
    text = raw.strip()
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        _log_agent_event(
            "parse_assessment_failed_no_json",
            {
                "text_preview": text[:500],
            },
        )
        raise ValueError(f"No JSON object found in response: {text[:200]}")
    
    json_str = text[start:end]
    json_str = re.sub(r",\s*}", "}", json_str)
    json_str = re.sub(r",\s*]", "]", json_str)
    
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        _log_agent_event(
            "parse_assessment_failed_invalid_json",
            {
                "error": str(e),
                "json_preview": json_str[:500],
            },
        )
        raise ValueError(f"JSON parse error: {e}\\nRaw: {json_str[:500]}")

    if not isinstance(data, dict):
        _log_agent_event(
            "parse_assessment_failed_not_object",
            {
                "parsed_type": type(data).__name__,
            },
        )
        raise ValueError("Parsed assessment must be a JSON object")
    
    defaults = {
        "severity": 3, "damage_type": "unknown", "damage_description": "Assessment incomplete",
        "structural_risk": "unknown", "building_type": None, "building_floors": None,
        "building_material": "unknown", "estimated_occupants": "unknown", "occupant_status": "unknown",
        "recommended_action": "structural_assessment", "action_priority": 2, "road_access": "unknown",
        "reasoning": "Automated assessment", "warnings": [], "confidence": 0.5, "turkish_summary": ""
    }
    # This variable tracks fields auto-filled from defaults for debugging weak model outputs.
    defaulted_fields: list[str] = []
    for key, default in defaults.items():
        if key not in data or data[key] is None:
            data[key] = default
            defaulted_fields.append(key)

    data["severity"] = max(1, min(5, int(data["severity"])))
    data["action_priority"] = max(1, min(5, int(data["action_priority"])))
    data["confidence"] = max(0.0, min(1.0, float(data["confidence"])))

    _log_agent_event(
        "parse_assessment_completed",
        {
            "defaulted_fields": defaulted_fields,
            "severity": data.get("severity"),
            "damage_type": data.get("damage_type"),
            "recommended_action": data.get("recommended_action"),
            "confidence": data.get("confidence"),
            "keys": sorted(list(data.keys())),
        },
    )
    return data
