# BhumiDrishti

Offline-first disaster damage assessment platform for the Gemma 4 Good Hackathon.

## Stack

- Frontend: Next.js 14 (App Router), TypeScript, Tailwind, shadcn/ui
- Backend: FastAPI (Python 3.11), asyncpg
- GIS/DB: PostGIS + local Turkey layers
- AI: Ollama with `gemma4:26b`
- Routing: OSRM (`osrm-routed` on port `5000`)

## Repository Structure

- `frontend` — web app (assessment, map, dashboard, chat)
- `backend` — API, agent loop, GIS tools, upload processing
- `data` — GIS files, DEM, OSRM routing files
- `uploads` — uploaded user files
- `docker-compose.yml` — local multi-service stack
- `run-backend.sh` — backend startup helper
- `setup_osrm.sh` — one-time OSRM data preparation

## Prerequisites

- Docker Desktop (or Docker Engine + Compose)
- Bash shell
- Internet only for initial model/data download

## One-Time Setup

### 1) Prepare backend environment file

- Copy `backend/.env.example` to `backend/.env` if needed.
- Update values for your machine.

### 2) Prepare OSRM data (one time)

```bash
bash ./setup_osrm.sh
```

What it does:
- downloads Turkey OSM PBF into `data/osrm`
- runs `osrm-extract`, `osrm-partition`, `osrm-customize`
- keeps generated files in `data/osrm`

The script is idempotent:
- if outputs already exist, steps are skipped.

## Run Backend Only

```bash
bash ./run-backend.sh
```

`run-backend.sh` now automatically:
1. checks OSRM prepared files in `data/osrm`
2. runs `setup_osrm.sh` only when missing
3. starts `ollama-init` to ensure required models exist
4. starts backend service via Docker Compose

Backend API:
- `http://localhost:8000`

## Run Full Stack

```bash
docker compose up -d --build
```

Services:
- Frontend: `http://localhost:3000`
- Backend: `http://localhost:8000`
- PostGIS: `localhost:5432`
- Ollama: `http://localhost:11434`
- TiTiler: `http://localhost:8001`
- OSRM: `http://localhost:5000`

## Key Commands

- Rebuild backend only:
  ```bash
  docker compose up -d --build backend
  ```

- View backend logs:
  ```bash
  docker compose logs -f backend
  ```

- Stop all services:
  ```bash
  docker compose down
  ```

## Notes

- OSRM setup can take time on first run.
- Keep `data/osrm` persisted; deleting it will require re-setup.
- The project is designed to work locally/offline after initial setup.
