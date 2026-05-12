"""Dispatch endpoints for field team assignment and status updates."""

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from db.postgres import get_pool
from services.gemma_pipeline import (
    dispatch_assessments,
    get_field_teams,
    get_field_workers,
    update_assessment_status,
)

router = APIRouter(prefix="/dispatch", tags=["dispatch"])
logger = logging.getLogger(__name__)


def _success(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data, "error": None}


class DispatchAssignRequest(BaseModel):
    assessment_id: str | None = None
    assessment_ids: list[str] | None = None
    team_name: str | None = Field(default=None, min_length=1, max_length=120)
    worker_name: str | None = Field(default=None, min_length=1, max_length=120)
    create_team_if_missing: bool = True
    create_worker_if_missing: bool = True
    site_name: str | None = None
    severity_min: int | None = None
    severity_max: int | None = None
    status: str | None = None
    limit: int | None = Field(default=50, ge=1, le=200)


class AssessmentStatusUpdateRequest(BaseModel):
    assessment_id: str | None = None
    assessment_ids: list[str] | None = None
    site_name: str | None = None
    current_status: str | None = None
    limit: int | None = Field(default=50, ge=1, le=200)
    status: str = Field(..., pattern="^(responded|closed)$")
    response_notes: str | None = None


class CreateFieldTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    workers: list[str] | None = None


@router.get("/field-teams")
async def list_field_teams(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    pool = get_pool()
    if not pool:
        return {"success": False, "data": None, "error": "Database pool not initialized"}
    result = await get_field_teams({"status": status, "limit": limit}, pool)
    return _success(result) if result.get("success") else {"success": False, "data": None, "error": result.get("error")}


@router.get("/field-workers")
async def list_field_workers(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    pool = get_pool()
    if not pool:
        return {"success": False, "data": None, "error": "Database pool not initialized"}
    result = await get_field_workers({"status": status, "limit": limit}, pool)
    return _success(result) if result.get("success") else {"success": False, "data": None, "error": result.get("error")}


@router.post("/assign")
async def assign_assessments(body: DispatchAssignRequest) -> dict[str, Any]:
    pool = get_pool()
    if not pool:
        return {"success": False, "data": None, "error": "Database pool not initialized"}
    result = await dispatch_assessments(body.model_dump(exclude_none=True), pool)
    if result.get("success"):
        return _success(result)
    logger.warning("dispatch_assign_failed args=%s error=%s", body.model_dump(exclude_none=True), result.get("error"))
    return {"success": False, "data": None, "error": result.get("error")}


@router.post("/status")
async def set_assessment_status(body: AssessmentStatusUpdateRequest) -> dict[str, Any]:
    pool = get_pool()
    if not pool:
        return {"success": False, "data": None, "error": "Database pool not initialized"}
    result = await update_assessment_status(body.model_dump(exclude_none=True), pool)
    return _success(result) if result.get("success") else {"success": False, "data": None, "error": result.get("error")}


@router.post("/field-teams")
async def create_field_team(body: CreateFieldTeamRequest) -> dict[str, Any]:
    pool = get_pool()
    if not pool:
        return {"success": False, "data": None, "error": "Database pool not initialized"}
    workers = [w.strip() for w in (body.workers or []) if w and w.strip()]
    if not workers:
        return {"success": False, "data": None, "error": "At least one worker is required for a field team"}
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS field_teams (
                    id BIGSERIAL PRIMARY KEY,
                    name VARCHAR(120) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'available',
                    current_assessment_id VARCHAR(50),
                    current_site_name VARCHAR(200),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_field_teams_name_unique ON field_teams (LOWER(name))")
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
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_field_team_members_unique ON field_team_members(team_id, LOWER(worker_name))"
            )
            row = await conn.fetchrow(
                """
                INSERT INTO field_teams (name, status)
                VALUES ($1, 'available')
                ON CONFLICT ((LOWER(name))) DO UPDATE
                SET
                    name = EXCLUDED.name,
                    updated_at = NOW()
                RETURNING id, name, status, current_assessment_id, current_site_name, created_at, updated_at
                """,
                body.name.strip(),
            )
            for worker in workers:
                await conn.execute(
                    """
                    INSERT INTO field_team_members (team_id, worker_name)
                    VALUES ($1, $2)
                    ON CONFLICT (team_id, LOWER(worker_name)) DO NOTHING
                    """,
                    int(row["id"]),
                    worker,
                )
            member_rows = await conn.fetch(
                """
                SELECT worker_name
                FROM field_team_members
                WHERE team_id = $1
                ORDER BY LOWER(worker_name)
                """,
                int(row["id"]),
            )
        return _success(
            {
                "success": True,
                "team": {
                    "id": int(row["id"]),
                    "name": row["name"],
                    "status": row["status"],
                    "current_assessment_id": row["current_assessment_id"],
                    "current_site_name": row["current_site_name"],
                    "workers": [str(member["worker_name"]) for member in member_rows],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                },
            }
        )
    except Exception as exc:
        return {"success": False, "data": None, "error": f"Failed to create field team: {exc}"}


@router.post("/field-workers")
async def create_field_worker(body: CreateFieldTeamRequest) -> dict[str, Any]:
    return await create_field_team(body)
