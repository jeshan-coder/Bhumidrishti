"""DEM (Digital Elevation Model) endpoints using TiTiler."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
import httpx
from typing import Any

from db.postgres import get_pool
from services.gis import query_dem_elevation_by_point

router = APIRouter(prefix="/dem", tags=["dem"])
logger = logging.getLogger(__name__)

TITILER_URL = "http://titiler:80"

DEM_FILES = {
    "adiyaman": "/data/adiyaman/Adiyaman_dem_cog.tif",
    "hatay": "/data/hatay/Hatay_dem_cog.tif",
}


@router.get("/tiles/{region}/{z}/{x}/{y}.png")
async def get_dem_tile(region: str, z: int, x: int, y: int) -> Response:
    """Proxy DEM tiles from TiTiler with terrain colormap."""
    if region not in DEM_FILES:
        raise HTTPException(status_code=404, detail=f"Region {region} not found")

    dem_path = DEM_FILES[region]
    
    # TiTiler tile endpoint with WebMercatorQuad TileMatrixSet
    tile_url = f"{TITILER_URL}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    params = {
        "url": dem_path,
        "colormap_name": "terrain",
        "rescale": "0,3000",  # Elevation range for Turkey region
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(tile_url, params=params)
            response.raise_for_status()
            return Response(
                content=response.content,
                media_type="image/png",
                headers={
                    "Cache-Control": "public, max-age=3600",
                },
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=500, detail=f"TiTiler request failed: {exc}")


@router.get("/info/{region}")
async def get_dem_info(region: str) -> dict[str, Any]:
    """Get DEM metadata from TiTiler."""
    if region not in DEM_FILES:
        raise HTTPException(status_code=404, detail=f"Region {region} not found")

    dem_path = DEM_FILES[region]
    info_url = f"{TITILER_URL}/cog/info"
    params = {"url": dem_path}

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(info_url, params=params)
            response.raise_for_status()
            return {
                "success": True,
                "data": response.json(),
                "error": None,
            }
        except httpx.HTTPError as exc:
            return {
                "success": False,
                "data": None,
                "error": f"TiTiler info request failed: {exc}",
            }


@router.get("/bounds/{region}")
async def get_dem_bounds(region: str) -> dict[str, Any]:
    """Get DEM geographic bounds — extracted from TiTiler /cog/info."""
    if region not in DEM_FILES:
        raise HTTPException(status_code=404, detail=f"Region {region} not found")

    dem_path = DEM_FILES[region]
    info_url = f"{TITILER_URL}/cog/info"
    params = {"url": dem_path}

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(info_url, params=params)
            response.raise_for_status()
            info = response.json()
            return {
                "success": True,
                "data": {
                    "bounds": info.get("bounds"),
                    "crs": info.get("crs"),
                    "width": info.get("width"),
                    "height": info.get("height"),
                },
                "error": None,
            }
        except httpx.HTTPError as exc:
            return {
                "success": False,
                "data": None,
                "error": f"TiTiler bounds request failed: {exc}",
            }


@router.post("/backfill-elevations")
async def backfill_assessment_elevations(overwrite: bool = False) -> dict[str, Any]:
    """Backfill elevation_m, slope_degrees, slope_risk for existing assessments.

    By default only fills rows where elevation_m IS NULL.
    Pass ?overwrite=true to re-compute for all rows regardless.
    """
    pool = get_pool()
    if not pool:
        return {"success": False, "data": None, "error": "Database pool not initialized"}

    where = "" if overwrite else "WHERE elevation_m IS NULL"
    fetch_query = f"SELECT id, lat, lon FROM assessments {where} ORDER BY created_at DESC"

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(fetch_query)
    except Exception as exc:
        return {"success": False, "data": None, "error": f"Failed to fetch assessments: {exc}"}

    total = len(rows)
    updated = 0
    skipped = 0
    errors: list[str] = []

    logger.info("dem.backfill_elevations.started total=%s overwrite=%s", total, overwrite)

    for row in rows:
        assessment_id = row["id"]
        lat = row["lat"]
        lon = row["lon"]
        if lat is None or lon is None:
            skipped += 1
            continue
        try:
            dem_result = await asyncio.to_thread(query_dem_elevation_by_point, lat, lon)
            if not dem_result.found:
                skipped += 1
                logger.warning(
                    "dem.backfill_elevations.not_found id=%s lat=%s lon=%s error=%s",
                    assessment_id, lat, lon, dem_result.error,
                )
                continue
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE assessments
                    SET elevation_m = $2, slope_degrees = $3, slope_risk = $4
                    WHERE id = $1
                    """,
                    assessment_id,
                    dem_result.elevation_m,
                    dem_result.slope_degrees,
                    dem_result.slope_risk,
                )
            updated += 1
            logger.info(
                "dem.backfill_elevations.updated id=%s elevation_m=%s slope_degrees=%s slope_risk=%s",
                assessment_id, dem_result.elevation_m, dem_result.slope_degrees, dem_result.slope_risk,
            )
        except Exception as exc:
            errors.append(f"{assessment_id}: {exc}")
            logger.exception("dem.backfill_elevations.row_failed id=%s error=%s", assessment_id, exc)

    logger.info(
        "dem.backfill_elevations.completed total=%s updated=%s skipped=%s errors=%s",
        total, updated, skipped, len(errors),
    )
    return {
        "success": True,
        "data": {
            "total_rows": total,
            "updated": updated,
            "skipped_no_dem": skipped,
            "errors": errors,
        },
        "error": None,
    }
