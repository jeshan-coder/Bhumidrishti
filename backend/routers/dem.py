"""DEM (Digital Elevation Model) endpoints using TiTiler."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
import httpx
from typing import Any

router = APIRouter(prefix="/dem", tags=["dem"])

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
