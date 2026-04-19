---
trigger: always_on
---

#Use comment properly . 
#in top of file use comment to explain what file is for
#in top of function use comment to explain what function is for
#in top of class use comment to explain what class is for
#in top of variable use comment to explain what variable is for
# BhumiDrishti — Windsurf Rules

## Project overview
BhumiDrishti (भूमिदृष्टि) is an offline-first disaster damage 
assessment platform for the Gemma 4 Good Hackathon 2026.
Track: Global Resilience + Ollama special track.
Single monorepo with Next.js frontend and FastAPI backend.
Everything runs locally — zero internet required during use.

## Monorepo structure
/frontend    — Next.js 14 App Router + TypeScript + shadcn/ui
/backend     — FastAPI + Python 3.11
/data        — GIS files (GeoPackage, GeoTIFF)
/sample_data — demo photos and seed JSON
docker-compose.yml — at root, starts everything
.env.example — all environment variables documented here

## Backend rules
- Python 3.11
- FastAPI with async/await on all routes
- Pydantic v2 for all models and validation
- Use ollama Python client directly — never LangChain or LlamaIndex
- Model is always gemma4:26b
- Ollama runs on localhost:11434
- FastAPI runs on localhost:8000
- POSTgres with postgis
- GeoPandas loaded once at startup and kept in memory
- Rasterio for DEM and GeoTIFF processing
- Pillow for image validation and EXIF extraction
- All coordinates WGS84 EPSG:4326
- Main GeoPackage is data/turkey.gpkg
- Layers inside: buildings, roads, destroyed_buildings,
  flood_zones, shelters
- DEM file is data/turkey_dem.tif
- Never use blocking code in async routes — use run_in_executor
- All responses follow this structure:
  {"success": bool, "data": any, "error": str | null}
- CORS enabled for localhost:3000
- python-multipart for file uploads
- Type hints on every function

## Frontend rules
- Next.js 14 App Router only — never Pages Router
- TypeScript strict mode — never use any type
- shadcn/ui for all components — never build from scratch
  what shadcn already provides
- Tailwind CSS for all styling
- MapLibre GL JS via react-map-gl for all maps
- Zustand for global state
- TanStack Query for all API calls — never raw fetch in components
- Dexie.js for IndexedDB offline storage
- next-pwa for PWA service worker
- lucide-react for all icons
- All API calls go to http://localhost:8000
- File uploads use FormData
- Map tiles from /public/tiles as PMTiles format
- Never useEffect for data fetching

## Pages
- / redirects to /assessment
- /assessment — new assessment upload
- /assessment/[id] — damage report result
- /map — full screen field map
- /dashboard — coordinator view
- /chat — AI assistant
- /reports — all assessments table

## Brand colors — use these exact values
--brand: #0F6E56
--brand-dark: #085041
--brand-light: #1D9E75
--brand-tint: #E1F5EE
--page-bg: #FAFAF8
--surface: #F1EFE8
--border: #D3D1C7
Severity 1: #639922
Severity 2: #97C459
Severity 3: #EF9F27
Severity 4: #E24B4A
Severity 5: #A32D2D
Font UI: Inter
Font mono: JetBrains Mono

## Assessment data model
id, lat, lon, severity (1-5), damage_type, building_type,
floors, material, estimated_occupants, recommended_action,
flood_zone (bool), slope_degrees, nearest_shelter,
shelter_distance_m, road_access, road_name, district,
confidence (0-1), input_type (ground_photo | drone_images |
orthophoto), worker_name, timestamp, status
(pending | in_review | responded), reasoning, warnings,
owner_name, owner_phone, turkish_summary, photo_path

## The agent loop — most critical part
File: backend/agent.py
1. Receives image bytes + lat + lon + optional note
2. Sends to Gemma 4 with 4 tool definitions
3. Gemma 4 calls tools autonomously
4. Tools query local GeoPackage via GeoPandas
5. Loop until no more tool calls
6. Parse final response as structured JSON
7. Validate with Pydantic
Never use any agent framework — implement loop directly

## GIS tools
get_building_info(lat, lon) — OSM building type, floors, material
get_flood_zone(lat, lon) — flood hazard status
get_elevation_slope(lat, lon) — elevation and slope from DEM
get_nearest_shelter(lat, lon) — closest school or hospital

## Demo context
Based on 2023 Turkey-Syria earthquake.
Two regions: Hatay and Adiyaman.
Coordinates: Hatay approx 36.2°N 36.1°E,
Adiyaman approx 37.7°N 38.2°E

## Absolute rules — never do these
- Never add authentication or login
- Never use Postgres — SQLite only
- Never deploy to cloud — offline first
- Never use Redux — Zustand only
- Never use LangChain, LlamaIndex, CrewAI
- Never use fetch directly in React components
- Never use severity red for non-severity UI elements
- Never add features requiring internet
- Never use any TypeScript type
- Never use Pages Router
- Never write blocking synchronous code in async routes

## Code style
- Python: black formatting, type hints everywhere
- TypeScript: ESLint strict
- No console.log in production — use logging module
- No hardcoded URLs — use environment variables
- Descriptive function names always
- Comments in English only

