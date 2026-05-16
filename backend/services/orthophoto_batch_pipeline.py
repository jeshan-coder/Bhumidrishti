"""Orthophoto batch pipeline: iterate buildings in an area polygon and assess each."""

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

from db.postgres import get_pool
from services.chip_extractor import (
    BuildingOutsideOrthoError,
    ChipExtractionError,
    extract_building_chips,
)
from services.gemma_pipeline import run_assessment_agent
from services.gis import query_location_info_by_point
from services.pipeline_worker import save_ai_assessment, _select_and_prepare_video_frames

logger = logging.getLogger(__name__)

# Where uploaded files and chips live.
UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR", "/app/data/uploads")).resolve()

# Pre-earthquake COG paths keyed by province name fragment (lowercase).
# Actual files confirmed at these paths inside the backend container.
PRE_COG_PATHS: dict[str, str] = {
    "hatay": "/app/data/turkey_data/Hatay/pre_earthquake_hatay_cog.tif",
    "adiyaman": "/app/data/turkey_data/Adiyaman/pre_earthquake_adiyaman_cog.tif",
}

# In-memory SSE event queues keyed by batch_id.
# Each queue holds serialized SSE event dicts for the stream endpoint.
BATCH_EVENT_QUEUES: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}
# In-memory running task registry keyed by batch_id for cancellation.
BATCH_TASKS: dict[str, asyncio.Task[Any]] = {}


def _generate_batch_id() -> str:
    return f"BATCH-{str(uuid.uuid4().int)[:4]}"


def _utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def _push_event(batch_id: str, event: dict[str, Any]) -> None:
    """Push one SSE event into the batch queue if a consumer is listening."""
    queue = BATCH_EVENT_QUEUES.get(batch_id)
    if queue:
        await queue.put(event)


async def subscribe_batch_events(batch_id: str) -> AsyncGenerator[dict[str, Any], None]:
    """
    Async generator that yields SSE event dicts for a batch.

    Yields None sentinel to signal stream end.
    """
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=512)
    BATCH_EVENT_QUEUES[batch_id] = queue
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
    finally:
        BATCH_EVENT_QUEUES.pop(batch_id, None)


async def _determine_pre_cog(lat: float, lon: float, pool) -> str | None:
    """Return path to pre-earthquake COG for the province at this coordinate."""
    try:
        loc = await query_location_info_by_point(lat=lat, lon=lon, db=pool)
        province = (loc.province or "").lower()
    except Exception:
        province = "hatay" if lon < 37.5 else "adiyaman"

    for key, path in PRE_COG_PATHS.items():
        if key in province:
            return path if os.path.exists(path) else None

    return None


async def _create_batch_record(
    pool,
    batch_id: str,
    site_id: int | None,
    site_name: str,
    ortho_upload_id: str,
    area_polygon_geojson: str,
    total_buildings: int,
    worker_name: str | None,
    force_reanalyze: bool,
) -> None:
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO batches (
                    id, site_id, site_name, ortho_upload_id, area_polygon,
                    total_buildings, status, worker_name, force_reanalyze, created_at
                ) VALUES (
                    $1, $2, $3, $4,
                    ST_GeomFromGeoJSON($5),
                    $6, 'processing', $7, $8, NOW()
                )
                """,
                batch_id,
                site_id,
                site_name,
                ortho_upload_id,
                area_polygon_geojson,
                total_buildings,
                worker_name,
                force_reanalyze,
            )
        except Exception as exc:
            # Backward-compat: older DBs may not have batches.site_id yet.
            if "site_id" not in str(exc).lower():
                raise
            logger.warning("batch_insert_without_site_id_fallback error=%s", exc)
            await conn.execute(
                """
                INSERT INTO batches (
                    id, site_name, ortho_upload_id, area_polygon,
                    total_buildings, status, worker_name, force_reanalyze, created_at
                ) VALUES (
                    $1, $2, $3,
                    ST_GeomFromGeoJSON($4),
                    $5, 'processing', $6, $7, NOW()
                )
                """,
                batch_id,
                site_name,
                ortho_upload_id,
                area_polygon_geojson,
                total_buildings,
                worker_name,
                force_reanalyze,
            )


async def _resolve_or_create_site(
    pool,
    site_name: str,
    area_polygon_geojson: str,
) -> int | None:
    """Resolve one canonical site row by name or create it."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO sites (name, boundary, status)
                VALUES ($1, ST_GeomFromGeoJSON($2), 'processing')
                ON CONFLICT ((LOWER(name)))
                DO UPDATE SET
                  boundary = EXCLUDED.boundary,
                  status = 'processing',
                  updated_at = NOW()
                RETURNING id
                """,
                site_name,
                area_polygon_geojson,
            )
        return int(row["id"])
    except Exception as exc:
        # Backward-compat: sites table may not exist yet.
        if "sites" in str(exc).lower():
            logger.warning("sites_table_missing_fallback error=%s", exc)
            return None
        raise


async def _sync_site_progress(
    pool,
    site_id: int | None,
    *,
    status: str | None = None,
    total_buildings: int | None = None,
) -> None:
    """Update site status/counters from batch lifecycle events."""
    if site_id is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE sites
                   SET status = COALESCE($2, status),
                       total_buildings = COALESCE($3, total_buildings),
                       updated_at = NOW()
                 WHERE id = $1
                """,
                site_id,
                status,
                total_buildings,
            )
    except Exception as exc:
        # Backward-compat: sites table may not exist yet.
        if "sites" in str(exc).lower():
            logger.warning("site_sync_skipped_sites_table_missing error=%s", exc)
            return
        raise


