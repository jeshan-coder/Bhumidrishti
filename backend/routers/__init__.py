# This module exports all API routers for BhumiDrishti backend.

from .health import router as health_router
from .chat import router as chat_router
from .gis import router as gis_router
from .dem import router as dem_router

__all__ = ["health_router", "chat_router", "gis_router", "dem_router"]
