"""Tool: get_field_teams — list field teams with availability and members."""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

from services.tools._shared import _AsyncNullContext

logger = logging.getLogger(__name__)


async def _ensure_field_team_tables(conn: asyncpg.Connection) -> None:
    await conn.execute(
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
        """
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_field_teams_name_unique ON field_teams (LOWER(name))"
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_field_teams_status ON field_teams(status)")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS field_team_members (
            id BIGSERIAL PRIMARY KEY,
            team_id BIGINT NOT NULL REFERENCES field_teams(id) ON DELETE CASCADE,
            worker_name VARCHAR(120) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_field_team_members_unique "
        "ON field_team_members(team_id, LOWER(worker_name))"
    )


async def get_field_teams(
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    """Return field teams with availability and member names."""
    if db is None:
        return {"success": False, "error": "Database not available", "items": []}

    limit = max(1, min(int(tool_args.get("limit") or 50), 200))
    status_value = str(tool_args.get("status") or "").strip().lower()
    status_filter = status_value if status_value in {"available", "busy"} else ""

    try:
        async with db.acquire() if hasattr(db, "acquire") else _AsyncNullContext(db) as conn:  # type: ignore[arg-type]
            await _ensure_field_team_tables(conn)
            rows = await conn.fetch(
                """
                SELECT
                    ft.id, ft.name, ft.status,
                    ft.current_assessment_id, ft.current_site_name,
                    ft.created_at, ft.updated_at,
                    COALESCE(COUNT(ftm.id), 0)::int AS worker_count,
                    COALESCE(
                        ARRAY_AGG(ftm.worker_name ORDER BY LOWER(ftm.worker_name))
                        FILTER (WHERE ftm.worker_name IS NOT NULL),
                        ARRAY[]::text[]
                    ) AS workers
                FROM field_teams ft
                LEFT JOIN field_team_members ftm ON ftm.team_id = ft.id
                WHERE ($1 = '' OR ft.status = $1)
                GROUP BY ft.id, ft.name, ft.status,
                         ft.current_assessment_id, ft.current_site_name,
                         ft.created_at, ft.updated_at
                ORDER BY CASE WHEN ft.status = 'available' THEN 0 ELSE 1 END, LOWER(ft.name) ASC
                LIMIT $2
                """,
                status_filter, limit,
            )
    except asyncpg.exceptions.UndefinedTableError:
        return {"success": False, "error": "field_teams table not available", "items": []}

    items: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        workers = [str(n).strip() for n in (payload.get("workers") or []) if str(n).strip()]
        payload["workers"] = workers
        payload["worker_count"] = int(payload.get("worker_count") or len(workers))
        for ts_col in ("created_at", "updated_at"):
            if payload.get(ts_col) is not None:
                payload[ts_col] = payload[ts_col].isoformat()
        items.append(payload)

    return {
        "success": True,
        "count": len(items),
        "filters_applied": {"status": status_filter or None, "limit": limit},
        "items": items,
    }
