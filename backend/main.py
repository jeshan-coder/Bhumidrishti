"""BhumiDrishti backend FastAPI application entry point."""

import logging
import os
from logging.config import dictConfig

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from db.postgres import init_pool, close_pool
from routers import health_router, chat_router, gis_router, dem_router, assessment_router, upload_router, satellite_router

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


@app.on_event("startup")
async def startup_event() -> None:
    """Initialize database pool on application startup."""
    logger.info("app.startup.started")
    try:
        await init_pool()
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
