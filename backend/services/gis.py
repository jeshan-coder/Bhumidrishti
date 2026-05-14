"""GIS service layer for PostGIS queries."""

import json
import os
import logging
from typing import Any
import asyncpg
import httpx

from models.gis import (
    DemElevationQueryResult,
    FloodZoneQueryResult,
    LocationInfoQueryResult,
    NearestRoadQueryResult,
    OsrmRouteQueryResult,
    TurkeyBuildingQueryResult,
)

# This variable stores the module logger for GIS query debugging.
logger = logging.getLogger(__name__)

# This variable stores the default OSRM base URL used for backend route lookups.
OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "http://osrm:5000")

# This variable stores the HTTP timeout for OSRM requests in seconds.
OSRM_TIMEOUT_SECONDS = 15.0


def _normalize_building_row(row: asyncpg.Record | None) -> dict[str, Any] | None:
    """Normalize one turkey_buildings query row into building_data with GeoJSON geometry."""
    if row is None:
        return None

    building_data_raw = row.get("building_data")
    # This variable stores normalized building attributes as a dictionary when available.
    building_data: dict[str, Any] | None = None
    if isinstance(building_data_raw, dict):
        building_data = building_data_raw
    elif isinstance(building_data_raw, str):
        try:
            parsed_building_data = json.loads(building_data_raw)
            if isinstance(parsed_building_data, dict):
                building_data = parsed_building_data
        except json.JSONDecodeError:
            logger.warning("gis.normalize_building_row.invalid_json")

    if isinstance(building_data, dict):
        building_data["geom_geojson"] = row.get("geom_geojson")

    return building_data


def _parse_geojson_geometry(geometry: dict[str, Any] | str) -> str | None:
    """Convert a GeoJSON geometry dictionary or string into a JSON string for PostGIS."""
    if isinstance(geometry, dict):
        return json.dumps(geometry)
    if isinstance(geometry, str) and geometry.strip():
        return geometry.strip()
    return None

# This variable stores the backend data directory used to resolve DEM paths.
DATA_DIR = os.getenv("DATA_DIR", "/app/data")

# This variable stores the Hatay DEM raster path.
DEM_HATAY_PATH = os.getenv(
    "DEM_HATAY_PATH",
    f"{DATA_DIR}/turkey_data/Hatay/dem_data/Hatay_dem_cog.tif",
)

# This variable stores the Adiyaman DEM raster path.
DEM_ADIYAMAN_PATH = os.getenv(
    "DEM_ADIYAMAN_PATH",
    f"{DATA_DIR}/turkey_data/Adiyaman/dem_data/Adiyaman_dem_cog.tif",
)


def quote_identifier(identifier: str) -> str:
    """Safely wrap SQL identifiers for dynamic query composition."""
    logger.debug("gis.quote_identifier.started identifier=%s", identifier)
    escaped_identifier = identifier.replace('"', '""')
    quoted_identifier = f'"{escaped_identifier}"'
    logger.debug("gis.quote_identifier.completed identifier=%s", identifier)
    return quoted_identifier


