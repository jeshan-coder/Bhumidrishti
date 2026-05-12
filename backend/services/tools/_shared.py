"""Shared helpers used by multiple tool modules."""
from __future__ import annotations

import asyncpg


class _AsyncNullContext:
    """Async context wrapper for an existing asyncpg connection."""

    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def _safe_float(value) -> float | None:
    """Convert value to float safely, returning None on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
