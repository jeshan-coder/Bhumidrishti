"""Background worker to dispatch uploads to the Gemma 4 pipeline."""

import asyncio
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

import cv2

from db.postgres import get_pool
from services.gemma_pipeline import run_assessment_agent
from services.gis import query_location_info_by_point

logger = logging.getLogger(__name__)

# Polling interval in seconds
POLL_INTERVAL = 10

# Configure root upload path as in routers
UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR", "/app/data/uploads")).resolve()
# This variable stores frame extraction frequency for video analysis.
FRAMES_PER_SECOND_EXTRACT = float(os.getenv("FRAMES_PER_SECOND_EXTRACT", "0.5"))
# This variable stores max frames sent to Gemma for one video.
MAX_FRAMES_TO_ANALYZE = int(os.getenv("MAX_FRAMES_TO_ANALYZE", "5"))
# This variable stores frame resolution for model input token efficiency.
FRAME_RESOLUTION_RAW = os.getenv("FRAME_RESOLUTION", "1024,1024")

# This variable stores allowed damage type values for strict photo assessment validation.
ALLOWED_DAMAGE_TYPES = {
    "no_visible_damage",
    "hairline_cracks",
    "structural_cracks",
    "facade_damage",
    "partial_wall_collapse",
    "roof_collapse",
    "partial_collapse",
    "full_collapse",
    "pancake_collapse",
    "lean_or_tilt",
    "fire_damage",
    "flood_damage",
}

# This variable stores allowed recommended actions for strict photo assessment validation.
ALLOWED_RECOMMENDED_ACTIONS = {
    "immediate_search_rescue",
    "urgent_evacuation",
    "evacuate_and_secure",
    "structural_assessment",
    "monitor",
    "no_action_needed",
}

# This variable stores allowed structural risk values from the photo assessment contract.
ALLOWED_STRUCTURAL_RISK = {"high", "moderate", "low", "unknown"}

# This variable stores allowed occupant status values for assessment consistency.
ALLOWED_OCCUPANT_STATUS = {
    "unknown",
    "potentially_trapped",
    "signs_of_life",
    "evacuated",
    "confirmed_clear",
    "trapped",
    "none_present",
}

# This variable stores allowed road access categories for routing and responder planning.
ALLOWED_ROAD_ACCESS = {"passable", "blocked", "unknown", "foot_only"}

# This variable lists required fields for minimum viable photo assessment persistence.
REQUIRED_SAVE_FIELDS = (
    "lat",
    "lon",
    "input_type",
    "photo_path",
    "severity",
    "damage_type",
    "recommended_action",
    "confidence",
    "reasoning",
)

# This variable lists strongly recommended fields that should normally be present.
STRONGLY_RECOMMENDED_FIELDS = (
    "damage_description",
    "structural_risk",
    "estimated_occupants",
    "occupant_status",
    "action_priority",
    "warnings",
    "turkish_summary",
    "building_type",
    "building_floors",
    "building_material",
    "road_access",
    "province",
    "district",
    "flood_zone",
    "elevation_m",
    "slope_degrees",
    "slope_risk",
    "nearest_shelter",
    "shelter_distance_m",
    "shelter_type",
    "nearest_road",
    "road_distance_m",
    "osm_building_id",
    "worker_name",
    "model_used",
    "inference_seconds",
    "status",
    "created_at",
    "updated_at",
)

# This variable stores live, in-memory analysis progress keyed by upload ID.
ANALYSIS_PROGRESS: dict[str, dict[str, Any]] = {}

# This variable maps upload ID to the running asyncio task for analysis.
ANALYSIS_TASKS: dict[str, asyncio.Task[Any]] = {}

# This variable maps task object ID to all upload IDs handled by that task.
ANALYSIS_TASK_UPLOAD_IDS: dict[int, set[str]] = {}


def _parse_frame_resolution(raw_resolution: str) -> tuple[int, int]:
    """Parse FRAME_RESOLUTION env value into (width, height)."""
    cleaned_resolution = (
        raw_resolution.strip()
        .replace("(", "")
        .replace(")", "")
        .replace(" ", "")
        .replace("x", ",")
        .replace("X", ",")
    )
    parts = [part for part in cleaned_resolution.split(",") if part]
    if len(parts) != 2:
        return (1024, 1024)
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError:
        return (1024, 1024)
    if width <= 0 or height <= 0:
        return (1024, 1024)
    return (width, height)


# This variable stores parsed frame resize dimensions used before Gemma inference.
FRAME_RESOLUTION = _parse_frame_resolution(FRAME_RESOLUTION_RAW)


def _extract_video_frames_with_ffmpeg(video_path: str, output_dir: Path, fps: float) -> list[str]:
    """Extract JPEG frames from a video file using FFmpeg at fixed FPS."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = str(output_dir / "frame_%04d.jpg")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_path,
        "-vf",
        f"fps={fps}",
        "-q:v",
        "3",
        output_pattern,
        "-y",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError(f"FFmpeg frame extraction failed: {result.stderr.strip() or result.stdout.strip()}")

    frame_paths = sorted(str(path) for path in output_dir.glob("frame_*.jpg"))
    return frame_paths


def _score_frame_sharpness(frame_path: str) -> float:
    """Score frame sharpness using Laplacian variance over grayscale image."""
    frame_image = cv2.imread(frame_path, cv2.IMREAD_GRAYSCALE)
    if frame_image is None:
        return 0.0
    laplacian = cv2.Laplacian(frame_image, cv2.CV_64F)
    return float(laplacian.var())


def _resize_frame_to_resolution(frame_path: str, resolution: tuple[int, int]) -> None:
    """Resize one frame image in-place to configured model input resolution."""
    frame_image = cv2.imread(frame_path)
    if frame_image is None:
        raise ValueError(f"Unable to read extracted frame: {frame_path}")
    resized_image = cv2.resize(frame_image, resolution, interpolation=cv2.INTER_AREA)
    cv2.imwrite(frame_path, resized_image)


def _select_and_prepare_video_frames(video_path: str, upload_id: str) -> list[str]:
    """Extract, rank, and resize top frames from a video for Gemma analysis."""
    # This variable stores per-upload frame directory for deterministic cleanup/reuse.
    frame_dir = UPLOAD_ROOT / "video_frames" / upload_id.replace("-", "_").lower()
    if frame_dir.exists():
        for existing_file in frame_dir.glob("*.jpg"):
            existing_file.unlink(missing_ok=True)

    extracted_frame_paths = _extract_video_frames_with_ffmpeg(
        video_path=video_path,
        output_dir=frame_dir,
        fps=max(FRAMES_PER_SECOND_EXTRACT, 0.1),
    )
    if not extracted_frame_paths:
        return []

    # This variable stores ranked frame paths by sharpness, highest first.
    ranked_frame_paths = sorted(
        extracted_frame_paths,
        key=_score_frame_sharpness,
        reverse=True,
    )
    target_frame_count = max(1, MAX_FRAMES_TO_ANALYZE)
    selected_frame_paths = ranked_frame_paths[:target_frame_count]

    if selected_frame_paths:
        while len(selected_frame_paths) < target_frame_count:
            selected_frame_paths.append(selected_frame_paths[-1])

    for frame_path in selected_frame_paths:
        _resize_frame_to_resolution(frame_path, FRAME_RESOLUTION)

    return selected_frame_paths


async def _set_upload_frames_extracted(pool, upload_id: str, frames_extracted: int) -> None:
    """Persist extracted-frame count for one upload row."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE uploads SET frames_extracted = $1 WHERE id = $2",
            frames_extracted,
            upload_id,
        )