async def get_geometry_column_name(pool: asyncpg.Pool, table_name: str) -> str | None:
    """Fetch the geometry column name for a whitelisted GIS table."""
    logger.debug("gis.get_geometry_column_name.started table=%s", table_name)
    query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = $1
          AND udt_name = 'geometry'
        ORDER BY ordinal_position
        LIMIT 1
    """
    row = await pool.fetchrow(query, table_name)
    if row is None:
        logger.debug("gis.get_geometry_column_name.completed table=%s geometry_column=None", table_name)
        return None
    column_name = row.get("column_name")
    geometry_column = column_name if isinstance(column_name, str) else None
    logger.debug("gis.get_geometry_column_name.completed table=%s geometry_column=%s", table_name, geometry_column)
    return geometry_column


async def fetch_layer_geojson(pool: asyncpg.Pool, table_name: str, max_features: int) -> dict[str, Any]:
    """Load a layer as a GeoJSON FeatureCollection from PostGIS."""
    logger.info("gis.fetch_layer_geojson.started table=%s max_features=%s", table_name, max_features)
    geometry_column = await get_geometry_column_name(pool, table_name)
    if geometry_column is None:
        logger.warning("gis.fetch_layer_geojson.no_geometry_column table=%s", table_name)
        return {
            "type": "FeatureCollection",
            "features": [],
        }

    table_identifier = quote_identifier(table_name)
    geometry_identifier = quote_identifier(geometry_column)
    properties_column_literal = geometry_column.replace("'", "''")

    query = f"""
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(feature), '[]'::jsonb)
        ) AS geojson
        FROM (
            SELECT jsonb_build_object(
                'type', 'Feature',
                'geometry', ST_AsGeoJSON(t.{geometry_identifier})::jsonb,
                'properties', to_jsonb(t) - '{properties_column_literal}'
            ) AS feature
            FROM {table_identifier} AS t
            WHERE t.{geometry_identifier} IS NOT NULL
            LIMIT $1
        ) AS layer_features
    """

    row = await pool.fetchrow(query, max_features)
    if row is None:
        logger.warning("gis.fetch_layer_geojson.no_rows table=%s", table_name)
        return {
            "type": "FeatureCollection",
            "features": [],
        }

    geojson_payload = row.get("geojson")
    if isinstance(geojson_payload, dict):
        feature_count = len(geojson_payload.get("features", [])) if isinstance(geojson_payload.get("features"), list) else 0
        logger.info("gis.fetch_layer_geojson.completed table=%s features=%s", table_name, feature_count)
        return geojson_payload
    if isinstance(geojson_payload, str):
        try:
            parsed_payload = json.loads(geojson_payload)
            if isinstance(parsed_payload, dict):
                feature_count = len(parsed_payload.get("features", [])) if isinstance(parsed_payload.get("features"), list) else 0
                logger.info("gis.fetch_layer_geojson.completed table=%s features=%s", table_name, feature_count)
                return parsed_payload
        except json.JSONDecodeError:
            pass

    logger.warning("gis.fetch_layer_geojson.invalid_payload table=%s", table_name)
    return {
        "type": "FeatureCollection",
        "features": [],
    }


async def query_turkey_building_by_point(
    lat: float,
    lon: float,
    db: asyncpg.Connection | asyncpg.Pool,
) -> TurkeyBuildingQueryResult:
    """Find building data for a GPS point using contains-first and nearest-within-30m fallback."""
    logger.info("gis.query_turkey_building_by_point.started lat=%s lon=%s", lat, lon)
    contains_query = """
        SELECT
            to_jsonb(turkey_buildings) - 'geom' AS building_data,
            ST_AsGeoJSON(geom)::jsonb AS geom_geojson
        FROM turkey_buildings
        WHERE ST_Contains(
            geom,
            ST_SetSRID(ST_MakePoint($1, $2), 4326)
        )
        LIMIT 1
    """

    nearest_query = """
        SELECT
            to_jsonb(turkey_buildings) - 'geom' AS building_data,
            ST_AsGeoJSON(geom)::jsonb AS geom_geojson,
            ST_Distance(
                geom::geography,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
            ) AS distance_m
        FROM turkey_buildings
        WHERE ST_DWithin(
            geom::geography,
            ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
            30
        )
        ORDER BY distance_m ASC
        LIMIT 1
    """

    contains_row = await db.fetchrow(contains_query, lon, lat)
    if contains_row is not None:
        building_data_raw = contains_row.get("building_data")
        # This variable stores normalized building attributes as a dictionary when available.
        building_data: dict[str, Any] | None = None
        if isinstance(building_data_raw, dict):
            building_data = building_data_raw
        elif isinstance(building_data_raw, str):
            try:
                parsed_building_data = json.loads(building_data_raw)
                if isinstance(parsed_building_data, dict):
                    building_data = parsed_building_data
            except json.JSONDecodeError:
                logger.warning("gis.query_turkey_building_by_point.contains_invalid_json lat=%s lon=%s", lat, lon)

        geom_geojson = contains_row.get("geom_geojson")
        if isinstance(building_data, dict):
            building_data["geom_geojson"] = geom_geojson
        result = TurkeyBuildingQueryResult(
            found=True,
            match_strategy="contains",
            distance_m=0.0,
            building_data=building_data if isinstance(building_data, dict) else None,
            warnings=[],
        )
        logger.info("gis.query_turkey_building_by_point.completed found=%s match=%s", result.found, result.match_strategy)
        return result

    nearest_row = await db.fetchrow(nearest_query, lon, lat)
    if nearest_row is not None:
        building_data_raw = nearest_row.get("building_data")
        # This variable stores normalized nearest-building attributes as a dictionary when available.
        building_data: dict[str, Any] | None = None
        if isinstance(building_data_raw, dict):
            building_data = building_data_raw
        elif isinstance(building_data_raw, str):
            try:
                parsed_building_data = json.loads(building_data_raw)
                if isinstance(parsed_building_data, dict):
                    building_data = parsed_building_data
            except json.JSONDecodeError:
                logger.warning("gis.query_turkey_building_by_point.nearest_invalid_json lat=%s lon=%s", lat, lon)

        geom_geojson = nearest_row.get("geom_geojson")
        distance_m_raw = nearest_row.get("distance_m")
        if isinstance(building_data, dict):
            building_data["geom_geojson"] = geom_geojson

        distance_m = None
        if isinstance(distance_m_raw, (int, float)):
            distance_m = round(float(distance_m_raw), 2)

        result = TurkeyBuildingQueryResult(
            found=True,
            match_strategy="nearest_within_30m",
            distance_m=distance_m,
            building_data=building_data if isinstance(building_data, dict) else None,
            warnings=[],
        )
        logger.info("gis.query_turkey_building_by_point.completed found=%s match=%s distance_m=%s", result.found, result.match_strategy, result.distance_m)
        return result

    result = TurkeyBuildingQueryResult(
        found=False,
        match_strategy="none",
        distance_m=None,
        building_data=None,
        warnings=["no_building_footprint"],
    )
    logger.info("gis.query_turkey_building_by_point.completed found=%s match=%s", result.found, result.match_strategy)
    return result


async def query_turkey_building_by_osm_id(
    osm_id: int,
    db: asyncpg.Connection | asyncpg.Pool,
) -> TurkeyBuildingQueryResult:
    """Find building data by turkey_buildings.osm_id."""
    logger.info("gis.query_turkey_building_by_osm_id.started osm_id=%s", osm_id)
    query = """
        SELECT
            to_jsonb(turkey_buildings) - 'geom' AS building_data,
            ST_AsGeoJSON(geom)::jsonb AS geom_geojson
        FROM turkey_buildings
        WHERE osm_id = $1
        LIMIT 1
    """
    row = await db.fetchrow(query, osm_id)
    building_data = _normalize_building_row(row)
    if building_data is None:
        result = TurkeyBuildingQueryResult(
            found=False,
            match_strategy="none",
            distance_m=None,
            building_data=None,
            warnings=["no_building_footprint"],
        )
        logger.info("gis.query_turkey_building_by_osm_id.completed found=%s", result.found)
        return result

    result = TurkeyBuildingQueryResult(
        found=True,
        match_strategy="osm_id",
        distance_m=0.0,
        building_data=building_data,
        warnings=[],
    )
    logger.info("gis.query_turkey_building_by_osm_id.completed found=%s", result.found)
    return result


async def query_turkey_building_by_geometry(
    geometry: dict[str, Any] | str,
    db: asyncpg.Connection | asyncpg.Pool,
) -> TurkeyBuildingQueryResult:
    """Find building data using a GeoJSON geometry by intersection area, then nearest centroid fallback."""
    geometry_text = _parse_geojson_geometry(geometry)
    logger.info("gis.query_turkey_building_by_geometry.started has_geometry=%s", bool(geometry_text))
    if geometry_text is None:
        return TurkeyBuildingQueryResult(
            found=False,
            match_strategy="none",
            distance_m=None,
            building_data=None,
            warnings=["invalid_geometry"],
        )

    intersects_query = """
        WITH input_geom AS (
            SELECT ST_SetSRID(ST_GeomFromGeoJSON($1), 4326) AS geom
        )
        SELECT
            to_jsonb(turkey_buildings) - 'geom' AS building_data,
            ST_AsGeoJSON(turkey_buildings.geom)::jsonb AS geom_geojson,
            ST_Area(ST_Intersection(turkey_buildings.geom, input_geom.geom)::geography) AS overlap_area_m2
        FROM turkey_buildings, input_geom
        WHERE ST_Intersects(turkey_buildings.geom, input_geom.geom)
        ORDER BY overlap_area_m2 DESC NULLS LAST
        LIMIT 1
    """

    nearest_query = """
        WITH input_geom AS (
            SELECT ST_SetSRID(ST_GeomFromGeoJSON($1), 4326) AS geom
        )
        SELECT
            to_jsonb(turkey_buildings) - 'geom' AS building_data,
            ST_AsGeoJSON(turkey_buildings.geom)::jsonb AS geom_geojson,
            ST_Distance(
                turkey_buildings.geom::geography,
                ST_Centroid(input_geom.geom)::geography
            ) AS distance_m
        FROM turkey_buildings, input_geom
        WHERE ST_DWithin(
            turkey_buildings.geom::geography,
            ST_Centroid(input_geom.geom)::geography,
            50
        )
        ORDER BY distance_m ASC
        LIMIT 1
    """

    try:
        intersects_row = await db.fetchrow(intersects_query, geometry_text)
        building_data = _normalize_building_row(intersects_row)
        if building_data is not None:
            result = TurkeyBuildingQueryResult(
                found=True,
                match_strategy="geometry_intersects",
                distance_m=0.0,
                building_data=building_data,
                warnings=[],
            )
            logger.info("gis.query_turkey_building_by_geometry.completed found=%s match=%s", result.found, result.match_strategy)
            return result

        nearest_row = await db.fetchrow(nearest_query, geometry_text)
        nearest_building_data = _normalize_building_row(nearest_row)
        if nearest_building_data is not None:
            distance_m_raw = nearest_row.get("distance_m") if nearest_row is not None else None
            distance_m = round(float(distance_m_raw), 2) if isinstance(distance_m_raw, (int, float)) else None
            result = TurkeyBuildingQueryResult(
                found=True,
                match_strategy="geometry_nearest",
                distance_m=distance_m,
                building_data=nearest_building_data,
                warnings=[],
            )
            logger.info("gis.query_turkey_building_by_geometry.completed found=%s match=%s", result.found, result.match_strategy)
            return result
    except (asyncpg.PostgresError, ValueError, TypeError) as exc:
        logger.warning("gis.query_turkey_building_by_geometry.failed error=%s", exc)
        return TurkeyBuildingQueryResult(
            found=False,
            match_strategy="none",
            distance_m=None,
            building_data=None,
            warnings=["invalid_geometry"],
        )

    result = TurkeyBuildingQueryResult(
        found=False,
        match_strategy="none",
        distance_m=None,
        building_data=None,
        warnings=["no_building_footprint"],
    )
    logger.info("gis.query_turkey_building_by_geometry.completed found=%s", result.found)
    return result


async def query_flood_zone_by_point(
    lat: float,
    lon: float,
    db: asyncpg.Connection | asyncpg.Pool,
) -> FloodZoneQueryResult:
    """Check flood-zone containment and always attach nearest-waterway proximity context."""
    logger.info("gis.query_flood_zone_by_point.started lat=%s lon=%s", lat, lon)
    # This query checks whether the point lies inside a flood zone polygon.
    flood_query = """
        SELECT
            to_jsonb(flood_zones) - 'geom' AS flood_zone_data,
            ST_AsGeoJSON(geom)::jsonb AS geom_geojson,
            waterway_type,
            waterway_name,
            province
        FROM flood_zones
        WHERE ST_Contains(
            geom,
            ST_SetSRID(ST_MakePoint($1, $2), 4326)
        )
        LIMIT 1
    """

    # This query always runs to provide nearest waterway distance context.
    waterway_query = """
        SELECT
            name AS waterway_name,
            waterway AS waterway_type,
            province,
            ST_Distance(
                geom::geography,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
            ) AS distance_m
        FROM turkey_lines
        WHERE waterway IS NOT NULL
        ORDER BY distance_m ASC
        LIMIT 1
    """

    flood_row = await db.fetchrow(flood_query, lon, lat)
    waterway_row = await db.fetchrow(waterway_query, lon, lat)

    flood_zone_data: dict[str, Any] | None = None
    if flood_row is not None:
        flood_zone_data_raw = flood_row.get("flood_zone_data")
        geom_geojson = flood_row.get("geom_geojson")
        # This variable stores normalized flood zone attributes as a dictionary when available.
        if isinstance(flood_zone_data_raw, dict):
            flood_zone_data = flood_zone_data_raw
        elif isinstance(flood_zone_data_raw, str):
            try:
                parsed_flood_data = json.loads(flood_zone_data_raw)
                if isinstance(parsed_flood_data, dict):
                    flood_zone_data = parsed_flood_data
            except json.JSONDecodeError:
                logger.warning("gis.query_flood_zone_by_point.invalid_json lat=%s lon=%s", lat, lon)
        if isinstance(flood_zone_data, dict):
            flood_zone_data["geom_geojson"] = geom_geojson

    nearest_distance = None
    if waterway_row is not None:
        distance_raw = waterway_row.get("distance_m")
        if isinstance(distance_raw, (int, float)):
            nearest_distance = round(float(distance_raw), 1)

    flood_waterway_type = flood_row.get("waterway_type") if flood_row is not None else None
    flood_waterway_name = flood_row.get("waterway_name") if flood_row is not None else None
    nearest_waterway_type = waterway_row.get("waterway_type") if waterway_row is not None else None
    nearest_waterway_name = waterway_row.get("waterway_name") if waterway_row is not None else None
    flood_province = flood_row.get("province") if flood_row is not None else None
    nearest_province = waterway_row.get("province") if waterway_row is not None else None

    result = FloodZoneQueryResult(
        is_flood_zone=flood_row is not None,
        flood_zone_data=flood_zone_data,
        waterway_type=flood_waterway_type if isinstance(flood_waterway_type, str) else (
            nearest_waterway_type if isinstance(nearest_waterway_type, str) else None
        ),
        waterway_name=flood_waterway_name if isinstance(flood_waterway_name, str) else (
            nearest_waterway_name if isinstance(nearest_waterway_name, str) else None
        ),
        distance_to_waterway_m=nearest_distance,
        province=flood_province if isinstance(flood_province, str) else (
            nearest_province if isinstance(nearest_province, str) else None
        ),
    )
    logger.info(
        "gis.query_flood_zone_by_point.completed is_flood_zone=%s waterway=%s distance_to_waterway_m=%s",
        result.is_flood_zone,
        result.waterway_name,
        result.distance_to_waterway_m,
    )
    return result


async def query_location_info_by_point(
    lat: float,
    lon: float,
    db: asyncpg.Connection | asyncpg.Pool,
) -> LocationInfoQueryResult:
    """Fetch location context using province containment, nearest district centroid, and nearest mapped point."""
    logger.info("gis.query_location_info_by_point.started lat=%s lon=%s", lat, lon)
    # This query finds the province polygon that contains the input point.
    province_query = """
        SELECT
            to_jsonb(turkey_provinces) - 'geom' AS province_data,
            ST_AsGeoJSON(geom)::jsonb AS geom_geojson
        FROM turkey_provinces
        WHERE ST_Contains(
            geom,
            ST_SetSRID(ST_MakePoint($1, $2), 4326)
        )
        LIMIT 1
    """

    # This query finds the nearest district centroid for district-level approximation.
    district_query = """
        SELECT
            to_jsonb(turkey_districts_pts) - 'geom' AS district_data,
            ST_AsGeoJSON(geom)::jsonb AS geom_geojson,
            ST_Distance(
                geom::geography,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
            ) AS distance_m
        FROM turkey_districts_pts
        ORDER BY distance_m ASC
        LIMIT 1
    """

    # This query provides nearest turkey_points feature context as a fallback locality indicator.
    nearest_point_query = """
        SELECT
            to_jsonb(turkey_points) - 'geom' AS point_data,
            ST_AsGeoJSON(geom)::jsonb AS geom_geojson,
            ST_Distance(
                geom::geography,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
            ) AS distance_m
        FROM turkey_points
        ORDER BY distance_m ASC
        LIMIT 1
    """

    province_row = await db.fetchrow(province_query, lon, lat)
    district_row = await db.fetchrow(district_query, lon, lat)
    nearest_point_row = await db.fetchrow(nearest_point_query, lon, lat)

    province_data: dict[str, Any] | None = None
    if province_row is not None:
        province_data_raw = province_row.get("province_data")
        province_geom_geojson = province_row.get("geom_geojson")
        # This variable stores normalized province attributes as a dictionary when available.
        if isinstance(province_data_raw, dict):
            province_data = province_data_raw
        elif isinstance(province_data_raw, str):
            try:
                parsed_province_data = json.loads(province_data_raw)
                if isinstance(parsed_province_data, dict):
                    province_data = parsed_province_data
            except json.JSONDecodeError:
                logger.warning("gis.query_location_info_by_point.province_invalid_json lat=%s lon=%s", lat, lon)
        if isinstance(province_data, dict):
            province_data["geom_geojson"] = province_geom_geojson

    district_data: dict[str, Any] | None = None
    district_distance_m = None
    if district_row is not None:
        district_data_raw = district_row.get("district_data")
        district_geom_geojson = district_row.get("geom_geojson")
        # This variable stores normalized district attributes as a dictionary when available.
        if isinstance(district_data_raw, dict):
            district_data = district_data_raw
        elif isinstance(district_data_raw, str):
            try:
                parsed_district_data = json.loads(district_data_raw)
                if isinstance(parsed_district_data, dict):
                    district_data = parsed_district_data
            except json.JSONDecodeError:
                logger.warning("gis.query_location_info_by_point.district_invalid_json lat=%s lon=%s", lat, lon)
        if isinstance(district_data, dict):
            district_data["geom_geojson"] = district_geom_geojson

        district_distance_raw = district_row.get("distance_m")
        if isinstance(district_distance_raw, (int, float)):
            district_distance_m = round(float(district_distance_raw), 1)

    nearest_point_data: dict[str, Any] | None = None
    nearest_point_distance_m = None
    if nearest_point_row is not None:
        nearest_point_data_raw = nearest_point_row.get("point_data")
        nearest_point_geom_geojson = nearest_point_row.get("geom_geojson")
        # This variable stores normalized point attributes as a dictionary when available.
        if isinstance(nearest_point_data_raw, dict):
            nearest_point_data = nearest_point_data_raw
        elif isinstance(nearest_point_data_raw, str):
            try:
                parsed_point_data = json.loads(nearest_point_data_raw)
                if isinstance(parsed_point_data, dict):
                    nearest_point_data = parsed_point_data
            except json.JSONDecodeError:
                logger.warning("gis.query_location_info_by_point.point_invalid_json lat=%s lon=%s", lat, lon)
        if isinstance(nearest_point_data, dict):
            nearest_point_data["geom_geojson"] = nearest_point_geom_geojson

        nearest_point_distance_raw = nearest_point_row.get("distance_m")
        if isinstance(nearest_point_distance_raw, (int, float)):
            nearest_point_distance_m = round(float(nearest_point_distance_raw), 1)

    province_name = None
    if isinstance(province_data, dict):
        province_name_raw = province_data.get("name_en") or province_data.get("name_tr") or province_data.get("province")
        if isinstance(province_name_raw, str):
            province_name = province_name_raw

    district_name = None
    if isinstance(district_data, dict):
        district_name_raw = district_data.get("district")
        if isinstance(district_name_raw, str):
            district_name = district_name_raw

    if province_name is None and isinstance(nearest_point_data, dict):
        point_province_raw = nearest_point_data.get("province")
        if isinstance(point_province_raw, str):
            province_name = point_province_raw

    if district_name is None and isinstance(nearest_point_data, dict):
        point_district_raw = nearest_point_data.get("district")
        if isinstance(point_district_raw, str):
            district_name = point_district_raw

    result = LocationInfoQueryResult(
        found=any(
            value is not None
            for value in (province_data, district_data, nearest_point_data)
        ),
        province=province_name,
        district=district_name,
        province_data=province_data,
        district_data=district_data,
        nearest_point_data=nearest_point_data,
        district_distance_m=district_distance_m,
        nearest_point_distance_m=nearest_point_distance_m,
    )
    logger.info(
        "gis.query_location_info_by_point.completed found=%s province=%s district=%s",
        result.found,
        result.province,
        result.district,
    )
    return result


def query_dem_elevation_by_point(lat: float, lon: float) -> DemElevationQueryResult:
    """Fetch elevation (and slope context) from local DEM rasters for a WGS84 coordinate."""
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    logger.info("gis.query_dem_elevation_by_point.started lat=%s lon=%s", lat, lon)

    # This variable sets the preferred DEM by longitude split between Hatay and Adiyaman.
    preferred_region = "hatay" if lon < 37.5 else "adiyaman"

    # This variable maps region keys to DEM raster file paths.
    dem_path_map = {
        "hatay": DEM_HATAY_PATH,
        "adiyaman": DEM_ADIYAMAN_PATH,
    }

    # This variable defines DEM lookup order with fallback to the other region file.
    dem_lookup_order = (
        (preferred_region, dem_path_map[preferred_region]),
        ("adiyaman", dem_path_map["adiyaman"]) if preferred_region == "hatay" else ("hatay", dem_path_map["hatay"]),
    )

    last_error: str | None = None

    for region_key, dem_path in dem_lookup_order:
        if not os.path.exists(dem_path):
            last_error = f"DEM file not found: {dem_path}"
            logger.warning("gis.query_dem_elevation_by_point.dem_missing region=%s dem_path=%s", region_key, dem_path)
            continue

        try:
            with rasterio.open(dem_path) as dem_dataset:
                bounds = dem_dataset.bounds
                if lon < bounds.left or lon > bounds.right or lat < bounds.bottom or lat > bounds.top:
                    last_error = f"Coordinate outside DEM bounds for {region_key}"
                    logger.debug("gis.query_dem_elevation_by_point.outside_bounds region=%s", region_key)
                    continue

                elevation_sample = next(dem_dataset.sample([(lon, lat)]))[0]
                nodata_value = dem_dataset.nodata

                if nodata_value is not None and float(elevation_sample) == float(nodata_value):
                    last_error = f"NoData elevation at coordinate for {region_key}"
                    logger.debug("gis.query_dem_elevation_by_point.nodata region=%s", region_key)
                    continue

                elevation_m = round(float(elevation_sample), 1)

                pixel_row, pixel_col = dem_dataset.index(lon, lat)
                slope_window = Window(
                    col_off=max(0, pixel_col - 1),
                    row_off=max(0, pixel_row - 1),
                    width=3,
                    height=3,
                )
                neighborhood = dem_dataset.read(1, window=slope_window).astype(float)

                if nodata_value is not None:
                    neighborhood[neighborhood == float(nodata_value)] = np.nan

                valid_values = neighborhood[~np.isnan(neighborhood)]
                slope_degrees = None
                slope_risk: str = "unknown"

                if valid_values.size > 0:
                    fill_value = float(np.nanmean(valid_values))
                    neighborhood_filled = np.nan_to_num(neighborhood, nan=fill_value)
                    pixel_size_m = abs(float(dem_dataset.res[0])) * 111000
                    gradient_y, gradient_x = np.gradient(neighborhood_filled, pixel_size_m)
                    slope_radians = np.arctan(np.sqrt(gradient_x**2 + gradient_y**2))
                    slope_degrees = round(float(np.degrees(slope_radians).mean()), 1)

                    if slope_degrees > 20:
                        slope_risk = "high"
                    elif slope_degrees > 10:
                        slope_risk = "moderate"
                    else:
                        slope_risk = "low"

                result = DemElevationQueryResult(
                    found=True,
                    elevation_m=elevation_m,
                    slope_degrees=slope_degrees,
                    slope_risk=slope_risk,
                    dem_region=region_key,
                    dem_path=dem_path,
                    error=None,
                )
                logger.info(
                    "gis.query_dem_elevation_by_point.completed found=%s region=%s elevation_m=%s slope_degrees=%s",
                    result.found,
                    result.dem_region,
                    result.elevation_m,
                    result.slope_degrees,
                )
                return result
        except Exception as exc:
            last_error = str(exc)
            logger.exception("gis.query_dem_elevation_by_point.failed region=%s error=%s", region_key, exc)

    result = DemElevationQueryResult(
        found=False,
        elevation_m=None,
        slope_degrees=None,
        slope_risk="unknown",
        dem_region="unknown",
        dem_path=None,
        error=last_error,
    )
    logger.warning("gis.query_dem_elevation_by_point.completed found=%s error=%s", result.found, result.error)
    return result


async def query_nearest_road_by_point(
    lat: float,
    lon: float,
    db: asyncpg.Connection | asyncpg.Pool,
) -> NearestRoadQueryResult:
    """Find the nearest road in turkey_lines for a GPS coordinate using highway features only."""
    logger.info("gis.query_nearest_road_by_point.started lat=%s lon=%s", lat, lon)
    # This variable maps OSM highway classes to plain-language access guidance.
    HIGHWAY_DESCRIPTIONS = {
        "trunk": "Major road — heavy vehicle access",
        "primary": "Major road — heavy vehicle access",
        "trunk_link": "Major road — heavy vehicle access",
        "primary_link": "Major road — heavy vehicle access",
        "secondary": "Medium road — vehicle access",
        "tertiary": "Medium road — vehicle access",
        "secondary_link": "Medium road — vehicle access",
        "tertiary_link": "Medium road — vehicle access",
        "residential": "Residential street — vehicle access",
        "unclassified": "Residential street — vehicle access",
        "living_street": "Residential street — vehicle access",
        "service": "Service road — limited vehicle access",
        "track": "Track — 4WD or foot only",
        "footway": "Foot access only",
        "path": "Foot access only",
        "pedestrian": "Foot access only",
        "steps": "Foot access only",
        "cycleway": "Foot access only",
        "construction": "Road under construction — access unclear",
    }

    query = """
        SELECT
            name,
            highway,
            surface,
            bridge,
            tunnel,
            oneway,
            province,
            ST_Distance(
                geom::geography,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
            ) AS distance_m
        FROM turkey_lines
        WHERE highway IS NOT NULL
        ORDER BY distance_m ASC
        LIMIT 1
    """

    row = await db.fetchrow(query, lon, lat)
    if row is None:
        result = NearestRoadQueryResult(
            found=False,
            road_name=None,
            highway_type=None,
            highway_description=None,
            surface=None,
            distance_m=None,
            bridge=None,
            tunnel=None,
            oneway=None,
            province=None,
            road_access="unknown",
        )
        logger.info("gis.query_nearest_road_by_point.completed found=%s", result.found)
        return result

    highway_type_raw = row.get("highway")
    highway_type = highway_type_raw if isinstance(highway_type_raw, str) else None

    if highway_type:
        highway_description = HIGHWAY_DESCRIPTIONS.get(
            highway_type,
            f"{highway_type} — vehicle access",
        )
    else:
        highway_description = None

    road_name_raw = row.get("name")
    road_name = road_name_raw if isinstance(road_name_raw, str) and road_name_raw else None

    foot_only_types = {"footway", "path", "pedestrian", "steps", "cycleway"}
    road_access = "foot_only" if highway_type in foot_only_types else "passable"

    distance_m_raw = row.get("distance_m")
    distance_m = None
    if isinstance(distance_m_raw, (int, float)):
        distance_m = round(float(distance_m_raw), 1)

    result = NearestRoadQueryResult(
        found=True,
        road_name=road_name,
        highway_type=highway_type,
        highway_description=highway_description,
        surface=row.get("surface") if isinstance(row.get("surface"), str) else None,
        distance_m=distance_m,
        bridge=row.get("bridge") if isinstance(row.get("bridge"), str) else None,
        tunnel=row.get("tunnel") if isinstance(row.get("tunnel"), str) else None,
        oneway=row.get("oneway") if isinstance(row.get("oneway"), str) else None,
        province=row.get("province") if isinstance(row.get("province"), str) else None,
        road_access=road_access,
    )
    logger.info(
        "gis.query_nearest_road_by_point.completed found=%s road_name=%s distance_m=%s",
        result.found,
        result.road_name,
        result.distance_m,
    )
    return result


async def query_osrm_route(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    profile: str = "driving",
) -> OsrmRouteQueryResult:
    """Fetch route distance, duration, and geometry from local OSRM for two WGS84 points."""
    logger.info(
        "gis.query_osrm_route.started start_lat=%s start_lon=%s end_lat=%s end_lon=%s profile=%s",
        start_lat,
        start_lon,
        end_lat,
        end_lon,
        profile,
    )
    # This variable defines profiles that are valid for OSRM route requests.
    valid_profiles = {"driving", "walking", "cycling"}
    normalized_profile = profile if profile in valid_profiles else "driving"

    # This variable builds the OSRM route endpoint using required lon,lat coordinate order.
    route_url = (
        f"{OSRM_BASE_URL}/route/v1/{normalized_profile}/"
        f"{start_lon},{start_lat};{end_lon},{end_lat}"
    )

    # This variable defines required query params for geojson route geometry output.
    params = {
        "overview": "full",
        "geometries": "geojson",
    }

    async with httpx.AsyncClient(timeout=OSRM_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(route_url, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            result = OsrmRouteQueryResult(
                found=False,
                profile=normalized_profile,
                distance_m=None,
                duration_s=None,
                geometry_geojson=None,
                start_lon=start_lon,
                start_lat=start_lat,
                end_lon=end_lon,
                end_lat=end_lat,
                warnings=["osrm_http_error"],
                error=str(exc),
            )
            logger.exception("gis.query_osrm_route.http_error profile=%s error=%s", normalized_profile, exc)
            return result

    if payload.get("code") != "Ok":
        result = OsrmRouteQueryResult(
            found=False,
            profile=normalized_profile,
            distance_m=None,
            duration_s=None,
            geometry_geojson=None,
            start_lon=start_lon,
            start_lat=start_lat,
            end_lon=end_lon,
            end_lat=end_lat,
            warnings=["osrm_no_route"],
            error=str(payload.get("message") or "OSRM route not available"),
        )
        logger.warning("gis.query_osrm_route.no_route profile=%s error=%s", normalized_profile, result.error)
        return result

    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        result = OsrmRouteQueryResult(
            found=False,
            profile=normalized_profile,
            distance_m=None,
            duration_s=None,
            geometry_geojson=None,
            start_lon=start_lon,
            start_lat=start_lat,
            end_lon=end_lon,
            end_lat=end_lat,
            warnings=["osrm_empty_routes"],
            error="OSRM returned no routes",
        )
        logger.warning("gis.query_osrm_route.empty_routes profile=%s", normalized_profile)
        return result

    first_route = routes[0]
    distance_raw = first_route.get("distance")
    duration_raw = first_route.get("duration")
    geometry_raw = first_route.get("geometry")

    distance_m = round(float(distance_raw), 1) if isinstance(distance_raw, (int, float)) else None
    duration_s = round(float(duration_raw), 1) if isinstance(duration_raw, (int, float)) else None
    geometry_geojson = geometry_raw if isinstance(geometry_raw, dict) else None

    result = OsrmRouteQueryResult(
        found=True,
        profile=normalized_profile,
        distance_m=distance_m,
        duration_s=duration_s,
        geometry_geojson=geometry_geojson,
        start_lon=start_lon,
        start_lat=start_lat,
        end_lon=end_lon,
        end_lat=end_lat,
        warnings=[],
        error=None,
    )
    logger.info(
        "gis.query_osrm_route.completed found=%s profile=%s distance_m=%s duration_s=%s",
        result.found,
        result.profile,
        result.distance_m,
        result.duration_s,
    )
    return result
