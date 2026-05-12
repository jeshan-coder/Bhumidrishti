"""Batch orthophoto analysis endpoints with SSE progress streaming."""

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services.orthophoto_batch_pipeline import (
    cancel_batch,
    find_covering_upload,
    get_batch_status,
    is_batch_active,
    start_batch,
    subscribe_batch_events,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/batch", tags=["batch"])


class StartBatchRequest(BaseModel):
    post_ortho_upload_id: str | None = Field(
        default=None,
        description="Optional upload ID of the post-earthquake GeoTIFF",
    )
    area_polygon: dict[str, Any] = Field(..., description="GeoJSON polygon drawn by user")
    site_name: str = Field(..., min_length=1, max_length=200, description="Human-readable site name")
    worker_name: str | None = Field(default=None, max_length=100)
    force_reanalyze: bool = Field(default=False)


def _success(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data, "error": None}


def _error(msg: str) -> dict[str, Any]:
    return {"success": False, "data": None, "error": msg}


def _polygon_centroid_lat_lon(area_polygon: dict[str, Any]) -> tuple[float, float] | None:
    """Best-effort centroid from GeoJSON Polygon coordinates."""
    if not isinstance(area_polygon, dict):
        return None
    if str(area_polygon.get("type") or "").lower() != "polygon":
        return None
    coordinates = area_polygon.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) == 0:
        return None
    outer_ring = coordinates[0]
    if not isinstance(outer_ring, list) or len(outer_ring) < 3:
        return None
    parsed: list[tuple[float, float]] = []
    for point in outer_ring:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            lon = float(point[0])
            lat = float(point[1])
        except Exception:
            continue
        parsed.append((lat, lon))
    if len(parsed) < 3:
        return None
    # Drop closing duplicate when ring explicitly repeats first point.
    if parsed[0] == parsed[-1]:
        parsed = parsed[:-1]
    if not parsed:
        return None
    lat = sum(p[0] for p in parsed) / len(parsed)
    lon = sum(p[1] for p in parsed) / len(parsed)
    return (lat, lon)


