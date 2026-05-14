"""Tool: get_assessments — query assessment records with rich filters."""
from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


PREFERRED_ASSESSMENT_COLUMNS = (
    "id", "lat", "lon", "province", "district", "address_note", "input_type",
    "photo_path", "video_path", "ortho_path", "chip_path", "pre_chip_path",
    "severity", "damage_type", "damage_description", "structural_risk",
    "building_type", "building_floors", "building_material", "osm_building_id",
    "building_area_m2", "building_width_m", "building_height_m",
    "estimated_occupants", "occupant_status", "recommended_action",
    "action_priority", "flood_zone", "flood_return_period", "elevation_m",
    "slope_degrees", "slope_risk", "nearest_shelter", "shelter_distance_m",
    "shelter_type", "road_access", "nearest_road", "road_distance_m",
    "reasoning", "warnings", "confidence", "turkish_summary", "model_used",
    "inference_seconds", "worker_name", "worker_device", "field_note",
    "site_id", "batch_id", "batch_building_count", "status",
    "verified_by_ground", "response_team", "response_notes", "created_at",
    "updated_at", "responded_at",
)


def _normalize_order_dir(raw: Any) -> str:
    """Normalize requested sort direction to a safe SQL keyword."""
    return "ASC" if str(raw or "desc").lower() == "asc" else "DESC"


def _as_text(raw: Any) -> str:
    """Convert a tool argument to trimmed text."""
    return str(raw or "").strip()


def _as_int(raw: Any) -> int | None:
    """Convert a tool argument to an integer when possible."""
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _as_float(raw: Any) -> float | None:
    """Convert a tool argument to a float when possible."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _as_bool(raw: Any) -> bool | None:
    """Convert bool-like tool arguments to bool values."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"true", "yes", "1", "verified"}:
            return True
        if normalized in {"false", "no", "0", "unverified"}:
            return False
    return None


def _normalize_geojson(raw: Any) -> str | None:
    """Normalize GeoJSON Feature, FeatureCollection, or geometry input to geometry JSON."""
    if raw is None or raw == "":
        return None

    parsed: Any = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw

    if not isinstance(parsed, dict):
        return None

    geojson_type = parsed.get("type")
    if geojson_type == "Feature":
        geometry = parsed.get("geometry")
        return json.dumps(geometry) if isinstance(geometry, dict) else None

    if geojson_type == "FeatureCollection":
        geometries = [
            feature.get("geometry")
            for feature in parsed.get("features", [])
            if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
        ]
        if not geometries:
            return None
        return json.dumps({"type": "GeometryCollection", "geometries": geometries})

    return json.dumps(parsed)


async def _get_table_columns(db: asyncpg.Connection | asyncpg.Pool, table_name: str) -> set[str]:
    """Return public table columns from information_schema."""
    rows = await db.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        """,
        table_name,
    )
    return {str(row["column_name"]) for row in rows}


def _serialize_value(value: Any) -> Any:
    """Convert database values to JSON-safe response values."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


