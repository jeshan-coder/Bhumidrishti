"""Satellite / pre-earthquake imagery endpoints using TiTiler.

Serves pre-earthquake COG imagery (true-colour RGB satellite/drone images)
for Adiyaman and Hatay through a TiTiler proxy.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
import httpx
from typing import Any

router = APIRouter(prefix="/satellite", tags=["satellite"])

TITILER_URL = "http://titiler:80"

# Pre-earthquake satellite imagery — Cloud-Optimised GeoTIFFs
SATELLITE_FILES = {
    "adiyaman": "/data/satellite/adiyaman/pre_earthquake_adiyaman_cog.tif",
    "hatay":    "/data/satellite/hatay/pre_earthquake_hatay_cog.tif",
}


@router.get("/tiles/{region}/{z}/{x}/{y}.png")
async def get_satellite_tile(region: str, z: int, x: int, y: int) -> Response:
    """Proxy pre-earthquake satellite tiles from TiTiler as natural-colour RGB."""
    if region not in SATELLITE_FILES:
        raise HTTPException(status_code=404, detail=f"Region '{region}' not found. Available: {list(SATELLITE_FILES)}")

    cog_path = SATELLITE_FILES[region]

    # Natural colour RGB — bidx selects bands 1,2,3 (R,G,B).
    # rescale auto-stretches to a sensible 8-bit range for typical satellite imagery.
    tile_url = f"{TITILER_URL}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    params: dict[str, Any] = {
        "url": cog_path,
        "bidx": ["1", "2", "3"],
        "rescale": "0,255",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
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
async def get_satellite_info(region: str) -> dict[str, Any]:
    """Return COG metadata (bands, CRS, bounds) from TiTiler for one region."""
    if region not in SATELLITE_FILES:
        raise HTTPException(status_code=404, detail=f"Region '{region}' not found")

    cog_path = SATELLITE_FILES[region]
    info_url = f"{TITILER_URL}/cog/info"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(info_url, params={"url": cog_path})
            response.raise_for_status()
            return {"success": True, "data": response.json(), "error": None}
        except httpx.HTTPError as exc:
            return {"success": False, "data": None, "error": f"TiTiler info failed: {exc}"}


@router.get("/bounds/{region}")
async def get_satellite_bounds(region: str) -> dict[str, Any]:
    """Return geographic bounding box of the COG for one region.

    TiTiler exposes bounds inside the /cog/info response (not a dedicated /cog/bounds).
    """
    if region not in SATELLITE_FILES:
        raise HTTPException(status_code=404, detail=f"Region '{region}' not found")

    cog_path = SATELLITE_FILES[region]
    info_url = f"{TITILER_URL}/cog/info"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(info_url, params={"url": cog_path})
            response.raise_for_status()
            info = response.json()
            return {
                "success": True,
                "data": {
                    "bounds": info.get("bounds"),
                    "crs": info.get("crs"),
                    "width": info.get("width"),
                    "height": info.get("height"),
                    "bands": info.get("count"),
                },
                "error": None,
            }
        except httpx.HTTPError as exc:
            return {"success": False, "data": None, "error": f"TiTiler bounds failed: {exc}"}


@router.get("/regions")
async def list_satellite_regions() -> dict[str, Any]:
    """List all available satellite image regions."""
    return {
        "success": True,
        "data": {
            "regions": list(SATELLITE_FILES.keys()),
            "description": "Pre-earthquake satellite imagery (Cloud-Optimised GeoTIFF)",
        },
        "error": None,
    }
