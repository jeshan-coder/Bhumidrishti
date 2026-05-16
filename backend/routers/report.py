"""Report endpoints for markdown generation, listing, and downloads."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from db.postgres import get_pool
from models.report import ReportGenerateRequest
from services.reporting import (
    REPORTS_DIR,
    create_report_record,
    generate_site_markdown,
    generate_site_report_v2,
    save_pdf_report,
    save_markdown_report,
    update_report_record,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reports", tags=["reports"])


def _success(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data, "error": None}


def _error(message: str) -> dict[str, Any]:
    return {"success": False, "data": None, "error": message}


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _resolve_report_path(file_path_value: str | None, report_id: str) -> Path:
    if isinstance(file_path_value, str) and file_path_value.strip():
        path_candidate = Path(file_path_value)
        if path_candidate.is_absolute():
            if path_candidate.exists():
                return path_candidate
        else:
            normalized = file_path_value.replace("\\", "/").lstrip("./")
            if normalized.startswith("uploads/reports/"):
                relative_name = normalized[len("uploads/reports/") :]
                candidate = REPORTS_DIR / relative_name
            else:
                candidate = REPORTS_DIR / normalized
            if candidate.exists():
                return candidate

    # Prefer PDF for the download endpoint; fall back gracefully to markdown if only that exists.
    pdf_fallback = REPORTS_DIR / f"{report_id}.pdf"
    if pdf_fallback.exists():
        return pdf_fallback
    return REPORTS_DIR / f"{report_id}.md"


@router.post("/stream")
async def generate_report_stream(payload: ReportGenerateRequest) -> StreamingResponse:
    """Generate report markdown and stream progress/tokens over SSE."""
    pool = get_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Database pool is not initialized")

    # Resolve site_id from site_name when the frontend sends only a name.
    # Without a resolved site_id the report tools cannot query the DB correctly.
    if payload.report_type == "site" and payload.site_id is None and payload.site_name:
        async with pool.acquire() as conn:
            site_row = await conn.fetchrow(
                "SELECT id FROM sites WHERE LOWER(TRIM(name)) = LOWER(TRIM($1)) LIMIT 1",
                payload.site_name,
            )
            if site_row:
                payload.site_id = int(site_row["id"])
                logger.info(
                    "reports.stream.site_id_resolved site_name=%s site_id=%s",
                    payload.site_name,
                    payload.site_id,
                )
            else:
                logger.warning(
                    "reports.stream.site_id_not_found site_name=%s — will proceed with name-based lookup",
                    payload.site_name,
                )

    report_id = await create_report_record(pool, payload)
    logger.info(
        "reports.stream.started report_id=%s report_type=%s site_id=%s site_name=%s assessment_id=%s language=%s",
        report_id,
        payload.report_type,
        payload.site_id,
        payload.site_name,
        payload.assessment_id,
        payload.language,
    )

    async def event_generator() -> AsyncIterator[str]:
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        report_markdown_parts: list[str] = []

        async def on_progress(message: str) -> None:
            logger.info("reports.stream.progress report_id=%s message=%s", report_id, message)
            await queue.put(("progress", {"message": message, "report_id": report_id}))
            await asyncio.sleep(0)

        async def on_token(token: str) -> None:
            report_markdown_parts.append(token)
            await queue.put(("token", {"token": token, "report_id": report_id}))
            await asyncio.sleep(0)

        async def on_thinking(text: str) -> None:
            await queue.put(("thinking", {"text": text, "report_id": report_id}))
            await asyncio.sleep(0)

        async def on_tool_call(name: str, arguments: dict[str, Any]) -> None:
            logger.info("reports.stream.tool_call report_id=%s tool=%s args=%s", report_id, name, arguments)
            await queue.put(("tool_call", {"name": name, "arguments": arguments, "report_id": report_id}))
            await asyncio.sleep(0)

        async def on_tool_result(name: str, result: dict[str, Any]) -> None:
            logger.info("reports.stream.tool_result report_id=%s tool=%s", report_id, name)
            await queue.put(("tool_result", {"name": name, "result": result, "report_id": report_id}))
            await asyncio.sleep(0)

        async def on_section(html: str) -> None:
            await queue.put(("section", {"html": html, "report_id": report_id}))
            await asyncio.sleep(0)

        async def run_generation() -> None:
            try:
                await on_progress("Report generation started...")

                markdown_text = await generate_site_report_v2(
                    pool=pool,
                    request=payload,
                    report_id=report_id,
                    on_progress=on_progress,
                    on_token=on_token,
                    on_thinking=on_thinking,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                    on_section=on_section,
                )

                if not markdown_text.strip():
                    markdown_text = "".join(report_markdown_parts).strip()
                if not markdown_text:
                    raise ValueError("Generated report is empty")

                await on_progress("Saving report files...")
                await save_markdown_report(report_id, markdown_text)
                pdf_relative_path = await save_pdf_report(report_id, markdown_text)
                await update_report_record(pool, report_id, status="ready", file_path=pdf_relative_path)
                logger.info("reports.stream.completed report_id=%s file_path=%s", report_id, pdf_relative_path)
                await queue.put(
                    (
                        "done",
                        {
                            "report_id": report_id,
                            "status": "ready",
                            "file_path": pdf_relative_path,
                            "download_url": f"/reports/{report_id}/download",
                        },
                    )
                )
            except Exception as exc:
                logger.exception("reports.generate.failed report_id=%s error=%s", report_id, exc)
                await update_report_record(pool, report_id, status="failed", error_message=str(exc))
                await queue.put(("error", {"message": f"Report generation failed: {exc}", "report_id": report_id}))

        worker_task = asyncio.create_task(run_generation())
        try:
            while True:
                event, data = await queue.get()
                yield _sse_event(event, data)
                if event in {"done", "error"}:
                    break
        finally:
            await worker_task

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("")
async def list_reports(limit: int = 100) -> dict[str, Any]:
    """List generated reports for re-download without regeneration."""
    pool = get_pool()
    if not pool:
        return _error("Database pool is not initialized")

    safe_limit = max(1, min(limit, 500))
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id, report_type, site_id, assessment_id, team_name, language,
                    file_path, status, created_by, created_at, error_message
                FROM reports
                ORDER BY created_at DESC
                LIMIT $1
                """,
                safe_limit,
            )
        data = [
            {
                "id": row["id"],
                "report_type": row["report_type"],
                "site_id": row["site_id"],
                "assessment_id": row["assessment_id"],
                "team_name": row["team_name"],
                "language": row["language"],
                "file_path": row["file_path"],
                "status": row["status"],
                "created_by": row["created_by"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "error_message": row["error_message"],
            }
            for row in rows
        ]
        return _success(data)
    except Exception as exc:
        logger.exception("reports.list.failed error=%s", exc)
        return _error(f"Failed to list reports: {exc}")


@router.get("/{report_id}")
async def get_report_content(report_id: str) -> dict[str, Any]:
    """Return report metadata plus markdown content for preview."""
    pool = get_pool()
    if not pool:
        return _error("Database pool is not initialized")

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, report_type, site_id, assessment_id, team_name, language,
                       file_path, status, created_by, created_at
                FROM reports
                WHERE id = $1
                LIMIT 1
                """,
                report_id,
            )
        if row is None:
            return _error("Report not found")

        markdown_content = ""
        markdown_path = REPORTS_DIR / f"{report_id}.md"
        if markdown_path.exists():
            markdown_content = markdown_path.read_text(encoding="utf-8")

        return _success(
            {
                "id": row["id"],
                "report_type": row["report_type"],
                "site_id": row["site_id"],
                "assessment_id": row["assessment_id"],
                "team_name": row["team_name"],
                "language": row["language"],
                "file_path": row["file_path"],
                "status": row["status"],
                "created_by": row["created_by"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "markdown_content": markdown_content,
            }
        )
    except Exception as exc:
        logger.exception("reports.get.failed report_id=%s error=%s", report_id, exc)
        return _error(f"Failed to fetch report: {exc}")


@router.get("/{report_id}/download")
async def download_report(report_id: str) -> FileResponse:
    """Download previously generated markdown report."""
    pool = get_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Database pool is not initialized")

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT file_path, status FROM reports WHERE id = $1 LIMIT 1", report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if str(row["status"]).lower() != "ready":
        raise HTTPException(status_code=409, detail=f"Report is not ready (status={row['status']})")

    file_path_value = row["file_path"]
    if not isinstance(file_path_value, str) or not file_path_value:
        raise HTTPException(status_code=404, detail="Report file path missing")

    absolute = _resolve_report_path(file_path_value, report_id)
    if absolute.suffix.lower() != ".pdf":
        absolute = REPORTS_DIR / f"{report_id}.pdf"
    if not absolute.exists():
        raise HTTPException(status_code=404, detail="Report file not found on disk")

    return FileResponse(
        path=str(absolute),
        media_type="application/pdf",
        filename=f"{report_id}.pdf",
    )

