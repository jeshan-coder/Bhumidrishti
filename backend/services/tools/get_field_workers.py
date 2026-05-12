"""Tool: get_field_workers — backward-compatible alias that returns teams as workers."""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

from services.tools.get_field_teams import get_field_teams

logger = logging.getLogger(__name__)


async def get_field_workers(
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    result = await get_field_teams(tool_args, db)
    if not result.get("success"):
        return result

    adapted = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "status": item.get("status"),
            "current_assessment_id": item.get("current_assessment_id"),
            "current_site_name": item.get("current_site_name"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "worker_count": item.get("worker_count", 0),
            "workers": item.get("workers", []),
        }
        for item in result.get("items", [])
    ]
    return {**result, "items": adapted}