@router.post("/orthophoto")
async def create_orthophoto_batch(body: StartBatchRequest) -> dict[str, Any]:
    """Start a new batch orthophoto analysis over all buildings in the drawn polygon."""
    try:
        upload_id = (body.post_ortho_upload_id or "").strip() or None
        if not upload_id:
            centroid = _polygon_centroid_lat_lon(body.area_polygon)
            if centroid is None:
                raise HTTPException(
                    status_code=400,
                    detail="Unable to infer polygon centroid. Provide post_ortho_upload_id.",
                )
            upload_id = await find_covering_upload(lat=centroid[0], lon=centroid[1])
            if not upload_id:
                raise HTTPException(
                    status_code=404,
                    detail="No orthophoto upload found covering this site. Provide post_ortho_upload_id.",
                )

        batch_id = await start_batch(
            ortho_upload_id=upload_id,
            area_polygon=body.area_polygon,
            site_name=body.site_name,
            worker_name=body.worker_name,
            force_reanalyze=body.force_reanalyze,
        )
        return _success(
            {
                "batch_id": batch_id,
                "site_name": body.site_name,
                "status": "queued",
                "ortho_upload_id": upload_id,
            }
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("batch_create_failed")
        raise HTTPException(status_code=500, detail="Failed to start batch") from exc


@router.get("/sites")
async def list_batch_sites() -> dict[str, Any]:
    """Return site names from canonical sites table, sorted alphabetically."""
    from db.postgres import get_pool
    pool = get_pool()
    if not pool:
        return _success([])
    try:
        async with pool.acquire() as conn:
            try:
                rows = await conn.fetch("SELECT name FROM sites ORDER BY name")
                return _success([row["name"] for row in rows])
            except Exception as exc:
                if "sites" not in str(exc).lower():
                    raise
                logger.warning("batch_sites_fallback_to_batches error=%s", exc)
                rows = await conn.fetch(
                    "SELECT DISTINCT site_name FROM batches ORDER BY site_name"
                )
        return _success([row["site_name"] for row in rows])
    except Exception as exc:
        logger.warning("batch_sites_query_failed error=%s", exc)
        return _success([])


@router.get("/pending")
async def list_pending_batches(limit: int = 20, only_active: bool = True) -> dict[str, Any]:
    """Return recent batches still running or failed before every building finished.

    Rows with ``status='complete'`` are never pending (avoid stale counter rows).
    """
    from db.postgres import get_pool

    pool = get_pool()
    if not pool:
        return _success([])

    safe_limit = max(1, min(limit, 100))
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH batch_coverage AS (
                    SELECT
                        b.id,
                        b.site_name,
                        ST_AsGeoJSON(b.area_polygon)::text AS area_geojson,
                        b.total_buildings,
                        b.processed,
                        b.failed,
                        b.skipped,
                        b.status,
                        b.created_at,
                        COUNT(DISTINCT tb.osm_id) AS actual_total_buildings,
                        COUNT(DISTINCT a.osm_building_id) AS assessed_buildings
                    FROM batches b
                    LEFT JOIN turkey_buildings tb
                      ON b.area_polygon IS NOT NULL
                     AND ST_Intersects(tb.geom, b.area_polygon)
                    LEFT JOIN assessments a
                      ON a.osm_building_id = tb.osm_id
                     AND a.status NOT IN ('false_positive')
                    WHERE b.status IN ('queued', 'processing', 'failed')
                    GROUP BY
                        b.id,
                        b.site_name,
                        b.area_polygon,
                        b.total_buildings,
                        b.processed,
                        b.failed,
                        b.skipped,
                        b.status,
                        b.created_at
                )
                SELECT *
                FROM batch_coverage
                WHERE actual_total_buildings > assessed_buildings
                ORDER BY created_at DESC
                LIMIT $1
                """,
                safe_limit,
            )
        pending_items = [
            {
                "batch_id": str(row["id"]),
                "site_name": row["site_name"],
                "area_geojson": json.loads(row["area_geojson"]) if row["area_geojson"] else None,
                "total_buildings": int(row["actual_total_buildings"] or row["total_buildings"] or 0),
                "processed": int(row["processed"] or 0),
                "failed": int(row["failed"] or 0),
                "skipped": int(row["skipped"] or 0),
                "remaining_buildings": max(
                    0,
                    int(row["actual_total_buildings"] or row["total_buildings"] or 0)
                    - int(row["assessed_buildings"] or 0),
                ),
                "status": row["status"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "is_active_task": is_batch_active(str(row["id"])),
            }
            for row in rows
        ]
        if only_active:
            pending_items = [item for item in pending_items if item.get("is_active_task")]
        return _success(pending_items)
    except Exception as exc:
        logger.warning("batch_pending_query_failed error=%s", exc)
        return _success([])


@router.get("/dashboard-metrics")
async def get_dashboard_metrics() -> dict[str, Any]:
    """Return site-centric dashboard metric counters."""
    from db.postgres import get_pool

    pool = get_pool()
    if not pool:
        return _success(
            {
                "total_assessed": 0,
                "critical": 0,
                "pending_response": 0,
                "responded": 0,
                "active_sites": 0,
            }
        )

    try:
        async with pool.acquire() as conn:
            has_sites_table = bool(
                await conn.fetchval("SELECT to_regclass('public.sites') IS NOT NULL")
            )
            has_field_workers_table = bool(
                await conn.fetchval("SELECT to_regclass('public.field_workers') IS NOT NULL")
            )
            has_field_teams_table = bool(
                await conn.fetchval("SELECT to_regclass('public.field_teams') IS NOT NULL")
            )
            has_assessments_site_id = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'assessments'
                          AND column_name = 'site_id'
                    )
                    """
                )
            )
            has_assessments_site_name = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'assessments'
                          AND column_name = 'site_name'
                    )
                    """
                )
            )
            has_batches_site_id = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'batches'
                          AND column_name = 'site_id'
                    )
                    """
                )
            )
            has_assessments_site_id = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'assessments'
                          AND column_name = 'site_id'
                    )
                    """
                )
            )
            has_assessments_site_name = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'assessments'
                          AND column_name = 'site_name'
                    )
                    """
                )
            )

            total_assessed = int(
                await conn.fetchval("SELECT COUNT(*)::int FROM assessments")
            )
            critical = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*)::int
                    FROM assessments
                    WHERE COALESCE(severity, 0) IN (4, 5)
                    """
                )
            )
            responded = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*)::int
                    FROM assessments
                    WHERE status IN ('responded', 'closed')
                    """
                )
            )
            pending_assessment = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*)::int
                    FROM assessments
                    WHERE status = 'pending'
                      AND COALESCE(severity, 0) >= 3
                    """
                )
            )
            pending_uploads = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*)::int
                    FROM uploads
                    WHERE COALESCE(status, '') <> 'done'
                    """
                )
            )
            remaining_latest_batches = int(
                await conn.fetchval(
                    """
                    WITH latest_site_batch AS (
                        SELECT DISTINCT ON (LOWER(site_name))
                            site_name,
                            total_buildings,
                            processed,
                            skipped,
                            created_at
                        FROM batches
                        WHERE site_name IS NOT NULL
                        ORDER BY LOWER(site_name), created_at DESC
                    )
                    SELECT COALESCE(
                        SUM(
                            GREATEST(
                                0,
                                COALESCE(total_buildings, 0)
                                - COALESCE(processed, 0)
                                - COALESCE(skipped, 0)
                            )
                        ),
                        0
                    )::int
                    FROM latest_site_batch
                    """
                )
            )

            if has_sites_table:
                try:
                    active_sites = int(
                        await conn.fetchval(
                            """
                            SELECT COUNT(*)::int
                            FROM sites
                            WHERE status IN ('active', 'processing')
                            """
                        )
                    )
                except Exception as exc:
                    logger.warning("dashboard_active_sites_primary_failed error=%s", exc)
                    active_sites = 0
                if active_sites == 0:
                    active_sites = int(
                        await conn.fetchval(
                            """
                            SELECT COUNT(DISTINCT LOWER(site_name))::int
                            FROM batches
                            WHERE status IN ('queued', 'processing')
                              AND site_name IS NOT NULL
                            """
                        )
                    )
            else:
                active_sites = int(
                    await conn.fetchval(
                        """
                        SELECT COUNT(DISTINCT LOWER(site_name))::int
                        FROM batches
                        WHERE status IN ('queued', 'processing')
                          AND site_name IS NOT NULL
                        """
                    )
                )

        return _success(
            {
                "total_assessed": total_assessed,
                "critical": critical,
                "pending_response": pending_assessment + pending_uploads + remaining_latest_batches,
                "responded": responded,
                "active_sites": active_sites,
            }
        )
    except Exception as exc:
        logger.warning("dashboard_metrics_query_failed error=%s", exc)
        return _success(
            {
                "total_assessed": 0,
                "critical": 0,
                "pending_response": 0,
                "responded": 0,
                "active_sites": 0,
            }
        )


