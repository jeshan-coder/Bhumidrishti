"""Dispatch endpoints for worker assignment and status updates."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from db.postgres import get_pool
from services.gemma_pipeline import (
    dispatch_assessments,
    get_field_workers,
    update_assessment_status,
)

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


def _success(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data, "error": None}


class DispatchAssignRequest(BaseModel):
    assessment_id: str | None = None
    assessment_ids: list[str] | None = None
    worker_name: str = Field(..., min_length=1, max_length=100)
    create_worker_if_missing: bool = True
    site_name: str | None = None
    severity_min: int | None = None
    severity_max: int | None = None
    status: str | None = None
    limit: int | None = Field(default=50, ge=1, le=200)


class AssessmentStatusUpdateRequest(BaseModel):
    assessment_id: str | None = None
    assessment_ids: list[str] | None = None
    status: str = Field(..., pattern="^(responded|closed)$")
    response_notes: str | None = None


class CreateFieldWorkerRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


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
    return _success(result) if result.get("success") else {"success": False, "data": None, "error": result.get("error")}


@router.post("/status")
async def set_assessment_status(body: AssessmentStatusUpdateRequest) -> dict[str, Any]:
    pool = get_pool()
    if not pool:
        return {"success": False, "data": None, "error": "Database pool not initialized"}
    result = await update_assessment_status(body.model_dump(exclude_none=True), pool)
    return _success(result) if result.get("success") else {"success": False, "data": None, "error": result.get("error")}


@router.post("/field-workers")
async def create_field_worker(body: CreateFieldWorkerRequest) -> dict[str, Any]:
    pool = get_pool()
    if not pool:
        return {"success": False, "data": None, "error": "Database pool not initialized"}
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO field_workers (name, status)
                VALUES ($1, 'available')
                ON CONFLICT ((LOWER(name))) DO UPDATE
                SET
                    name = EXCLUDED.name,
                    updated_at = NOW()
                RETURNING id, name, status, current_assessment_id, current_site_name, created_at, updated_at
                """,
                body.name.strip(),
            )
        return _success(
            {
                "success": True,
                "worker": {
                    "id": int(row["id"]),
                    "name": row["name"],
                    "status": row["status"],
                    "current_assessment_id": row["current_assessment_id"],
                    "current_site_name": row["current_site_name"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                },
            }
        )
    except Exception as exc:
        return {"success": False, "data": None, "error": f"Failed to create field worker: {exc}"}