def _safe_json(data: Any) -> str:
    """Serialize arbitrary payloads for debug logs without crashing."""
    try:
        return json.dumps(data, default=str, ensure_ascii=False)
    except Exception:
        return str(data)


def _log_pipeline_event(stage: str, payload: dict[str, Any]) -> None:
    """Emit one structured pipeline log line with stage and payload."""
    logger.info("pipeline_stage=%s payload=%s", stage, _safe_json(payload))


def _coerce_int(value: Any, field_name: str) -> int:
    """Coerce a value to integer and raise a clear validation error on failure."""
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _coerce_float(value: Any, field_name: str) -> float:
    """Coerce a value to float and raise a clear validation error on failure."""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a float") from exc


def _validate_photo_assessment_save_gate(
    *,
    lat: float,
    lon: float,
    input_type: str,
    photo_path: str | None,
    assessment_data: dict[str, Any],
) -> dict[str, Any]:
    """Validate and normalize photo assessment payload before DB insert."""
    # This variable stores normalized assessment values after type/range enforcement.
    normalized = dict(assessment_data)
    # This variable stores validation errors that must block DB save.
    errors: list[str] = []

    required_values = {
        "lat": lat,
        "lon": lon,
        "input_type": input_type,
        "photo_path": photo_path,
        "severity": normalized.get("severity"),
        "damage_type": normalized.get("damage_type"),
        "recommended_action": normalized.get("recommended_action"),
        "confidence": normalized.get("confidence"),
        "reasoning": normalized.get("reasoning"),
    }

    # This variable tracks missing required keys for fail-fast save gate behavior.
    missing_required_fields = [
        field_name
        for field_name in REQUIRED_SAVE_FIELDS
        if required_values.get(field_name) in (None, "")
    ]
    if missing_required_fields:
        errors.append(f"missing required fields: {', '.join(missing_required_fields)}")

    try:
        normalized_lat = _coerce_float(lat, "lat")
        if normalized_lat < 35.0 or normalized_lat > 42.0:
            errors.append("lat must be between 35.0 and 42.0")
    except ValueError as exc:
        errors.append(str(exc))

    try:
        normalized_lon = _coerce_float(lon, "lon")
        if normalized_lon < 25.0 or normalized_lon > 45.0:
            errors.append("lon must be between 25.0 and 45.0")
    except ValueError as exc:
        errors.append(str(exc))

    try:
        normalized_severity = _coerce_int(normalized.get("severity"), "severity")
        if normalized_severity not in {1, 2, 3, 4, 5}:
            errors.append("severity must be one of 1,2,3,4,5")
        normalized["severity"] = normalized_severity
    except ValueError as exc:
        errors.append(str(exc))

    if normalized.get("action_priority") is not None:
        try:
            normalized_action_priority = _coerce_int(normalized.get("action_priority"), "action_priority")
            if normalized_action_priority not in {1, 2, 3, 4, 5}:
                errors.append("action_priority must be one of 1,2,3,4,5")
            normalized["action_priority"] = normalized_action_priority
        except ValueError as exc:
            errors.append(str(exc))

    try:
        normalized_confidence = _coerce_float(normalized.get("confidence"), "confidence")
        if normalized_confidence < 0.30 or normalized_confidence > 0.95:
            errors.append("confidence must be between 0.30 and 0.95")
        normalized["confidence"] = normalized_confidence
    except ValueError as exc:
        errors.append(str(exc))

    damage_type = normalized.get("damage_type")
    if isinstance(damage_type, str):
        damage_type = damage_type.strip()
        normalized["damage_type"] = damage_type
    if damage_type and damage_type not in ALLOWED_DAMAGE_TYPES:
        errors.append(f"damage_type is invalid: {damage_type}")

    recommended_action = normalized.get("recommended_action")
    if isinstance(recommended_action, str):
        recommended_action = recommended_action.strip()
        normalized["recommended_action"] = recommended_action
    if recommended_action and recommended_action not in ALLOWED_RECOMMENDED_ACTIONS:
        errors.append(f"recommended_action is invalid: {recommended_action}")

    structural_risk = normalized.get("structural_risk")
    if structural_risk is not None and structural_risk not in ALLOWED_STRUCTURAL_RISK:
        errors.append(f"structural_risk is invalid: {structural_risk}")

    occupant_status = normalized.get("occupant_status")
    if occupant_status is not None and occupant_status not in ALLOWED_OCCUPANT_STATUS:
        errors.append(f"occupant_status is invalid: {occupant_status}")

    road_access = normalized.get("road_access")
    if road_access is not None and road_access not in ALLOWED_ROAD_ACCESS:
        errors.append(f"road_access is invalid: {road_access}")

    warnings_value = normalized.get("warnings")
    if warnings_value is not None:
        if not isinstance(warnings_value, list):
            errors.append("warnings must be a list")
        elif not all(isinstance(item, str) for item in warnings_value):
            errors.append("warnings must be a list of strings")

    if errors:
        _log_pipeline_event(
            "save_assessment_validation_failed",
            {
                "required_fields": REQUIRED_SAVE_FIELDS,
                "errors": errors,
                "lat": lat,
                "lon": lon,
                "input_type": input_type,
                "photo_path": photo_path,
                "assessment_data": normalized,
            },
        )
        raise ValueError("; ".join(errors))

    # This variable tracks missing recommended fields for non-blocking debug visibility.
    missing_recommended_fields = [
        field_name
        for field_name in STRONGLY_RECOMMENDED_FIELDS
        if normalized.get(field_name) in (None, "")
    ]
    if missing_recommended_fields:
        _log_pipeline_event(
            "save_assessment_validation_recommended_missing",
            {
                "missing_recommended_fields": missing_recommended_fields,
                "assessment_data": normalized,
            },
        )

    _log_pipeline_event(
        "save_assessment_validation_passed",
        {
            "required_fields": REQUIRED_SAVE_FIELDS,
            "missing_recommended_fields": missing_recommended_fields,
            "normalized_assessment_data": normalized,
        },
    )

    return normalized


