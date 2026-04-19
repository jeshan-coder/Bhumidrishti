"""PostgreSQL connection pool management."""

import os
import asyncpg

_pool: asyncpg.Pool | None = None

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "bhumidrishti")
DB_USER = os.getenv("DB_USER", "bhumidrishti")
DB_PASSWORD = os.getenv("DB_PASSWORD", "bhumidrishti")


async def init_pool() -> None:
    """Initialize asyncpg connection pool."""
    global _pool
    _pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        min_size=1,
        max_size=5,
    )


async def close_pool() -> None:
    """Close asyncpg connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool | None:
    """Return active connection pool."""
    return _pool
