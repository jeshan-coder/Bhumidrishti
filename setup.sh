#!/usr/bin/env bash
# =============================================================================
#  BhumiDrishti — One-Command Setup Script (Linux / macOS)
#  Offline AI Disaster Assessment Platform
#
#  Usage:  chmod +x setup.sh && ./setup.sh
#
#  What this script does (fully automated, no manual steps):
#    1. Check Docker + Docker Compose
#    2. Detect NVIDIA GPU / Container Toolkit
#    3. Create .env files from examples
#    4. Download ~10 GB data from Google Drive
#    5. Create required directories
#    6. Verify critical data files
#    7. Check / build OSRM routing files
#    8. Launch all services with docker compose up --build -d
# =============================================================================
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

GDRIVE_FILE_ID="1vDWLi18YpW0o8s54FrV7mNO3XjBBpJ_K"
DATA_ZIP="bhumidrishti_data.zip"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Banner ────────────────────────────────────────────────────────────────────
echo -e "${BLUE}${BOLD}"
echo "  ██████╗ ██╗  ██╗██╗   ██╗███╗   ███╗██╗██████╗ ██████╗ ██╗███████╗██╗  ██╗████████╗██╗"
echo "  ██╔══██╗██║  ██║██║   ██║████╗ ████║██║██╔══██╗██╔══██╗██║██╔════╝██║  ██║╚══██╔══╝██║"
echo "  ██████╔╝███████║██║   ██║██╔████╔██║██║██║  ██║██████╔╝██║███████╗███████║   ██║   ██║"
echo "  ██╔══██╗██╔══██║██║   ██║██║╚██╔╝██║██║██║  ██║██╔══██╗██║╚════██║██╔══██║   ██║   ██║"
echo "  ██████╔╝██║  ██║╚██████╔╝██║ ╚═╝ ██║██║██████╔╝██║  ██║██║███████║██║  ██║   ██║   ██║"
echo "  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═╝╚═╝╚═════╝ ╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝   ╚═╝"
echo -e "${NC}"
echo -e "${CYAN}  Offline AI Field Coordination & Disaster Damage Assessment${NC}"
echo -e "${CYAN}  One-Command Setup — Linux / macOS${NC}"
echo -e "${YELLOW}  AI Model: gemma4:e4b  (~4 GB download on first run)${NC}"
echo ""

step() { echo -e "\n${YELLOW}${BOLD}[$1/$TOTAL_STEPS] $2${NC}"; }
ok()   { echo -e "${GREEN}  ✓ $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $1${NC}"; }
fail() { echo -e "${RED}  ✗ $1${NC}"; }

TOTAL_STEPS=8

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Docker
# ─────────────────────────────────────────────────────────────────────────────
step 1 "Checking Docker..."

if ! command -v docker &>/dev/null; then
  fail "Docker not found."
  echo "  Install Docker Engine: https://docs.docker.com/engine/install/"
  echo "  Or Docker Desktop:     https://www.docker.com/products/docker-desktop/"
  exit 1
fi

if ! docker info &>/dev/null 2>&1; then
  fail "Docker daemon is not running. Start Docker and retry."
  exit 1
fi

DOCKER_VERSION=$(docker --version | grep -oP '\d+\.\d+\.\d+' | head -1)
ok "Docker ${DOCKER_VERSION}"

if docker compose version &>/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
  ok "Docker Compose v2 (plugin)"
elif command -v docker-compose &>/dev/null; then
  COMPOSE_CMD="docker-compose"
  ok "Docker Compose v1 (standalone)"
else
  fail "Docker Compose not found. Install: https://docs.docker.com/compose/install/"
  exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — NVIDIA GPU / Container Toolkit
# ─────────────────────────────────────────────────────────────────────────────
step 2 "Checking NVIDIA GPU support..."

GPU_AVAILABLE=false
if command -v nvidia-smi &>/dev/null; then
  DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || echo "unknown")
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "unknown")
  ok "GPU detected: ${GPU_NAME} (driver ${DRIVER_VER})"

  if docker info 2>/dev/null | grep -q "nvidia"; then
    ok "NVIDIA Container Toolkit is active"
    GPU_AVAILABLE=true
  else
    warn "nvidia-smi found but Docker cannot see the GPU."
    warn "Install NVIDIA Container Toolkit:"
    warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
    warn "Continuing in CPU-only mode (AI inference will be very slow)."
  fi
