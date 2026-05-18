# BhumiDrishti — Offline AI Disaster Assessment Platform

> **Offline-first** field coordination and disaster damage assessment powered by local AI (**Gemma 4 e4b** via Ollama), PostGIS spatial analysis, and drone orthophoto processing — no internet required after setup.

---

## System Requirements

### Hardware

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| GPU | NVIDIA 6 GB VRAM | NVIDIA 8+ GB VRAM |
| RAM | 16 GB | 32 GB |
| Disk | 30 GB free | 60+ GB free |
| CPU | 4 cores | 8+ cores |

> **No GPU?** The system will run in CPU-only mode. AI inference will be very slow (minutes per assessment instead of seconds). A GPU with at least 6 GB VRAM is strongly recommended.

### Software

| Tool | Version | Notes |
|------|---------|-------|
| Docker Desktop / Engine | 24+ | With Compose v2 plugin |
| NVIDIA Driver | 525+ | For GPU acceleration |
| NVIDIA Container Toolkit | Latest | For GPU access inside Docker |
| Python 3 + pip | 3.8+ | For `gdown` — auto-installed by setup script |

---

## Quick Start — One Command

The setup script handles **everything automatically**:
- Checks Docker and GPU
- Creates `.env` config files
- Downloads ~10 GB data from Google Drive
- Verifies all required data files
- Builds OSRM routing files if needed
- Launches all Docker services

### Linux / macOS

```bash
git clone https://github.com/jeshan-coder/Bhumidrishti.git
cd Bhumidrishti
chmod +x setup.sh
./setup.sh
```

### Windows (PowerShell)

```powershell
git clone https://github.com/jeshan-coder/Bhumidrishti.git
cd Bhumidrishti
powershell -ExecutionPolicy Bypass -File setup.ps1
```

That's it. No other manual steps required.

---

## What the Setup Script Does

| Step | Action | Time |
|------|--------|------|
| 1 | Check Docker + Docker Compose | ~5 sec |
| 2 | Detect NVIDIA GPU + Container Toolkit | ~5 sec |
| 3 | Create `backend/.env` and `frontend/.env.local` from examples | ~2 sec |
| 4 | Download ~10 GB data zip from Google Drive | 10–30 min |
| 5 | Create required directories | ~2 sec |
| 6 | Verify all critical data files (COGs, shapefiles, PMTiles) | ~5 sec |
| 7 | Check OSRM routing files; rebuild from PBF if missing | 0 or ~40 min |
| 8 | `docker compose up --build -d` | 5–10 min |

After step 8 completes, Ollama downloads **gemma4:e4b** (~4 GB) in the background automatically.

---

## AI Model — Gemma 4 e4b

This project uses **`gemma4:e4b`** — the 4-billion-parameter efficient variant of Gemma 4.

| Model | VRAM | Download | Speed |
|-------|------|----------|-------|
| **gemma4:e4b** ✅ | ~4 GB | ~4 GB | Fast |

The model is pulled automatically by the `ollama-init` container on first startup.  
**AI chat and building assessments will not work until the model finishes downloading.**

To watch the download progress:
```bash
docker compose logs -f ollama-init
```

To use a larger model (better quality, requires more VRAM), set `GEMMA_MODEL` in `backend/.env`:
```env
GEMMA_MODEL=gemma4:12b   # 8 GB VRAM
GEMMA_MODEL=gemma4:26b   # 16 GB VRAM
```

---

## Accessing the System

Once all containers are up (allow 2–5 minutes after step 8):

| Service | URL | Description |
|---------|-----|-------------|
| **Frontend** | http://localhost:3000 | Map, dashboard, AI chat |
| **Backend API** | http://localhost:8000 | FastAPI REST endpoints |
| **API Docs** | http://localhost:8000/docs | Interactive Swagger UI |
| **TiTiler** | http://localhost:8001 | COG tile server (drone/satellite imagery) |
| **TileServer** | http://localhost:8090 | PMTiles base map |
| **OSRM** | http://localhost:5000 | Routing engine |

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
│  • Interactive map (MapLibre GL + PMTiles)                       │
│  • AI chat sidebar (SSE streaming)                               │
│  • Batch orthophoto assessment UI                                │
│  • Site & field team management                                  │
└────────┬────────────────────────────┬─────────────────────────────┘
         │ :8000                      │ :8001 / :8090
         ▼                            ▼
┌─────────────────┐   ┌─────────────────────┐  ┌──────────────────┐
│  Backend API    │   │  TiTiler  (:8001)   │  │  TileServer GL   │
│  (FastAPI)      │   │  COG tile serving   │  │  PMTiles base    │
│                 │   │  (drone ortho +     │  │  map (:8090)     │
│  /chat          │   │   satellite COGs)   │  └──────────────────┘
│  /assessments   │   └─────────────────────┘
│  /sites         │
│  /uploads       │   ┌─────────────────────────────────────────────┐
│  /reports       │──▶│           Ollama  (:11434)                  │
│  /field-teams   │   │  gemma4:e4b  (~4 GB, auto-downloaded)       │
│  /routing       │   │  • Building damage assessment (vision)      │
└────────┬────────┘   │  • AI chat with tool use                    │
         │            │  • Report generation                        │
         ▼            └─────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│               PostgreSQL + PostGIS  (:5432)                     │
