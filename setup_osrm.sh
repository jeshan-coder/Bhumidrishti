#!/bin/bash

# This script prepares OSRM Turkey routing data once and skips completed steps on reruns.
set -euo pipefail

# This variable stores the repository root directory for stable relative paths.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# This variable stores where OSRM data files are persisted.
OSRM_DATA_DIR="$REPO_ROOT/data/osrm"

# This variable stores the Turkey OSM PBF file path.
PBF_FILE="$OSRM_DATA_DIR/turkey-latest.osm.pbf"

# This variable stores the base OSRM dataset path (without extension).
OSRM_BASE_FILE="$OSRM_DATA_DIR/turkey-latest.osrm"

mkdir -p "$OSRM_DATA_DIR"

# This function checks if all MLD customize outputs are already present.
is_osrm_prepared() {
  [[ -f "$OSRM_BASE_FILE" ]] && \
  [[ -f "$OSRM_BASE_FILE.partition" ]] && \
  [[ -f "$OSRM_BASE_FILE.cells" ]] && \
  [[ -f "$OSRM_BASE_FILE.mldgr" ]]
}

# This block downloads Turkey OSM data if not already present.
if [[ -f "$PBF_FILE" ]]; then
  echo "Turkey OSM data already exists, skipping download: $PBF_FILE"
else
  echo "Downloading Turkey OSM data..."
  curl -L https://download.geofabrik.de/europe/turkey-latest.osm.pbf -o "$PBF_FILE"
fi

# This block runs extract only if base OSRM file does not exist.
if [[ -f "$OSRM_BASE_FILE" ]]; then
  echo "Extract output already exists, skipping extraction."
else
  echo "Extracting..."
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*" docker run -t -v "$OSRM_DATA_DIR:/data" \
    osrm/osrm-backend \
    osrm-extract -p /opt/car.lua /data/turkey-latest.osm.pbf
fi

# This block runs partition only if partition outputs do not already exist.
if [[ -f "$OSRM_BASE_FILE.partition" && -f "$OSRM_BASE_FILE.cells" ]]; then
  echo "Partition outputs already exist, skipping partitioning."
else
  echo "Partitioning..."
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*" docker run -t -v "$OSRM_DATA_DIR:/data" \
    osrm/osrm-backend \
    osrm-partition /data/turkey-latest.osrm
fi

# This block runs customize only if customize output does not already exist.
if [[ -f "$OSRM_BASE_FILE.mldgr" ]]; then
  echo "Customize output already exists, skipping customizing."
else
  echo "Customizing..."
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*" docker run -t -v "$OSRM_DATA_DIR:/data" \
    osrm/osrm-backend \
    osrm-customize /data/turkey-latest.osrm
fi

# This block reports whether the setup is complete.
if is_osrm_prepared; then
  echo "OSRM setup complete"
else
  echo "OSRM setup incomplete: expected files are missing."
  exit 1
fi