else
  warn "No NVIDIA GPU detected. AI inference will run on CPU (very slow)."
  warn "Recommended: NVIDIA GPU with 6+ GB VRAM for gemma4:e4b."
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Create .env files
# ─────────────────────────────────────────────────────────────────────────────
step 3 "Creating environment files..."

if [ ! -f "${REPO_ROOT}/backend/.env" ]; then
  cp "${REPO_ROOT}/backend/.env.example" "${REPO_ROOT}/backend/.env"
  ok "Created backend/.env from example"
else
  ok "backend/.env already exists — skipping"
fi

if [ ! -f "${REPO_ROOT}/frontend/.env.local" ]; then
  cp "${REPO_ROOT}/frontend/.env.local.example" "${REPO_ROOT}/frontend/.env.local"
  ok "Created frontend/.env.local from example"
else
  ok "frontend/.env.local already exists — skipping"
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Download data from Google Drive
# ─────────────────────────────────────────────────────────────────────────────
step 4 "Downloading data (~10 GB from Google Drive)..."

DATA_ALREADY_PRESENT=false
if [ -d "${REPO_ROOT}/data/turkey_data/Adiyaman" ] && \
   [ -d "${REPO_ROOT}/data/turkey_data/Hatay" ] && \
   [ -d "${REPO_ROOT}/data/osrm" ] && \
   [ -d "${REPO_ROOT}/data/tiles_data" ]; then
  ok "Data directory already populated — skipping download."
  DATA_ALREADY_PRESENT=true
fi

