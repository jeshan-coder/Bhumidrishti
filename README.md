# BhumiDrishti — Offline AI Disaster Assessment Platform

> **Offline-first** field coordination and disaster damage assessment powered by local AI (Gemma 4 via Ollama), PostGIS spatial analysis, and drone orthophoto processing — no internet required after setup.

---

## System Requirements

### Hardware

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| GPU | NVIDIA 8 GB VRAM | NVIDIA 16+ GB VRAM |
| RAM | 16 GB | 32 GB |
| Disk | 30 GB free | 60+ GB free |
| CPU | 4 cores | 8+ cores |

> **No GPU?** The system will run in CPU-only mode. AI inference will be very slow (minutes per assessment instead of seconds).

### Software

| Tool | Version | Notes |
|------|---------|-------|
| Docker Desktop / Engine | 24+ | With Compose v2 plugin |
| NVIDIA Driver | 525+ | For GPU acceleration |
| NVIDIA Container Toolkit | Latest | For GPU access inside Docker |
| Python 3 + pip | 3.8+ | For `gdown` (auto-installed by setup script) |

---

## Quick Start

### Linux / macOS

```bash
git clone <your-repo-url> BhumiDrishti
cd BhumiDrishti
chmod +x setup.sh
./setup.sh
```

### Windows (PowerShell)

```powershell
git clone <your-repo-url> BhumiDrishti
cd BhumiDrishti
powershell -ExecutionPolicy Bypass -File setup.ps1
```

### What the setup script does

| Step | Action |
|------|--------|
| 1 | Verify Docker + Docker Compose are installed and running |
| 2 | Detect NVIDIA GPU and Container Toolkit |
| 3 | Download ~10 GB data zip from Google Drive (skipped if already present) |
| 4 | Create required directories (`uploads/`, `data/osrm/`, etc.) |
| 5 | Verify all critical data files (COGs, shapefiles, PMTiles) |
| 6 | Check OSRM routing files; rebuild from PBF if missing (~40 min) |
| 7 | Run `docker compose up --build -d` |

---

## Manual Setup (without the script)

If you prefer to set up manually or the script fails at a specific step:

### 1. Clone and prepare directories

```bash
git clone <your-repo-url> BhumiDrishti
cd BhumiDrishti
mkdir -p uploads data/osrm data/tiles_data docker/postgres/init
echo "CREATE EXTENSION IF NOT EXISTS postgis;" > docker/postgres/init/01-postgis.sql
```

### 2. Download data

Install `gdown` and download the data archive:

```bash
pip install gdown
gdown 1vDWLi18YpW0o8s54FrV7mNO3XjBBpJ_K -O bhumidrishti_data.zip --fuzzy
unzip bhumidrishti_data.zip -d .
rm bhumidrishti_data.zip
```

### 3. Configure environment

```bash
# Backend
cp backend/.env.example backend/.env

# Frontend
cp frontend/.env.local.example frontend/.env.local
```

Edit `backend/.env` if needed (defaults work with docker-compose out of the box).

### 4. Build OSRM routing files (if not included in the data zip)

```bash
cd data/osrm

# Download Turkey OSM data (~600 MB)
curl -L https://download.geofabrik.de/europe/turkey-latest.osm.pbf -o turkey-latest.osm.pbf

# Process (runs inside Docker — no local OSRM install needed)
docker run --rm -v "$(pwd):/data" osrm/osrm-backend osrm-extract -p /opt/car.lua /data/turkey-latest.osm.pbf
docker run --rm -v "$(pwd):/data" osrm/osrm-backend osrm-partition /data/turkey-latest.osrm
docker run --rm -v "$(pwd):/data" osrm/osrm-backend osrm-customize /data/turkey-latest.osrm

cd ../..
```

### 5. Launch

```bash
docker compose up --build -d
```

---

## Accessing the System

Once all containers are running (allow 2–5 minutes for first startup):

| Service | URL | Description |
|---------|-----|-------------|
| **Frontend** | http://localhost:3000 | Map, dashboard, AI chat |
| **Backend API** | http://localhost:8000 | FastAPI REST endpoints |
| **API Docs** | http://localhost:8000/docs | Interactive Swagger UI |
| **TiTiler** | http://localhost:8080 | COG tile server |
| **TileServer** | http://localhost:8081 | PMTiles base map |
| **OSRM** | http://localhost:5000 | Routing engine |
| **Ollama** | http://localhost:11434 | AI inference (internal) |

