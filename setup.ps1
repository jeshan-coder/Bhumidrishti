# =============================================================================
#  BhumiDrishti — One-Command Setup Script (Windows PowerShell)
#  Offline AI Disaster Assessment Platform
#  Run as:  powershell -ExecutionPolicy Bypass -File setup.ps1
# =============================================================================

$ErrorActionPreference = "Stop"

$GDRIVE_FILE_ID = "1vDWLi18YpW0o8s54FrV7mNO3XjBBpJ_K"
$DATA_ZIP       = "bhumidrishti_data.zip"
$REPO_ROOT      = Split-Path -Parent $MyInvocation.MyCommand.Path
$TOTAL_STEPS    = 7

function Write-Step($n, $msg) { Write-Host "`n[$n/$TOTAL_STEPS] $msg" -ForegroundColor Yellow }
function Write-Ok($msg)   { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  ✗ $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "  ██████╗ ██╗  ██╗██╗   ██╗███╗   ███╗██╗██████╗ ██████╗ ██╗███████╗██╗  ██╗████████╗██╗" -ForegroundColor Blue
Write-Host "  ██╔══██╗██║  ██║██║   ██║████╗ ████║██║██╔══██╗██╔══██╗██║██╔════╝██║  ██║╚══██╔══╝██║" -ForegroundColor Blue
Write-Host "  Offline AI Disaster Assessment Platform — Windows Setup" -ForegroundColor Cyan
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Docker
# ─────────────────────────────────────────────────────────────────────────────
Write-Step 1 "Checking Docker..."

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Fail "Docker not found."
    Write-Host "  Install Docker Desktop for Windows: https://www.docker.com/products/docker-desktop/"
    Write-Host "  Enable WSL2 backend during installation."
    exit 1
}

try {
    docker info | Out-Null
} catch {
    Write-Fail "Docker daemon is not running. Start Docker Desktop and retry."
    exit 1
}

$dockerVer = (docker --version) -replace ".*version ([0-9.]+).*",'$1'
Write-Ok "Docker $dockerVer"