if [ "$DATA_ALREADY_PRESENT" = false ]; then
  if ! command -v gdown &>/dev/null; then
    echo "  Installing gdown (Google Drive downloader)..."
    if command -v pip3 &>/dev/null; then
      pip3 install --quiet --user gdown
    elif command -v pip &>/dev/null; then
      pip install --quiet --user gdown
    else
      fail "pip / pip3 not found. Install Python 3 first: https://www.python.org/downloads/"
      exit 1
    fi
    export PATH="$HOME/.local/bin:$PATH"
  fi
  ok "gdown ready"

  echo "  Downloading data zip (this may take 10–30 min depending on connection speed)..."
  gdown "${GDRIVE_FILE_ID}" -O "${REPO_ROOT}/${DATA_ZIP}" --fuzzy

  echo "  Extracting archive..."
  cd "${REPO_ROOT}"

  FIRST_ENTRY=$(unzip -Z1 "${DATA_ZIP}" | head -1)
  if [[ "$FIRST_ENTRY" == data/* ]]; then
    unzip -q "${DATA_ZIP}" -d "${REPO_ROOT}"
  else
    mkdir -p "${REPO_ROOT}/data"
    unzip -q "${DATA_ZIP}" -d "${REPO_ROOT}/data"
  fi

  rm -f "${DATA_ZIP}"
  ok "Data extracted successfully."
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Create required directories
# ─────────────────────────────────────────────────────────────────────────────
step 5 "Creating required directories..."

mkdir -p "${REPO_ROOT}/uploads"
mkdir -p "${REPO_ROOT}/data/osrm"
mkdir -p "${REPO_ROOT}/data/tiles_data"
mkdir -p "${REPO_ROOT}/docker/postgres/init"

POSTGIS_INIT="${REPO_ROOT}/docker/postgres/init/01-postgis.sql"
if [ ! -f "$POSTGIS_INIT" ]; then
  echo "CREATE EXTENSION IF NOT EXISTS postgis;" > "$POSTGIS_INIT"
fi

ok "Directories ready."

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Verify critical data files
# ─────────────────────────────────────────────────────────────────────────────
step 6 "Verifying critical data files..."

MISSING=0
check_file() {
  local path="$1"
  local label="$2"
  if [ ! -f "$path" ]; then
    fail "Missing: $label"
    MISSING=$((MISSING + 1))
  else
    ok "Found: $label"
  fi
}

check_file "${REPO_ROOT}/data/turkey_data/Adiyaman/pre_earthquake_adiyaman_cog.tif"  "Adiyaman pre-earthquake COG"
check_file "${REPO_ROOT}/data/turkey_data/Adiyaman/post_earthquake_adiyaman_cog.tif" "Adiyaman post-earthquake COG"
check_file "${REPO_ROOT}/data/turkey_data/Hatay/pre_earthquake_hatay_cog.tif"        "Hatay pre-earthquake COG"
check_file "${REPO_ROOT}/data/turkey_data/Adiyaman/dem_data/Adiyaman_dem_cog.tif"    "Adiyaman DEM"
check_file "${REPO_ROOT}/data/turkey_data/Hatay/dem_data/Hatay_dem_cog.tif"          "Hatay DEM"
check_file "${REPO_ROOT}/data/turkey_data/Adiyaman/buildings/buildings.shp"          "Adiyaman buildings shapefile"
check_file "${REPO_ROOT}/data/turkey_data/Hatay/buildings/buildings.shp"             "Hatay buildings shapefile"
check_file "${REPO_ROOT}/data/tiles_data/turkey.pmtiles"                             "Turkey PMTiles (base map)"
check_file "${REPO_ROOT}/data/tiles_data/config.json"                                "TileServer config"

if [ $MISSING -gt 0 ]; then
  fail "${MISSING} critical file(s) missing."
  echo ""
  echo "  Check that your data zip from Google Drive contains the full data/ folder."
  echo "  Re-download and re-run this script."
  exit 1
fi

ok "All critical data files verified."

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — OSRM routing files
# ─────────────────────────────────────────────────────────────────────────────
step 7 "Checking OSRM routing files..."

OSRM_FILES_OK=true
for f in turkey-latest.osrm turkey-latest.osrm.partition turkey-latest.osrm.cells turkey-latest.osrm.mldgr; do
  if [ ! -f "${REPO_ROOT}/data/osrm/$f" ]; then
    OSRM_FILES_OK=false
    warn "Missing OSRM file: $f"
  fi
done

if [ "$OSRM_FILES_OK" = true ]; then
  ok "OSRM routing files present."
else
  warn "Pre-processed OSRM files not found. Rebuilding from PBF (~40 min, requires internet)..."

  PBF="${REPO_ROOT}/data/osrm/turkey-latest.osm.pbf"
  if [ ! -f "$PBF" ]; then
    echo "  Downloading Turkey OSM PBF (~600 MB) from Geofabrik..."
    curl -L --progress-bar \
      "https://download.geofabrik.de/europe/turkey-latest.osm.pbf" \
      -o "$PBF"
    ok "PBF downloaded."
  else
    ok "PBF already present — skipping download."
  fi

  OSRM_VOL="${REPO_ROOT}/data/osrm:/data"

  echo "  Step A: osrm-extract (10–15 min)..."
  docker run --rm -v "${OSRM_VOL}" osrm/osrm-backend \
    osrm-extract -p /opt/car.lua /data/turkey-latest.osm.pbf

  echo "  Step B: osrm-partition (10–15 min)..."
  docker run --rm -v "${OSRM_VOL}" osrm/osrm-backend \
    osrm-partition /data/turkey-latest.osrm

  echo "  Step C: osrm-customize (5–10 min)..."
  docker run --rm -v "${OSRM_VOL}" osrm/osrm-backend \
    osrm-customize /data/turkey-latest.osrm

  ok "OSRM routing files built successfully."
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 8 — Launch
# ─────────────────────────────────────────────────────────────────────────────
step 8 "Starting BhumiDrishti..."

cd "${REPO_ROOT}"
$COMPOSE_CMD up --build -d

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║              Setup Complete!  🎉                     ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${CYAN}  Service URLs:${NC}"
echo -e "    Frontend (Map + Dashboard):  ${BLUE}http://localhost:3000${NC}"
echo -e "    Backend API:                 ${BLUE}http://localhost:8000${NC}"
echo -e "    API Docs (Swagger):          ${BLUE}http://localhost:8000/docs${NC}"
echo ""
echo -e "${YELLOW}  First-start notes:${NC}"
echo -e "    • Ollama will download ${BOLD}gemma4:e4b${NC}${YELLOW} (~4 GB) in the background."
echo -e "    • AI chat won't work until the model download is complete."
echo -e "    • Watch model download:  ${CYAN}docker compose logs -f ollama-init${NC}"
echo -e "    • Watch all services:    ${CYAN}docker compose logs -f${NC}"
echo ""
echo -e "${YELLOW}  To stop:    ${CYAN}docker compose down${NC}"
echo -e "${YELLOW}  To restart: ${CYAN}docker compose up -d${NC}"
echo ""
