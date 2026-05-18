# =============================================================================
#  BhumiDrishti - One-Command Setup Script (Windows PowerShell)
#  Offline AI Disaster Assessment Platform
#
#  Usage:  powershell -ExecutionPolicy Bypass -File setup.ps1
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

$ErrorActionPreference = "Stop"

$GDRIVE_FILE_ID = "1vDWLi18YpW0o8s54FrV7mNO3XjBBpJ_K"
$DATA_ZIP       = "bhumidrishti_data.zip"
$REPO_ROOT      = Split-Path -Parent $MyInvocation.MyCommand.Path
$TOTAL_STEPS    = 8

function Write-Step($n, $msg) { Write-Host "`n[$n/$TOTAL_STEPS] $msg" -ForegroundColor Yellow }
function Write-Ok($msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  !!  $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  XX  $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "  BHUMIDRISHTI" -ForegroundColor Blue
Write-Host "  Offline AI Disaster Assessment Platform - Windows Setup" -ForegroundColor Cyan
Write-Host "  AI Model: gemma4:e4b  (~4 GB download on first run)" -ForegroundColor Yellow
Write-Host ""

# -----------------------------------------------------------------------------
# STEP 1 - Docker
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# STEP 2 - NVIDIA GPU
# -----------------------------------------------------------------------------
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
    Write-Warn "Recommended: NVIDIA GPU with 6+ GB VRAM for gemma4:e4b."
}

# -----------------------------------------------------------------------------
# STEP 3 - Create .env files
# -----------------------------------------------------------------------------
Write-Step 3 "Creating environment files..."

$backendEnv = "$REPO_ROOT\backend\.env"
$backendEnvExample = "$REPO_ROOT\backend\.env.example"
if (-not (Test-Path $backendEnv)) {
    Copy-Item $backendEnvExample $backendEnv
    Write-Ok "Created backend/.env from example"
} else {
    Write-Ok "backend/.env already exists - skipping"
}

$frontendEnv = "$REPO_ROOT\frontend\.env.local"
$frontendEnvExample = "$REPO_ROOT\frontend\.env.local.example"
if (-not (Test-Path $frontendEnv)) {
    Copy-Item $frontendEnvExample $frontendEnv
    Write-Ok "Created frontend/.env.local from example"
} else {
    Write-Ok "frontend/.env.local already exists - skipping"
}

# -----------------------------------------------------------------------------
# STEP 4 - Download data from Google Drive
# -----------------------------------------------------------------------------
Write-Step 4 "Downloading data (~10 GB from Google Drive)..."

$DataPopulated = (Test-Path "$REPO_ROOT\data\turkey_data\Adiyaman") -and
                 (Test-Path "$REPO_ROOT\data\turkey_data\Hatay") -and
                 (Test-Path "$REPO_ROOT\data\osrm") -and
                 (Test-Path "$REPO_ROOT\data\tiles_data")

if ($DataPopulated) {
    Write-Ok "Data directory already populated - skipping download."
} else {
    # ---- Ensure Python is available ----
    $PythonCmd = $null
    if (Get-Command python3 -ErrorAction SilentlyContinue) {
        $PythonCmd = "python3"
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $PythonCmd = "python"
    }

    if (-not $PythonCmd) {
        Write-Warn "Python not found. Installing Python 3 automatically..."

        # Try winget first (available on Windows 10 1709+ / Windows 11)
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Host "  Using winget to install Python 3..."
            winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
            # Refresh PATH so python is visible in this session
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
        } else {
            # Fallback: download the official Python installer
            Write-Host "  Downloading Python 3.12 installer (~25 MB)..."
            $pyInstaller = "$env:TEMP\python-3.12-installer.exe"
            Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe" `
                              -OutFile $pyInstaller -UseBasicParsing
            Write-Host "  Running Python installer (silent)..."
            Start-Process -FilePath $pyInstaller -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0" -Wait
            Remove-Item $pyInstaller -Force
            # Refresh PATH
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
        }

        # Verify
        if (Get-Command python -ErrorAction SilentlyContinue) {
            $PythonCmd = "python"
            Write-Ok "Python installed successfully."
        } elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
            $PythonCmd = "python3"
            Write-Ok "Python installed successfully."
        } else {
            Write-Fail "Python installation failed."
            Write-Host "  Please install Python 3 manually: https://www.python.org/downloads/"
            Write-Host "  Make sure to check 'Add Python to PATH' during installation."
            Write-Host "  Then re-run this script."
            exit 1
        }
    } else {
        $pyVersion = (& $PythonCmd --version 2>&1)
        Write-Ok "Python found: $pyVersion"
    }

    # ---- Ensure gdown is available ----
    if (-not (Get-Command gdown -ErrorAction SilentlyContinue)) {
        Write-Host "  Installing gdown..."
        & $PythonCmd -m pip install --quiet gdown
        # Refresh PATH so gdown script is visible
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
        # Also check local Scripts folder (pip --user installs here)
        $localScripts = "$env:APPDATA\Python\Python312\Scripts"
        if (Test-Path $localScripts) { $env:PATH += ";$localScripts" }
        $localScripts2 = "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts"
        if (Test-Path $localScripts2) { $env:PATH += ";$localScripts2" }
    }
    Write-Ok "gdown ready"

    Write-Host "  Downloading data zip (10-30 min)..."
    Set-Location $REPO_ROOT
    gdown $GDRIVE_FILE_ID -O $DATA_ZIP --fuzzy

    Write-Host "  Extracting archive..."
    if (Test-Path "C:\Program Files\7-Zip\7z.exe") {
        & "C:\Program Files\7-Zip\7z.exe" x $DATA_ZIP -o"$REPO_ROOT" -y | Out-Null
    } else {
        Expand-Archive -Path $DATA_ZIP -DestinationPath $REPO_ROOT -Force
    }

    Remove-Item $DATA_ZIP -Force
    Write-Ok "Data extracted."
}

# -----------------------------------------------------------------------------
# STEP 5 - Create directories
# -----------------------------------------------------------------------------
Write-Step 5 "Creating required directories..."

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

# -----------------------------------------------------------------------------
# STEP 6 - Verify critical files
# -----------------------------------------------------------------------------
Write-Step 6 "Verifying critical data files..."

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

# -----------------------------------------------------------------------------
# STEP 7 - OSRM
# -----------------------------------------------------------------------------
Write-Step 7 "Checking OSRM routing files..."

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

# -----------------------------------------------------------------------------
# STEP 8 - Launch
# -----------------------------------------------------------------------------
Write-Step 8 "Starting BhumiDrishti..."

Set-Location $REPO_ROOT
if ($ComposeCmd -eq "docker compose") {
    docker compose up --build -d
} else {
    docker-compose up --build -d
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Setup Complete!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Service URLs:" -ForegroundColor Cyan
Write-Host "    Frontend:   http://localhost:3000" -ForegroundColor Blue
Write-Host "    Backend:    http://localhost:8000" -ForegroundColor Blue
Write-Host "    API Docs:   http://localhost:8000/docs" -ForegroundColor Blue
Write-Host ""
Write-Host "  First-start notes:" -ForegroundColor Yellow
Write-Host "    Ollama will download gemma4:e4b (~4 GB) in the background."
Write-Host "    AI chat will not work until the model download is complete."
Write-Host "    Watch: docker compose logs -f ollama-init"
Write-Host ""
Write-Host "  Stop:    docker compose down" -ForegroundColor Yellow
Write-Host "  Restart: docker compose up -d" -ForegroundColor Yellow
Write-Host ""