│  • Buildings  • Sites  • Field teams  • Assessments             │
│  • Orthophoto upload metadata        • Batch progress           │
└──────────────────────────────────────────────────────────────────┘
         │
         ├── OSRM (:5000)          Turkey road network routing
         └── GIS Loader            Imports shapefiles → PostGIS (one-time)
```

### Batch Orthophoto Assessment — Data Flow

```
Upload drone COG (orthophoto)
         │
         ▼
find_covering_upload()
  Pass 1: check DB bounds columns
  Pass 2: read COG via rasterio, backfill DB bounds
         │
         ▼
For each building in the site:
  chip_extractor → crop building chip from COG
         │
         ▼
Gemma 4 e4b Vision → analyse pre + post image chips
         │
         ▼
Structured JSON assessment → stored in PostgreSQL
         │
         ▼
SSE events → live progress updates in frontend
```

---

## Configuration

### Backend (`backend/.env`)

> Auto-created from `backend/.env.example` by the setup script.

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `http://ollama:11434` | Ollama API endpoint |
| `DB_HOST` | `postgres` | PostgreSQL host |
| `DB_NAME` | `bhumidrishti` | Database name |
| `DB_USER` | `bhumidrishti` | Database user |
| `DB_PASSWORD` | `bhumidrishti` | Database password |
| `GEMMA_MODEL` | `gemma4:e4b` | AI model — change to pull a different variant |
| `MAX_VIDEO_SIZE_MB` | `500` | Max drone video upload size |
| `MAX_FRAMES_TO_ANALYZE` | `5` | Frames sampled per video |

### Frontend (`frontend/.env.local`)

> Auto-created from `frontend/.env.local.example` by the setup script.

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

# Rebuild and restart (after code changes)
docker compose up --build -d

# View all logs
docker compose logs -f

# Watch model download
docker compose logs -f ollama-init

# Watch GIS data import
docker compose logs -f gis-loader

# View backend logs
docker compose logs -f backend

# Restart a single service
docker compose restart backend

# Check which Ollama models are downloaded
docker compose exec ollama ollama list

# Open a shell in the backend container
docker compose exec backend bash
```

---

## Troubleshooting

### "AI chat not working" / model not responding

The model is still downloading. Check progress:
```bash
docker compose logs -f ollama-init
```
Wait for: `Model ready: gemma4:e4b`

### GIS loader fails to import shapefiles

```bash
docker compose logs gis-loader
```
Verify `data/turkey_data/*/buildings/buildings.shp` exists. Re-run setup if data is missing.

### Ollama model download interrupted

```bash
docker compose restart ollama-init
```
The download will resume from where it stopped.

### Backend cannot connect to database

```bash
docker compose ps          # check postgres is healthy
docker compose logs backend | grep -i "db\|postgres"
```
Wait 30 seconds after postgres starts before the backend connects.

### Port already in use

```bash
# Linux / macOS
lsof -i :3000

# Windows
netstat -ano | findstr :3000
```
Edit `docker-compose.yml` to change the host-side port number.

### OSRM routing returns no routes

Rebuild OSRM files:
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

```bash
# Try with no-cookies flag
gdown 1vDWLi18YpW0o8s54FrV7mNO3XjBBpJ_K --fuzzy --no-cookies
```

---

## Stopping & Restarting

```bash
# Stop all services (data is preserved)
docker compose down

# Stop and wipe the database (DESTRUCTIVE)
docker compose down -v

# Restart after stopping
docker compose up -d
```

---

## Repository Structure

```
BhumiDrishti/
├── frontend/               # Next.js web app (map, chat, dashboard)
├── backend/                # FastAPI API + AI pipeline
│   ├── .env.example        # Backend config template
│   ├── routers/            # API route handlers
│   ├── services/           # AI pipeline, tools, orthophoto processing
│   └── models/             # Pydantic data models
├── data/
│   ├── turkey_data/        # Satellite COGs, DEMs, building shapefiles
│   ├── osrm/               # OSRM routing files (Turkey road network)
│   └── tiles_data/         # PMTiles base map + TileServer config
├── docker/                 # Docker init scripts
├── uploads/                # User-uploaded drone orthophotos
├── docker-compose.yml      # Full service stack
├── setup.sh                # Linux/macOS one-command setup
└── setup.ps1               # Windows one-command setup
```

---

## Data Credits

- **Building footprints**: OpenStreetMap contributors (ODbL)
- **Road network**: Geofabrik Turkey extract (OpenStreetMap)
- **DEM**: SRTM / Copernicus DEM

---

*BhumiDrishti — Sanskrit for "Earth Vision" — built for offline field coordination in disaster response scenarios.*
