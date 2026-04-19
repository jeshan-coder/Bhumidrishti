import httpx
import asyncio

async def test_titiler():
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # Test basic TiTiler connection
            print("Testing TiTiler connection...")
            response = await client.get("http://titiler:80/")
            print(f"TiTiler root: {response.status_code}")
            
            # Test DEM tile request
            print("\nTesting DEM tile request...")
            tile_url = "http://titiler:80/cog/tiles/15/19865/12667.png"
            params = {
                "url": "/data/adiyaman/Adiyaman_dem_cog.tif",
                "colormap_name": "terrain",
                "rescale": "0,3000",
            }
            response = await client.get(tile_url, params=params)
            print(f"Tile request status: {response.status_code}")
            print(f"Response: {response.text[:200]}")
            
        except Exception as e:
            print(f"Error: {e}")

asyncio.run(test_titiler())
