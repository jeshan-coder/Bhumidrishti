"""Centralized tool registry for all BhumiDrishti AI agents.

Package structure (one file per tool implementation):
    _shared.py                  — shared helpers (_AsyncNullContext, _safe_float)
    get_building_info.py        — OSM building lookup
    get_flood_zone.py           — flood zone check
    get_location_info.py        — province/district resolution
    get_nearest_road.py         — nearest road query
    get_elevation_slope.py      — DEM elevation + slope
    get_nearest_shelter.py      — nearest shelter with OSRM route
    get_assessments.py          — filtered assessment query
    get_sites.py                — site listing with counts
    get_field_teams.py          — field team listing
    get_field_workers.py        — backward-compat alias
    dispatch_assessments.py     — assign assessments to a team
    update_assessment_status.py — mark assessments responded/closed
    get_building_report_data.py — single building data fetcher
    get_building_route.py       — OSRM route between coordinates

Tool schema subsets exported:
    ASSESSMENT_TOOLS    — GIS / spatial lookups
    COORDINATION_TOOLS  — coordinator queries: assessments, sites, teams, dispatch
    REPORT_TOOLS        — report-specific data fetchers
    CHAT_TOOLS          — ASSESSMENT_TOOLS + COORDINATION_TOOLS
    ALL_TOOLS           — every tool (report agent)
"""

from __future__ import annotations

import asyncio as _asyncio
import logging as _logging
import time as _time
from typing import Any as _Any

# ---------------------------------------------------------------------------
# ASSESSMENT TOOLS — spatial / GIS lookups
# ---------------------------------------------------------------------------

ASSESSMENT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_building_info",
            "description": (
                "Get building information from the local PostGIS database "
                "for a building by GPS coordinates, OSM ID, or GeoJSON geometry. "
                "Returns building type, number of floors, construction material, "
                "and OSM building ID if a matching footprint exists. "
                "Use osm_id when the user mentions a specific OSM building. "
                "Use geometry when a selected map feature provides a polygon. "
                "Use lat/lon when only a point location is available. "
                "Call this first before any other spatial tool during assessment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude in decimal degrees WGS84"},
                    "lon": {"type": "number", "description": "Longitude in decimal degrees WGS84"},
                    "osm_id": {
                        "type": "integer",
                        "description": "Optional turkey_buildings.osm_id for exact building lookup",
                    },
                    "geometry": {
                        "type": "object",
                        "description": "Optional GeoJSON Polygon or MultiPolygon geometry for spatial building lookup",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_flood_zone",
            "description": (
                "Check if the given GPS coordinates fall within a flood risk zone. "
                "Flood zones are derived from a 300 m buffer around all waterways. "
                "Returns whether the location is in a flood zone and the return period. "
                "Buildings in flood zones face additional rescue urgency."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude in decimal degrees WGS84"},
                    "lon": {"type": "number", "description": "Longitude in decimal degrees WGS84"},
                },
                "required": ["lat", "lon"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_location_info",
            "description": (
                "Get location context for given GPS coordinates from local GIS layers. "
                "Returns exact province from turkey_provinces polygon containment, "
                "district from nearest turkey_districts_pts centroid, "
                "and nearest turkey_points feature as fallback locality context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude in decimal degrees WGS84"},
                    "lon": {"type": "number", "description": "Longitude in decimal degrees WGS84"},
                },
                "required": ["lat", "lon"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_nearest_road",
            "description": (
                "Find the nearest road to the given GPS coordinates from local turkey_lines data. "
                "Returns road name, highway type, surface, bridge/tunnel flags, "
                "distance in metres, and road access category."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude in decimal degrees WGS84"},
                    "lon": {"type": "number", "description": "Longitude in decimal degrees WGS84"},
                },
                "required": ["lat", "lon"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_elevation_slope",
            "description": (
                "Get terrain elevation and slope at the given GPS coordinates "
                "from the local GLO-30 DEM. "
                "Returns elevation in metres ASL, slope in degrees, and slope risk level. "
                "High slope increases collapse risk and complicates rescue access."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude in decimal degrees WGS84"},
                    "lon": {"type": "number", "description": "Longitude in decimal degrees WGS84"},
                },
                "required": ["lat", "lon"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_nearest_shelter",
            "description": (
                "Find the nearest shelter or safe facility from a GPS coordinate "
                "or site reference in the local PostGIS database. "
                "Searches hospitals, clinics, schools, town halls, places of worship, "
                "police stations, and pharmacies. "
                "Returns facility name, straight-line distance in metres, type, "
                "nearest road name, and route geometry."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude in decimal degrees WGS84"},
                    "lon": {"type": "number", "description": "Longitude in decimal degrees WGS84"},
                    "site_id": {
                        "type": "integer",
                        "description": "sites.id — use when asking for shelter for a whole site",
                    },
                    "site_name": {
                        "type": "string",
                        "description": "Site name reference (e.g. 'Antakya Ward 3')",
                    },
                },
            },
        },
    },
]

