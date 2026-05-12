"""Tool: dispatch_assessments — assign assessments to a field team."""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

from services.tools._shared import _AsyncNullContext
from services.tools.get_field_teams import _ensure_field_team_tables

logger = logging.getLogger(__name__)


async def dispatch_assessments(
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    """Assign assessments to a field team (or single worker) and mark them responded."""
    if db is None:
        return {"success": False, "error": "Database not available", "updated_count": 0, "assessment_ids": []}

    team_name = str(tool_args.get("team_name") or "").strip()
    worker_name = str(tool_args.get("worker_name") or "").strip()
    dispatch_team_name = team_name or worker_name
    if not dispatch_team_name:
        return {"success": False, "error": "team_name or worker_name is required", "updated_count": 0, "assessment_ids": []}

    worker_label = worker_name or dispatch_team_name
    create_team_if_missing = bool(
        tool_args.get("create_team_if_missing", tool_args.get("create_worker_if_missing", True))
    )

    explicit_ids: list[str] = []
    single_id = str(tool_args.get("assessment_id") or "").strip()
    if single_id:
        explicit_ids.append(single_id)
    raw_ids = tool_args.get("assessment_ids")
    if isinstance(raw_ids, list):
        explicit_ids.extend([str(item).strip() for item in raw_ids if str(item).strip()])
    explicit_ids = list(dict.fromkeys(explicit_ids))

    limit = max(1, min(int(tool_args.get("limit") or 50), 200))
    site_name = str(tool_args.get("site_name") or "").strip()
    status_filter = str(tool_args.get("status") or "pending").strip().lower() or "pending"
    severity_min = tool_args.get("severity_min")
    severity_max = tool_args.get("severity_max")

    try:
        async with db.acquire() if hasattr(db, "acquire") else _AsyncNullContext(db) as conn:  # type: ignore[arg-type]
            await _ensure_field_team_tables(conn)

            team_row = await conn.fetchrow(
                "SELECT id, name, status, current_assessment_id FROM field_teams WHERE LOWER(name) = LOWER($1) LIMIT 1",
                dispatch_team_name,
            )
            if team_row is None:
                if not create_team_if_missing:
                    return {"success": False, "error": "team_not_found", "updated_count": 0, "assessment_ids": []}
                team_row = await conn.fetchrow(
                    "INSERT INTO field_teams (name, status) VALUES ($1, 'available') "
                    "RETURNING id, name, status, current_assessment_id",
                    dispatch_team_name,
                )

            if str(team_row["status"] or "").lower() == "busy":
                return {
                    "success": False, "error": "team_busy",
                    "team_name": team_row["name"],
                    "current_assessment_id": team_row["current_assessment_id"],
                    "updated_count": 0, "assessment_ids": [],
                }

            if worker_label:
                await conn.execute(
                    "INSERT INTO field_team_members (team_id, worker_name) VALUES ($1, $2) "
                    "ON CONFLICT (team_id, LOWER(worker_name)) DO NOTHING",
                    int(team_row["id"]), worker_label,
                )

            has_assessments_site_name = bool(await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='assessments' AND column_name='site_name')"
            ))
            has_batches_site_name = bool(await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='batches' AND column_name='site_name')"
            ))

            selected_ids: list[str] = explicit_ids
            if not selected_ids:
                if has_assessments_site_name and has_batches_site_name:
                    site_name_expr = "LOWER(COALESCE(a.site_name, b.site_name, ''))"
                elif has_assessments_site_name:
                    site_name_expr = "LOWER(COALESCE(a.site_name, ''))"
                elif has_batches_site_name:
                    site_name_expr = "LOWER(COALESCE(b.site_name, ''))"
                else:
                    site_name_expr = "LOWER('')"

                filters: list[str] = [
                    "LOWER(COALESCE(a.status, '')) = $1",
                    "LOWER(COALESCE(a.status, '')) <> 'closed'",
                ]
                args: list[Any] = [status_filter]
                arg_idx = 2
                if site_name:
                    filters.append(site_name_expr + " LIKE LOWER($" + str(arg_idx) + ")")
                    args.append(f"%{site_name}%")
                    arg_idx += 1
                if severity_min is not None:
                    filters.append("COALESCE(a.severity, 0) >= $" + str(arg_idx))
                    args.append(int(severity_min))
                    arg_idx += 1
                if severity_max is not None:
                    filters.append("COALESCE(a.severity, 0) <= $" + str(arg_idx))
                    args.append(int(severity_max))
                    arg_idx += 1
                args.append(limit)
                rows = await conn.fetch(
                    f"""
                    SELECT a.id FROM assessments a
                    LEFT JOIN batches b ON a.batch_id = b.id
                    WHERE {" AND ".join(filters)}
                    ORDER BY COALESCE(a.severity, 0) DESC, a.created_at DESC
                    LIMIT ${arg_idx}
                    """,
                    *args,
                )
                selected_ids = [str(row["id"]) for row in rows]

            if not selected_ids:
                return {"success": False, "error": "no_assessments_matched", "updated_count": 0, "assessment_ids": []}

            await conn.execute(
                """
                UPDATE assessments
                SET status = 'responded', response_team = $1, worker_name = $2,
                    updated_at = NOW(), responded_at = COALESCE(responded_at, NOW())
                WHERE id = ANY($3::text[]) AND LOWER(COALESCE(status, '')) <> 'closed'
                """,
                dispatch_team_name, worker_label, selected_ids,
            )

            if has_assessments_site_name and has_batches_site_name:
                first_site_expr = "COALESCE(a.site_name, b.site_name)"
            elif has_assessments_site_name:
                first_site_expr = "COALESCE(a.site_name, '')"
            elif has_batches_site_name:
                first_site_expr = "COALESCE(b.site_name, '')"
            else:
                first_site_expr = "''"

            first_site = await conn.fetchval(
                f"""
                SELECT {first_site_expr} FROM assessments a
                LEFT JOIN batches b ON a.batch_id = b.id
                WHERE a.id = ANY($1::text[])
                ORDER BY a.created_at DESC LIMIT 1
                """,
                selected_ids,
            )

            await conn.execute(
                """
                UPDATE field_teams
                SET status = 'busy', current_assessment_id = $2,
                    current_site_name = $3, updated_at = NOW()
                WHERE LOWER(name) = LOWER($1)
                """,
                dispatch_team_name, selected_ids[0], first_site,
            )

        return {
            "success": True,
            "team_name": dispatch_team_name,
            "worker_name": worker_label,
            "updated_count": len(selected_ids),
            "assessment_ids": selected_ids,
            "status_set": "responded",
        }
    except asyncpg.exceptions.UndefinedTableError as exc:
        return {"success": False, "error": str(exc), "updated_count": 0, "assessment_ids": []}
    except asyncpg.exceptions.UndefinedColumnError as exc:
        return {"success": False, "error": str(exc), "updated_count": 0, "assessment_ids": []}
