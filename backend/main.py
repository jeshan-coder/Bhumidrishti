"""BhumiDrishti backend FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from db.postgres import init_pool, close_pool
from routers import health_router, chat_router, gis_router, dem_router

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
    await init_pool()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Close database pool on application shutdown."""
    await close_pool()


app.include_router(health_router)
app.include_router(chat_router)
app.include_router(gis_router)
app.include_router(dem_router)
