#!/bin/bash
# BhumiDrishti - Backend Only Runner
# This script starts the FastAPI backend and its required Ollama dependency via Docker.
# The Next.js frontend container will be skipped.

set -euo pipefail

# This variable stores the repository root directory for stable relative paths.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# This variable stores the expected OSRM base file path.
OSRM_BASE_FILE="$REPO_ROOT/data/osrm/turkey-latest.osrm"

# This function checks whether OSRM MLD dataset files are ready.
is_osrm_ready() {
  [[ -f "$OSRM_BASE_FILE" ]] && \
  [[ -f "$OSRM_BASE_FILE.partition" ]] && \
  [[ -f "$OSRM_BASE_FILE.cells" ]] && \
  [[ -f "$OSRM_BASE_FILE.mldgr" ]]
}

echo "🟢 Step 0: Checking OSRM data..."
if is_osrm_ready; then
  echo "OSRM data already prepared. Skipping setup_osrm.sh"
else
  echo "OSRM data missing. Running one-time setup_osrm.sh"
  bash "$REPO_ROOT/setup_osrm.sh"
fi

echo "🟢 Step 1: Starting Ollama and checking only missing models..."
# ollama-init now pulls only missing models and skips already-downloaded ones.
docker compose -f "$REPO_ROOT/docker-compose.yml" up ollama-init

echo "🟢 Step 2: Model check done. Starting backend in detached mode..."
# Now we start the backend detached (-d) so you get your terminal back.
docker compose -f "$REPO_ROOT/docker-compose.yml" up -d --build backend

echo "✅ Ready! The API is available at http://localhost:8000"
