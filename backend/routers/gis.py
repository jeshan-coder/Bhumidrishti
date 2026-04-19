"""GIS layer endpoints for map overlays."""

from typing import Any
from fastapi import APIRouter, Query, HTTPException
from db.postgres import get_pool
from services.gis import fetch_layer_geojson
from models.gis import GisLayerListResponse, GisLayerResponse

router = APIRouter(prefix="/gis", tags=["gis"])

GIS_LAYER_TABLES: dict[str, str] = {
    "turkey_provinces": "turkey_provinces",
    "turkey_points": "turkey_points",
    "turkey_lines": "turkey_lines",
    "turkey_districts_pts": "turkey_districts_pts",
    "turkey_buildings": "turkey_buildings",
    "flood_zones": "flood_zones",
    "destroyed_buildings": "destroyed_buildings",
}

GIS_LAYER_ALIASES: dict[str, str] = {
    "descriyed_buildings": "destroyed_buildings",
}


def _resolve_layer_name(layer_name: str) -> str | None:
    """Resolve a requested layer name to a canonical table key."""
    normalized = layer_name.strip().lower()
    if normalized in GIS_LAYER_TABLES:
        return normalized
    return GIS_LAYER_ALIASES.get(normalized)


@router.get("/layers")
async def list_gis_layers() -> dict[str, Any]:
    """List available GIS overlay layers."""
    return {
        "success": True,
        "data": GisLayerListResponse(layers=list(GIS_LAYER_TABLES.keys())).model_dump(),
        "error": None,
    }


@router.get("/layers/{layer_name}")
async def get_gis_layer(
    layer_name: str,
    max_features: int = Query(default=10000, ge=1, le=100000),
) -> dict[str, Any]:
    """Return a layer as GeoJSON for map overlays."""
    resolved_layer_name = _resolve_layer_name(layer_name)
    if resolved_layer_name is None:
        return {
            "success": False,
            "data": None,
            "error": f"Unknown layer: {layer_name}",
        }

    pool = get_pool()
    if pool is None:
        return {
            "success": False,
            "data": None,
            "error": "Database pool is not initialized",
        }

    table_name = GIS_LAYER_TABLES[resolved_layer_name]

    try:
        geojson = await fetch_layer_geojson(pool, table_name, max_features)
        features = geojson.get("features", []) if isinstance(geojson, dict) else []
        feature_count = len(features) if isinstance(features, list) else 0

        response_data = GisLayerResponse(
            layer=resolved_layer_name,
            table=table_name,
            max_features=max_features,
            feature_count=feature_count,
            geojson=geojson,
        )

        return {
            "success": True,
            "data": response_data.model_dump(),
            "error": None,
        }
    except Exception as exc:
        return {
            "success": False,
            "data": None,
            "error": f"Failed to load layer {resolved_layer_name}: {exc}",
        }