---

## First Startup Notes

- **Ollama will download Gemma 4 models automatically** (~4–20 GB depending on the model variant). This runs in the background after the containers start.
- **AI features won't work** until the model download is complete.
- Monitor model download progress:
  ```bash
  docker compose logs -f ollama-init
  ```
- The GIS loader will import building shapefiles into PostGIS on first start. Monitor with:
  ```bash
  docker compose logs -f gis-loader
  ```

---

## Service Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        User / Browser                           │
└──────────────────────────┬──────────────────────────────────────┘
                           │ :3000
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Frontend (Next.js)                            │
│  • Interactive map (MapLibre GL)                                 │
│  • AI chat sidebar (SSE streaming)                               │
│  • Batch orthophoto assessment UI                                │
│  • Site & field team management                                  │
└────────┬─────────────────────────┬───────────────────────────────┘
         │ :8000                   │ :8080/:8081
         ▼                         ▼
┌─────────────────┐   ┌────────────────────┐   ┌──────────────────┐
│  Backend API    │   │  TiTiler (COG)     │   │  TileServer GL   │
│  (FastAPI)      │   │  Drone orthophoto  │   │  (PMTiles base   │
│                 │   │  tile serving      │   │   map tiles)     │
│  • /chat        │   └────────────────────┘   └──────────────────┘
│  • /assessments │
│  • /sites       │   ┌────────────────────────────────────────────┐
│  • /uploads     │──▶│           Ollama (AI Engine)               │
│  • /reports     │   │  Gemma 4 (e4b / 12b / 26b / 31b)          │
│  • /field-teams │   │  • Building damage assessment              │
│  • /routing     │   │  • AI chat with tool use                   │
└────────┬────────┘   │  • Report generation                       │
         │            └────────────────────────────────────────────┘
         ▼
┌─────────────────────────────────────────────────────────────────┐
│               PostgreSQL + PostGIS                              │
│  • Buildings (OpenStreetMap + damage assessments)               │
│  • Sites, field teams, dispatch records                         │
│  • Orthophoto upload metadata + bounds                          │
│  • Batch processing progress                                    │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────┐   ┌──────────────────────────────────────────┐
│  OSRM           │   │  GIS Loader (one-shot init container)    │
│  (Routing)      │   │  Imports shapefiles → PostGIS on startup │
│  Turkey road    │   └──────────────────────────────────────────┘
│  network :5000  │
└─────────────────┘
```

### Data flow — Batch Orthophoto Assessment

```
Upload COG (drone orthophoto)
         │
         ▼
find_covering_upload()
  Pass 1: check DB bounds columns
  Pass 2: read COG from disk (rasterio), backfill DB
         │
         ▼
For each building in site:
  chip_extractor.py → extract building chip from COG
         │
         ▼
Gemma 4 Vision → analyse chip (pre + post images)
         │
         ▼
JSON assessment → stored in PostgreSQL
         │
         ▼
SSE events → live progress in frontend
```

---

## Configuration

### Backend (`backend/.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `http://ollama:11434` | Ollama API endpoint |
| `DB_HOST` | `postgres` | PostgreSQL host |
| `DB_NAME` | `bhumidrishti` | Database name |
| `DB_USER` | `bhumidrishti` | Database user |
| `DB_PASSWORD` | `bhumidrishti` | Database password |
| `GEMMA_MODEL` | `gemma4:e4b` | AI model variant |
| `MAX_VIDEO_SIZE_MB` | `500` | Max drone video size |
| `MAX_FRAMES_TO_ANALYZE` | `5` | Frames sampled per video |

#### Model variants

| Value | Size | VRAM | Speed |
|-------|------|------|-------|
| `gemma4:e4b` | 4B | ~3–4 GB | Fastest (recommended for demos) |
| `gemma4:12b` | 12B | ~8 GB | Balanced |
| `gemma4:26b` | 26B | ~16 GB | High quality |
| `gemma4:31b` | 31B | ~20 GB | Highest quality |