@router.get("/dashboard-details")
async def get_dashboard_details() -> dict[str, Any]:
    """Return detailed dashboard sections for coordinator view."""
    from db.postgres import get_pool

    pool = get_pool()
    if not pool:
        return _success(
            {
                "sites": [],
                "severity_distribution": [],
                "recent_activity": [],
                "triage": [],
                "field_teams": [],
                "field_workers": [],
            }
        )

    try:
        async with pool.acquire() as conn:
            async def _fetch_fallback_site_rows():
                return await conn.fetch(
                    """
                    WITH latest_site_batch AS (
                        SELECT DISTINCT ON (LOWER(site_name))
                            id,
                            site_name,
                            status,
                            total_buildings,
                            worker_name,
                            created_at
                        FROM batches
                        WHERE site_name IS NOT NULL
                        ORDER BY LOWER(site_name), created_at DESC
                    ),
                    fallback_site_stats AS (
                        SELECT
                            l.site_name,
                            CASE
                              WHEN l.status IN ('queued', 'processing') THEN 'processing'
                              WHEN l.status = 'complete' THEN 'completed'
                              ELSE 'active'
                            END AS site_status,
                            COALESCE(l.total_buildings, 0) AS total_buildings,
                            COALESCE(COUNT(a.id), 0)::int AS assessed_buildings,
                            COALESCE(SUM(CASE WHEN a.severity = 5 THEN 1 ELSE 0 END), 0)::int AS sev5,
                            COALESCE(SUM(CASE WHEN a.severity = 4 THEN 1 ELSE 0 END), 0)::int AS sev4,
                            COALESCE(SUM(CASE WHEN a.severity = 3 THEN 1 ELSE 0 END), 0)::int AS sev3,
                            COALESCE(SUM(CASE WHEN a.severity = 2 THEN 1 ELSE 0 END), 0)::int AS sev2,
                            COALESCE(SUM(CASE WHEN a.severity = 1 THEN 1 ELSE 0 END), 0)::int AS sev1,
                            COALESCE(NULLIF(l.worker_name, ''), 'Unknown') AS created_by,
                            l.created_at AS updated_at
                        FROM latest_site_batch l
                        LEFT JOIN batches b2
                          ON LOWER(b2.site_name) = LOWER(l.site_name)
                        LEFT JOIN assessments a
                          ON a.batch_id = b2.id
                        GROUP BY
                            l.site_name,
                            l.status,
                            l.total_buildings,
                            l.worker_name,
                            l.created_at
                    )
                    SELECT *
                    FROM fallback_site_stats
                    ORDER BY
                      CASE
                        WHEN site_status = 'processing' THEN 0
                        WHEN site_status = 'active' THEN 1
                        ELSE 2
                      END,
                      updated_at DESC
                    """
                )

            has_sites_table = bool(
                await conn.fetchval("SELECT to_regclass('public.sites') IS NOT NULL")
            )
            has_field_workers_table = bool(
                await conn.fetchval("SELECT to_regclass('public.field_workers') IS NOT NULL")
            )
            has_field_teams_table = bool(
                await conn.fetchval("SELECT to_regclass('public.field_teams') IS NOT NULL")
            )
            has_assessments_site_id = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'assessments'
                          AND column_name = 'site_id'
                    )
                    """
                )
            )
            has_assessments_site_name = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'assessments'
                          AND column_name = 'site_name'
                    )
                    """
                )
            )
            has_batches_site_id = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'batches'
                          AND column_name = 'site_id'
                    )
                    """
                )
            )

            try:
                severity_rows = await conn.fetch(
                    """
                    SELECT COALESCE(severity, 0) AS severity, COUNT(*)::int AS cnt
                    FROM assessments
                    GROUP BY COALESCE(severity, 0)
                    """
                )
            except Exception as exc:
                logger.warning("dashboard_severity_query_failed error=%s", exc)
                severity_rows = []
            severity_counts = {int(row["severity"]): int(row["cnt"] or 0) for row in severity_rows}

            if has_sites_table:
                try:
                    site_batch_join = (
                        "b.site_id = s.id OR (b.site_id IS NULL AND LOWER(b.site_name) = LOWER(s.name))"
                        if has_batches_site_id
                        else "LOWER(b.site_name) = LOWER(s.name)"
                    )
                    site_rows = await conn.fetch(
                        f"""
                        WITH site_batch_map AS (
                            SELECT
                                s.id AS site_id,
                                b.id AS batch_id
                            FROM sites s
                            LEFT JOIN batches b
                              ON {site_batch_join}
                        ),
                        site_assessment_map AS (
                            SELECT DISTINCT
                                sbm.site_id,
                                a.id AS assessment_id,
                                a.severity
                            FROM site_batch_map sbm
                            JOIN assessments a
                              ON a.batch_id = sbm.batch_id
                            UNION
                            SELECT DISTINCT
                                s.id AS site_id,
                                a.id AS assessment_id,
                                a.severity
                            FROM sites s
                            JOIN assessments a
                              ON a.site_id = s.id
                        ),
                        site_assessment_stats AS (
                            SELECT
                                site_id,
                                COUNT(assessment_id)::int AS assessed_buildings,
                                COALESCE(SUM(CASE WHEN severity = 5 THEN 1 ELSE 0 END), 0)::int AS sev5,
                                COALESCE(SUM(CASE WHEN severity = 4 THEN 1 ELSE 0 END), 0)::int AS sev4,
                                COALESCE(SUM(CASE WHEN severity = 3 THEN 1 ELSE 0 END), 0)::int AS sev3,
                                COALESCE(SUM(CASE WHEN severity = 2 THEN 1 ELSE 0 END), 0)::int AS sev2,
                                COALESCE(SUM(CASE WHEN severity = 1 THEN 1 ELSE 0 END), 0)::int AS sev1
                            FROM site_assessment_map
                            GROUP BY site_id
                        ),
                        latest_site_batch AS (
                            SELECT
                                s.id AS site_id,
                                s.name AS site_name,
                                s.status AS site_status_base,
                                s.total_buildings AS site_total_buildings,
                                s.updated_at AS site_updated_at,
                                b.status AS batch_status,
                                b.total_buildings AS batch_total_buildings,
                                b.worker_name AS batch_worker_name,
                                b.created_at AS batch_created_at
                            FROM sites s
                            LEFT JOIN batches b
                              ON {site_batch_join}
                            ORDER BY s.id, b.created_at DESC NULLS LAST
                        ),
                        site_stats AS (
                            SELECT
                                lb.site_id,
                                lb.site_name,
                                CASE
                                  WHEN lb.batch_status IN ('queued', 'processing') THEN 'processing'
                                  WHEN lb.batch_status = 'complete' THEN 'completed'
                                  ELSE COALESCE(lb.site_status_base, 'active')
                                END AS site_status,
                                COALESCE(
                                    NULLIF(lb.site_total_buildings, 0),
                                    lb.batch_total_buildings,
                                    0
                                ) AS total_buildings,
                                COALESCE(st.assessed_buildings, 0)::int AS assessed_buildings,
                                COALESCE(st.sev5, 0)::int AS sev5,
                                COALESCE(st.sev4, 0)::int AS sev4,
                                COALESCE(st.sev3, 0)::int AS sev3,
                                COALESCE(st.sev2, 0)::int AS sev2,
                                COALESCE(st.sev1, 0)::int AS sev1,
                                COALESCE(NULLIF(lb.batch_worker_name, ''), 'Unknown') AS created_by,
                                COALESCE(lb.batch_created_at, lb.site_updated_at) AS updated_at
                            FROM latest_site_batch lb
                            LEFT JOIN site_assessment_stats st
                              ON st.site_id = lb.site_id
                        )
                        SELECT *
                        FROM site_stats
                        ORDER BY
                          CASE
                            WHEN site_status = 'processing' THEN 0
                            WHEN site_status = 'active' THEN 1
                            ELSE 2
                          END,
                          updated_at DESC
                        """
                    )
                except Exception as exc:
                    logger.warning("dashboard_site_stats_primary_failed error=%s", exc)
                    site_rows = []
                # Some environments have an empty sites table while historical rows
                # still exist in batches; keep dashboard cards populated.
                if not site_rows:
                    site_rows = await _fetch_fallback_site_rows()
            else:
                site_rows = await _fetch_fallback_site_rows()

            try:
                if has_sites_table:
                    if has_assessments_site_id:
                        recent_site_join = "LEFT JOIN sites s ON a.site_id = s.id"
                    elif has_assessments_site_name:
                        recent_site_join = (
                            "LEFT JOIN sites s "
                            "ON LOWER(TRIM(a.site_name)) = LOWER(TRIM(s.name))"
                        )
                    else:
                        recent_site_join = "LEFT JOIN sites s ON FALSE"
                    # Recent activity must be last 5 assessments with only sites join.
                    recent_rows = await conn.fetch(
                        f"""
                        SELECT
                            a.id,
                            a.severity,
                            a.osm_building_id,
                            COALESCE(NULLIF(s.name, ''), 'Unknown') AS site_name,
                            COALESCE(NULLIF(a.response_team, ''), NULLIF(a.worker_name, ''), 'Unknown') AS worker_name,
                            a.input_type,
                            a.created_at,
                            FALSE AS signs_of_life
                        FROM assessments a
                        {recent_site_join}
                        ORDER BY a.created_at DESC
                        LIMIT 5
                        """
                    )
                else:
                    recent_rows = await conn.fetch(
                        """
                        SELECT
                            a.id,
                            a.severity,
                            a.osm_building_id,
                            'Unknown' AS site_name,
                            COALESCE(NULLIF(a.response_team, ''), NULLIF(a.worker_name, ''), 'Unknown') AS worker_name,
                            a.input_type,
                            a.created_at,
                            FALSE AS signs_of_life
                        FROM assessments a
                        ORDER BY a.created_at DESC
                        LIMIT 5
                        """
                    )
            except Exception as exc:
                logger.warning("dashboard_recent_activity_query_failed error=%s", exc)
                recent_rows = []

            try:
                if has_sites_table:
                    if has_assessments_site_id:
                        triage_site_join = "LEFT JOIN sites s ON a.site_id = s.id"
                    elif has_assessments_site_name:
                        triage_site_join = (
                            "LEFT JOIN sites s "
                            "ON LOWER(TRIM(a.site_name)) = LOWER(TRIM(s.name))"
                        )
                    else:
                        triage_site_join = "LEFT JOIN sites s ON FALSE"
                    triage_rows = await conn.fetch(
                        f"""
                        SELECT
                            a.id,
                            a.severity,
                            a.osm_building_id,
                            COALESCE(NULLIF(s.name, ''), NULLIF(b.site_name, ''), 'Unknown') AS site_name,
                            COALESCE(NULLIF(a.response_team, ''), NULLIF(a.worker_name, ''), NULLIF(b.worker_name, ''), 'Unknown') AS worker_name,
                            a.input_type,
                            a.status,
                            a.created_at,
                            CASE
                              WHEN a.occupant_status IN ('trapped', 'evacuated') THEN TRUE
                              ELSE FALSE
                            END AS signs_of_life
                        FROM assessments a
                        {triage_site_join}
                        LEFT JOIN batches b ON a.batch_id = b.id
                        WHERE LOWER(COALESCE(a.status, '')) NOT IN ('closed', 'false_positive')
                        ORDER BY COALESCE(a.severity, 0) DESC, a.created_at DESC
                        LIMIT 120
                        """
                    )
                else:
                    triage_rows = await conn.fetch(
                        """
                        SELECT
                            a.id,
                            a.severity,
                            a.osm_building_id,
                            COALESCE(NULLIF(b.site_name, ''), 'Unknown') AS site_name,
                            COALESCE(NULLIF(a.response_team, ''), NULLIF(a.worker_name, ''), NULLIF(b.worker_name, ''), 'Unknown') AS worker_name,
                            a.input_type,
                            a.status,
                            a.created_at,
                            CASE
                              WHEN a.occupant_status IN ('trapped', 'evacuated') THEN TRUE
                              ELSE FALSE
                            END AS signs_of_life
                        FROM assessments a
                        LEFT JOIN batches b ON a.batch_id = b.id
                        WHERE LOWER(COALESCE(a.status, '')) NOT IN ('closed', 'false_positive')
                        ORDER BY COALESCE(a.severity, 0) DESC, a.created_at DESC
                        LIMIT 120
                        """
                    )
            except Exception as exc:
                logger.warning("dashboard_triage_query_failed error=%s", exc)
                triage_rows = []

            try:
                if has_field_teams_table:
                    worker_rows = await conn.fetch(
                        """
                        SELECT
                            LOWER(TRIM(ft.name)) AS worker_key,
                            ft.name AS worker_name,
                            ft.name AS team_name,
                            COALESCE(COUNT(ftm.id), 0)::int AS worker_count,
                            COALESCE(
                                ARRAY_AGG(ftm.worker_name ORDER BY LOWER(ftm.worker_name))
                                FILTER (WHERE ftm.worker_name IS NOT NULL),
                                ARRAY[]::text[]
                            ) AS workers,
                            0::int AS assessment_count,
                            ft.updated_at AS last_activity_at,
                            ft.status AS worker_status
                        FROM field_teams ft
                        LEFT JOIN field_team_members ftm ON ftm.team_id = ft.id
                        GROUP BY ft.id, ft.name, ft.updated_at, ft.status
                        ORDER BY
                            CASE WHEN ft.status = 'available' THEN 0 ELSE 1 END,
                            LOWER(ft.name)
                        """
                    )
                elif has_field_workers_table:
                    worker_rows = await conn.fetch(
                        """
                        SELECT
                            LOWER(TRIM(name)) AS worker_key,
                            name AS worker_name,
                            name AS team_name,
                            1::int AS worker_count,
                            ARRAY[name]::text[] AS workers,
                            0::int AS assessment_count,
                            updated_at AS last_activity_at,
                            status AS worker_status
                        FROM field_workers
                        ORDER BY
                            CASE WHEN status = 'available' THEN 0 ELSE 1 END,
                            LOWER(name)
                        """
                    )
                else:
                    worker_rows = await conn.fetch(
                        """
                        SELECT
                            LOWER(TRIM(worker_name)) AS worker_key,
                            MIN(TRIM(worker_name)) AS worker_name,
                            MIN(TRIM(worker_name)) AS team_name,
                            1::int AS worker_count,
                            ARRAY[MIN(TRIM(worker_name))]::text[] AS workers,
                            COUNT(*)::int AS assessment_count,
                            MAX(created_at) AS last_activity_at,
                            'available'::text AS worker_status
                        FROM assessments
                        WHERE worker_name IS NOT NULL
                          AND TRIM(worker_name) <> ''
                        GROUP BY LOWER(TRIM(worker_name))
                        ORDER BY last_activity_at DESC
                        """
                    )
            except Exception as exc:
                logger.warning("dashboard_worker_query_failed error=%s", exc)
                worker_rows = []

        deduped_sites: dict[str, dict[str, Any]] = {}
        for row in site_rows:
            raw_name = str(row["site_name"] or "Unknown")
            normalized_name = " ".join(raw_name.split()).strip()
            if not normalized_name:
                normalized_name = "Unknown"
            site_key = normalized_name.casefold()
            site_payload = {
                "site_name": normalized_name,
                "status": row["site_status"],
                "total_buildings": int(row["total_buildings"] or 0),
                "assessed_buildings": int(row["assessed_buildings"] or 0),
                "created_by": row["created_by"] or "Unknown",
                "severity_breakdown": {
                    "sev5": int(row["sev5"] or 0),
                    "sev4": int(row["sev4"] or 0),
                    "sev3": int(row["sev3"] or 0),
                    "sev2": int(row["sev2"] or 0),
                    "sev1": int(row["sev1"] or 0),
                },
            }
            existing = deduped_sites.get(site_key)
            if existing is None:
                deduped_sites[site_key] = site_payload
                continue
            # Keep the richer/more recent-looking row when duplicates exist.
            if site_payload["assessed_buildings"] > existing["assessed_buildings"]:
                deduped_sites[site_key] = site_payload
            elif (
                site_payload["assessed_buildings"] == existing["assessed_buildings"]
                and site_payload["total_buildings"] > existing["total_buildings"]
            ):
                deduped_sites[site_key] = site_payload
        sites = list(deduped_sites.values())

        severity_distribution = [
            {"severity": sev, "count": int(severity_counts.get(sev, 0))}
            for sev in [5, 4, 3, 2, 1]
        ]

        recent_activity = [
            {
                "assessment_id": str(row["id"]),
                "severity": int(row["severity"] or 0),
                "building_id": int(row["osm_building_id"] or 0) if row["osm_building_id"] else None,
                "site_name": row["site_name"] or "Unknown",
                "worker_name": row["worker_name"] or "Unknown",
                "input_type": row["input_type"] or "unknown",
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "signs_of_life": bool(row["signs_of_life"]),
            }
            for row in recent_rows
        ]

        triage = [
            {
                "assessment_id": str(row["id"]),
                "severity": int(row["severity"] or 0),
                "building_id": int(row["osm_building_id"] or 0) if row["osm_building_id"] else None,
                "site_name": row["site_name"] or "Unknown",
                "worker_name": row["worker_name"] or "Unknown",
                "input_type": row["input_type"] or "unknown",
                "status": row["status"] or "pending",
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "signs_of_life": bool(row["signs_of_life"]),
            }
            for row in triage_rows
        ]

        field_teams: list[dict[str, Any]] = []
        for row in worker_rows:
            payload = dict(row)
            field_teams.append(
                {
                    "team_name": payload.get("team_name") or payload.get("worker_name") or "Unknown",
                    "worker_name": payload.get("worker_name") or "Unknown",
                    "worker_count": int(payload.get("worker_count") or 0),
                    "workers": [str(name) for name in (payload.get("workers") or []) if str(name).strip()],
                    "assessment_count": int(payload.get("assessment_count") or 0),
                    "last_activity_at": payload["last_activity_at"].isoformat() if payload.get("last_activity_at") else None,
                    "status": payload.get("worker_status") or "available",
                }
            )
        field_workers = [
            {
                "worker_name": item["team_name"],
                "assessment_count": item["assessment_count"],
                "last_activity_at": item["last_activity_at"],
                "status": item["status"],
            }
            for item in field_teams
        ]

        return _success(
            {
                "sites": sites,
                "severity_distribution": severity_distribution,
                "recent_activity": recent_activity,
                "triage": triage,
                "field_teams": field_teams,
                "field_workers": field_workers,
            }
        )
    except Exception as exc:
        logger.warning("dashboard_details_query_failed error=%s", exc)
        return _success(
            {
                "sites": [],
                "severity_distribution": [],
                "recent_activity": [],
                "triage": [],
                "field_teams": [],
                "field_workers": [],
            }
        )


@router.get("/{batch_id}")
async def get_batch(batch_id: str) -> dict[str, Any]:
    """Get current status of a batch by ID."""
    batch = await get_batch_status(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _success(batch)


@router.get("/{batch_id}/buildings")
async def get_batch_buildings(batch_id: str, limit: int = 5000) -> dict[str, Any]:
    """Return building osm_ids covered by this batch area polygon."""
    from db.postgres import get_pool

    pool = get_pool()
    if not pool:
        return _success({"batch_id": batch_id, "osm_ids": [], "bbox": None})

    safe_limit = max(1, min(limit, 10000))
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT tb.osm_id
                FROM batches b
                JOIN turkey_buildings tb
                  ON b.area_polygon IS NOT NULL
                 AND ST_Intersects(tb.geom, b.area_polygon)
                WHERE b.id = $1
                LIMIT $2
                """,
                batch_id,
                safe_limit,
            )
            bbox_row = await conn.fetchrow(
                """
                SELECT
                  ST_XMin(extent_box) AS west,
                  ST_YMin(extent_box) AS south,
                  ST_XMax(extent_box) AS east,
                  ST_YMax(extent_box) AS north
                FROM (
                  SELECT ST_Extent(tb.geom)::box2d AS extent_box
                  FROM batches b
                  JOIN turkey_buildings tb
                    ON b.area_polygon IS NOT NULL
                   AND ST_Intersects(tb.geom, b.area_polygon)
                  WHERE b.id = $1
                ) extent_sub
                """,
                batch_id,
            )

        osm_ids = [int(row["osm_id"]) for row in rows if row.get("osm_id") is not None]
        bbox = None
        if bbox_row and all(bbox_row.get(key) is not None for key in ("west", "south", "east", "north")):
            bbox = {
                "west": float(bbox_row["west"]),
                "south": float(bbox_row["south"]),
                "east": float(bbox_row["east"]),
                "north": float(bbox_row["north"]),
            }

        return _success(
            {
                "batch_id": batch_id,
                "osm_ids": osm_ids,
                "bbox": bbox,
            }
        )
    except Exception as exc:
        logger.warning("batch_buildings_query_failed batch_id=%s error=%s", batch_id, exc)
        return _success({"batch_id": batch_id, "osm_ids": [], "bbox": None})


