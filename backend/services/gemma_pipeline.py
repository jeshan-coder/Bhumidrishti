"""Gemma-4 analysis pipeline for ground photos and video frames."""

import asyncio
import base64
import json
import logging
import time
from typing import Any, Awaitable, Callable

import asyncpg
import ollama
from prompts.gemma_system_prompt import PHOTO_ASSESSMENT_SYSTEM_PROMPT
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
    
    user_prompt = build_user_prompt(
        lat=lat, lon=lon, input_type=input_type, image_count=len(images_b64),
        building_type=building_type, building_floors=building_floors,
        field_note=field_note, pre_image_available=pre_image_path is not None
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
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
            response = ollama.chat(
                model=model,
                messages=messages,
                tools=TOOLS,
                options={"temperature": 0.1, "num_ctx": 8192}
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

        assistant_message = response.message

        _log_agent_event(
            "assessment_chat_received",
            {
                "iteration": iteration,
                "assistant_content_preview": (assistant_message.content or "")[:500],
                "tool_call_count": len(assistant_message.tool_calls or []),
                "elapsed_ms": round((time.perf_counter() - iteration_started_at) * 1000, 2),
            },
        )
        
        messages.append({
            "role": "assistant",
            "content": assistant_message.content or "",
            "tool_calls": assistant_message.tool_calls or []
        })

        if assistant_message.tool_calls:
            for tool_call in assistant_message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = tool_call.function.arguments
                _log_agent_event(
                    "assessment_tool_call",
                    {
                        "iteration": iteration,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                    },
                )

                if progress_callback:
                    tool_thought_map = {
                        "get_building_info": "AI is checking building details from GIS layers.",
                        "get_flood_zone": "AI is evaluating flood risk context.",
                        "get_location_info": "AI is resolving district and province context.",
                        "get_nearest_road": "AI is checking road access for responders.",
                        "get_elevation_slope": "AI is analyzing elevation and terrain slope.",
                        "get_nearest_shelter": "AI is finding nearest shelter route context.",
                    }
                    await progress_callback(
                        {
                            "stage": "tool_call",
                            "progress_percent": min(82, 35 + (iteration * 6)),
                            "thought": tool_thought_map.get(tool_name, f"AI is calling {tool_name}."),
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
                    "thought": "AI is finalizing structured assessment output.",
                }
            )

        parsed_assessment = parse_assessment_json(assistant_message.content or "")
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