async def _update_batch_progress(
    pool,
    batch_id: str,
    processed: int,
    failed: int,
    skipped: int,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE batches
               SET processed = $2, failed = $3, skipped = $4
             WHERE id = $1
            """,
            batch_id,
            processed,
            failed,
            skipped,
        )


async def _complete_batch(
    pool,
    batch_id: str,
    processed: int,
    failed: int,
    skipped: int,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE batches
               SET status = 'complete',
                   processed = $2, failed = $3, skipped = $4,
                   completed_at = NOW()
             WHERE id = $1
            """,
            batch_id,
            processed,
            failed,
            skipped,
        )


async def _fail_batch(
    pool,
    batch_id: str,
    processed: int,
    failed: int,
    skipped: int,
) -> None:
    """Mark batch as failed/canceled while preserving progress counters."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE batches
               SET status = 'failed',
                   processed = $2, failed = $3, skipped = $4,
                   completed_at = NOW()
             WHERE id = $1
            """,
            batch_id,
            processed,
            failed,
            skipped,
        )


async def _check_existing_assessment(
    pool,
    osm_id: int,
    force_reanalyze: bool,
    *,
    input_type_filter: list[str] | None = None,
) -> str | None:
    """Return existing assessment id if building was already assessed, else None.

    When input_type_filter is given only assessments of those types are considered.
    This lets us check "has this building already been assessed via ground_photo?"
    independently of orthophoto assessments.
    """
    if force_reanalyze:
        return None

    type_clause = ""
    args: list = [osm_id]
    if input_type_filter:
        placeholders = ", ".join(f"${i + 2}" for i in range(len(input_type_filter)))
        type_clause = f"AND input_type IN ({placeholders})"
        args.extend(input_type_filter)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT id FROM assessments
             WHERE osm_building_id = $1
               AND status NOT IN ('false_positive')
               {type_clause}
             LIMIT 1
            """,
            *args,
        )
    return str(row["id"]) if row else None


async def _lookup_ground_data_for_building(
    pool,
    osm_id: int,
    centroid_lat: float,
    centroid_lon: float,
    radius_m: float = 30.0,
) -> dict[str, Any] | None:
    """Return ground photo or video data for a building if any exists.

    Checks two sources in order:
    1. assessments table — already-analysed ground_photo / video records
    2. uploads table — unanalysed raw uploads (is_analyzed = false) within radius

    Priority order: ground_photo > video.
    Returns a dict with keys: input_type, photo_paths (list), or None.
    """
    async with pool.acquire() as conn:
        # Source 1: existing assessments (analysed ground data).
        assessed_rows = await conn.fetch(
            """
            SELECT input_type, photo_path
              FROM assessments
             WHERE (
                     osm_building_id = $1
                     OR (
                         geom IS NOT NULL
                         AND ST_DWithin(
                             geom::geography,
                             ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
                             $4
                         )
                     )
                   )
               AND input_type IN ('ground_photo', 'video')
               AND status NOT IN ('false_positive')
               AND photo_path IS NOT NULL
             ORDER BY
                 CASE WHEN input_type = 'ground_photo' THEN 1 ELSE 2 END,
                 created_at DESC
            """,
            osm_id,
            centroid_lon,
            centroid_lat,
            radius_m,
        )

        # Source 2: raw uploads not yet analysed (is_analyzed = false).
        upload_rows = await conn.fetch(
            """
            SELECT
                file_type AS input_type,
                saved_path AS photo_path
            FROM uploads
            WHERE file_type IN ('ground_photo', 'video')
              AND is_analyzed = false
              AND lat IS NOT NULL
              AND lon IS NOT NULL
              AND ST_DWithin(
                  ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography,
                  ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                  $3
              )
            ORDER BY
                CASE WHEN file_type = 'ground_photo' THEN 1 ELSE 2 END,
                uploaded_at DESC
            """,
            centroid_lon,
            centroid_lat,
            radius_m,
        )

    # Merge: assessed data takes precedence over raw uploads.
    all_rows = list(assessed_rows) + list(upload_rows)

    if not all_rows:
        return None

    # Ground photos (from either source) take priority.
    ground_paths: list[str] = [
        str(r["photo_path"]) for r in all_rows
        if r["input_type"] == "ground_photo" and r["photo_path"]
    ]
    if ground_paths:
        return {"input_type": "ground_photo", "photo_paths": ground_paths}

    video_row = next(
        (r for r in all_rows if r["input_type"] == "video" and r["photo_path"]),
        None,
    )
    if video_row:
        return {"input_type": "video", "photo_paths": [str(video_row["photo_path"])]}

    return None


async def _get_cog_path_for_upload(pool, upload_id: str) -> str | None:
    """Resolve the COG file path for an upload record."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cog_path, saved_path FROM uploads WHERE id = $1",
            upload_id,
        )
    if not row:
        return None
    cog = row["cog_path"]
    if cog and os.path.exists(str(UPLOAD_ROOT.parent / cog)):
        return str(UPLOAD_ROOT.parent / cog)
    saved = row["saved_path"]
    if saved and os.path.exists(str(UPLOAD_ROOT.parent / saved)):
        return str(UPLOAD_ROOT.parent / saved)
    return None