def _utc_iso_now() -> str:
    """Return current UTC time as ISO string for progress snapshots."""
    return datetime.now(timezone.utc).isoformat()


def _build_progress_payload(
    upload_id: str,
    progress_percent: int,
    stage: str,
    thought: str,
    *,
    status: str = "processing",
    is_active: bool = True,
    assessment_id: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Build one normalized progress payload for ongoing AI analysis."""
    # This variable clamps progress for stable UI progress bar rendering.
    safe_progress_percent = max(0, min(100, progress_percent))
    return {
        "upload_id": upload_id,
        "status": status,
        "progress_percent": safe_progress_percent,
        "stage": stage,
        "thought": thought,
        "is_active": is_active,
        "assessment_id": assessment_id,
        "error_message": error_message,
        "updated_at": _utc_iso_now(),
    }


def set_analysis_progress_for_upload(
    upload_id: str,
    progress_percent: int,
    stage: str,
    thought: str,
    *,
    status: str = "processing",
    is_active: bool = True,
    assessment_id: str | None = None,
    error_message: str | None = None,
) -> None:
    """Store live analysis progress for a single upload ID."""
    ANALYSIS_PROGRESS[upload_id] = _build_progress_payload(
        upload_id=upload_id,
        progress_percent=progress_percent,
        stage=stage,
        thought=thought,
        status=status,
        is_active=is_active,
        assessment_id=assessment_id,
        error_message=error_message,
    )


def set_analysis_progress_for_uploads(
    upload_ids: list[str],
    progress_percent: int,
    stage: str,
    thought: str,
    *,
    status: str = "processing",
    is_active: bool = True,
    assessment_id: str | None = None,
    error_message: str | None = None,
) -> None:
    """Store the same live analysis progress state for many uploads."""
    for upload_id in upload_ids:
        set_analysis_progress_for_upload(
            upload_id=upload_id,
            progress_percent=progress_percent,
            stage=stage,
            thought=thought,
            status=status,
            is_active=is_active,
            assessment_id=assessment_id,
            error_message=error_message,
        )


def get_analysis_progress_for_upload(upload_id: str) -> dict[str, Any] | None:
    """Fetch current analysis progress snapshot for one upload if present."""
    progress = ANALYSIS_PROGRESS.get(upload_id)
    return dict(progress) if isinstance(progress, dict) else None


def get_active_analysis_progress() -> list[dict[str, Any]]:
    """Return active analysis progress payloads for all currently-running uploads."""
    return [
        dict(progress)
        for progress in ANALYSIS_PROGRESS.values()
        if isinstance(progress, dict) and progress.get("is_active")
    ]


def get_recent_analysis_progress(retain_seconds: int = 120) -> list[dict[str, Any]]:
    """Return active and recently-finished progress payloads for UI updates."""
    # This variable stores current UTC timestamp used for recency checks.
    now_utc = datetime.now(timezone.utc)
    # This variable stores normalized retention threshold for terminal progress snapshots.
    safe_retain_seconds = max(1, int(retain_seconds))
    recent_progress_items: list[dict[str, Any]] = []

    for progress in ANALYSIS_PROGRESS.values():
        if not isinstance(progress, dict):
            continue

        if progress.get("is_active"):
            recent_progress_items.append(dict(progress))
            continue

        updated_at_raw = progress.get("updated_at")
        if not isinstance(updated_at_raw, str):
            continue

        try:
            parsed_updated_at = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
        except ValueError:
            continue

        if parsed_updated_at.tzinfo is None:
            parsed_updated_at = parsed_updated_at.replace(tzinfo=timezone.utc)

        age_seconds = (now_utc - parsed_updated_at).total_seconds()
        if age_seconds <= safe_retain_seconds:
            recent_progress_items.append(dict(progress))

    return recent_progress_items


def clear_analysis_progress(upload_id: str) -> None:
    """Remove analysis progress snapshot for an upload ID."""
    ANALYSIS_PROGRESS.pop(upload_id, None)


def _register_analysis_task(upload_ids: list[str], task: asyncio.Task[Any]) -> None:
    """Register one running analysis task for all related upload IDs."""
    # This variable stores normalized upload IDs linked to one running task.
    normalized_upload_ids = [str(upload_id) for upload_id in upload_ids if upload_id]
    if not normalized_upload_ids:
        return

    # This variable stores stable key for reverse mapping and cleanup.
    task_key = id(task)
    ANALYSIS_TASK_UPLOAD_IDS[task_key] = set(normalized_upload_ids)
    for upload_id in normalized_upload_ids:
        ANALYSIS_TASKS[upload_id] = task

    def _cleanup_task_done(done_task: asyncio.Task[Any]) -> None:
        """Cleanup task mappings when analysis task completes or fails."""
        done_task_key = id(done_task)
        mapped_upload_ids = ANALYSIS_TASK_UPLOAD_IDS.pop(done_task_key, set())
        for mapped_upload_id in mapped_upload_ids:
            active_task = ANALYSIS_TASKS.get(mapped_upload_id)
            if active_task is done_task:
                ANALYSIS_TASKS.pop(mapped_upload_id, None)

    task.add_done_callback(_cleanup_task_done)


def is_upload_analysis_task_active(upload_id: str) -> bool:
    """Return whether a live asyncio analysis task is running for upload."""
    task = ANALYSIS_TASKS.get(upload_id)
    return bool(task and not task.done())


def trigger_upload_analysis_task(upload_id: str) -> bool:
    """Start a background analysis task for one upload if not already active."""
    if is_upload_analysis_task_active(upload_id):
        return False

    task = asyncio.create_task(analyze_upload(upload_id))
    _register_analysis_task([upload_id], task)
    return True


def trigger_upload_group_analysis_task(upload_ids: list[str]) -> list[str]:
    """Start one grouped background task and return upload IDs that were started."""
    # This variable deduplicates IDs and ignores currently active uploads.
    startable_upload_ids: list[str] = []
    seen_upload_ids: set[str] = set()
    for upload_id in upload_ids:
        if not upload_id:
            continue
        normalized_upload_id = str(upload_id)
        if normalized_upload_id in seen_upload_ids:
            continue
        seen_upload_ids.add(normalized_upload_id)
        if is_upload_analysis_task_active(normalized_upload_id):
            continue
        startable_upload_ids.append(normalized_upload_id)

    if not startable_upload_ids:
        return []

    task = asyncio.create_task(analyze_upload_group(startable_upload_ids))
    _register_analysis_task(startable_upload_ids, task)
    return startable_upload_ids


def cancel_analysis_task_for_upload(upload_id: str) -> list[str]:
    """Cancel running analysis task for an upload and return affected upload IDs."""
    task = ANALYSIS_TASKS.get(upload_id)
    if task is None or task.done():
        return []

    # This variable stores all uploads linked to the same grouped task.
    affected_upload_ids = list(ANALYSIS_TASK_UPLOAD_IDS.get(id(task), {upload_id}))
    task.cancel()
    return affected_upload_ids


async def generate_assessment_id() -> str:
    """Generate a unique ID for an assessment."""
    return f"ASS-{str(uuid.uuid4().int)[:6]}"


async def save_ai_assessment(
    pool,
    lat: float,
    lon: float,
    input_type: str,
    photo_path: str | None,
    field_note: str | None,
    assessment_data: dict[str, Any],
    extra_fields: dict[str, Any] | None = None,
) -> str:
    """Save a single AI-generated assessment row into the assessments table."""
    _log_pipeline_event(
        "save_assessment_started",
        {
            "lat": lat,
            "lon": lon,
            "input_type": input_type,
            "photo_path": photo_path,
            "field_note": field_note,
            "assessment_data": assessment_data,
        },
    )

    # This variable stores assessment payload normalized by strict save gate checks.
    normalized_assessment_data = _validate_photo_assessment_save_gate(
        lat=lat,
        lon=lon,
        input_type=input_type,
        photo_path=photo_path,
        assessment_data=assessment_data,
    )

    # This variable stores the generated assessment identifier used as primary key.
    assessment_id = await generate_assessment_id()

    # This variable stores administrative location fields persisted with assessment rows.
    province_value = normalized_assessment_data.get("province")
    district_value = normalized_assessment_data.get("district")
    address_note_value = normalized_assessment_data.get("address_note") or field_note

    if not province_value or not district_value:
        try:
            # This variable stores GIS-derived location context when model output misses admin fields.
            location_info = await query_location_info_by_point(lat=lat, lon=lon, db=pool)
            if not province_value and location_info.province:
                province_value = location_info.province
            if not district_value and location_info.district:
                district_value = location_info.district
            _log_pipeline_event(
                "save_assessment_location_context_enriched",
                {
                    "assessment_id": assessment_id,
                    "province": province_value,
                    "district": district_value,
                    "address_note": address_note_value,
                    "location_found": location_info.found,
                },
            )
        except Exception as exc:
            _log_pipeline_event(
                "save_assessment_location_context_failed",
                {
                    "assessment_id": assessment_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    if isinstance(province_value, str):
        province_value = province_value.strip() or None
    if isinstance(district_value, str):
        district_value = district_value.strip() or None
    if isinstance(address_note_value, str):
        address_note_value = address_note_value.strip() or None

    # Merge extra_fields (orthophoto-specific) into normalized data for lookup below.
    extra = extra_fields or {}

    sql_insert = """
        INSERT INTO assessments (
            id, lat, lon, geom, input_type, photo_path,
            severity, damage_type, damage_description, structural_risk,
            building_type, building_floors, building_material,
            estimated_occupants, occupant_status, recommended_action,
            action_priority, flood_zone, elevation_m, slope_degrees,
            slope_risk, nearest_shelter, shelter_distance_m, shelter_type,
            road_access, nearest_road, road_distance_m, reasoning,
            confidence, turkish_summary, field_note,
            province, district, address_note, status,
            osm_building_id, batch_id, chip_path,
            site_id, site_name, pre_chip_path,
            building_area_m2, building_width_m, building_height_m
        ) VALUES (
            $1, $2, $3,
            COALESCE(
                -- 1. Exact OSM-ID lookup (batch/single-building path — osm_building_id = $34).
                --    turkey_buildings.geom is MULTIPOLYGON EPSG:4326 — same SRID as assessments.geom.
                (
                    SELECT tb.geom
                    FROM turkey_buildings AS tb
                    WHERE $34::bigint IS NOT NULL
                      AND tb.osm_id = $34::bigint
                    LIMIT 1
                ),
                -- 2. Spatial containment: building whose polygon contains the centroid point.
                (
                    SELECT tb.geom
                    FROM turkey_buildings AS tb
                    WHERE ST_Contains(
                        tb.geom,
                        ST_SetSRID(ST_MakePoint($3, $2), 4326)
                    )
                    LIMIT 1
                ),
                -- 3. Nearest building within 30 m (handles slight centroid offsets).
                (
                    SELECT tb.geom
                    FROM turkey_buildings AS tb
                    WHERE ST_DWithin(
                        tb.geom::geography,
                        ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography,
                        30
                    )
                    ORDER BY ST_Distance(
                        tb.geom::geography,
                        ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography
                    ) ASC
                    LIMIT 1
                ),
                -- 4. Absolute fallback: store centroid as Point (EPSG:4326).
                ST_SetSRID(ST_MakePoint($3, $2), 4326)
            ),
            $4, $5,
            $6, $7, $8, $9,
            $10, $11, $12,
            $13, $14, $15,
            $16, $17, $18, $19,
            $20, $21, $22, $23,
            $24, $25, $26, $27,
            $28, $29, $30,
            $31, $32, $33, 'pending',
            $34, $35, $36,
            $37, $38, $39,
            $40, $41, $42
        )
    """

    _log_pipeline_event(
        "save_assessment_db_insert_payload",
        {
            "assessment_id": assessment_id,
            "lat": lat,
            "lon": lon,
            "input_type": input_type,
            "photo_path": photo_path,
            "field_note": field_note,
            "assessment_data": normalized_assessment_data,
        },
    )

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                sql_insert,
                assessment_id,
                lat,
                lon,
                input_type,
                photo_path,
                normalized_assessment_data.get("severity"),
                normalized_assessment_data.get("damage_type"),
                normalized_assessment_data.get("damage_description"),
                normalized_assessment_data.get("structural_risk"),
                normalized_assessment_data.get("building_type"),
                normalized_assessment_data.get("building_floors"),
                normalized_assessment_data.get("building_material"),
                normalized_assessment_data.get("estimated_occupants"),
                normalized_assessment_data.get("occupant_status"),
                normalized_assessment_data.get("recommended_action"),
                normalized_assessment_data.get("action_priority"),
                normalized_assessment_data.get("flood_zone", False),
                normalized_assessment_data.get("elevation_m"),
                normalized_assessment_data.get("slope_degrees"),
                normalized_assessment_data.get("slope_risk"),
                normalized_assessment_data.get("nearest_shelter"),
                normalized_assessment_data.get("shelter_distance_m"),
                normalized_assessment_data.get("shelter_type"),
                normalized_assessment_data.get("road_access"),
                normalized_assessment_data.get("nearest_road"),
                normalized_assessment_data.get("road_distance_m"),
                normalized_assessment_data.get("reasoning"),
                normalized_assessment_data.get("confidence"),
                normalized_assessment_data.get("turkish_summary"),
                field_note,
                province_value,
                district_value,
                address_note_value,
                # Extra orthophoto fields ($34–$42)
                extra.get("osm_building_id") or normalized_assessment_data.get("osm_building_id"),
                extra.get("batch_id") or normalized_assessment_data.get("batch_id"),
                extra.get("chip_path"),
                extra.get("site_id"),
                extra.get("site_name"),
                extra.get("pre_chip_path"),
                extra.get("building_area_m2"),
                extra.get("building_width_m"),
                extra.get("building_height_m"),
            )
    except Exception as exc:
        # Backward-compat: older DBs may not have assessments.site_id yet.
        if "site_id" in str(exc).lower():
            logger.warning("assessment_insert_without_site_id_fallback error=%s", exc)
            legacy_sql_insert = """
                INSERT INTO assessments (
                    id, lat, lon, geom, input_type, photo_path,
                    severity, damage_type, damage_description, structural_risk,
                    building_type, building_floors, building_material,
                    estimated_occupants, occupant_status, recommended_action,
                    action_priority, flood_zone, elevation_m, slope_degrees,
                    slope_risk, nearest_shelter, shelter_distance_m, shelter_type,
                    road_access, nearest_road, road_distance_m, reasoning,
                    confidence, turkish_summary, field_note,
                    province, district, address_note, status,
                    osm_building_id, batch_id, chip_path,
                    site_name, pre_chip_path,
                    building_area_m2, building_width_m, building_height_m
                ) VALUES (
                    $1, $2, $3,
                    COALESCE(
                        (
                            SELECT tb.geom
                            FROM turkey_buildings AS tb
                            WHERE $34::bigint IS NOT NULL
                              AND tb.osm_id = $34::bigint
                            LIMIT 1
                        ),
                        (
                            SELECT tb.geom
                            FROM turkey_buildings AS tb
                            WHERE ST_Contains(
                                tb.geom,
                                ST_SetSRID(ST_MakePoint($3, $2), 4326)
                            )
                            LIMIT 1
                        ),
                        (
                            SELECT tb.geom
                            FROM turkey_buildings AS tb
                            WHERE ST_DWithin(
                                tb.geom::geography,
                                ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography,
                                30
                            )
                            ORDER BY ST_Distance(
                                tb.geom::geography,
                                ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography
                            ) ASC
                            LIMIT 1
                        ),
                        ST_SetSRID(ST_MakePoint($3, $2), 4326)
                    ),
                    $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12,
                    $13, $14, $15,
                    $16, $17, $18, $19,
                    $20, $21, $22, $23,
                    $24, $25, $26, $27,
                    $28, $29, $30,
                    $31, $32, $33, 'pending',
                    $34, $35, $36,
                    $37, $38,
                    $39, $40, $41
                )
            """
            async with pool.acquire() as conn:
                await conn.execute(
                    legacy_sql_insert,
                    assessment_id,
                    lat,
                    lon,
                    input_type,
                    photo_path,
                    normalized_assessment_data.get("severity"),
                    normalized_assessment_data.get("damage_type"),
                    normalized_assessment_data.get("damage_description"),
                    normalized_assessment_data.get("structural_risk"),
                    normalized_assessment_data.get("building_type"),
                    normalized_assessment_data.get("building_floors"),
                    normalized_assessment_data.get("building_material"),
                    normalized_assessment_data.get("estimated_occupants"),
                    normalized_assessment_data.get("occupant_status"),
                    normalized_assessment_data.get("recommended_action"),
                    normalized_assessment_data.get("action_priority"),
                    normalized_assessment_data.get("flood_zone", False),
                    normalized_assessment_data.get("elevation_m"),
                    normalized_assessment_data.get("slope_degrees"),
                    normalized_assessment_data.get("slope_risk"),
                    normalized_assessment_data.get("nearest_shelter"),
                    normalized_assessment_data.get("shelter_distance_m"),
                    normalized_assessment_data.get("shelter_type"),
                    normalized_assessment_data.get("road_access"),
                    normalized_assessment_data.get("nearest_road"),
                    normalized_assessment_data.get("road_distance_m"),
                    normalized_assessment_data.get("reasoning"),
                    normalized_assessment_data.get("confidence"),
                    normalized_assessment_data.get("turkish_summary"),
                    field_note,
                    province_value,
                    district_value,
                    address_note_value,
                    extra.get("osm_building_id") or normalized_assessment_data.get("osm_building_id"),
                    extra.get("batch_id") or normalized_assessment_data.get("batch_id"),
                    extra.get("chip_path"),
                    extra.get("site_name"),
                    extra.get("pre_chip_path"),
                    extra.get("building_area_m2"),
                    extra.get("building_width_m"),
                    extra.get("building_height_m"),
                )
            _log_pipeline_event(
                "save_assessment_db_insert_legacy_site_id_fallback_succeeded",
                {"assessment_id": assessment_id},
            )
            return assessment_id
        _log_pipeline_event(
            "save_assessment_db_insert_failed",
            {
                "assessment_id": assessment_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "assessment_data": normalized_assessment_data,
            },
        )
        raise

    _log_pipeline_event(
        "save_assessment_db_insert_succeeded",
        {
            "assessment_id": assessment_id,
            "status": "pending",
        },
    )

    return assessment_id


async def process_upload(upload_row: dict[str, Any], pool) -> None:
    """Process a single upload through Gemma 4."""
    upload_id = upload_row["id"]
    file_type = upload_row["file_type"]
    saved_path = upload_row["saved_path"]
    lat = upload_row["lat"]
    lon = upload_row["lon"]
    field_note = upload_row["field_note"]
    # This variable keeps the current upload ID in list form for shared progress helpers.
    upload_ids = [upload_id]

    _log_pipeline_event(
        "process_upload_start",
        {
            "upload_id": upload_id,
            "upload_row": upload_row,
            "photo_only_pipeline": True,
        },
    )
    set_analysis_progress_for_uploads(
        upload_ids,
        progress_percent=15,
        stage="prepare_input",
        thought="Preparing image for AI analysis.",
    )

    absolute_path = str(UPLOAD_ROOT.parent / saved_path) if saved_path else ""
    _log_pipeline_event(
        "process_upload_path_resolved",
        {
            "upload_id": upload_id,
            "saved_path": saved_path,
            "absolute_path": absolute_path,
            "exists": os.path.exists(absolute_path),
        },
    )
    if not os.path.exists(absolute_path):
        logger.error(f"File not found on disk: {absolute_path}")
        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=100,
            stage="failed",
            thought="Input image file was not found.",
            status="failed",
            is_active=False,
            error_message="File not found on disk",
        )
        await set_upload_status(pool, upload_id, "failed", error_message="File not found on disk")
        return

    # This variable stores image inputs prepared for Gemma assessment (photos or selected video frames).
    image_paths = [absolute_path] if file_type in ("ground_photo", "drone_image", "video_frame") else []
    if file_type == "video":
        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=20,
            stage="extract_frames",
            thought="Extracting video frames with FFmpeg.",
        )
        selected_video_frames = await asyncio.to_thread(
            _select_and_prepare_video_frames,
            absolute_path,
            upload_id,
        )
        image_paths = selected_video_frames
        await _set_upload_frames_extracted(pool, upload_id, len(selected_video_frames))

    _log_pipeline_event(
        "process_upload_image_inputs",
        {
            "upload_id": upload_id,
            "file_type": file_type,
            "image_paths": image_paths,
            "image_count": len(image_paths),
        },
    )

    if not image_paths:
        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=100,
            stage="failed",
            thought="No supported image found for AI analysis.",
            status="failed",
            is_active=False,
            error_message="No images extracted or supported for analysis.",
        )
        await set_upload_status(pool, upload_id, "failed", error_message="No images extracted or supported for analysis.")
        return

    try:
        _log_pipeline_event(
            "process_upload_agent_start",
            {
                "upload_id": upload_id,
                "lat": lat,
                "lon": lon,
                "file_type": file_type,
                "field_note": field_note,
                "image_paths": image_paths,
            },
        )
        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=30,
            stage="ai_reasoning",
            thought="AI is reviewing the uploaded photo.",
        )

        async def _on_agent_progress(event: dict[str, Any]) -> None:
            """Bridge Gemma loop events into upload progress updates."""
            event_stage = str(event.get("stage") or "ai_reasoning")
            event_thought = str(event.get("thought") or "AI is analyzing the scene.")
            if event_stage == "ai_reasoning_stream":
                thinking_text = event.get("thinking_text")
                if isinstance(thinking_text, str) and thinking_text.strip():
                    event_thought = thinking_text[-480:]
            elif event_stage == "ai_response_stream":
                response_text = event.get("response_text")
                if isinstance(response_text, str) and response_text.strip():
                    event_thought = response_text[-480:]
            event_progress = int(event.get("progress_percent") or 35)
            _log_pipeline_event(
                "process_upload_agent_progress",
                {
                    "upload_id": upload_id,
                    "event": event,
                    "event_stage": event_stage,
                    "event_progress": event_progress,
                },
            )
            set_analysis_progress_for_uploads(
                upload_ids,
                progress_percent=event_progress,
                stage=event_stage,
                thought=event_thought,
            )

        # Run agent loop
        assessment_data = await run_assessment_agent(
            image_paths=image_paths,
            lat=lat,
            lon=lon,
            input_type=file_type,
            db=pool,
            field_note=field_note,
            progress_callback=_on_agent_progress,
        )
        _log_pipeline_event(
            "process_upload_agent_done",
            {
                "upload_id": upload_id,
                "assessment_data": assessment_data,
            },
        )

        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=85,
            stage="save_assessment",
            thought="Saving AI assessment to database.",
        )

        # This helper persists AI output into the assessments table.
        assessment_id = await save_ai_assessment(
            pool=pool,
            lat=lat,
            lon=lon,
            input_type=file_type,
            photo_path=saved_path,
            field_note=field_note,
            assessment_data=assessment_data,
        )
        _log_pipeline_event(
            "process_upload_assessment_saved",
            {
                "upload_id": upload_id,
                "assessment_id": assessment_id,
                "lat": lat,
                "lon": lon,
                "file_type": file_type,
                "saved_path": saved_path,
                "field_note": field_note,
            },
        )

        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=95,
            stage="link_upload",
            thought="Linking upload record with the new assessment.",
        )

        # Update uploads table
        sql_update = """
            UPDATE uploads 
            SET status = 'done', is_analyzed = TRUE, assessment_id = $1, processing_done_at = NOW() 
            WHERE id = $2
        """
        async with pool.acquire() as conn:
            await conn.execute(sql_update, assessment_id, upload_id)
        _log_pipeline_event(
            "process_upload_upload_row_updated",
            {
                "upload_id": upload_id,
                "assessment_id": assessment_id,
                "new_status": "done",
                "is_analyzed": True,
            },
        )

        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=100,
            stage="completed",
            thought="Analysis complete.",
            status="done",
            is_active=False,
            assessment_id=assessment_id,
        )

        logger.info(f"Successfully processed upload {upload_id} -> Assessment {assessment_id}")
        _log_pipeline_event(
            "process_upload_completed",
            {
                "upload_id": upload_id,
                "assessment_id": assessment_id,
            },
        )

    except Exception as e:
        logger.error(f"Error processing upload {upload_id}: {e}")
        _log_pipeline_event(
            "process_upload_failed",
            {
                "upload_id": upload_id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )
        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=100,
            stage="failed",
            thought="AI analysis failed.",
            status="failed",
            is_active=False,
            error_message=str(e),
        )
        await set_upload_status(pool, upload_id, "failed", error_message=str(e))


async def process_upload_group(upload_rows: list[dict[str, Any]], pool) -> None:
    """Process a location-grouped set of uploads as one AI assessment."""
    if not upload_rows:
        return

    # This variable stores upload IDs for all rows in the incoming group.
    upload_ids = [str(row["id"]) for row in upload_rows]
    # This variable stores all on-disk image paths used as model input.
    image_paths: list[str] = []
    # This variable stores representative photo path persisted in assessments table.
    representative_photo_path: str | None = None

    set_analysis_progress_for_uploads(
        upload_ids,
        progress_percent=15,
        stage="prepare_batch",
        thought="Preparing grouped photos for AI analysis.",
    )

    for row in upload_rows:
        saved_path = row.get("saved_path")
        file_type = row.get("file_type")
        upload_id = str(row.get("id") or "")
        absolute_path = str(UPLOAD_ROOT.parent / saved_path) if isinstance(saved_path, str) else ""
        if file_type in ("ground_photo", "drone_image", "video_frame") and absolute_path and os.path.exists(absolute_path):
            image_paths.append(absolute_path)
            if representative_photo_path is None and isinstance(saved_path, str):
                representative_photo_path = saved_path
        if file_type == "video" and upload_id and absolute_path and os.path.exists(absolute_path):
            set_analysis_progress_for_uploads(
                upload_ids,
                progress_percent=20,
                stage="extract_frames",
                thought="Extracting and selecting sharp frames from grouped videos.",
            )
            selected_video_frames = await asyncio.to_thread(
                _select_and_prepare_video_frames,
                absolute_path,
                upload_id,
            )
            image_paths.extend(selected_video_frames)
            await _set_upload_frames_extracted(pool, upload_id, len(selected_video_frames))
            if representative_photo_path is None and isinstance(saved_path, str):
                representative_photo_path = saved_path

    if not image_paths:
        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=100,
            stage="failed",
            thought="No valid images found in this location group.",
            status="failed",
            is_active=False,
            error_message="No valid images found for grouped analysis.",
        )
        for upload_id in upload_ids:
            await set_upload_status(pool, upload_id, "failed", error_message="No valid images found for grouped analysis.")
        return

    first_row = upload_rows[0]
    lat = float(first_row["lat"])
    lon = float(first_row["lon"])
    input_type = str(first_row["file_type"])

    # This variable merges available field notes so model sees full context for this location.
    merged_notes = [str(row.get("field_note")).strip() for row in upload_rows if row.get("field_note")]
    field_note = " | ".join([note for note in merged_notes if note]) if merged_notes else None

    try:
        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=30,
            stage="ai_reasoning",
            thought="AI is analyzing grouped photos from this location.",
        )

        async def _on_agent_progress(event: dict[str, Any]) -> None:
            """Bridge Gemma loop events into grouped-upload progress updates."""
            event_stage = str(event.get("stage") or "ai_reasoning")
            event_thought = str(event.get("thought") or "AI is analyzing grouped photos.")
            if event_stage == "ai_reasoning_stream":
                thinking_text = event.get("thinking_text")
                if isinstance(thinking_text, str) and thinking_text.strip():
                    event_thought = thinking_text[-480:]
            elif event_stage == "ai_response_stream":
                response_text = event.get("response_text")
                if isinstance(response_text, str) and response_text.strip():
                    event_thought = response_text[-480:]
            event_progress = int(event.get("progress_percent") or 35)
            set_analysis_progress_for_uploads(
                upload_ids,
                progress_percent=event_progress,
                stage=event_stage,
                thought=event_thought,
            )

        assessment_data = await run_assessment_agent(
            image_paths=image_paths,
            lat=lat,
            lon=lon,
            input_type=input_type,
            db=pool,
            field_note=field_note,
            progress_callback=_on_agent_progress,
        )

        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=85,
            stage="save_assessment",
            thought="Saving grouped assessment to database.",
        )

        assessment_id = await save_ai_assessment(
            pool=pool,
            lat=lat,
            lon=lon,
            input_type=input_type,
            photo_path=representative_photo_path,
            field_note=field_note,
            assessment_data=assessment_data,
        )

        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=95,
            stage="link_uploads",
            thought="Linking all grouped uploads to one assessment.",
        )

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE uploads
                SET status = 'done',
                    is_analyzed = TRUE,
                    assessment_id = $1,
                    processing_done_at = NOW()
                WHERE id = ANY($2::text[])
                """,
                assessment_id,
                upload_ids,
            )

        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=100,
            stage="completed",
            thought="Grouped analysis complete.",
            status="done",
            is_active=False,
            assessment_id=assessment_id,
        )
    except asyncio.CancelledError:
        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=100,
            stage="canceled",
            thought="Grouped analysis canceled by user.",
            status="failed",
            is_active=False,
            error_message="Grouped analysis canceled by user.",
        )
        pool = get_pool()
        if pool:
            for upload_id in upload_ids:
                await set_upload_status(pool, upload_id, "failed", error_message="Grouped analysis canceled by user.")
        raise
    except Exception as exc:
        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=100,
            stage="failed",
            thought="Grouped AI analysis failed.",
            status="failed",
            is_active=False,
            error_message=str(exc),
        )
        for upload_id in upload_ids:
            await set_upload_status(pool, upload_id, "failed", error_message=str(exc))