# ---------------------------------------------------------------------------
# COORDINATION TOOLS — coordinator queries, dispatch, status updates
# ---------------------------------------------------------------------------

COORDINATION_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_assessments",
            "description": (
                "Query one or many disaster damage assessment records with rich filters. "
                "Use this for assessment search, triage lists, dashboard/map filtering, "
                "spatial queries by GeoJSON or point radius, and questions about severity, "
                "damage type, structural risk, building properties, road access, flood risk, "
                "field worker, response team, verification, status, and timestamps. "
                "Returns assessment rows plus geom_geojson by default so results can be displayed on the map."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "single": {
                        "type": "boolean",
                        "description": (
                            "Set true ONLY when user explicitly asks for ONE record: "
                            "'the latest assessment', 'give me one building', 'show me the top result'. "
                            "NEVER set single=true when user asks for 'buildings' (plural), "
                            "'all buildings', 'show me buildings that...', or any multi-record query."
                        ),
                    },
                    "site_id": {"type": "integer", "description": "Filter by sites.id"},
                    "site_name": {"type": "string", "description": "Partial match on assessment/site/batch site name"},
                    "assessment_id": {"type": "string", "description": "Exact assessment id (e.g. ASS-2847)"},
                    "assessment_ids": {
                        "type": "array",
                        "description": "Exact list of assessment IDs",
                        "items": {"type": "string"},
                    },
                    "building_id": {"type": "integer", "description": "Filter by assessments.osm_building_id"},
                    "osm_building_id": {"type": "integer", "description": "Alias for building_id"},
                    "batch_id": {"type": "string", "description": "Filter by assessments.batch_id"},
                    "province": {"type": "string", "description": "Partial match on province, e.g. Hatay or Adiyaman"},
                    "district": {"type": "string", "description": "Partial match on district"},
                    "input_type": {
                        "type": "string",
                        "description": "ground_photo, drone_images, orthophoto, satellite, or video",
                    },
                    "damage_type": {
                        "type": "string",
                        "description": "Partial match on damage_type such as full_collapse, partial_collapse, facade_damage, structural_crack, roof_damage, no_visible_damage",
                    },
                    "structural_risk": {"type": "string", "description": "high, moderate, low, or unknown"},
                    "building_type": {"type": "string", "description": "Partial match on building_type such as residential, school, hospital, mosque, commercial"},
                    "building_material": {"type": "string", "description": "Partial match on building_material"},
                    "severity": {"type": "integer", "description": "Exact severity (1-5). Use this when user says 'severity 3' or 'severity equal to 3'"},
                    "severity_min": {"type": "integer", "description": "severity >= N (1-5). Use for 'severity 3 or higher' / 'at least 3'"},
                    "severity_max": {"type": "integer", "description": "severity <= N (1-5). Use for 'severity 3 or lower' / 'at most 3'"},
                    "severity_gt": {"type": "integer", "description": "severity > N (1-5). Use for 'severity greater than 3'"},
                    "severity_lt": {"type": "integer", "description": "severity < N (1-5). Use for 'severity less than 3'"},
                    "action_priority": {"type": "integer", "description": "Exact action priority (1-5)"},
                    "action_priority_min": {"type": "integer", "description": "action_priority >= N"},
                    "action_priority_max": {"type": "integer", "description": "action_priority <= N"},
                    "action_priority_gt": {"type": "integer", "description": "action_priority > N"},
                    "action_priority_lt": {"type": "integer", "description": "action_priority < N"},
                    "recommended_action": {
                        "type": "string",
                        "description": (
                            "Partial match on recommended action. "
                            "Values: immediate_search_rescue, urgent_evacuation, structural_assessment, "
                            "monitor, no_action_needed. "
                            "Use this when user asks for buildings needing 'immediate search rescue', "
                            "'urgent evacuation', 'structural assessment', etc."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "description": "pending, in_review, responded, closed, false_positive",
                    },
                    "occupant_status": {
                        "type": "string",
                        "description": "trapped, signs_of_life, potentially_trapped, evacuated, unknown",
                    },
                    "flood_zone": {"type": "boolean", "description": "Filter flood zone true/false"},
                    "flood_return_period": {"type": "string", "description": "Partial match, e.g. 100yr, 50yr, none"},
                    "elevation": {"type": "number", "description": "Exact elevation in metres"},
                    "elevation_min": {"type": "number", "description": "elevation >= N metres"},
                    "elevation_max": {"type": "number", "description": "elevation <= N metres"},
                    "elevation_gt": {"type": "number", "description": "elevation > N metres"},
                    "elevation_lt": {"type": "number", "description": "elevation < N metres"},
                    "slope": {"type": "number", "description": "Exact slope in degrees"},
                    "slope_min": {"type": "number", "description": "slope >= N degrees"},
                    "slope_max": {"type": "number", "description": "slope <= N degrees"},
                    "slope_gt": {"type": "number", "description": "slope > N degrees"},
                    "slope_lt": {"type": "number", "description": "slope < N degrees"},
                    "slope_risk": {"type": "string", "description": "high, moderate, or low"},
                    "road_access": {"type": "string", "description": "passable, blocked, or unknown"},
                    "nearest_road": {"type": "string", "description": "Partial match on nearest road name"},
                    "road_distance": {"type": "number", "description": "Exact road distance in metres"},
                    "road_distance_min": {"type": "number", "description": "road_distance >= N metres"},
                    "road_distance_max": {"type": "number", "description": "road_distance <= N metres"},
                    "road_distance_gt": {"type": "number", "description": "road_distance > N metres"},
                    "road_distance_lt": {"type": "number", "description": "road_distance < N metres"},
                    "nearest_shelter": {"type": "string", "description": "Partial match on nearest shelter name"},
                    "shelter_type": {"type": "string", "description": "school, hospital, community_centre, mosque, etc."},
                    "shelter_distance": {"type": "number", "description": "Exact shelter distance in metres"},
                    "shelter_distance_min": {"type": "number", "description": "shelter_distance >= N metres"},
                    "shelter_distance_max": {"type": "number", "description": "shelter_distance <= N metres"},
                    "shelter_distance_gt": {"type": "number", "description": "shelter_distance > N metres"},
                    "shelter_distance_lt": {"type": "number", "description": "shelter_distance < N metres"},
                    "building_area": {"type": "number", "description": "Exact building footprint area in square metres"},
                    "building_area_min": {"type": "number", "description": "building_area >= N m²"},
                    "building_area_max": {"type": "number", "description": "building_area <= N m²"},
                    "building_area_gt": {"type": "number", "description": "building_area > N m²"},
                    "building_area_lt": {"type": "number", "description": "building_area < N m²"},
                    "building_width": {"type": "number", "description": "Exact building width in metres"},
                    "building_width_min": {"type": "number", "description": "building_width >= N metres"},
                    "building_width_max": {"type": "number", "description": "building_width <= N metres"},
                    "building_width_gt": {"type": "number", "description": "building_width > N metres"},
                    "building_width_lt": {"type": "number", "description": "building_width < N metres"},
                    "building_height": {"type": "number", "description": "Exact building height in metres"},
                    "building_height_min": {"type": "number", "description": "building_height >= N metres"},
                    "building_height_max": {"type": "number", "description": "building_height <= N metres"},
                    "building_height_gt": {"type": "number", "description": "building_height > N metres"},
                    "building_height_lt": {"type": "number", "description": "building_height < N metres"},
                    "confidence": {"type": "number", "description": "Exact model confidence 0-1"},
                    "confidence_min": {"type": "number", "description": "confidence >= N (0-1)"},
                    "confidence_max": {"type": "number", "description": "confidence <= N (0-1)"},
                    "confidence_gt": {"type": "number", "description": "confidence > N (0-1)"},
                    "confidence_lt": {"type": "number", "description": "confidence < N (0-1)"},
                    "worker_name": {"type": "string", "description": "Partial match on submitting field worker name"},
                    "worker_device": {"type": "string", "description": "Partial match on field worker device"},
                    "team_name": {"type": "string", "description": "Partial match on response_team / rescue team name"},
                    "response_team": {"type": "string", "description": "Partial match on response_team / rescue team name"},
                    "verified": {"type": "boolean", "description": "Filter verified_by_ground true/false"},
                    "verified_by_ground": {"type": "boolean", "description": "Filter verified_by_ground true/false"},
                    "created_after": {"type": "string", "description": "Created at or after this ISO timestamp/date"},
                    "created_before": {"type": "string", "description": "Created at or before this ISO timestamp/date"},
                    "updated_after": {"type": "string", "description": "Updated at or after this ISO timestamp/date"},
                    "updated_before": {"type": "string", "description": "Updated at or before this ISO timestamp/date"},
                    "responded_after": {"type": "string", "description": "Responded at or after this ISO timestamp/date"},
                    "responded_before": {"type": "string", "description": "Responded at or before this ISO timestamp/date"},
                    "lat": {"type": "number", "description": "Latitude for point-radius spatial assessment search"},
                    "lon": {"type": "number", "description": "Longitude for point-radius spatial assessment search"},
                    "within_meters": {"type": "number", "description": "Radius in metres for lat/lon spatial search"},
                    "geometry": {
                        "type": "object",
                        "description": "GeoJSON geometry, Feature, or FeatureCollection for spatial assessment filtering",
                    },
                    "geometry_geojson": {
                        "type": "object",
                        "description": "Alias for geometry; GeoJSON geometry for spatial filtering",
                    },
                    "spatial_relation": {
                        "type": "string",
                        "description": "intersects (default), within, or contains for geometry spatial filtering",
                    },
                    "search": {"type": "string", "description": "Free-text search across IDs, location, damage, reasoning, worker, and response fields"},
                    "warning": {"type": "string", "description": "Partial match inside warnings JSON"},
                    "include_geometry": {
                        "type": "boolean",
                        "description": "Return geom_geojson for map display; default true",
                    },
                    "limit": {"type": "integer", "description": "Rows to return (default 20, max 500)"},
                    "order_by": {
                        "type": "string",
                        "description": "Sort field such as severity, created_at, updated_at, responded_at, action_priority, confidence, distance_m, elevation_m, slope_degrees, building_area_m2, building_height_m",
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
                "Return sites with assessment summary counts. "
                "Call with {} (no arguments) to list ALL sites regardless of status — "
                "this is the correct call for 'list sites', 'all sites', 'available sites', "
                "'what sites exist', 'how many sites'. "
                "Use filters only when the user is explicit: a specific name, id, or status keyword."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {
                        "type": "integer",
                        "description": "Filter by exact numeric site ID.",
                    },
                    "site_name": {
                        "type": "string",
                        "description": "Partial (case-insensitive) site name match. Only set when user names a specific site.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "processing", "completed"],
                        "description": (
                            "Only set when user says the exact word 'active', 'processing', or 'completed'. "
                            "Words like 'available', 'existing', 'all', 'every' are NOT a status filter — "
                            "omit this field entirely in those cases."
                        ),
                    },
                    "contains_lat": {
                        "type": "number",
                        "description": "Return only the site whose boundary contains this latitude.",
                    },
                    "contains_lon": {
                        "type": "number",
                        "description": "Paired with contains_lat for point-in-site lookup.",
                    },
                    "building_id": {
                        "type": "integer",
                        "description": "Return the site whose boundary contains this OSM building ID.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows to return (default 20, max 200).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_field_teams",
            "description": (
                "List field teams and their assignment status. "
                "Each team can contain one or more field workers. "
                "Use this before dispatch to select an available team."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Optional filter: available or busy"},
                    "limit": {"type": "integer", "description": "Rows to return (default 50, max 200)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_field_workers",
            "description": "Compatibility alias for get_field_teams.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Optional filter: available or busy"},
                    "limit": {"type": "integer", "description": "Rows to return (default 50, max 200)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_assessments",
            "description": (
                "Dispatch one or more assessments to a field team or single worker. "
                "Sets assessment status to 'responded' and stores team assignment. "
                "Supports direct assessment_ids or filtered bulk dispatch by site/severity/status. "
                "If the selected team is busy, dispatch is rejected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "assessment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Explicit assessment ids to dispatch",
                    },
                    "assessment_id": {"type": "string", "description": "Single assessment id shortcut"},
                    "site_name": {"type": "string", "description": "Optional site_name filter for bulk dispatch"},
                    "severity": {"type": "integer", "description": "Optional exact severity filter (= N)"},
                    "severity_min": {"type": "integer", "description": "Optional severity >= N filter"},
                    "severity_max": {"type": "integer", "description": "Optional severity <= N filter"},
                    "severity_gt": {"type": "integer", "description": "Optional severity > N filter"},
                    "severity_lt": {"type": "integer", "description": "Optional severity < N filter"},
                    "status": {"type": "string", "description": "Optional current status filter (default pending)"},
                    "limit": {"type": "integer", "description": "Bulk dispatch row limit (default 50, max 200)"},
                    "worker_name": {"type": "string", "description": "Optional worker name to assign"},
                    "team_name": {"type": "string", "description": "Optional team name to assign"},
                    "create_team_if_missing": {
                        "type": "boolean",
                        "description": "Create team if it does not exist (default true)",
                    },
                    "create_worker_if_missing": {
                        "type": "boolean",
                        "description": "Backward-compatible alias of create_team_if_missing",
                    },
                },
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
                    "assessment_id": {"type": "string", "description": "Single assessment id shortcut"},
                    "status": {"type": "string", "description": "Target status: responded or closed"},
                    "response_notes": {"type": "string", "description": "Optional response notes to store"},
                },
                "required": ["status"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# REPORT TOOLS — data fetchers used only by the report-generation agent
# ---------------------------------------------------------------------------

REPORT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_building_report_data",
            "description": (
                "Get one building's full assessment record by assessment_id. "
                "Call this FIRST when generating a building report. "
                "Returns all assessment fields including photos, reasoning, and warnings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "assessment_id": {
                        "type": "string",
                        "description": "Assessment id (e.g. ASS-2847)",
                    },
                },
                "required": ["assessment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_building_route",
            "description": (
                "Get step-by-step OSRM driving or walking route between two GPS coordinates. "
                "Use for inter-building routes and building-to-shelter evacuation routes. "
                "Returns distance_m, duration_s, and a list of turn-by-turn step strings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_lat": {"type": "number", "description": "Origin latitude WGS84"},
                    "from_lon": {"type": "number", "description": "Origin longitude WGS84"},
                    "to_lat": {"type": "number", "description": "Destination latitude WGS84"},
                    "to_lon": {"type": "number", "description": "Destination longitude WGS84"},
                    "profile": {
                        "type": "string",
                        "description": "Routing profile: driving (default) or foot",
                    },
                },
                "required": ["from_lat", "from_lon", "to_lat", "to_lon"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# COMPOSED SUBSETS
# ---------------------------------------------------------------------------

CHAT_TOOLS: list[dict] = [*ASSESSMENT_TOOLS, *COORDINATION_TOOLS]
ALL_TOOLS: list[dict] = [*ASSESSMENT_TOOLS, *COORDINATION_TOOLS, *REPORT_TOOLS]

ASSESSMENT_TOOL_NAMES: frozenset[str] = frozenset(
    t["function"]["name"] for t in ASSESSMENT_TOOLS
)
COORDINATION_TOOL_NAMES: frozenset[str] = frozenset(
    t["function"]["name"] for t in COORDINATION_TOOLS
)
REPORT_TOOL_NAMES: frozenset[str] = frozenset(
    t["function"]["name"] for t in REPORT_TOOLS
)
ALL_TOOL_NAMES: frozenset[str] = (
    ASSESSMENT_TOOL_NAMES | COORDINATION_TOOL_NAMES | REPORT_TOOL_NAMES
)

_logger = _logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NORMALISATION TABLES
# ---------------------------------------------------------------------------

# Common tool-name hallucinations the model emits → canonical name.
# These are logged as warnings so they're easy to spot in production.
_TOOL_ALIASES: dict[str, str] = {
    # ---- get_building_info variants ----
    "get_building_info_at_location": "get_building_info",
    "get_building_info_by_location": "get_building_info",
    "get_building_info_by_coordinates": "get_building_info",
    "get_building_info_by_coords": "get_building_info",
    "get_building_info_by_coord": "get_building_info",
    "get_building_info_by_point": "get_building_info",
    "get_building_info_by_geometry": "get_building_info",
    "get_building_info_from_geometry": "get_building_info",
    "get_building_info_from_coordinates": "get_building_info",
    "get_building_info_from_coords": "get_building_info",
    "get_building_info_from_point": "get_building_info",
    "get_building_info_from_location": "get_building_info",
    "get_building_by_location": "get_building_info",
    "get_building_by_coords": "get_building_info",
    "get_building_at_location": "get_building_info",
    "get_building_at_coords": "get_building_info",
    "find_building": "get_building_info",
    "lookup_building": "get_building_info",
    "building_info": "get_building_info",
    # ---- get_assessments variants ----
    "get_assessments_at_location": "get_assessments",
    "get_assessments_by_location": "get_assessments",
    "get_assessments_by_coords": "get_assessments",
    "get_assessments_by_point": "get_assessments",
    "get_assessments_by_id": "get_assessments",
    "get_assessments_by_assessment_id": "get_assessments",
    "get_assessments_by_damage_type": "get_assessments",
    "get_assessments_by_severity": "get_assessments",
    "get_assessments_by_status": "get_assessments",
    "get_assessments_by_structural_risk": "get_assessments",
    "get_assessments_by_site": "get_assessments",
    "get_assessments_by_site_name": "get_assessments",
    "get_assessments_by_worker": "get_assessments",
    "get_assessments_by_worker_name": "get_assessments",
    "get_assessments_by_team": "get_assessments",
    "get_assessments_by_province": "get_assessments",
    "get_assessments_by_district": "get_assessments",
    "get_assessments_filter": "get_assessments",
    "get_assessment_by_id": "get_assessments",
    "get_assessment": "get_assessments",
    "filter_assessments": "get_assessments",
    "search_assessments": "get_assessments",
    "query_assessments": "get_assessments",
    "list_assessments": "get_assessments",
    "find_assessments": "get_assessments",
}


def resolve_tool_name(name: str) -> str:
    """Return canonical tool name, resolving any known hallucinated aliases."""
    return _TOOL_ALIASES.get(name, name)


def parse_colon_tool_call(
    tool_name: str,
    tool_args: dict[str, _Any],
) -> tuple[str, dict[str, _Any]]:
    """Parse hallucinated 'tool:param:value[:param:value...]' syntax.

    Gemma4 sometimes embeds parameters inside the tool name itself using colon
    notation, e.g. 'get_assessments:severity:5' with empty args {}.
    This function detects that pattern, extracts the embedded param/value
    pairs, and returns the canonical tool name + merged args dict.
    """
    if ":" not in tool_name:
        return tool_name, tool_args

    parts = tool_name.split(":")
    base = parts[0]
    canonical_base = _TOOL_ALIASES.get(base, base)

    # Only proceed if the base is a known dispatched tool.
    if canonical_base not in ALL_TOOL_NAMES:
        return tool_name, tool_args

    # Parts after the base must come in param/value pairs.
    tail = parts[1:]
    if len(tail) % 2 != 0 or not tail:
        return tool_name, tool_args

    extra_args: dict[str, _Any] = {}
    for i in range(0, len(tail), 2):
        param, raw_value = tail[i], tail[i + 1]
        try:
            extra_args[param] = int(raw_value)
        except ValueError:
            try:
                extra_args[param] = float(raw_value)
            except ValueError:
                extra_args[param] = raw_value

    merged = {**tool_args, **extra_args}
    _logger.warning(
        "tools.colon_tool_call_parsed original=%s canonical=%s extracted_args=%s merged_args=%s",
        tool_name, canonical_base, extra_args, merged,
    )
    return canonical_base, merged


def _normalise_tool_args(tool_name: str, tool_args: dict[_Any, _Any]) -> dict[str, _Any]:
    """Return a normalised copy of tool_args, fixing common model mistakes."""
    args = dict(tool_args)

    # Gemma sometimes emits 'lng' instead of the schema-defined 'lon'.
    if "lng" in args and "lon" not in args:
        _logger.warning(
            "tools.dispatch.param_normalised tool=%s key=lng→lon value=%s",
            tool_name, args["lng"],
        )
        args["lon"] = args.pop("lng")

    # Gemma sometimes emits 'longitude'/'latitude' long-forms.
    if "longitude" in args and "lon" not in args:
        _logger.warning("tools.dispatch.param_normalised tool=%s key=longitude→lon", tool_name)
        args["lon"] = args.pop("longitude")
    if "latitude" in args and "lat" not in args:
        _logger.warning("tools.dispatch.param_normalised tool=%s key=latitude→lat", tool_name)
        args["lat"] = args.pop("latitude")

    return args


# ---------------------------------------------------------------------------
# UNIFIED DISPATCHER
# Each tool is imported from its own module — no monolithic imports needed.
# ---------------------------------------------------------------------------

async def dispatch_tool(
    tool_name: str,
    tool_args: dict[_Any, _Any],
    db: _Any,
) -> dict[str, _Any]:
    """Route a tool call to its dedicated implementation module."""
    started_at = _time.perf_counter()

    # Resolve alias first so all downstream checks use the canonical name.
    canonical = _TOOL_ALIASES.get(tool_name)
    if canonical is not None:
        _logger.warning(
            "tools.dispatch.alias_resolved hallucinated=%s canonical=%s args=%s",
            tool_name, canonical, tool_args,
        )
        tool_name = canonical

    # Detect and parse 'tool:param:value' colon-embedding hallucinations.
    tool_name, tool_args = parse_colon_tool_call(tool_name, tool_args)

    # Normalise common parameter-name mistakes before routing.
    tool_args = _normalise_tool_args(tool_name, tool_args)

    _logger.info("tools.dispatch tool=%s args=%s", tool_name, tool_args)

    # ---- Report-specific tools ----------------------------------------
    if tool_name in REPORT_TOOL_NAMES:
        if tool_name == "get_building_report_data":
            from services.tools.get_building_report_data import get_building_report_data  # noqa: PLC0415
            result = await get_building_report_data(tool_args, db)
        else:  # get_building_route
            from services.tools.get_building_route import get_building_route  # noqa: PLC0415
            result = await get_building_route(tool_args, db)

    # ---- GIS / spatial tools ------------------------------------------
    elif tool_name in ASSESSMENT_TOOL_NAMES:
        lat = tool_args.get("lat")
        lon = tool_args.get("lon")
        if tool_name == "get_building_info":
            from services.tools.get_building_info import get_building_info  # noqa: PLC0415
            result = await get_building_info(
                lat=lat,
                lon=lon,
                db=db,
                osm_id=tool_args.get("osm_id"),
                geometry=tool_args.get("geometry") or tool_args.get("geometry_geojson"),
            )
        elif tool_name == "get_flood_zone":
            from services.tools.get_flood_zone import get_flood_zone  # noqa: PLC0415
            result = await get_flood_zone(lat, lon, db)
        elif tool_name == "get_location_info":
            from services.tools.get_location_info import get_location_info  # noqa: PLC0415
            result = await get_location_info(lat, lon, db)
        elif tool_name == "get_nearest_road":
            from services.tools.get_nearest_road import get_nearest_road  # noqa: PLC0415
            result = await get_nearest_road(lat, lon, db)
        elif tool_name == "get_elevation_slope":
            from services.tools.get_elevation_slope import get_elevation_slope  # noqa: PLC0415
            loop = _asyncio.get_event_loop()
            result = await loop.run_in_executor(None, get_elevation_slope, lat, lon, None)
        else:  # get_nearest_shelter
            from services.tools.get_nearest_shelter import get_nearest_shelter  # noqa: PLC0415
            result = await get_nearest_shelter(tool_args, db)

    # ---- Coordination tools -------------------------------------------
    elif tool_name in COORDINATION_TOOL_NAMES:
        if tool_name == "get_assessments":
            from services.tools.get_assessments import get_assessments  # noqa: PLC0415
            result = await get_assessments(tool_args, db)
        elif tool_name == "get_sites":
            from services.tools.get_sites import get_sites  # noqa: PLC0415
            result = await get_sites(tool_args, db)
        elif tool_name == "get_field_teams":
            from services.tools.get_field_teams import get_field_teams  # noqa: PLC0415
            result = await get_field_teams(tool_args, db)
        elif tool_name == "get_field_workers":
            from services.tools.get_field_workers import get_field_workers  # noqa: PLC0415
            result = await get_field_workers(tool_args, db)
        elif tool_name == "dispatch_assessments":
            from services.tools.dispatch_assessments import dispatch_assessments  # noqa: PLC0415
            result = await dispatch_assessments(tool_args, db)
        elif tool_name == "update_assessment_status":
            from services.tools.update_assessment_status import update_assessment_status  # noqa: PLC0415
            result = await update_assessment_status(tool_args, db)

    else:
        result = {"error": f"Unknown tool: {tool_name}"}

    _logger.info(
        "tools.dispatch_done tool=%s elapsed_ms=%.1f",
        tool_name,
        (_time.perf_counter() - started_at) * 1000,
    )
    return result