async def run_orthophoto_batch(
    pool,
    batch_id: str,
    site_id: int | None,
    site_name: str,
    ortho_upload_id: str,
    area_polygon: dict[str, Any],
    worker_name: str | None,
    force_reanalyze: bool,
    osm_id_filter: int | None = None,
) -> None:
    """
    Main batch processing coroutine. Runs as a background task.

    When osm_id_filter is set, only that single building is processed
    (used by the single-building popup Analyse flow).
    Streams SSE events via BATCH_EVENT_QUEUES[batch_id].
    """
    started_at = time.perf_counter()
    processed = failed = skipped = 0

    try:
        # ── Resolve post-earthquake COG ───────────────────────────────────────
        post_cog_path = await _get_cog_path_for_upload(pool, ortho_upload_id)
        if not post_cog_path:
            logger.error("batch_no_cog batch_id=%s upload_id=%s", batch_id, ortho_upload_id)
            await _push_event(batch_id, {
                "type": "batch_failed",
                "batch_id": batch_id,
                "error": "Post-earthquake COG not found",
                "timestamp": _utc_iso(),
            })
            await _fail_batch(pool, batch_id, processed, failed, skipped)
            await _sync_site_progress(pool, site_id, status="active")
            await _push_event(batch_id, None)
            return

        # ── Query buildings ───────────────────────────────────────────────────
        # Single-building mode: fetch by exact osm_id — no polygon query needed.
        # Batch mode: spatial intersect with the drawn area polygon.
        async with pool.acquire() as conn:
            if osm_id_filter is not None:
                building_rows = await conn.fetch(
                    """
                    SELECT
                        osm_id,
                        ST_AsGeoJSON(geom)              AS polygon_geojson,
                        ST_Area(geom::geography)         AS area_m2,
                        ST_XMax(geom) - ST_XMin(geom)   AS width_degrees,
                        ST_YMax(geom) - ST_YMin(geom)   AS height_degrees,
                        ST_X(ST_Centroid(geom))          AS centroid_lon,
                        ST_Y(ST_Centroid(geom))          AS centroid_lat,
                        ST_AsGeoJSON(ST_Envelope(geom))  AS bbox_geojson
                    FROM turkey_buildings
                    WHERE osm_id = $1
                    LIMIT 1
                    """,
                    osm_id_filter,
                )
                logger.info("batch_single_building_lookup batch_id=%s osm_id=%s found=%d",
                            batch_id, osm_id_filter, len(building_rows))
            else:
                area_geojson_str = json.dumps(area_polygon)
                building_rows = await conn.fetch(
                    """
                    SELECT
                        osm_id,
                        ST_AsGeoJSON(geom)              AS polygon_geojson,
                        ST_Area(geom::geography)         AS area_m2,
                        ST_XMax(geom) - ST_XMin(geom)   AS width_degrees,
                        ST_YMax(geom) - ST_YMin(geom)   AS height_degrees,
                        ST_X(ST_Centroid(geom))          AS centroid_lon,
                        ST_Y(ST_Centroid(geom))          AS centroid_lat,
                        ST_AsGeoJSON(ST_Envelope(geom))  AS bbox_geojson
                    FROM turkey_buildings
                    WHERE ST_Intersects(geom, ST_GeomFromGeoJSON($1))
                    """,
                    area_geojson_str,
                )

        total_buildings = len(building_rows)
        logger.info("batch_buildings_found batch_id=%s total=%d", batch_id, total_buildings)

        # Update DB record with total count.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE batches SET total_buildings = $2 WHERE id = $1",
                batch_id,
                total_buildings,
            )
        await _sync_site_progress(
            pool,
            site_id,
            status="processing",
            total_buildings=total_buildings,
        )

        await _push_event(batch_id, {
            "type": "batch_started",
            "batch_id": batch_id,
            "site_name": site_name,
            "total_buildings": total_buildings,
            "timestamp": _utc_iso(),
        })

        if total_buildings == 0:
            await _complete_batch(pool, batch_id, 0, 0, 0)
            await _sync_site_progress(pool, site_id, status="completed", total_buildings=0)
            await _push_event(batch_id, {
                "type": "batch_complete",
                "batch_id": batch_id,
                "site_name": site_name,
                "total": 0,
                "processed": 0,
                "failed": 0,
                "skipped": 0,
                "elapsed_seconds": round(time.perf_counter() - started_at, 1),
            })
            await _push_event(batch_id, None)
            return

        # ── Process each building ─────────────────────────────────────────────
        chips_dir = UPLOAD_ROOT / "chips" / batch_id

        for idx, row in enumerate(building_rows, start=1):
            osm_id: int = row["osm_id"]
            centroid_lat: float = float(row["centroid_lat"])
            centroid_lon: float = float(row["centroid_lon"])

            await _push_event(batch_id, {
                "type": "building_started",
                "batch_id": batch_id,
                "osm_id": osm_id,
                "index": idx,
                "total": total_buildings,
                "status": "clipping",
            })

            # ── Check for ground-level data (ground_photo > video > orthophoto) ──
            ground_data = await _lookup_ground_data_for_building(
                pool, osm_id, centroid_lat, centroid_lon
            )
            effective_input_type = ground_data["input_type"] if ground_data else "orthophoto"

            # ── Deduplicate check ──────────────────────────────────────────────
            # When ground data exists, only skip if a ground/video assessment
            # already exists for this building. An orthophoto-only assessment is
            # not sufficient — we still want to re-analyse with better data.
            if ground_data:
                existing_id = await _check_existing_assessment(
                    pool, osm_id, force_reanalyze,
                    input_type_filter=["ground_photo", "video"],
                )
            else:
                existing_id = await _check_existing_assessment(pool, osm_id, force_reanalyze)

            if existing_id:
                skipped += 1
                await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                await _push_event(batch_id, {
                    "type": "building_skipped",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "reason": "already_assessed",
                    "existing_assessment_id": existing_id,
                    "status": "skipped",
                })
                continue

            # ── Shared SSE progress callback ───────────────────────────────────
            async def _ai_progress(event: dict[str, Any]) -> None:
                stream_thinking = event.get("thinking_text")
                stream_response = event.get("response_text")
                stage = event.get("stage")
                thought = event.get("thought")
                if stage == "ai_reasoning_stream" and isinstance(stream_thinking, str) and stream_thinking.strip():
                    thought = stream_thinking[-480:]
                elif stage == "ai_response_stream" and isinstance(stream_response, str) and stream_response.strip():
                    thought = stream_response[-480:]
                await _push_event(batch_id, {
                    "type": "building_ai_stage",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "stage": stage,
                    "thought": thought,
                    "thinking_text": stream_thinking,
                    "response_text": stream_response,
                    "progress_percent": event.get("progress_percent"),
                })

            # ══════════════════════════════════════════════════════════════════
            # PATH A — ground photo: use field photos directly, skip chip work
            # ══════════════════════════════════════════════════════════════════
            if effective_input_type == "ground_photo":
                logger.info(
                    "batch_ground_photo_priority batch_id=%s osm_id=%s photos=%d",
                    batch_id, osm_id, len(ground_data["photo_paths"]),
                )
                # Resolve relative DB paths to absolute filesystem paths.
                abs_photo_paths = [
                    str(UPLOAD_ROOT.parent / p)
                    for p in ground_data["photo_paths"]
                    if p and (UPLOAD_ROOT.parent / p).exists()
                ]
                if not abs_photo_paths:
                    failed += 1
                    await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                    await _push_event(batch_id, {
                        "type": "building_failed",
                        "batch_id": batch_id,
                        "osm_id": osm_id,
                        "error": "ground_photo_files_missing",
                        "status": "failed",
                    })
                    continue

                await _push_event(batch_id, {
                    "type": "building_analyzing",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "status": "analyzing",
                    "data_source": "ground_photo",
                    "photo_count": len(abs_photo_paths),
                })

                try:
                    assessment_data = await run_assessment_agent(
                        image_paths=abs_photo_paths,
                        lat=centroid_lat,
                        lon=centroid_lon,
                        input_type="ground_photo",
                        db=pool,
                        field_note=None,
                        progress_callback=_ai_progress,
                    )
                except Exception as exc:
                    failed += 1
                    await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                    await _push_event(batch_id, {
                        "type": "building_failed",
                        "batch_id": batch_id,
                        "osm_id": osm_id,
                        "error": f"ai_error: {exc}",
                        "status": "failed",
                    })
                    logger.exception("batch_ai_failed_ground_photo osm_id=%s", osm_id)
                    continue

                save_photo_path = ground_data["photo_paths"][0]  # representative path
                try:
                    assessment_id = await save_ai_assessment(
                        pool=pool,
                        lat=centroid_lat,
                        lon=centroid_lon,
                        input_type="ground_photo",
                        photo_path=save_photo_path,
                        field_note=None,
                        assessment_data=assessment_data,
                        extra_fields={
                            "osm_building_id": osm_id,
                            "batch_id": batch_id,
                            "site_id": site_id,
                            "site_name": site_name,
                        },
                    )
                except Exception as exc:
                    failed += 1
                    await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                    await _push_event(batch_id, {
                        "type": "building_failed",
                        "batch_id": batch_id,
                        "osm_id": osm_id,
                        "error": f"save_error: {exc}",
                        "status": "failed",
                    })
                    logger.exception("batch_save_failed_ground_photo osm_id=%s", osm_id)
                    continue

                processed += 1
                await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                await _push_event(batch_id, {
                    "type": "building_done",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "assessment_id": assessment_id,
                    "severity": assessment_data.get("severity"),
                    "damage_type": assessment_data.get("damage_type"),
                    "lat": centroid_lat,
                    "lon": centroid_lon,
                    "confidence": assessment_data.get("confidence"),
                    "data_source": "ground_photo",
                    "status": "done",
                    "prompt_tokens": assessment_data.get("prompt_tokens"),
                    "completion_tokens": assessment_data.get("completion_tokens"),
                    "total_tokens": assessment_data.get("total_tokens"),
                    "context_window": assessment_data.get("context_window"),
                    "inference_seconds": assessment_data.get("inference_seconds"),
                })
                logger.info(
                    "batch_building_done batch_id=%s osm_id=%s assessment_id=%s severity=%s source=ground_photo",
                    batch_id, osm_id, assessment_id, assessment_data.get("severity"),
                )
                continue

            # ══════════════════════════════════════════════════════════════════
            # PATH B — video: extract frames then assess
            # ══════════════════════════════════════════════════════════════════
            if effective_input_type == "video":
                video_rel_path = ground_data["photo_paths"][0]
                abs_video_path = str(UPLOAD_ROOT.parent / video_rel_path)
                logger.info(
                    "batch_video_priority batch_id=%s osm_id=%s video=%s",
                    batch_id, osm_id, abs_video_path,
                )

                if not Path(abs_video_path).exists():
                    failed += 1
                    await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                    await _push_event(batch_id, {
                        "type": "building_failed",
                        "batch_id": batch_id,
                        "osm_id": osm_id,
                        "error": "video_file_missing",
                        "status": "failed",
                    })
                    continue

                await _push_event(batch_id, {
                    "type": "building_analyzing",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "status": "analyzing",
                    "data_source": "video",
                })

                try:
                    video_upload_id = f"batch_{batch_id}_osm_{osm_id}"
                    frame_paths = await asyncio.to_thread(
                        _select_and_prepare_video_frames,
                        abs_video_path,
                        video_upload_id,
                    )
                except Exception as exc:
                    failed += 1
                    await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                    await _push_event(batch_id, {
                        "type": "building_failed",
                        "batch_id": batch_id,
                        "osm_id": osm_id,
                        "error": f"video_frame_extract_error: {exc}",
                        "status": "failed",
                    })
                    logger.exception("batch_video_frame_extract_failed osm_id=%s", osm_id)
                    continue

                if not frame_paths:
                    failed += 1
                    await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                    await _push_event(batch_id, {
                        "type": "building_failed",
                        "batch_id": batch_id,
                        "osm_id": osm_id,
                        "error": "video_no_frames_extracted",
                        "status": "failed",
                    })
                    continue

                try:
                    assessment_data = await run_assessment_agent(
                        image_paths=frame_paths,
                        lat=centroid_lat,
                        lon=centroid_lon,
                        input_type="video",
                        db=pool,
                        field_note=None,
                        progress_callback=_ai_progress,
                    )
                except Exception as exc:
                    failed += 1
                    await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                    await _push_event(batch_id, {
                        "type": "building_failed",
                        "batch_id": batch_id,
                        "osm_id": osm_id,
                        "error": f"ai_error: {exc}",
                        "status": "failed",
                    })
                    logger.exception("batch_ai_failed_video osm_id=%s", osm_id)
                    continue

                try:
                    assessment_id = await save_ai_assessment(
                        pool=pool,
                        lat=centroid_lat,
                        lon=centroid_lon,
                        input_type="video",
                        photo_path=video_rel_path,
                        field_note=None,
                        assessment_data=assessment_data,
                        extra_fields={
                            "osm_building_id": osm_id,
                            "batch_id": batch_id,
                            "site_id": site_id,
                            "site_name": site_name,
                        },
                    )
                except Exception as exc:
                    failed += 1
                    await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                    await _push_event(batch_id, {
                        "type": "building_failed",
                        "batch_id": batch_id,
                        "osm_id": osm_id,
                        "error": f"save_error: {exc}",
                        "status": "failed",
                    })
                    logger.exception("batch_save_failed_video osm_id=%s", osm_id)
                    continue

                processed += 1
                await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                await _push_event(batch_id, {
                    "type": "building_done",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "assessment_id": assessment_id,
                    "severity": assessment_data.get("severity"),
                    "damage_type": assessment_data.get("damage_type"),
                    "lat": centroid_lat,
                    "lon": centroid_lon,
                    "confidence": assessment_data.get("confidence"),
                    "data_source": "video",
                    "status": "done",
                    "prompt_tokens": assessment_data.get("prompt_tokens"),
                    "completion_tokens": assessment_data.get("completion_tokens"),
                    "total_tokens": assessment_data.get("total_tokens"),
                    "context_window": assessment_data.get("context_window"),
                    "inference_seconds": assessment_data.get("inference_seconds"),
                })
                logger.info(
                    "batch_building_done batch_id=%s osm_id=%s assessment_id=%s severity=%s source=video",
                    batch_id, osm_id, assessment_id, assessment_data.get("severity"),
                )
                continue

            # ══════════════════════════════════════════════════════════════════
            # PATH C — orthophoto (default): chip extraction → AI analysis
            # ══════════════════════════════════════════════════════════════════

            # ── Chip extraction ────────────────────────────────────────────────
            await _push_event(batch_id, {
                "type": "building_clipping",
                "batch_id": batch_id,
                "osm_id": osm_id,
                "status": "clipping",
            })

            polygon_geojson_str: str = row["polygon_geojson"]
            try:
                polygon_geojson = json.loads(polygon_geojson_str)
            except Exception:
                failed += 1
                await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                await _push_event(batch_id, {
                    "type": "building_failed",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "error": "invalid_geometry_json",
                    "status": "failed",
                })
                continue

            pre_cog_path = await _determine_pre_cog(centroid_lat, centroid_lon, pool)

            try:
                chip_meta = await asyncio.to_thread(
                    extract_building_chips,
                    post_cog_path,
                    pre_cog_path,
                    polygon_geojson,
                    osm_id,
                    batch_id,
                    chips_dir,
                )
            except BuildingOutsideOrthoError as exc:
                failed += 1
                await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                await _push_event(batch_id, {
                    "type": "building_failed",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "error": "building_outside_ortho_bounds",
                    "status": "failed",
                })
                logger.info("batch_building_outside_ortho osm_id=%s error=%s", osm_id, exc)
                continue
            except ChipExtractionError as exc:
                failed += 1
                await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                await _push_event(batch_id, {
                    "type": "building_failed",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "error": str(exc),
                    "status": "failed",
                })
                continue
            except Exception as exc:
                failed += 1
                await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                await _push_event(batch_id, {
                    "type": "building_failed",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "error": f"chip_error: {exc}",
                    "status": "failed",
                })
                logger.exception("batch_chip_unexpected osm_id=%s", osm_id)
                continue

            # This variable stores whether pre-chip should be used for AI comparison.
            pre_available_for_ai = bool(chip_meta.get("pre_available"))
            # This variable stores pre/post chip bbox overlap percentage in WGS84.
            pre_post_overlap_pct = float(chip_meta.get("pre_post_overlap_pct") or 0.0)

            # Validate visual alignment before sending both chips.
            if pre_available_for_ai and pre_post_overlap_pct < 80.0:
                logger.warning(
                    "batch_pre_post_alignment_low_overlap batch_id=%s osm_id=%s overlap_pct=%.2f action=post_only",
                    batch_id, osm_id, pre_post_overlap_pct,
                )
                chip_meta_warnings = chip_meta.get("warnings") or []
                if "pre_post_low_overlap" not in chip_meta_warnings:
                    chip_meta_warnings.append("pre_post_low_overlap")
                chip_meta["warnings"] = chip_meta_warnings
                pre_available_for_ai = False

            # ── Gemma analysis ─────────────────────────────────────────────────
            await _push_event(batch_id, {
                "type": "building_analyzing",
                "batch_id": batch_id,
                "osm_id": osm_id,
                "status": "analyzing",
                "data_source": "orthophoto",
                "pre_available": pre_available_for_ai,
                "post_available": True,
                "pre_post_overlap_pct": round(pre_post_overlap_pct, 2),
            })

            image_paths: list[str] = []
            if pre_available_for_ai and chip_meta["pre_chip_abs"]:
                image_paths.append(chip_meta["pre_chip_abs"])
            image_paths.append(chip_meta["post_chip_abs"])

            area_m2 = float(row["area_m2"] or chip_meta["area_m2"])
            width_m = chip_meta["width_m"]
            height_m = chip_meta["height_m"]

            try:
                assessment_data = await run_assessment_agent(
                    image_paths=image_paths,
                    lat=centroid_lat,
                    lon=centroid_lon,
                    input_type="orthophoto",
                    db=pool,
                    field_note=None,
                    progress_callback=_ai_progress,
                    orthophoto_context={
                        "osm_id": osm_id,
                        "batch_id": batch_id,
                        "site_name": site_name,
                        "building_index": idx,
                        "total_buildings": total_buildings,
                        "pre_available": pre_available_for_ai,
                        "is_dark": chip_meta["is_dark"],
                        "width_m": width_m,
                        "height_m": height_m,
                        "area_m2": area_m2,
                    },
                )
            except Exception as exc:
                failed += 1
                await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                await _push_event(batch_id, {
                    "type": "building_failed",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "error": f"ai_error: {exc}",
                    "status": "failed",
                })
                logger.exception("batch_ai_failed osm_id=%s", osm_id)
                continue

            # Merge chip warnings into assessment warnings.
            existing_warnings = assessment_data.get("warnings") or []
            combined_warnings = list(set(existing_warnings + chip_meta["warnings"]))
            assessment_data["warnings"] = combined_warnings

            # Reduce confidence for dark images.
            if chip_meta["is_dark"]:
                assessment_data["confidence"] = max(
                    0.30, float(assessment_data.get("confidence", 0.5)) - 0.25
                )

            # ── Persist assessment ─────────────────────────────────────────────
            try:
                assessment_id = await save_ai_assessment(
                    pool=pool,
                    lat=centroid_lat,
                    lon=centroid_lon,
                    input_type="orthophoto",
                    photo_path=chip_meta["post_chip_path"],
                    field_note=None,
                    assessment_data=assessment_data,
                    extra_fields={
                        "osm_building_id": osm_id,
                        "batch_id": batch_id,
                        "site_id": site_id,
                        "chip_path": chip_meta["post_chip_path"],
                        "pre_chip_path": chip_meta["pre_chip_path"] if pre_available_for_ai else None,
                        "site_name": site_name,
                        "building_area_m2": area_m2,
                        "building_width_m": width_m,
                        "building_height_m": height_m,
                    },
                )
            except Exception as exc:
                failed += 1
                await _update_batch_progress(pool, batch_id, processed, failed, skipped)
                await _push_event(batch_id, {
                    "type": "building_failed",
                    "batch_id": batch_id,
                    "osm_id": osm_id,
                    "error": f"save_error: {exc}",
                    "status": "failed",
                })
                logger.exception("batch_save_failed osm_id=%s", osm_id)
                continue

            processed += 1
            await _update_batch_progress(pool, batch_id, processed, failed, skipped)

            await _push_event(batch_id, {
                "type": "building_done",
                "batch_id": batch_id,
                "osm_id": osm_id,
                "assessment_id": assessment_id,
                "severity": assessment_data.get("severity"),
                "damage_type": assessment_data.get("damage_type"),
                "lat": centroid_lat,
                "lon": centroid_lon,
                "confidence": assessment_data.get("confidence"),
                "chip_path": chip_meta["post_chip_path"],
                "pre_chip_path": chip_meta["pre_chip_path"] if pre_available_for_ai else None,
                "data_source": "orthophoto",
                "status": "done",
                "prompt_tokens": assessment_data.get("prompt_tokens"),
                "completion_tokens": assessment_data.get("completion_tokens"),
                "total_tokens": assessment_data.get("total_tokens"),
                "context_window": assessment_data.get("context_window"),
                "inference_seconds": assessment_data.get("inference_seconds"),
            })

            logger.info(
                "batch_building_done batch_id=%s osm_id=%s assessment_id=%s severity=%s source=orthophoto",
                batch_id, osm_id, assessment_id, assessment_data.get("severity"),
            )

        # ── Finalize batch ────────────────────────────────────────────────────
        elapsed = round(time.perf_counter() - started_at, 1)
        await _complete_batch(pool, batch_id, processed, failed, skipped)
        await _sync_site_progress(
            pool,
            site_id,
            status="completed",
            total_buildings=total_buildings,
        )

        await _push_event(batch_id, {
            "type": "batch_complete",
            "batch_id": batch_id,
            "site_name": site_name,
            "total": total_buildings,
            "processed": processed,
            "failed": failed,
            "skipped": skipped,
            "elapsed_seconds": elapsed,
        })
        await _push_event(batch_id, None)

        logger.info(
            "batch_complete batch_id=%s processed=%d failed=%d skipped=%d elapsed=%.1fs",
            batch_id,
            processed,
            failed,
            skipped,
            elapsed,
        )
    except asyncio.CancelledError:
        elapsed = round(time.perf_counter() - started_at, 1)
        logger.info("batch_canceled batch_id=%s", batch_id)
        await _fail_batch(pool, batch_id, processed, failed, skipped)
        await _sync_site_progress(pool, site_id, status="active")
        await _push_event(batch_id, {
            "type": "batch_failed",
            "batch_id": batch_id,
            "error": "Batch canceled by user",
            "processed": processed,
            "failed": failed,
            "skipped": skipped,
            "elapsed_seconds": elapsed,
            "timestamp": _utc_iso(),
        })
        await _push_event(batch_id, None)
        raise
    finally:
        BATCH_TASKS.pop(batch_id, None)