async def set_upload_status(pool, upload_id: str, status: str, error_message: str | None = None) -> None:
    sql = """
        UPDATE uploads 
        SET status = $1, error_message = $2
        WHERE id = $3
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, status, error_message, upload_id)


async def analyze_upload(upload_id: str) -> None:
    """Analyze a specific upload immediately."""
    logger.info(f"Triggering manual analysis for upload {upload_id}")
    _log_pipeline_event(
        "analyze_upload_start",
        {
            "upload_id": upload_id,
            "photo_only_pipeline": True,
        },
    )
    set_analysis_progress_for_upload(
        upload_id,
        progress_percent=5,
        stage="queued",
        thought="Queued for AI analysis.",
    )
    try:
        pool = get_pool()
        _log_pipeline_event(
            "analyze_upload_pool_check",
            {
                "upload_id": upload_id,
                "pool_available": bool(pool),
            },
        )
        if not pool:
            logger.error("No database pool available.")
            set_analysis_progress_for_upload(
                upload_id,
                progress_percent=100,
                stage="failed",
                thought="Database pool not available.",
                status="failed",
                is_active=False,
                error_message="Database pool not available.",
            )
            return

        query = """
            SELECT id, file_type, saved_path, lat, lon, field_note 
            FROM uploads 
            WHERE id = $1 AND is_analyzed = FALSE
        """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, upload_id)
        _log_pipeline_event(
            "analyze_upload_db_row_fetched",
            {
                "upload_id": upload_id,
                "row_found": bool(row),
                "row": dict(row) if row else None,
            },
        )
        
        if row:
            upload_dict = dict(row)
            _log_pipeline_event(
                "analyze_upload_preprocess_payload",
                {
                    "upload_id": upload_id,
                    "upload_dict": upload_dict,
                },
            )
            
            # Mark as processing
            async with pool.acquire() as conn:
                await conn.execute("UPDATE uploads SET status = 'processing', processing_started_at = NOW() WHERE id = $1", upload_id)
            _log_pipeline_event(
                "analyze_upload_marked_processing",
                {
                    "upload_id": upload_id,
                    "new_status": "processing",
                },
            )

            await process_upload(upload_dict, pool)
            _log_pipeline_event(
                "analyze_upload_process_upload_returned",
                {
                    "upload_id": upload_id,
                },
            )
        else:
            logger.warning(f"Upload {upload_id} not found or already analyzed.")
            set_analysis_progress_for_upload(
                upload_id,
                progress_percent=100,
                stage="failed",
                thought="Upload not found or already analyzed.",
                status="failed",
                is_active=False,
                error_message="Upload not found or already analyzed.",
            )

    except asyncio.CancelledError:
        logger.info(f"Analysis canceled for upload {upload_id}")
        _log_pipeline_event(
            "analyze_upload_canceled",
            {
                "upload_id": upload_id,
            },
        )
        set_analysis_progress_for_upload(
            upload_id,
            progress_percent=100,
            stage="canceled",
            thought="Analysis canceled by user.",
            status="failed",
            is_active=False,
            error_message="Analysis canceled by user.",
        )
        pool = get_pool()
        if pool:
            await set_upload_status(pool, upload_id, "failed", error_message="Analysis canceled by user.")
            _log_pipeline_event(
                "analyze_upload_canceled_status_updated",
                {
                    "upload_id": upload_id,
                    "new_status": "failed",
                    "error_message": "Analysis canceled by user.",
                },
            )
        raise
    except Exception as e:
        logger.error(f"Error in manual analysis for upload {upload_id}: {e}")
        _log_pipeline_event(
            "analyze_upload_failed",
            {
                "upload_id": upload_id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )
        set_analysis_progress_for_upload(
            upload_id,
            progress_percent=100,
            stage="failed",
            thought="Background analysis task crashed.",
            status="failed",
            is_active=False,
            error_message=str(e),
        )


async def analyze_upload_group(upload_ids: list[str]) -> None:
    """Analyze multiple uploads from one location as a single AI batch."""
    if not upload_ids:
        return

    set_analysis_progress_for_uploads(
        upload_ids,
        progress_percent=5,
        stage="queued",
        thought="Queued grouped photos for AI analysis.",
    )

    try:
        pool = get_pool()
        if not pool:
            set_analysis_progress_for_uploads(
                upload_ids,
                progress_percent=100,
                stage="failed",
                thought="Database pool not available.",
                status="failed",
                is_active=False,
                error_message="Database pool not available.",
            )
            return

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, file_type, saved_path, lat, lon, field_note
                FROM uploads
                WHERE id = ANY($1::text[])
                  AND is_analyzed = FALSE
                """,
                upload_ids,
            )

        if not rows:
            set_analysis_progress_for_uploads(
                upload_ids,
                progress_percent=100,
                stage="failed",
                thought="No eligible uploads found for grouped analysis.",
                status="failed",
                is_active=False,
                error_message="No eligible uploads found for grouped analysis.",
            )
            return

        rows_as_dict = [dict(row) for row in rows]
        matched_ids = [str(row["id"]) for row in rows_as_dict]

        set_analysis_progress_for_uploads(
            matched_ids,
            progress_percent=10,
            stage="start_processing",
            thought="Starting grouped AI analysis.",
        )

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE uploads
                SET status = 'processing', processing_started_at = NOW()
                WHERE id = ANY($1::text[])
                """,
                matched_ids,
            )

        await process_upload_group(rows_as_dict, pool)
    except Exception as exc:
        set_analysis_progress_for_uploads(
            upload_ids,
            progress_percent=100,
            stage="failed",
            thought="Grouped background analysis task crashed.",
            status="failed",
            is_active=False,
            error_message=str(exc),
        )
