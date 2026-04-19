"""GIS service layer for PostGIS queries."""

import json
from typing import Any
import asyncpg


def quote_identifier(identifier: str) -> str:
    """Safely wrap SQL identifiers for dynamic query composition."""
    escaped_identifier = identifier.replace('"', '""')
    return f'"{escaped_identifier}"'


async def get_geometry_column_name(pool: asyncpg.Pool, table_name: str) -> str | None:
    """Fetch the geometry column name for a whitelisted GIS table."""
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
        return None
    column_name = row.get("column_name")
    return column_name if isinstance(column_name, str) else None


async def fetch_layer_geojson(pool: asyncpg.Pool, table_name: str, max_features: int) -> dict[str, Any]:
    """Load a layer as a GeoJSON FeatureCollection from PostGIS."""
    geometry_column = await get_geometry_column_name(pool, table_name)
    if geometry_column is None:
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
        return {
            "type": "FeatureCollection",
            "features": [],
        }

    geojson_payload = row.get("geojson")
    if isinstance(geojson_payload, dict):
        return geojson_payload
    if isinstance(geojson_payload, str):
        try:
            parsed_payload = json.loads(geojson_payload)
            if isinstance(parsed_payload, dict):
                return parsed_payload
        except json.JSONDecodeError:
            pass

    return {
        "type": "FeatureCollection",
        "features": [],
    }