async def start_batch(
    ortho_upload_id: str,
    area_polygon: dict[str, Any],
    site_name: str,
    worker_name: str | None,
    force_reanalyze: bool,
    osm_id_filter: int | None = None,
) -> str:
    """Create a batch record, start background processing, return batch_id."""
    pool = get_pool()
    if not pool:
        raise RuntimeError("Database pool not available")

    batch_id = _generate_batch_id()
    area_polygon_geojson = json.dumps(area_polygon)
    site_id = await _resolve_or_create_site(pool, site_name, area_polygon_geojson)

    # Persist initial batch record with placeholder total (updated inside pipeline).
    await _create_batch_record(
        pool=pool,
        batch_id=batch_id,
        site_id=site_id,
        site_name=site_name,
        ortho_upload_id=ortho_upload_id,
        area_polygon_geojson=area_polygon_geojson,
        total_buildings=0,
        worker_name=worker_name,
        force_reanalyze=force_reanalyze,
    )

    batch_task = asyncio.create_task(
        run_orthophoto_batch(
            pool=pool,
            batch_id=batch_id,
            site_id=site_id,
            site_name=site_name,
            ortho_upload_id=ortho_upload_id,
            area_polygon=area_polygon,
            worker_name=worker_name,
            force_reanalyze=force_reanalyze,
            osm_id_filter=osm_id_filter,
        )
    )
    BATCH_TASKS[batch_id] = batch_task

    return batch_id