@router.post("/{batch_id}/cancel")
async def cancel_batch_run(batch_id: str) -> dict[str, Any]:
    """Cancel a running batch/single-building analysis job."""
    batch = await get_batch_status(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    canceled = cancel_batch(batch_id)
    if not canceled:
        status = str(batch.get("status") or "")
        if status in {"complete", "failed"}:
            return _success({"batch_id": batch_id, "status": status, "canceled": False})
        return _success({"batch_id": batch_id, "status": status or "processing", "canceled": False})

    return _success({"batch_id": batch_id, "status": "canceling", "canceled": True})


@router.post("/{batch_id}/analyze")
async def analyze_existing_batch(batch_id: str) -> dict[str, Any]:
    """Start a new analysis run for remaining buildings of an existing batch site."""
    from db.postgres import get_pool

    pool = get_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Database pool not available")

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    id,
                    site_name,
                    ortho_upload_id,
                    ST_AsGeoJSON(area_polygon)::text AS area_geojson,
                    worker_name
                FROM batches
                WHERE id = $1
                LIMIT 1
                """,
                batch_id,
            )
        if not row:
            raise HTTPException(status_code=404, detail="Batch not found")

        area_geojson_text = row.get("area_geojson")
        if not area_geojson_text:
            raise HTTPException(status_code=400, detail="Batch area polygon is missing")

        area_polygon = json.loads(area_geojson_text)
        new_batch_id = await start_batch(
            ortho_upload_id=str(row["ortho_upload_id"]),
            area_polygon=area_polygon,
            site_name=str(row["site_name"] or f"Retry from {batch_id}"),
            worker_name=row["worker_name"],
            force_reanalyze=False,
        )
        return _success(
            {
                "source_batch_id": batch_id,
                "batch_id": new_batch_id,
                "status": "queued",
            }
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("batch_reanalyze_failed batch_id=%s", batch_id)
        raise HTTPException(status_code=500, detail="Failed to start analysis for this batch") from exc


@router.get("/{batch_id}/stream")
async def stream_batch_events(batch_id: str) -> StreamingResponse:
    """
    SSE stream of real-time batch progress events.

    Streams until batch_complete event, then closes.
    Each SSE message: data: <json>\\n\\n
    """
    batch = await get_batch_status(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    async def _event_generator():
        try:
            async for event in subscribe_batch_events(batch_id):
                payload = json.dumps(event, default=str, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        finally:
            yield "data: {\"type\":\"stream_closed\"}\n\n"

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/building/{osm_id}/check-coverage")
async def check_building_coverage(osm_id: int, lat: float, lon: float) -> dict[str, Any]:
    """
    Check whether any uploaded orthophoto covers the given building coordinate.
    Used by the map popup Analyse button to decide which flow to launch.
    """
    upload_id = await find_covering_upload(lat=lat, lon=lon)
    return _success({
        "osm_id": osm_id,
        "has_coverage": upload_id is not None,
        "upload_id": upload_id,
    })


@router.post("/building/{osm_id}/analyze")
async def analyze_single_building(
    osm_id: int, lat: float, lon: float, site_name: str | None = None
) -> dict[str, Any]:
    """
    Trigger orthophoto analysis for a single building from the map popup.
    Creates a single-building batch using the most recent covering orthophoto.
    Optional site_name overrides the default auto-generated name.
    """
    upload_id = await find_covering_upload(lat=lat, lon=lon)
    if not upload_id:
        raise HTTPException(
            status_code=404,
            detail="No orthophoto upload found covering this building location",
        )

    actual_site_name = (site_name.strip() if site_name and site_name.strip()
                        else f"OSM:{osm_id} single-building")

    # Use a minimal 1m² dummy polygon — the pipeline ignores it when
    # osm_id_filter is set and queries the building directly by osm_id.
    area_polygon = {
        "type": "Polygon",
        "coordinates": [[
            [lon, lat], [lon + 0.00001, lat],
            [lon + 0.00001, lat + 0.00001], [lon, lat + 0.00001],
            [lon, lat],
        ]],
    }

    try:
        batch_id = await start_batch(
            ortho_upload_id=upload_id,
            area_polygon=area_polygon,
            site_name=actual_site_name,
            worker_name=None,
            force_reanalyze=True,
            osm_id_filter=osm_id,
        )
        return _success({
            "batch_id": batch_id,
            "osm_id": osm_id,
            "upload_id": upload_id,
            "stream_url": f"/batch/{batch_id}/stream",
        })
    except Exception as exc:
        logger.exception("single_building_batch_failed osm_id=%s", osm_id)
        raise HTTPException(status_code=500, detail="Failed to start single-building analysis") from exc