# Prefer compose v2 plugin
$ComposeCmd = $null
if (docker compose version 2>$null) {
    $ComposeCmd = "docker compose"
    Write-Ok "Docker Compose v2 (plugin)"
} elseif (Get-Command docker-compose -ErrorAction SilentlyContinue) {
    $ComposeCmd = "docker-compose"
    Write-Ok "Docker Compose v1 (standalone)"
} else {
    Write-Fail "Docker Compose not found."
    exit 1
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — NVIDIA GPU
# ─────────────────────────────────────────────────────────────────────────────
Write-Step 2 "Checking NVIDIA GPU support..."

$GpuAvailable = $false
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpuName = (nvidia-smi --query-gpu=name --format=csv,noheader 2>$null)
    Write-Ok "GPU detected: $gpuName"

    $dockerInfo = docker info 2>$null | Out-String
    if ($dockerInfo -match "nvidia") {
        Write-Ok "NVIDIA Container Toolkit active"
        $GpuAvailable = $true
    } else {
        Write-Warn "GPU found but Docker cannot access it."
        Write-Warn "Install NVIDIA Container Toolkit for WSL2:"
        Write-Warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
        Write-Warn "Continuing in CPU-only mode."
    }
} else {
    Write-Warn "No NVIDIA GPU detected. AI will run on CPU (very slow)."
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Download data from Google Drive
# ─────────────────────────────────────────────────────────────────────────────
Write-Step 3 "Downloading data (~10 GB from Google Drive)..."

$DataPopulated = (Test-Path "$REPO_ROOT\data\turkey_data\Adiyaman") -and
                 (Test-Path "$REPO_ROOT\data\turkey_data\Hatay") -and
                 (Test-Path "$REPO_ROOT\data\osrm") -and
                 (Test-Path "$REPO_ROOT\data\tiles_data")

if ($DataPopulated) {
    Write-Ok "Data directory already populated — skipping download."
} else {
    # Check gdown
    if (-not (Get-Command gdown -ErrorAction SilentlyContinue)) {
        Write-Host "  Installing gdown..."
        if (Get-Command pip3 -ErrorAction SilentlyContinue) {
            pip3 install --quiet gdown
        } elseif (Get-Command pip -ErrorAction SilentlyContinue) {
            pip install --quiet gdown
        } else {
            Write-Fail "pip not found. Install Python 3 from https://www.python.org/downloads/"
            exit 1
        }
    }
    Write-Ok "gdown ready"

    Write-Host "  Downloading data zip (10–30 min)..."
    Set-Location $REPO_ROOT
    gdown $GDRIVE_FILE_ID -O $DATA_ZIP --fuzzy

    Write-Host "  Extracting archive..."
    # Detect top-level structure
    $entries = (& 7z l $DATA_ZIP 2>$null | Select-String "data/")
    if ($entries -or (Test-Path "C:\Program Files\7-Zip\7z.exe")) {
        # Try 7-Zip first (faster)
        if (Test-Path "C:\Program Files\7-Zip\7z.exe") {
            & "C:\Program Files\7-Zip\7z.exe" x $DATA_ZIP -o"$REPO_ROOT" -y | Out-Null
        } else {
            Expand-Archive -Path $DATA_ZIP -DestinationPath $REPO_ROOT -Force
        }
    } else {
        # PowerShell built-in
        Expand-Archive -Path $DATA_ZIP -DestinationPath $REPO_ROOT -Force
    }

    Remove-Item $DATA_ZIP -Force
    Write-Ok "Data extracted."
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Create directories
# ─────────────────────────────────────────────────────────────────────────────
Write-Step 4 "Creating required directories..."

@(
    "$REPO_ROOT\uploads",
    "$REPO_ROOT\data\osrm",
    "$REPO_ROOT\data\tiles_data",
    "$REPO_ROOT\docker\postgres\init"
) | ForEach-Object { New-Item -ItemType Directory -Path $_ -Force | Out-Null }

$pgInit = "$REPO_ROOT\docker\postgres\init\01-postgis.sql"
if (-not (Test-Path $pgInit)) {
    "CREATE EXTENSION IF NOT EXISTS postgis;" | Out-File -FilePath $pgInit -Encoding utf8
}

Write-Ok "Directories ready."

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Verify critical files
# ─────────────────────────────────────────────────────────────────────────────
Write-Step 5 "Verifying critical data files..."

$criticalFiles = @{
    "Adiyaman pre-earthquake COG"  = "$REPO_ROOT\data\turkey_data\Adiyaman\pre_earthquake_adiyaman_cog.tif"
    "Adiyaman post-earthquake COG" = "$REPO_ROOT\data\turkey_data\Adiyaman\post_earthquake_adiyaman_cog.tif"
    "Hatay pre-earthquake COG"     = "$REPO_ROOT\data\turkey_data\Hatay\pre_earthquake_hatay_cog.tif"
    "Adiyaman DEM"                 = "$REPO_ROOT\data\turkey_data\Adiyaman\dem_data\Adiyaman_dem_cog.tif"
    "Hatay DEM"                    = "$REPO_ROOT\data\turkey_data\Hatay\dem_data\Hatay_dem_cog.tif"
    "Adiyaman buildings shapefile" = "$REPO_ROOT\data\turkey_data\Adiyaman\buildings\buildings.shp"
    "Hatay buildings shapefile"    = "$REPO_ROOT\data\turkey_data\Hatay\buildings\buildings.shp"
    "Turkey PMTiles"               = "$REPO_ROOT\data\tiles_data\turkey.pmtiles"
    "TileServer config"            = "$REPO_ROOT\data\tiles_data\config.json"
}

$missing = 0
foreach ($kv in $criticalFiles.GetEnumerator()) {
    if (-not (Test-Path $kv.Value)) {
        Write-Fail "Missing: $($kv.Key)"
        $missing++
    } else {
        Write-Ok "Found: $($kv.Key)"
    }
}

if ($missing -gt 0) {
    Write-Fail "$missing critical file(s) missing. Check the data zip and re-run."
    exit 1
}
Write-Ok "All critical files verified."

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — OSRM
# ─────────────────────────────────────────────────────────────────────────────
Write-Step 6 "Checking OSRM routing files..."

$osrmOk = (Test-Path "$REPO_ROOT\data\osrm\turkey-latest.osrm") -and
          (Test-Path "$REPO_ROOT\data\osrm\turkey-latest.osrm.partition") -and
          (Test-Path "$REPO_ROOT\data\osrm\turkey-latest.osrm.cells") -and
          (Test-Path "$REPO_ROOT\data\osrm\turkey-latest.osrm.mldgr")

if ($osrmOk) {
    Write-Ok "OSRM routing files present."
} else {
    Write-Warn "Pre-processed OSRM files not found. Building from PBF (~40 min)..."

    $PBF = "$REPO_ROOT\data\osrm\turkey-latest.osm.pbf"
    if (-not (Test-Path $PBF)) {
        Write-Host "  Downloading Turkey OSM PBF (~600 MB)..."
        Invoke-WebRequest -Uri "https://download.geofabrik.de/europe/turkey-latest.osm.pbf" `
                          -OutFile $PBF -UseBasicParsing
    } else {
        Write-Ok "PBF already present."
    }

    $osrmVol = "$REPO_ROOT\data\osrm:/data"

    Write-Host "  osrm-extract (~15 min)..."
    docker run --rm -v "${osrmVol}" osrm/osrm-backend osrm-extract -p /opt/car.lua /data/turkey-latest.osm.pbf

    Write-Host "  osrm-partition (~15 min)..."
    docker run --rm -v "${osrmVol}" osrm/osrm-backend osrm-partition /data/turkey-latest.osrm

    Write-Host "  osrm-customize (~10 min)..."
    docker run --rm -v "${osrmVol}" osrm/osrm-backend osrm-customize /data/turkey-latest.osrm

    Write-Ok "OSRM routing files built."
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — Launch
# ─────────────────────────────────────────────────────────────────────────────
Write-Step 7 "Starting BhumiDrishti..."

Set-Location $REPO_ROOT
if ($ComposeCmd -eq "docker compose") {
    docker compose up --build -d
} else {
    docker-compose up --build -d
}

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║              Setup Complete!  🎉                     ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  Service URLs:" -ForegroundColor Cyan
Write-Host "    Frontend:   http://localhost:3000" -ForegroundColor Blue
Write-Host "    Backend:    http://localhost:8000" -ForegroundColor Blue
Write-Host "    API Docs:   http://localhost:8000/docs" -ForegroundColor Blue
Write-Host ""
Write-Host "  First-start notes:" -ForegroundColor Yellow
Write-Host "    Ollama downloads Gemma 4 models in the background (~4-20 GB)."
Write-Host "    Watch: docker compose logs -f ollama-init"
Write-Host ""
Write-Host "  Stop:    docker compose down" -ForegroundColor Yellow
Write-Host "  Restart: docker compose up -d" -ForegroundColor Yellow
Write-Host ""