def cancel_batch(batch_id: str) -> bool:
    """Cancel a running batch task by batch ID."""
    task = BATCH_TASKS.get(batch_id)
    if not task or task.done():
        return False
    task.cancel()
    return True


def is_batch_active(batch_id: str) -> bool:
    """Return whether a batch task is currently active in memory."""
    task = BATCH_TASKS.get(batch_id)
    return bool(task and not task.done())


async def get_batch_status(batch_id: str) -> dict[str, Any] | None:
    """Fetch current batch record from DB."""
    pool = get_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                SELECT id, site_id, site_name, total_buildings, processed, failed,
                       skipped, status, worker_name, created_at, completed_at
                  FROM batches
                 WHERE id = $1
                """,
                batch_id,
            )
        except Exception as exc:
            # Backward-compat: older DBs may not have batches.site_id.
            if "site_id" not in str(exc).lower():
                raise
            logger.warning("batch_status_without_site_id_fallback error=%s", exc)
            row = await conn.fetchrow(
                """
                SELECT id, site_name, total_buildings, processed, failed,
                       skipped, status, worker_name, created_at, completed_at
                  FROM batches
                 WHERE id = $1
                """,
                batch_id,
            )
    if not row:
        return None
    return {
        "batch_id": str(row["id"]),
        "site_id": row.get("site_id"),
        "site_name": row["site_name"],
        "total_buildings": row["total_buildings"],
        "processed": row["processed"],
        "failed": row["failed"],
        "skipped": row["skipped"],
        "status": row["status"],
        "worker_name": row["worker_name"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
    }


async def find_covering_upload(lat: float, lon: float) -> str | None:
    """
    Return upload_id of the most recent orthophoto upload whose bounding box
    covers the given coordinate. Falls back to the most recent orthophoto
    upload regardless of bounds if no exact spatial match is found.
    """
    pool = get_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        # First: try exact bounds match for any raster upload.
        row = await conn.fetchrow(
            """
            SELECT id
              FROM uploads
             WHERE (cog_path IS NOT NULL OR saved_path LIKE '%.tif%' OR saved_path LIKE '%.tiff%')
               AND bounds_west IS NOT NULL
               AND $1 BETWEEN bounds_south AND bounds_north
               AND $2 BETWEEN bounds_west  AND bounds_east
             ORDER BY uploaded_at DESC
             LIMIT 1
            """,
            lat,
            lon,
        )
        if row:
            return str(row["id"])

        # Fallback: most recent raster upload regardless of bounds or type.
        row = await conn.fetchrow(
            """
            SELECT id
              FROM uploads
             WHERE cog_path IS NOT NULL
                OR saved_path LIKE '%.tif%'
                OR saved_path LIKE '%.tiff%'
                OR file_type IN ('drone_orthophoto', 'orthophoto', 'raster')
             ORDER BY uploaded_at DESC
             LIMIT 1
            """
        )
    return str(row["id"]) if row else None
