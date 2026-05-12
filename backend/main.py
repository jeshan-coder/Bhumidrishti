"""BhumiDrishti backend FastAPI application entry point."""

import logging
import os
from logging.config import dictConfig

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from db.postgres import init_pool, close_pool
from routers import health_router, chat_router, gis_router, dem_router, assessment_router, upload_router, satellite_router, batch_router, dispatch_router, report_router

# This variable stores the backend-wide default log level.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# This function configures structured console logging for all backend modules.
def configure_logging() -> None:
    """Set global logging handlers and levels so module logs are visible in Docker output."""
    # This variable validates the configured level against standard logging levels.
    normalized_level = LOG_LEVEL if LOG_LEVEL in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO"
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "root": {
                "level": normalized_level,
                "handlers": ["console"],
            },
            "loggers": {
                "uvicorn": {"level": normalized_level},
                "uvicorn.error": {"level": normalized_level},
                "uvicorn.access": {"level": normalized_level},
            },
        }
    )


configure_logging()

# This variable stores the module logger for app lifecycle events.
logger = logging.getLogger(__name__)

app = FastAPI(
    title="BhumiDrishti API",
    description="Offline-first disaster damage assessment platform API",
    version="1.0.0"
)

# CORS enabled for localhost:3000
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _run_startup_migrations() -> None:
    """Apply incremental schema migrations that are safe to run on every startup."""
    from db.postgres import get_pool
    pool = get_pool()
    if not pool:
        logger.warning("app.startup.migrations.skipped pool_unavailable")
        return
    migrations = [
        # Widen assessments.geom to accept any geometry (MultiPolygon, Point, etc.)
        # all in EPSG:4326.  Safe to run multiple times — ALTER TYPE to same
        # supertype is a no-op when already correct.
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM geometry_columns
                WHERE f_table_name = 'assessments'
                  AND f_geometry_column = 'geom'
                  AND type = 'POINT'
            ) THEN
                ALTER TABLE assessments
                    ALTER COLUMN geom TYPE geometry(GEOMETRY, 4326)
                    USING geom::geometry(GEOMETRY, 4326);
            END IF;
        END $$
        """,
        # Orthophoto pipeline columns (idempotent — IF NOT EXISTS)
        "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS site_id BIGINT",
        "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS site_name VARCHAR(200)",
        "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS pre_chip_path VARCHAR(500)",
        "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS building_area_m2 FLOAT",
        "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS building_width_m FLOAT",
        "ALTER TABLE assessments ADD COLUMN IF NOT EXISTS building_height_m FLOAT",
        # Batches table
        """
        CREATE TABLE IF NOT EXISTS batches (
            id               VARCHAR(50)  PRIMARY KEY,
            site_name        VARCHAR(200) NOT NULL,
            ortho_upload_id  VARCHAR(20),
            area_polygon     GEOMETRY(Polygon, 4326),
            total_buildings  INTEGER      DEFAULT 0,
            processed        INTEGER      DEFAULT 0,
            failed           INTEGER      DEFAULT 0,
            skipped          INTEGER      DEFAULT 0,
            status           VARCHAR(20)  DEFAULT 'queued',
            worker_name      VARCHAR(100),
            force_reanalyze  BOOLEAN      DEFAULT FALSE,
            created_at       TIMESTAMPTZ  DEFAULT NOW(),
            started_at       TIMESTAMPTZ,
            completed_at     TIMESTAMPTZ
        )
        """,
        "ALTER TABLE batches ADD COLUMN IF NOT EXISTS site_id BIGINT",
        "CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status)",
        "CREATE INDEX IF NOT EXISTS idx_batches_area ON batches USING GIST(area_polygon)",
        "CREATE INDEX IF NOT EXISTS idx_batches_site_id ON batches(site_id)",
        """
        CREATE TABLE IF NOT EXISTS field_workers (
            id BIGSERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'available',
            current_assessment_id VARCHAR(50),
            current_site_name VARCHAR(200),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_field_workers_status CHECK (status IN ('available', 'busy'))
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_field_workers_name_unique ON field_workers (LOWER(name))",
        "CREATE INDEX IF NOT EXISTS idx_field_workers_status ON field_workers(status)",
        """
        INSERT INTO field_workers (name, status)
        SELECT DISTINCT TRIM(worker_name), 'available'
        FROM assessments
        WHERE worker_name IS NOT NULL AND TRIM(worker_name) <> ''
        ON CONFLICT ((LOWER(name))) DO NOTHING
        """,
        """
        CREATE TABLE IF NOT EXISTS field_teams (
            id BIGSERIAL PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'available',
            current_assessment_id VARCHAR(50),
            current_site_name VARCHAR(200),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_field_teams_status CHECK (status IN ('available', 'busy'))
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_field_teams_name_unique ON field_teams (LOWER(name))",
        "CREATE INDEX IF NOT EXISTS idx_field_teams_status ON field_teams(status)",
        """
        CREATE TABLE IF NOT EXISTS field_team_members (
            id BIGSERIAL PRIMARY KEY,
            team_id BIGINT NOT NULL REFERENCES field_teams(id) ON DELETE CASCADE,
            worker_name VARCHAR(120) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_field_team_members_unique ON field_team_members(team_id, LOWER(worker_name))",
        "CREATE INDEX IF NOT EXISTS idx_field_team_members_team_id ON field_team_members(team_id)",
        """
        INSERT INTO field_teams (name, status, current_assessment_id, current_site_name)
        SELECT fw.name, fw.status, fw.current_assessment_id, fw.current_site_name
        FROM field_workers fw
        ON CONFLICT ((LOWER(name))) DO UPDATE
        SET
            status = EXCLUDED.status,
            current_assessment_id = EXCLUDED.current_assessment_id,
            current_site_name = EXCLUDED.current_site_name,
            updated_at = NOW()
        """,
        """
        INSERT INTO field_team_members (team_id, worker_name)
        SELECT ft.id, ft.name
        FROM field_teams ft
        ON CONFLICT (team_id, LOWER(worker_name)) DO NOTHING
        """,
        """
        CREATE TABLE IF NOT EXISTS reports (
            id            VARCHAR(20) PRIMARY KEY,
            report_type   VARCHAR(20) NOT NULL,
            site_id       VARCHAR(40),
            assessment_id VARCHAR(50),
            team_name     VARCHAR(100),
            language      VARCHAR(10) DEFAULT 'en',
            file_path     VARCHAR(500),
            status        VARCHAR(20) NOT NULL DEFAULT 'generating',
            created_by    VARCHAR(100),
            error_message TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status)",
        "CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_reports_type ON reports(report_type)",
        "CREATE INDEX IF NOT EXISTS idx_reports_site_id ON reports(site_id)",
        "CREATE INDEX IF NOT EXISTS idx_reports_assessment_id ON reports(assessment_id)",
    ]
    async with pool.acquire() as conn:
        for sql in migrations:
            try:
                await conn.execute(sql)
            except Exception as exc:
                logger.warning("app.startup.migration.failed sql_preview=%s error=%s", sql[:80], exc)
    logger.info("app.startup.migrations.completed")


@app.on_event("startup")
async def startup_event() -> None:
    """Initialize database pool on application startup."""
    logger.info("app.startup.started")
    try:
        await init_pool()
        await _run_startup_migrations()
        logger.info("app.startup.completed")
    except Exception as exc:
        logger.exception("app.startup.failed error=%s", exc)
        raise


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Close database pool on application shutdown."""
    logger.info("app.shutdown.started")
    try:
        await close_pool()
        logger.info("app.shutdown.completed")
    except Exception as exc:
        logger.exception("app.shutdown.failed error=%s", exc)
        raise


app.include_router(health_router)
app.include_router(chat_router)
app.include_router(gis_router)
app.include_router(dem_router)
app.include_router(assessment_router)
app.include_router(upload_router)
app.include_router(satellite_router)
app.include_router(batch_router)
app.include_router(dispatch_router)
app.include_router(report_router)

# Serve chip images and uploaded files so the frontend can preview them.
_uploads_dir = os.getenv("UPLOAD_DIR", "/app/data/uploads")
import pathlib as _pl
_pl.Path(_uploads_dir).mkdir(parents=True, exist_ok=True)
app.mount("/media/uploads", StaticFiles(directory=_uploads_dir), name="uploads_media")
