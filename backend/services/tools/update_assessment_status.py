"""Tool: update_assessment_status — mark assessments as responded or closed."""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

from services.tools._shared import _AsyncNullContext
from services.tools.get_field_teams import _ensure_field_team_tables

logger = logging.getLogger(__name__)


async def update_assessment_status(
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    """Update assessment status and release assigned teams if closed."""
    if db is None:
        return {"success": False, "error": "Database not available", "updated_count": 0, "assessment_ids": []}

    status = str(tool_args.get("status") or "").strip().lower()
    if status not in {"responded", "closed"}:
        return {"success": False, "error": "status must be responded or closed", "updated_count": 0, "assessment_ids": []}

    explicit_ids: list[str] = []
    single_id = str(tool_args.get("assessment_id") or "").strip()
    if single_id:
        explicit_ids.append(single_id)
    raw_ids = tool_args.get("assessment_ids")
    if isinstance(raw_ids, list):
        explicit_ids.extend([str(item).strip() for item in raw_ids if str(item).strip()])
    assessment_ids = list(dict.fromkeys(explicit_ids))

    site_name = str(tool_args.get("site_name") or "").strip()
    current_status = str(tool_args.get("current_status") or "").strip().lower()
    limit = max(1, min(int(tool_args.get("limit") or 50), 200))
    response_notes = str(tool_args.get("response_notes") or "").strip() or None

    if not assessment_ids and not site_name:
        return {
            "success": False,
            "error": "assessment_id, assessment_ids, or site_name required",
            "updated_count": 0, "assessment_ids": [],
        }

    try:
        async with db.acquire() if hasattr(db, "acquire") else _AsyncNullContext(db) as conn:  # type: ignore[arg-type]
            if assessment_ids:
                rows = await conn.fetch(
                    "SELECT id, COALESCE(response_team, worker_name, '') AS assigned_team "
                    "FROM assessments WHERE id = ANY($1::text[])",
                    assessment_ids,
                )
            else:
                has_a = bool(await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='assessments' AND column_name='site_name')"
                ))
                has_b = bool(await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='batches' AND column_name='site_name')"
                ))
                if has_a and has_b:
                    site_expr = "LOWER(COALESCE(a.site_name, b.site_name, ''))"
                elif has_a:
                    site_expr = "LOWER(COALESCE(a.site_name, ''))"
                elif has_b:
                    site_expr = "LOWER(COALESCE(b.site_name, ''))"
                else:
                    return {"success": False, "error": "site_name not available in schema", "updated_count": 0, "assessment_ids": []}

                filters = [site_expr + " LIKE LOWER($1)", "LOWER(COALESCE(a.status, '')) <> 'closed'"]
                args: list[Any] = [f"%{site_name}%"]
                arg_idx = 2
                if current_status:
                    filters.append("LOWER(COALESCE(a.status, '')) = $" + str(arg_idx))
                    args.append(current_status)
                    arg_idx += 1
                args.append(limit)
                rows = await conn.fetch(
                    f"""
                    SELECT a.id, COALESCE(a.response_team, a.worker_name, '') AS assigned_team
                    FROM assessments a
                    LEFT JOIN batches b ON a.batch_id = b.id
                    WHERE {" AND ".join(filters)}
                    ORDER BY COALESCE(a.severity, 0) DESC, a.created_at DESC
                    LIMIT ${arg_idx}
                    """,
                    *args,
                )

            found_ids = [str(row["id"]) for row in rows]
            if not found_ids:
                return {"success": False, "error": "assessments_not_found", "updated_count": 0, "assessment_ids": []}

            if status == "closed":
                await conn.execute(
                    """
                    UPDATE assessments
                    SET status = 'closed',
                        response_notes = COALESCE($2, response_notes),
                        updated_at = NOW(),
                        responded_at = COALESCE(responded_at, NOW())
                    WHERE id = ANY($1::text[])
                    """,
                    found_ids, response_notes,
                )
                worker_names = sorted({
                    str(row["assigned_team"]).strip()
                    for row in rows if str(row["assigned_team"]).strip()
                })
                if worker_names:
                    await _ensure_field_team_tables(conn)
                    await conn.execute(
                        """
                        UPDATE field_teams
                        SET status = 'available', current_assessment_id = NULL,
                            current_site_name = NULL, updated_at = NOW()
                        WHERE LOWER(name) = ANY($1::text[])
                        """,
                        [n.lower() for n in worker_names],
                    )
            else:
                await conn.execute(
                    """
                    UPDATE assessments
                    SET status = 'responded',
                        response_notes = COALESCE($2, response_notes),
                        updated_at = NOW(),
                        responded_at = COALESCE(responded_at, NOW())
                    WHERE id = ANY($1::text[])
                    """,
                    found_ids, response_notes,
                )

        return {"success": True, "status_set": status, "updated_count": len(found_ids), "assessment_ids": found_ids}
    except asyncpg.exceptions.UndefinedTableError as exc:
        return {"success": False, "error": str(exc), "updated_count": 0, "assessment_ids": []}
    except asyncpg.exceptions.UndefinedColumnError as exc:
        return {"success": False, "error": str(exc), "updated_count": 0, "assessment_ids": []}