### Frontend (`frontend/.env.local`)

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXT_PUBLIC_BACKEND_URL` | `http://localhost:8000` | Backend API base URL |

---

## Common Commands

```bash
# Start all services
docker compose up -d

# Stop all services
docker compose down

# Rebuild and restart
docker compose up --build -d

# View all logs
docker compose logs -f

# View specific service logs
docker compose logs -f backend
docker compose logs -f ollama-init
docker compose logs -f gis-loader

# Restart a single service
docker compose restart backend

# Open a shell in the backend container
docker compose exec backend bash

# Check database tables
docker compose exec postgres psql -U bhumidrishti -d bhumidrishti -c "\dt"

# Check Ollama models
docker compose exec ollama ollama list
```

---

## Troubleshooting

### GIS loader fails to import shapefiles

```bash
docker compose logs gis-loader
```

Common causes:
- Shapefile not found — verify `data/turkey_data/*/buildings/buildings.shp` exists
- PostGIS extension not installed — check `docker/postgres/init/01-postgis.sql` exists

### Ollama model download stuck or failed

```bash
docker compose logs -f ollama-init
docker compose restart ollama-init
```

If the model is partially downloaded, it will resume from where it left off.

### Backend cannot connect to database

```bash
docker compose logs backend | grep "DB\|postgres\|asyncpg"
```

- Ensure the `postgres` container is healthy: `docker compose ps`
- Wait 30 seconds after `postgres` starts before `backend` connects

### Port already in use

If a port (3000, 8000, 8080, etc.) is already in use:

```bash
# Linux / macOS
lsof -i :3000

# Windows
netstat -ano | findstr :3000
```

Edit `docker-compose.yml` to change the host port mapping.

### OSRM routing returns no routes

The OSRM pre-processed files may be missing or corrupt. Rebuild:

```bash
cd data/osrm
docker run --rm -v "$(pwd):/data" osrm/osrm-backend osrm-extract -p /opt/car.lua /data/turkey-latest.osm.pbf
docker run --rm -v "$(pwd):/data" osrm/osrm-backend osrm-partition /data/turkey-latest.osrm
docker run --rm -v "$(pwd):/data" osrm/osrm-backend osrm-customize /data/turkey-latest.osrm
```

### Windows: "Access denied" when extracting zip

Run PowerShell as Administrator, or use 7-Zip:

```powershell
& "C:\Program Files\7-Zip\7z.exe" x bhumidrishti_data.zip -o"C:\Bhumidrishti" -y
```

### gdown fails with quota error

The Google Drive file may have hit a download quota. Try:

```bash
# Add --no-cookies flag
gdown 1vDWLi18YpW0o8s54FrV7mNO3XjBBpJ_K --fuzzy --no-cookies

# Or use wget with the direct URL
wget "https://drive.google.com/uc?export=download&id=1vDWLi18YpW0o8s54FrV7mNO3XjBBpJ_K" -O bhumidrishti_data.zip
```

### AI chat returns no response or times out

- Check Ollama is running: `docker compose ps ollama`
- Check the model is loaded: `docker compose exec ollama ollama list`
- The `gemma4:e4b` model is recommended for faster responses on limited hardware

---

## Stopping & Restarting

```bash
# Stop (preserves all data)
docker compose down

# Stop and remove all data volumes (DESTRUCTIVE — deletes database)
docker compose down -v

# Restart after stopping
docker compose up -d
```

---

## Repository Structure

```
BhumiDrishti/
├── frontend/          # Next.js web app
├── backend/           # FastAPI API + AI pipeline
├── data/
│   ├── turkey_data/   # Satellite COGs, DEMs, shapefiles
│   ├── osrm/          # OSRM routing files
│   └── tiles_data/    # PMTiles base map + config
├── docker/            # Docker init scripts
├── uploads/           # User-uploaded orthophotos
├── docker-compose.yml
├── setup.sh           # Linux/macOS one-command setup
└── setup.ps1          # Windows one-command setup
```

---

## Data Credits

- **Building footprints**: OpenStreetMap contributors (ODbL)
- **Road network**: Geofabrik Turkey extract (OpenStreetMap)
- **DEM**: SRTM / Copernicus DEM

---

*BhumiDrishti — Sanskrit for "Earth Vision" — built for offline field coordination in disaster response scenarios.*