async def get_assessments(
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    """Return one or many assessments filtered by attributes, time, and geometry."""
    if db is None:
        return {"success": False, "error": "Database not available", "items": []}

    assessment_columns = await _get_table_columns(db, "assessments")
    if not assessment_columns:
        return {"success": False, "error": "assessments table not available", "items": []}

    site_columns = await _get_table_columns(db, "sites")
    batch_columns = await _get_table_columns(db, "batches")
    can_join_sites = bool(site_columns) and "site_id" in assessment_columns and "id" in site_columns
    can_join_batches = bool(batch_columns) and "batch_id" in assessment_columns and "id" in batch_columns

    requested_limit = 1 if _as_bool(tool_args.get("single")) else _as_int(tool_args.get("limit"))
    if tool_args.get("assessment_id") or tool_args.get("id"):
        requested_limit = requested_limit or 1
    limit = max(1, min(requested_limit or 20, 500))
    order_dir = _normalize_order_dir(tool_args.get("order_dir"))
    include_geometry = _as_bool(tool_args.get("include_geometry"))
    if include_geometry is None:
        include_geometry = True

    filters: list[str] = []
    args: list[Any] = []
    applied_filters: dict[str, Any] = {}
    unsupported_filters: list[str] = []

    def add_param(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    def add_filter(name: str, clause: str, value: Any) -> None:
        filters.append(clause)
        applied_filters[name] = value

    def add_text_filter(arg_name: str, column: str, partial: bool = True) -> None:
        value = _as_text(tool_args.get(arg_name))
        if not value:
            return
        if column not in assessment_columns:
            unsupported_filters.append(arg_name)
            return
        placeholder = add_param(f"%{value}%" if partial else value.lower())
        operator = "ILIKE" if partial else "="
        expression = f"a.{column} {operator} {placeholder}" if partial else f"LOWER(a.{column}) = {placeholder}"
        add_filter(arg_name, expression, value)

    def add_numeric_range(arg_min: str, arg_max: str, column: str) -> None:
        min_value = _as_float(tool_args.get(arg_min))
        max_value = _as_float(tool_args.get(arg_max))
        if min_value is None and max_value is None:
            return
        if column not in assessment_columns:
            unsupported_filters.extend([name for name, value in ((arg_min, min_value), (arg_max, max_value)) if value is not None])
            return
        if min_value is not None:
            add_filter(arg_min, f"a.{column} >= {add_param(min_value)}", min_value)
        if max_value is not None:
            add_filter(arg_max, f"a.{column} <= {add_param(max_value)}", max_value)

    assessment_id = _as_text(tool_args.get("assessment_id") or tool_args.get("id"))
    if assessment_id and "id" in assessment_columns:
        add_filter("assessment_id", f"a.id = {add_param(assessment_id)}", assessment_id)

    raw_assessment_ids = tool_args.get("assessment_ids")
    if isinstance(raw_assessment_ids, list) and "id" in assessment_columns:
        assessment_ids = [str(item).strip() for item in raw_assessment_ids if str(item).strip()]
        if assessment_ids:
            add_filter("assessment_ids", f"a.id = ANY({add_param(assessment_ids)}::text[])", assessment_ids)

    site_id = _as_int(tool_args.get("site_id"))
    if site_id is not None:
        if "site_id" in assessment_columns:
            add_filter("site_id", f"a.site_id = {add_param(site_id)}", site_id)
        else:
            unsupported_filters.append("site_id")

    site_expr_parts: list[str] = []
    if "site_name" in assessment_columns:
        site_expr_parts.append("a.site_name")
    if can_join_sites and "name" in site_columns:
        site_expr_parts.append("s.name")
    if can_join_batches and "site_name" in batch_columns:
        site_expr_parts.append("b.site_name")
    if len(site_expr_parts) > 1:
        site_expr = f"COALESCE({', '.join(site_expr_parts)})"
    elif site_expr_parts:
        site_expr = site_expr_parts[0]
    else:
        site_expr = "NULL::text"

    if "geom" in assessment_columns and {"lat", "lon"}.issubset(assessment_columns):
        assessment_geom_expr = "COALESCE(a.geom, ST_SetSRID(ST_Point(a.lon, a.lat), 4326))"
    elif "geom" in assessment_columns:
        assessment_geom_expr = "a.geom"
    else:
        assessment_geom_expr = ""

    site_name = _as_text(tool_args.get("site_name"))
    if site_name:
        if site_expr_parts:
            add_filter("site_name", f"{site_expr} ILIKE {add_param(f'%{site_name}%')}", site_name)
        else:
            unsupported_filters.append("site_name")

    building_id = _as_int(tool_args.get("building_id") or tool_args.get("osm_building_id"))
    if building_id is not None:
        if "osm_building_id" in assessment_columns:
            add_filter("building_id", f"a.osm_building_id = {add_param(building_id)}", building_id)
        else:
            unsupported_filters.append("building_id")

    batch_id = _as_text(tool_args.get("batch_id"))
    if batch_id:
        if "batch_id" in assessment_columns:
            add_filter("batch_id", f"a.batch_id = {add_param(batch_id)}", batch_id)
        else:
            unsupported_filters.append("batch_id")

    bool_filters = {
        "flood_zone": "flood_zone",
        "verified": "verified_by_ground",
        "verified_by_ground": "verified_by_ground",
    }
    for arg_name, column in bool_filters.items():
        bool_value = _as_bool(tool_args.get(arg_name))
        if bool_value is None:
            continue
        if column in assessment_columns:
            add_filter(arg_name, f"COALESCE(a.{column}, false) = {add_param(bool_value)}", bool_value)
        else:
            unsupported_filters.append(arg_name)

    text_filters = {
        "province": "province",
        "district": "district",
        "input_type": "input_type",
        "damage_type": "damage_type",
        "structural_risk": "structural_risk",
        "building_type": "building_type",
        "building_material": "building_material",
        "occupant_status": "occupant_status",
        "recommended_action": "recommended_action",
        "slope_risk": "slope_risk",
        "road_access": "road_access",
        "nearest_road": "nearest_road",
        "nearest_shelter": "nearest_shelter",
        "shelter_type": "shelter_type",
        "worker_name": "worker_name",
        "worker_device": "worker_device",
        "team_name": "response_team",
        "response_team": "response_team",
        "status": "status",
        "flood_return_period": "flood_return_period",
    }
    for arg_name, column in text_filters.items():
        add_text_filter(arg_name, column)

    search = _as_text(tool_args.get("search"))
    searchable_columns = [
        column for column in (
            "id", "province", "district", "damage_type", "damage_description",
            "structural_risk", "building_type", "recommended_action",
            "reasoning", "turkish_summary", "worker_name", "field_note",
            "response_team", "response_notes",
        )
        if column in assessment_columns
    ]
    if search and searchable_columns:
        placeholder = add_param(f"%{search}%")
        search_sql = " OR ".join(f"CAST(a.{column} AS text) ILIKE {placeholder}" for column in searchable_columns)
        add_filter("search", f"({search_sql})", search)

    warning = _as_text(tool_args.get("warning"))
    if warning:
        if "warnings" in assessment_columns:
            add_filter("warning", f"CAST(a.warnings AS text) ILIKE {add_param(f'%{warning}%')}", warning)
        else:
            unsupported_filters.append("warning")

    add_numeric_range("severity_min", "severity_max", "severity")
    add_numeric_range("action_priority_min", "action_priority_max", "action_priority")
    add_numeric_range("confidence_min", "confidence_max", "confidence")
    add_numeric_range("elevation_min", "elevation_max", "elevation_m")
    add_numeric_range("slope_min", "slope_max", "slope_degrees")
    add_numeric_range("shelter_distance_min", "shelter_distance_max", "shelter_distance_m")
    add_numeric_range("road_distance_min", "road_distance_max", "road_distance_m")
    add_numeric_range("building_area_min", "building_area_max", "building_area_m2")
    add_numeric_range("building_width_min", "building_width_max", "building_width_m")
    add_numeric_range("building_height_min", "building_height_max", "building_height_m")

    time_ranges = {
        "created": "created_at",
        "updated": "updated_at",
        "responded": "responded_at",
    }
    for prefix, column in time_ranges.items():
        after_value = _as_text(tool_args.get(f"{prefix}_after"))
        before_value = _as_text(tool_args.get(f"{prefix}_before"))
        if not after_value and not before_value:
            continue
        if column not in assessment_columns:
            unsupported_filters.extend([name for name, value in ((f"{prefix}_after", after_value), (f"{prefix}_before", before_value)) if value])
            continue
        if after_value:
            add_filter(f"{prefix}_after", f"a.{column} >= {add_param(after_value)}::timestamptz", after_value)
        if before_value:
            add_filter(f"{prefix}_before", f"a.{column} <= {add_param(before_value)}::timestamptz", before_value)

    lat = _as_float(tool_args.get("lat") or tool_args.get("contains_lat"))
    lon = _as_float(tool_args.get("lon") or tool_args.get("contains_lon"))
    within_meters = _as_float(tool_args.get("within_meters") or tool_args.get("radius_m"))
    distance_select = "NULL::float AS distance_m"
    if lat is not None and lon is not None:
        if assessment_geom_expr:
            lon_placeholder = add_param(lon)
            lat_placeholder = add_param(lat)
            point_sql = f"ST_SetSRID(ST_Point({lon_placeholder}, {lat_placeholder}), 4326)"
            radius = max(1.0, within_meters or 50.0)
            add_filter(
                "point_radius",
                f"{assessment_geom_expr} IS NOT NULL AND ST_DWithin({assessment_geom_expr}::geography, {point_sql}::geography, {add_param(radius)})",
                {"lat": lat, "lon": lon, "within_meters": radius},
            )
            distance_select = f"ST_Distance({assessment_geom_expr}::geography, {point_sql}::geography) AS distance_m"
        else:
            unsupported_filters.append("lat_lon")

    geometry_geojson = _normalize_geojson(tool_args.get("geometry") or tool_args.get("geometry_geojson"))
    if geometry_geojson:
        if assessment_geom_expr:
            geometry_placeholder = add_param(geometry_geojson)
            query_geom = f"ST_SetSRID(ST_GeomFromGeoJSON({geometry_placeholder}), 4326)"
            spatial_relation = _as_text(tool_args.get("spatial_relation")).lower() or "intersects"
            if spatial_relation == "within":
                spatial_clause = f"{assessment_geom_expr} IS NOT NULL AND ST_Within({assessment_geom_expr}, {query_geom})"
            elif spatial_relation == "contains":
                spatial_clause = f"{assessment_geom_expr} IS NOT NULL AND ST_Contains({assessment_geom_expr}, {query_geom})"
            else:
                spatial_clause = f"{assessment_geom_expr} IS NOT NULL AND ST_Intersects({assessment_geom_expr}, {query_geom})"
                spatial_relation = "intersects"
            add_filter("geometry", spatial_clause, {"spatial_relation": spatial_relation})
        else:
            unsupported_filters.append("geometry")

    excluded_columns = {"geom", "site_name"}
    selected_columns = [
        column for column in PREFERRED_ASSESSMENT_COLUMNS
        if column in assessment_columns and column not in excluded_columns
    ]
    selected_columns.extend(sorted(assessment_columns - set(PREFERRED_ASSESSMENT_COLUMNS) - excluded_columns))
    selected_sql = ",\n            ".join(f"a.{column}" for column in selected_columns)
    site_select = f"{site_expr} AS site_name"
    geom_select = f"ST_AsGeoJSON({assessment_geom_expr})::text AS geom_geojson" if include_geometry and assessment_geom_expr else "NULL::text AS geom_geojson"

    joins: list[str] = []
    if can_join_sites:
        joins.append("LEFT JOIN sites s ON a.site_id = s.id")
    if can_join_batches:
        joins.append("LEFT JOIN batches b ON a.batch_id = b.id")

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    requested_order_by = _as_text(tool_args.get("order_by")).lower() or "created_at"
    orderable_columns = set(selected_columns) | {"distance_m"}
    order_by = requested_order_by if requested_order_by in orderable_columns else "created_at"
    if order_by not in orderable_columns:
        order_by = "id"
    if order_by == "distance_m":
        order_sql = f"ORDER BY distance_m {order_dir} NULLS LAST"
    else:
        order_sql = f"ORDER BY a.{order_by} {order_dir} NULLS LAST"
    if "created_at" in assessment_columns and order_by != "created_at":
        order_sql += ", a.created_at DESC"
    limit_placeholder = add_param(limit)

    query = f"""
        SELECT
            {selected_sql},
            {site_select},
            {geom_select},
            {distance_select},
            COUNT(*) OVER()::int AS total_matching
        FROM assessments a
        {' '.join(joins)}
        {where_sql}
        {order_sql}
        LIMIT {limit_placeholder}
    """

    try:
        rows = await db.fetch(query, *args)
    except asyncpg.PostgresError as exc:
        logger.exception("tool.get_assessments.failed")
        return {"success": False, "error": f"Assessment query failed: {exc}", "items": []}

    items: list[dict[str, Any]] = []
    total_matching = 0
    for row in rows:
        payload = dict(row)
        total_matching = int(payload.pop("total_matching", total_matching) or total_matching)
        geom_text = payload.pop("geom_geojson", None)
        payload["geom_geojson"] = json.loads(geom_text) if geom_text else None
        for key, value in list(payload.items()):
            payload[key] = _serialize_value(value)
        items.append(payload)

    return {
        "success": True,
        "count": len(items),
        "total_matching": total_matching,
        "limit": limit,
        "returned_geometry": include_geometry,
        "filters_applied": applied_filters,
        "unsupported_filters": sorted(set(unsupported_filters)),
        "order_by": order_by,
        "order_dir": order_dir.lower(),
        "items": items,
    }
