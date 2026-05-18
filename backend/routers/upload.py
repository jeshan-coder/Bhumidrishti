"""Uploads router — saves files to disk and records every upload in DB.

Endpoints
---------
POST   /uploads/ground-photo    Upload a ground photo (JPG/PNG)
POST   /uploads/orthophoto      Upload an orthophoto / drone image (GeoTIFF, JPG, PNG)
POST   /uploads/video           Upload a video (MP4, MOV)
GET    /uploads                 List all uploads (paginated, filterable)
GET    /uploads/{upload_id}     Get a single upload record
DELETE /uploads/{upload_id}     Delete upload record (and optionally the file)
"""

import asyncio
import json
import logging
import os
import random
import string
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel

from db.postgres import get_pool
from models.upload import Upload, UploadCreate, UploadListResponse
from services.pipeline_worker import (
    cancel_analysis_task_for_upload,
    get_analysis_progress_for_upload,
    get_recent_analysis_progress,
    is_upload_analysis_task_active,
    trigger_upload_analysis_task,
    trigger_upload_group_analysis_task,
)

router = APIRouter(prefix="/uploads", tags=["uploads"])
logger = logging.getLogger(__name__)
TITILER_URL = os.getenv("TITILER_INTERNAL_URL", "http://titiler:80")


# This class defines grouped analysis payload for one location's uploads.
class LocationAnalyzeRequest(BaseModel):
    """Request model used to trigger grouped AI analysis by upload IDs."""

    # This variable stores upload IDs selected from the same location group.
    upload_ids: list[str]

# ── Storage config ──────────────────────────────────────────────────────────

UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR", "/app/data/uploads")).resolve()
MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
# This variable stores max allowed uploaded video size in megabytes.
MAX_VIDEO_SIZE_MB = int(os.getenv("MAX_VIDEO_SIZE_MB", "500"))
# This variable stores max allowed uploaded video size in bytes.
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024
# This variable stores max allowed uploaded video duration in seconds.
MAX_VIDEO_DURATION_SECONDS = float(os.getenv("MAX_VIDEO_DURATION_SECONDS", "300"))

ALLOWED_GROUND_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png"}
ALLOWED_ORTHO_EXTENSIONS: set[str] = {".tif", ".tiff", ".geotiff", ".cog", ".jpg", ".jpeg", ".png"}
ALLOWED_VIDEO_EXTENSIONS: set[str] = {".mp4", ".mov"}

MIME_MAP: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".geotiff": "image/tiff",
    ".cog": "image/tiff",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}

FOLDER_MAP: dict[str, str] = {
    "ground_photo": "photo",
    "drone_orthophoto": "orthophoto",
    "drone_image": "orthophoto",
    "video": "video",
    "video_frame": "video_frames",
}


# ── Utilities ───────────────────────────────────────────────────────────────

def _generate_upload_id() -> str:
    """Generate a short upload ID in the form UPL-XXXXX (alphanumeric)."""
    suffix = "".join(random.choices(string.digits, k=4))
    return f"UPL-{suffix}"


def _success(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data, "error": None}


def _error(message: str) -> dict[str, Any]:
    return {"success": False, "data": None, "error": message}


def _ext(filename: str | None) -> str:
    if not filename:
        return ""
    return Path(filename).suffix.lower()


def _parse_coord(raw: str | None, lo: float, hi: float) -> float | None:
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    val = float(stripped)
    if val < lo or val > hi:
        raise ValueError(f"Coordinate {val} out of range [{lo}, {hi}]")
    return val


def _validate_image(file_bytes: bytes) -> dict[str, Any]:
    with Image.open(BytesIO(file_bytes)) as img:
        img.verify()
    with Image.open(BytesIO(file_bytes)) as img:
        w, h = img.size
        fmt = img.format or "UNKNOWN"
    return {"format": fmt, "width": w, "height": h}


# This function extracts WGS84 bounds and centroid from GeoTIFF/COG bytes when available.
def _extract_raster_geo_bounds(file_bytes: bytes) -> dict[str, float] | None:
    try:
        from rasterio.io import MemoryFile
        from rasterio.warp import transform_bounds
    except Exception:
        return None

    try:
        with MemoryFile(file_bytes) as mem:
            with mem.open() as dataset:
                if dataset.crs is None:
                    return None

                raw_bounds = dataset.bounds
                west = float(raw_bounds.left)
                south = float(raw_bounds.bottom)
                east = float(raw_bounds.right)
                north = float(raw_bounds.top)

                if str(dataset.crs).upper() not in {"EPSG:4326", "OGC:CRS84"}:
                    west, south, east, north = transform_bounds(
                        dataset.crs,
                        "EPSG:4326",
                        west,
                        south,
                        east,
                        north,
                        densify_pts=21,
                    )

                center_lat = (south + north) / 2.0
                center_lon = (west + east) / 2.0
                return {
                    "bounds_west": west,
                    "bounds_south": south,
                    "bounds_east": east,
                    "bounds_north": north,
                    "center_lat": center_lat,
                    "center_lon": center_lon,
                }
    except Exception:
        return None


# This function builds a GeoJSON Polygon from WGS84 bounding box values.
def _build_bbox_polygon_geometry(
    west: float,
    south: float,
    east: float,
    north: float,
) -> dict[str, Any]:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]
        ],
    }


# This function builds one post-earthquake fallback feature from upload row fields.
def _build_post_earthquake_feature_from_row(
    row: dict[str, Any],
) -> dict[str, Any] | None:
    geometry: dict[str, Any] | None = None

    lat_raw = row.get("lat")
    lon_raw = row.get("lon")
    west_raw = row.get("bounds_west")
    south_raw = row.get("bounds_south")
    east_raw = row.get("bounds_east")
    north_raw = row.get("bounds_north")

    if (
        west_raw is not None
        and south_raw is not None
        and east_raw is not None
        and north_raw is not None
    ):
        geometry = _build_bbox_polygon_geometry(
            float(west_raw),
            float(south_raw),
            float(east_raw),
            float(north_raw),
        )
    elif lat_raw is not None and lon_raw is not None:
        geometry = {
            "type": "Point",
            "coordinates": [float(lon_raw), float(lat_raw)],
        }

    if geometry is None:
        return None

    return {
        "type": "Feature",
        "id": row.get("id"),
        "geometry": geometry,
        "properties": {
            "id": row.get("id"),
            "original_filename": row.get("original_filename"),
            "saved_path": row.get("saved_path"),
            "file_type": row.get("file_type"),
            "status": row.get("status"),
            "worker_name": row.get("worker_name"),
            "uploaded_at": str(row.get("uploaded_at")) if row.get("uploaded_at") is not None else None,
        },
    }


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def _save_file(file_bytes: bytes, folder: str, ext: str, upload_id: str) -> tuple[str, str]:
    """Write bytes to disk and return (relative_path, absolute_path)."""
    safe_id = upload_id.replace("-", "_").lower()
    filename = f"{safe_id}{ext}"
    relative = f"uploads/{folder}/{filename}"
    absolute = UPLOAD_ROOT / folder / filename
    await asyncio.to_thread(_write_bytes, absolute, file_bytes)
    return relative, str(absolute)


# This function probes media duration in seconds using ffprobe.
def _probe_video_duration_seconds(video_path: str) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError(f"ffprobe failed: {result.stderr.strip() or result.stdout.strip()}")

    payload = json.loads(result.stdout or "{}")
    format_payload = payload.get("format") if isinstance(payload, dict) else None
    if not isinstance(format_payload, dict):
        raise ValueError("Unable to read video metadata from ffprobe output")

    raw_duration = format_payload.get("duration")
    try:
        parsed_duration = float(raw_duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid video duration metadata") from exc

    if parsed_duration <= 0:
        raise ValueError("Video duration must be greater than zero")

    return parsed_duration


# ── Database helpers ────────────────────────────────────────────────────────

async def _insert_upload(payload: UploadCreate) -> None:
    """Insert a new row into the uploads table."""
    pool = get_pool()
    if pool is None:
        raise RuntimeError("Database pool not available")

    sql = """
        INSERT INTO uploads (
            id, original_filename, saved_path, file_type, mime_type,
            file_size_bytes, lat, lon, location_source,
            gps_accuracy_m, duration_seconds, frames_extracted,
            worker_name, field_note,
            is_georeferenced, bounds_west, bounds_south,
            bounds_east, bounds_north, batch_id, parent_upload_id
        ) VALUES (
            $1,  $2,  $3,  $4,  $5,
            $6,  $7,  $8,  $9,
            $10, $11, $12,
            $13, $14,
            $15, $16, $17,
            $18, $19, $20, $21
        )
    """
    async with pool.acquire() as conn:
        await conn.execute(
            sql,
            payload.id,
            payload.original_filename,
            payload.saved_path,
            payload.file_type,
            payload.mime_type,
            payload.file_size_bytes,
            payload.lat,
            payload.lon,
            payload.location_source,
            payload.gps_accuracy_m,
            payload.duration_seconds,
            payload.frames_extracted,
            payload.worker_name,
            payload.field_note,
            payload.is_georeferenced,
            payload.bounds_west,
            payload.bounds_south,
            payload.bounds_east,
            payload.bounds_north,
            payload.batch_id,
            payload.parent_upload_id,
        )


# This function finds existing upload with same file bytes for deduplication.
async def _find_duplicate_upload(
    *,
    file_bytes: bytes,
    file_type: str,
    file_size_bytes: int,
) -> dict[str, Any] | None:
    pool = get_pool()
    if pool is None:
        return None

    sql = """
        SELECT id, saved_path, original_filename, status, uploaded_at
        FROM uploads
        WHERE file_type = $1
          AND file_size_bytes = $2
        ORDER BY uploaded_at DESC
        LIMIT 25
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, file_type, file_size_bytes)

    for row in rows:
        saved_path = row.get("saved_path")
        if not isinstance(saved_path, str) or not saved_path:
            continue
        absolute_path = UPLOAD_ROOT.parent / saved_path
        if not absolute_path.exists() or not absolute_path.is_file():
            continue
        try:
            existing_bytes = await asyncio.to_thread(Path(absolute_path).read_bytes)
        except Exception:
            continue
        if existing_bytes == file_bytes:
            return dict(row)

    return None


# This function returns success response for duplicate upload that was ignored.
def _duplicate_ignored_response(duplicate_row: dict[str, Any], file_type: str, filename: str | None) -> dict[str, Any]:
    duplicate_upload_id = str(duplicate_row.get("id") or "")
    duplicate_saved_path = str(duplicate_row.get("saved_path") or "")
    logger.info(
        "uploads.duplicate_ignored: file_type=%s original_filename=%s duplicate_upload_id=%s",
        file_type,
        filename,
        duplicate_upload_id,
    )
    return _success(
        {
            "upload_id": duplicate_upload_id,
            "file_type": file_type,
            "original_filename": filename,
            "saved_path": duplicate_saved_path,
            "status": "skipped_duplicate",
            "duplicate": True,
            "message": "Same file already uploaded; ignored duplicate upload.",
        }
    )


# This function maps one saved upload path to the matching TiTiler-accessible COG path.
def _to_titiler_upload_path(saved_path: str) -> str:
    normalized = saved_path.strip().replace("\\", "/")
    if normalized.startswith("uploads/"):
        normalized = normalized[len("uploads/"):]
    return f"/data/uploads/{normalized}"


def _row_to_upload(row: Any) -> Upload:
    """Convert an asyncpg Record to an Upload model."""
    d = dict(row)
    # remove geom (binary PostGIS type — not needed in API response)
    d.pop("geom", None)
    return Upload(**d)


# ── POST /uploads/ground-photo ──────────────────────────────────────────────

@router.post("/ground-photo", summary="Upload a ground photo")
async def upload_ground_photo(
    file: UploadFile = File(...),
    lat: str = Form(...),
    lon: str = Form(...),
    worker_name: str | None = Form(None),
    field_note: str | None = Form(None),
    location_source: str = Form("device_gps"),
    gps_accuracy_m: str | None = Form(None),
) -> dict[str, Any]:
    """Save a ground photo to disk and create an uploads record."""
    try:
        logger.info("uploads.ground_photo.started: filename=%s", file.filename)
        latitude = _parse_coord(lat, -90.0, 90.0)
        longitude = _parse_coord(lon, -180.0, 180.0)
        if latitude is None or longitude is None:
            return _error("lat and lon are required for ground photo uploads")

        ext = _ext(file.filename)
        if ext not in ALLOWED_GROUND_EXTENSIONS:
            return _error(f"Unsupported extension '{ext}'. Allowed: {ALLOWED_GROUND_EXTENSIONS}")

        file_bytes = await file.read()
        if not file_bytes:
            return _error("Uploaded file is empty")
        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            return _error("File exceeds 2 GB limit")

        duplicate_row = await _find_duplicate_upload(
            file_bytes=file_bytes,
            file_type="ground_photo",
            file_size_bytes=len(file_bytes),
        )
        if duplicate_row is not None:
            return _duplicate_ignored_response(duplicate_row, "ground_photo", file.filename)

        image_meta = await asyncio.to_thread(_validate_image, file_bytes)
        upload_id = _generate_upload_id()
        relative_path, absolute_path = await _save_file(file_bytes, FOLDER_MAP["ground_photo"], ext, upload_id)

        accuracy = float(gps_accuracy_m.strip()) if gps_accuracy_m and gps_accuracy_m.strip() else None

        payload = UploadCreate(
            id=upload_id,
            original_filename=file.filename or "",
            saved_path=relative_path,
            file_type="ground_photo",
            mime_type=MIME_MAP.get(ext),
            file_size_bytes=len(file_bytes),
            lat=latitude,
            lon=longitude,
            location_source=location_source,  # type: ignore[arg-type]
            gps_accuracy_m=accuracy,
            worker_name=worker_name,
            field_note=field_note,
        )
        await _insert_upload(payload)
        logger.info("uploads.ground_photo.saved: upload_id=%s filename=%s", upload_id, file.filename)

        return _success({
            "upload_id": upload_id,
            "file_type": "ground_photo",
            "original_filename": file.filename,
            "saved_path": relative_path,
            "stored_at": absolute_path,
            "size_bytes": len(file_bytes),
            "lat": latitude,
            "lon": longitude,
            "worker_name": worker_name,
            "image": image_meta,
            "status": "uploaded",
        })

    except ValueError as exc:
        logger.warning("uploads.ground_photo.validation_failed: filename=%s error=%s", file.filename, exc)
        return _error(f"Ground photo upload failed: {exc}")
    except Exception as exc:
        logger.exception("uploads.ground_photo.failed: filename=%s", file.filename)
        return _error(f"Ground photo upload failed: {exc}")


# ── POST /uploads/orthophoto ────────────────────────────────────────────────

@router.post("/orthophoto", summary="Upload an orthophoto or drone image")
async def upload_orthophoto(
    file: UploadFile = File(...),
    lat: str | None = Form(None),
    lon: str | None = Form(None),
    worker_name: str | None = Form(None),
    field_note: str | None = Form(None),
    location_source: str = Form("device_gps"),
    is_georeferenced: str = Form("false"),
    bounds_west: str | None = Form(None),
    bounds_south: str | None = Form(None),
    bounds_east: str | None = Form(None),
    bounds_north: str | None = Form(None),
    batch_id: str | None = Form(None),
) -> dict[str, Any]:
    """Save an orthophoto / drone image to disk and create an uploads record."""
    try:
        logger.info("uploads.orthophoto.started: filename=%s", file.filename)
        latitude = _parse_coord(lat, -90.0, 90.0)
        longitude = _parse_coord(lon, -180.0, 180.0)

        ext = _ext(file.filename)
        if ext not in ALLOWED_ORTHO_EXTENSIONS:
            return _error(f"Unsupported extension '{ext}'. Allowed: {ALLOWED_ORTHO_EXTENSIONS}")

        # Determine if image extension or orthophoto raster extension.
        file_type: str = "drone_orthophoto" if ext in {".tif", ".tiff", ".geotiff", ".cog"} else "drone_image"

        file_bytes = await file.read()
        if not file_bytes:
            return _error("Uploaded file is empty")

        duplicate_row = await _find_duplicate_upload(
            file_bytes=file_bytes,
            file_type=file_type,
            file_size_bytes=len(file_bytes),
        )
        if duplicate_row is not None:
            return _duplicate_ignored_response(duplicate_row, file_type, file.filename)

        # This variable stores extracted raster geospatial metadata for orthophoto files.
        raster_geo = None
        if file_type == "drone_orthophoto":
            raster_geo = await asyncio.to_thread(_extract_raster_geo_bounds, file_bytes)
            if raster_geo is not None:
                logger.info(
                    "uploads.orthophoto.geo_extracted: filename=%s center=(%s,%s)",
                    file.filename,
                    round(float(raster_geo["center_lat"]), 6),
                    round(float(raster_geo["center_lon"]), 6),
                )

        # This variable stores optional orthophoto metadata; upload should not fail if metadata probing fails.
        image_meta: dict[str, Any]
        try:
            image_meta = await asyncio.to_thread(_validate_image, file_bytes)
        except Exception:
            image_meta = {"format": "UNKNOWN", "width": None, "height": None}
        upload_id = _generate_upload_id()

        relative_path, absolute_path = await _save_file(file_bytes, FOLDER_MAP["drone_orthophoto"], ext, upload_id)

        geo_flag_from_form = is_georeferenced.strip().lower() in ("true", "1", "yes")

        def _opt_float(v: str | None) -> float | None:
            return float(v.strip()) if v and v.strip() else None

        bounds_west_value = _opt_float(bounds_west)
        bounds_south_value = _opt_float(bounds_south)
        bounds_east_value = _opt_float(bounds_east)
        bounds_north_value = _opt_float(bounds_north)

        if raster_geo is not None:
            if latitude is None:
                latitude = float(raster_geo["center_lat"])
            if longitude is None:
                longitude = float(raster_geo["center_lon"])
            if bounds_west_value is None:
                bounds_west_value = float(raster_geo["bounds_west"])
            if bounds_south_value is None:
                bounds_south_value = float(raster_geo["bounds_south"])
            if bounds_east_value is None:
                bounds_east_value = float(raster_geo["bounds_east"])
            if bounds_north_value is None:
                bounds_north_value = float(raster_geo["bounds_north"])

        geo = geo_flag_from_form or (
            bounds_west_value is not None
            and bounds_south_value is not None
            and bounds_east_value is not None
            and bounds_north_value is not None
        )

        payload = UploadCreate(
            id=upload_id,
            original_filename=file.filename or "",
            saved_path=relative_path,
            file_type=file_type,  # type: ignore[arg-type]
            mime_type=MIME_MAP.get(ext),
            file_size_bytes=len(file_bytes),
            lat=latitude,
            lon=longitude,
            location_source=location_source,  # type: ignore[arg-type]
            worker_name=worker_name,
            field_note=field_note,
            is_georeferenced=geo,
            bounds_west=bounds_west_value,
            bounds_south=bounds_south_value,
            bounds_east=bounds_east_value,
            bounds_north=bounds_north_value,
            batch_id=batch_id,
        )
        await _insert_upload(payload)
        logger.info("uploads.orthophoto.saved: upload_id=%s filename=%s", upload_id, file.filename)

        return _success({
            "upload_id": upload_id,
            "file_type": file_type,
            "original_filename": file.filename,
            "saved_path": relative_path,
            "stored_at": absolute_path,
            "size_bytes": len(file_bytes),
            "lat": latitude,
            "lon": longitude,
            "is_georeferenced": geo,
            "worker_name": worker_name,
            "image": image_meta,
            "status": "uploaded",
        })

    except ValueError as exc:
        logger.warning("uploads.orthophoto.validation_failed: filename=%s error=%s", file.filename, exc)
        return _error(f"Orthophoto upload failed: {exc}")
    except Exception as exc:
        logger.exception("uploads.orthophoto.failed: filename=%s", file.filename)
        return _error(f"Orthophoto upload failed: {exc}")


# ── POST /uploads/video ─────────────────────────────────────────────────────

@router.post("/video", summary="Upload a video")
async def upload_video(
    file: UploadFile = File(...),
    lat: str = Form(...),
    lon: str = Form(...),
    worker_name: str | None = Form(None),
    field_note: str | None = Form(None),
    location_source: str = Form("device_gps"),
    gps_accuracy_m: str | None = Form(None),
) -> dict[str, Any]:
    """Save a video to disk and create an uploads record."""
    try:
        logger.info("uploads.video.started: filename=%s", file.filename)
        latitude = _parse_coord(lat, -90.0, 90.0)
        longitude = _parse_coord(lon, -180.0, 180.0)
        if latitude is None or longitude is None:
            return _error("lat and lon are required for video uploads")

        ext = _ext(file.filename)
        if ext not in ALLOWED_VIDEO_EXTENSIONS:
            return _error(f"Unsupported extension '{ext}'. Allowed: {ALLOWED_VIDEO_EXTENSIONS}")

        file_bytes = await file.read()
        if not file_bytes:
            return _error("Uploaded file is empty")
        if len(file_bytes) > MAX_VIDEO_SIZE_BYTES:
            return _error(f"Video exceeds {MAX_VIDEO_SIZE_MB} MB limit")

        # Videos are intentional field recordings — skip byte-level deduplication so
        # every upload always creates its own record (even if the same file was uploaded
        # before under a different name or for a different location).
        upload_id = _generate_upload_id()
        relative_path, absolute_path = await _save_file(file_bytes, FOLDER_MAP["video"], ext, upload_id)
        # This variable stores probed video length used for upload policy enforcement.
        duration_seconds = await asyncio.to_thread(_probe_video_duration_seconds, absolute_path)
        if duration_seconds > MAX_VIDEO_DURATION_SECONDS:
            await asyncio.to_thread(Path(absolute_path).unlink, True)
            return _error(
                f"Video duration {duration_seconds:.1f}s exceeds {int(MAX_VIDEO_DURATION_SECONDS)} seconds limit"
            )

        accuracy = float(gps_accuracy_m.strip()) if gps_accuracy_m and gps_accuracy_m.strip() else None

        payload = UploadCreate(
            id=upload_id,
            original_filename=file.filename or "",
            saved_path=relative_path,
            file_type="video",
            mime_type=MIME_MAP.get(ext),
            file_size_bytes=len(file_bytes),
            lat=latitude,
            lon=longitude,
            location_source=location_source,  # type: ignore[arg-type]
            gps_accuracy_m=accuracy,
            duration_seconds=round(duration_seconds, 2),
            frames_extracted=0,
            worker_name=worker_name,
            field_note=field_note,
        )
        await _insert_upload(payload)
        logger.info("uploads.video.saved: upload_id=%s filename=%s", upload_id, file.filename)

        return _success({
            "upload_id": upload_id,
            "file_type": "video",
            "original_filename": file.filename,
            "saved_path": relative_path,
            "stored_at": absolute_path,
            "size_bytes": len(file_bytes),
            "lat": latitude,
            "lon": longitude,
            "duration_seconds": round(duration_seconds, 2),
            "worker_name": worker_name,
            "status": "uploaded",
        })

    except ValueError as exc:
        logger.warning("uploads.video.validation_failed: filename=%s error=%s", file.filename, exc)
        return _error(f"Video upload failed: {exc}")
    except Exception as exc:
        logger.exception("uploads.video.failed: filename=%s", file.filename)
        return _error(f"Video upload failed: {exc}")


# This endpoint returns user-uploaded post-earthquake orthophoto and drone image points as GeoJSON.
@router.get("/post-earthquake-layer", summary="Post-earthquake uploaded imagery layer")
async def get_post_earthquake_layer(
    max_features: int = Query(5000, ge=1, le=20000),
) -> dict[str, Any]:
    pool = get_pool()
    if pool is None:
        return _error("Database not available")

    sql = """
        SELECT jsonb_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(jsonb_agg(feature_row.feature), '[]'::jsonb)
        ) AS geojson
        FROM (
            SELECT jsonb_build_object(
                'type', 'Feature',
                'id', u.id,
                'geometry', ST_AsGeoJSON(
                    COALESCE(
                        u.geom,
                        CASE
                            WHEN u.bounds_west IS NOT NULL
                             AND u.bounds_south IS NOT NULL
                             AND u.bounds_east IS NOT NULL
                             AND u.bounds_north IS NOT NULL
                            THEN ST_SetSRID(
                                ST_MakeEnvelope(
                                    u.bounds_west,
                                    u.bounds_south,
                                    u.bounds_east,
                                    u.bounds_north
                                ),
                                4326
                            )
                            ELSE NULL
                        END,
                        CASE
                            WHEN u.lon IS NOT NULL AND u.lat IS NOT NULL
                            THEN ST_SetSRID(ST_MakePoint(u.lon, u.lat), 4326)
                            ELSE NULL
                        END
                    )
                )::jsonb,
                'properties', jsonb_build_object(
                    'id', u.id,
                    'original_filename', u.original_filename,
                    'saved_path', u.saved_path,
                    'file_type', u.file_type,
                    'status', u.status,
                    'worker_name', u.worker_name,
                    'uploaded_at', u.uploaded_at
                )
            ) AS feature
            FROM uploads AS u
            WHERE u.file_type IN ('drone_orthophoto', 'drone_image')
              AND (
                  u.geom IS NOT NULL
                  OR (
                      u.bounds_west IS NOT NULL
                      AND u.bounds_south IS NOT NULL
                      AND u.bounds_east IS NOT NULL
                      AND u.bounds_north IS NOT NULL
                  )
                  OR (u.lat IS NOT NULL AND u.lon IS NOT NULL)
              )
            ORDER BY u.uploaded_at DESC
            LIMIT $1
        ) AS feature_row
    """

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, max_features)
        geojson_payload = row.get("geojson") if row else None
        features = geojson_payload.get("features") if isinstance(geojson_payload, dict) else []
        feature_count = len(features) if isinstance(features, list) else 0

        # This block provides best-effort fallback for older uploads with missing DB geometry.
        if feature_count == 0:
            fallback_sql = """
                SELECT id, original_filename, saved_path, file_type, status, worker_name, uploaded_at,
                       lat, lon, bounds_west, bounds_south, bounds_east, bounds_north
                FROM uploads
                WHERE file_type IN ('drone_orthophoto', 'drone_image')
                ORDER BY uploaded_at DESC
                LIMIT $1
            """
            async with pool.acquire() as conn:
                fallback_rows = await conn.fetch(fallback_sql, max_features)

            fallback_features: list[dict[str, Any]] = []
            for fallback_row in fallback_rows:
                fallback_row_dict = dict(fallback_row)
                feature = _build_post_earthquake_feature_from_row(fallback_row_dict)
                if feature is None and fallback_row_dict.get("file_type") == "drone_orthophoto":
                    saved_path = fallback_row_dict.get("saved_path")
                    if isinstance(saved_path, str) and saved_path:
                        absolute_path = UPLOAD_ROOT.parent / saved_path
                        if absolute_path.exists() and absolute_path.is_file():
                            try:
                                file_bytes = await asyncio.to_thread(Path(absolute_path).read_bytes)
                                extracted_geo = await asyncio.to_thread(_extract_raster_geo_bounds, file_bytes)
                            except Exception:
                                extracted_geo = None

                            if extracted_geo is not None:
                                fallback_row_dict["bounds_west"] = float(extracted_geo["bounds_west"])
                                fallback_row_dict["bounds_south"] = float(extracted_geo["bounds_south"])
                                fallback_row_dict["bounds_east"] = float(extracted_geo["bounds_east"])
                                fallback_row_dict["bounds_north"] = float(extracted_geo["bounds_north"])
                                fallback_row_dict["lat"] = float(extracted_geo["center_lat"])
                                fallback_row_dict["lon"] = float(extracted_geo["center_lon"])
                                feature = _build_post_earthquake_feature_from_row(fallback_row_dict)

                if feature is not None:
                    fallback_features.append(feature)

            if fallback_features:
                geojson_payload = {
                    "type": "FeatureCollection",
                    "features": fallback_features,
                }
                feature_count = len(fallback_features)
                logger.info("uploads.post_earthquake_layer.fallback_loaded: features=%s", feature_count)

        logger.info("uploads.post_earthquake_layer.loaded: features=%s", feature_count)
        return _success(
            {
                "layer": "post_earthquake_images",
                "feature_count": feature_count,
                "geojson": geojson_payload
                if isinstance(geojson_payload, dict)
                else {"type": "FeatureCollection", "features": []},
            }
        )
    except Exception as exc:
        logger.exception("uploads.post_earthquake_layer.failed")
        return _error(f"Failed to load post-earthquake layer: {exc}")


# This endpoint proxies one uploaded post-earthquake orthophoto tile from TiTiler.
@router.get("/post-earthquake-tiles/{upload_id}/{z}/{x}/{y}.png")
async def get_post_earthquake_tile(upload_id: str, z: int, x: int, y: int) -> Response:
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")

    sql = """
        SELECT id, saved_path, file_type
        FROM uploads
        WHERE id = $1
          AND file_type = 'drone_orthophoto'
        LIMIT 1
    """

    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, upload_id)

    if row is None:
        raise HTTPException(status_code=404, detail=f"Orthophoto upload '{upload_id}' not found")

    saved_path_raw = row.get("saved_path")
    if not isinstance(saved_path_raw, str) or not saved_path_raw:
        raise HTTPException(status_code=404, detail="Upload has no saved path")

    cog_path = _to_titiler_upload_path(saved_path_raw)
    tile_url = f"{TITILER_URL}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    params: dict[str, Any] = {
        "url": cog_path,
        "bidx": ["1", "2", "3"],
        "rescale": "0,255",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            tile_response = await client.get(tile_url, params=params)
            tile_response.raise_for_status()
            return Response(
                content=tile_response.content,
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=3600"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "uploads.post_earthquake_tile.failed: upload_id=%s z=%s x=%s y=%s error=%s",
                upload_id,
                z,
                x,
                y,
                exc,
            )
            raise HTTPException(status_code=500, detail=f"TiTiler request failed: {exc}")


# ── GET /uploads ────────────────────────────────────────────────────────────

@router.get("", summary="List uploads (paginated)")
async def list_uploads(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    file_type: str | None = Query(None),
    status: str | None = Query(None),
    worker_name: str | None = Query(None),
    is_analyzed: bool | None = Query(None),
) -> dict[str, Any]:
    """Return a paginated, filterable list of upload records."""
    try:
        pool = get_pool()
        if pool is None:
            return _error("Database not available")

        conditions: list[str] = []
        params: list[Any] = []
        idx = 1

        if file_type:
            conditions.append(f"file_type = ${idx}")
            params.append(file_type)
            idx += 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        if worker_name:
            conditions.append(f"worker_name ILIKE ${idx}")
            params.append(f"%{worker_name}%")
            idx += 1
        if is_analyzed is not None:
            conditions.append(f"is_analyzed = ${idx}")
            params.append(is_analyzed)
            idx += 1

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        offset = (page - 1) * page_size
        count_sql = f"SELECT COUNT(*) FROM uploads {where}"
        data_sql = (
            f"SELECT id, original_filename, saved_path, file_type, mime_type, "
            f"file_size_bytes, lat, lon, location_source, gps_accuracy_m, "
            f"status, is_analyzed, duration_seconds, frames_extracted, "
            f"is_georeferenced, cog_path, bounds_west, bounds_south, bounds_east, bounds_north, "
            f"assessment_id, batch_id, parent_upload_id, "
            f"error_message, retry_count, worker_name, field_note, "
            f"uploaded_at, processing_started_at, processing_done_at, updated_at "
            f"FROM uploads {where} "
            f"ORDER BY uploaded_at DESC "
            f"LIMIT ${idx} OFFSET ${idx + 1}"
        )

        async with pool.acquire() as conn:
            total = await conn.fetchval(count_sql, *params)
            rows = await conn.fetch(data_sql, *params, page_size, offset)

        uploads = [_row_to_upload(r) for r in rows]
        return _success(
            UploadListResponse(
                uploads=uploads,
                total_count=total,
                page=page,
                page_size=page_size,
            ).model_dump()
        )

    except Exception as exc:
        return _error(f"Failed to list uploads: {exc}")


# ── GET /uploads/unfinished ──────────────────────────────────────────────────

@router.get("/unfinished", summary="List unfinished uploads for assessment continuation")
async def list_unfinished_uploads(
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Return uploads that are not analyzed and still in unfinished statuses."""
    try:
        pool = get_pool()
        if pool is None:
            return _error("Database not available")

        statuses = ("uploaded", "queued", "processing", "failed")
        sql = """
            SELECT id, original_filename, saved_path, file_type, mime_type,
                   file_size_bytes, lat, lon, location_source, gps_accuracy_m,
                   status, is_analyzed, duration_seconds, frames_extracted,
                   is_georeferenced, cog_path, bounds_west, bounds_south, bounds_east, bounds_north,
                   assessment_id, batch_id, parent_upload_id,
                   error_message, retry_count, worker_name, field_note,
                   uploaded_at, processing_started_at, processing_done_at, updated_at
            FROM uploads
            WHERE status = ANY($1::text[])
              AND COALESCE(is_analyzed, FALSE) = FALSE
              AND assessment_id IS NULL
            ORDER BY uploaded_at DESC
            LIMIT $2
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, list(statuses), limit)

        uploads = [_row_to_upload(row) for row in rows]

        return _success(
            {
                "statuses": list(statuses),
                "count": len(uploads),
                "uploads": [upload.model_dump() for upload in uploads],
            }
        )
    except Exception as exc:
        return _error(f"Failed to list unfinished uploads: {exc}")


# ── GET /uploads/by-location ─────────────────────────────────────────────────

@router.get("/by-location", summary="List unfinished uploads grouped by location")
async def list_unfinished_uploads_by_location(
    radius_meters: float = Query(10.0, ge=1.0, le=100.0, description="Radius in meters to consider uploads as same location"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Return unfinished uploads grouped by geographic location.
    
    Uploads within radius_meters of each other are grouped together,
    allowing multiple photos/videos from the same location to be
    processed as a single assessment batch.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        pool = get_pool()
        if pool is None:
            logger.error("uploads.by_location: Database pool not available")
            return _error("Database not available")

        statuses = ("uploaded", "queued", "processing", "failed")
        logger.info(f"uploads.by_location: Querying with radius={radius_meters}m, statuses={statuses}")
        
        # Use PostGIS to cluster uploads by proximity
        # ST_ClusterDBSCAN works with geometry - convert 10m to degrees (~0.00009 at equator)
        # Using a simpler approach: group by exact lat/lon coordinates (rounded to ~10m precision)
        # 0.0001 degrees ≈ 11 meters
        precision = 5  # 5 decimal places ≈ 1.1m precision, group within ~10m radius
        sql = f"""
            WITH unfinished AS (
                SELECT 
                    id, original_filename, saved_path, file_type, mime_type,
                    file_size_bytes, lat, lon, location_source, gps_accuracy_m,
                    status, is_analyzed, duration_seconds, frames_extracted,
                    is_georeferenced, cog_path, bounds_west, bounds_south, bounds_east, bounds_north,
                    assessment_id, batch_id, parent_upload_id,
                    error_message, retry_count, worker_name, field_note,
                    uploaded_at, processing_started_at, processing_done_at, updated_at,
                    ROUND(lat::numeric, 6) AS lat_rounded,
                    ROUND(lon::numeric, 6) AS lon_rounded
                FROM uploads
                WHERE status = ANY($1::text[])
                  AND COALESCE(is_analyzed, FALSE) = FALSE
                  AND assessment_id IS NULL
                  AND lat IS NOT NULL 
                  AND lon IS NOT NULL
            )
            SELECT 
                lat_rounded || '_' || lon_rounded AS cluster_id,
                AVG(lat) AS center_lat,
                AVG(lon) AS center_lon,
                COUNT(*) AS upload_count,
                jsonb_agg(
                    jsonb_build_object(
                        'id', id,
                        'original_filename', original_filename,
                        'saved_path', saved_path,
                        'file_type', file_type,
                        'mime_type', mime_type,
                        'file_size_bytes', file_size_bytes,
                        'lat', lat,
                        'lon', lon,
                        'location_source', location_source,
                        'gps_accuracy_m', gps_accuracy_m,
                        'status', status,
                        'duration_seconds', duration_seconds,
                        'frames_extracted', frames_extracted,
                        'is_georeferenced', is_georeferenced,
                        'worker_name', worker_name,
                        'field_note', field_note,
                        'uploaded_at', uploaded_at,
                        'error_message', error_message,
                        'retry_count', retry_count
                    ) ORDER BY uploaded_at DESC
                ) AS uploads
            FROM unfinished
            GROUP BY lat_rounded, lon_rounded
            ORDER BY upload_count DESC, MAX(uploaded_at) DESC
            LIMIT $2
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, list(statuses), limit)

        logger.info(f"uploads.by_location: Found {len(rows)} location groups from clustering query")
        
        # Also do a simple count to debug
        count_sql = """
            SELECT COUNT(*) FROM uploads 
            WHERE status = ANY($1::text[])
              AND COALESCE(is_analyzed, FALSE) = FALSE
              AND assessment_id IS NULL
        """
        async with pool.acquire() as conn:
            total_unfinished = await conn.fetchval(count_sql, list(statuses))
        logger.info(f"uploads.by_location: Total unfinished uploads (any status): {total_unfinished}")

        # Format response
        import json
        location_groups = []
        for row in rows:
            cluster_id = row["cluster_id"]
            uploads = row["uploads"]
            
            # jsonb_agg may return as string - parse if needed
            if isinstance(uploads, str):
                uploads = json.loads(uploads)

            # This loop enriches grouped uploads with live analysis progress when available.
            for upload_payload in uploads:
                upload_id = upload_payload.get("id")
                if isinstance(upload_id, str):
                    progress_payload = get_analysis_progress_for_upload(upload_id)
                    if isinstance(progress_payload, dict):
                        upload_payload["progress_percent"] = progress_payload.get("progress_percent")
                        upload_payload["analysis_stage"] = progress_payload.get("stage")
                        upload_payload["analysis_thought"] = progress_payload.get("thought")
                        upload_payload["analysis_active"] = progress_payload.get("is_active")
                    else:
                        upload_payload["progress_percent"] = None
                        upload_payload["analysis_stage"] = None
                        upload_payload["analysis_thought"] = None
                        upload_payload["analysis_active"] = False
            
            # Determine location name from uploads if available
            location_name = None
            if uploads and len(uploads) > 0:
                first_upload = uploads[0]
                if first_upload.get("field_note"):
                    location_name = first_upload["field_note"][:50]
            
            location_groups.append({
                "group_id": f"loc_{cluster_id}" if cluster_id is not None else f"loc_single_{rows.index(row)}",
                "center_lat": round(float(row["center_lat"]), 6) if row["center_lat"] else None,
                "center_lon": round(float(row["center_lon"]), 6) if row["center_lon"] else None,
                "upload_count": row["upload_count"],
                "location_name": location_name,
                "uploads": uploads,
            })

        # Also get uploads without coordinates
        no_coords_sql = """
            SELECT 
                id, original_filename, saved_path, file_type, mime_type,
                file_size_bytes, lat, lon, status, worker_name, field_note,
                uploaded_at, error_message, retry_count
            FROM uploads
            WHERE status = ANY($1::text[])
              AND COALESCE(is_analyzed, FALSE) = FALSE
              AND assessment_id IS NULL
              AND (lat IS NULL OR lon IS NULL)
            ORDER BY uploaded_at DESC
        """
        
        async with pool.acquire() as conn:
            no_coords_rows = await conn.fetch(no_coords_sql, list(statuses))
        
        uploads_without_coords = []
        for row in no_coords_rows:
            uploads_without_coords.append({
                "id": row["id"],
                "original_filename": row["original_filename"],
                "saved_path": row["saved_path"],
                "file_type": row["file_type"],
                "mime_type": row["mime_type"],
                "file_size_bytes": row["file_size_bytes"],
                "lat": row["lat"],
                "lon": row["lon"],
                "status": row["status"],
                "worker_name": row["worker_name"],
                "field_note": row["field_note"],
                "uploaded_at": row["uploaded_at"],
                "error_message": row["error_message"],
                "retry_count": row["retry_count"],
            })

        return _success({
            "radius_meters": radius_meters,
            "location_groups": location_groups,
            "uploads_without_coords": uploads_without_coords,
            "total_locations": len(location_groups),
            "total_uploads_without_coords": len(uploads_without_coords),
            "total_uploads": sum(g["upload_count"] for g in location_groups) + len(uploads_without_coords),
        })
        
    except Exception as exc:
        return _error(f"Failed to list uploads by location: {exc}")


# ── GET /uploads/ongoing-assessments ────────────────────────────────────────

@router.get("/ongoing-assessments", summary="List live ongoing AI analyses")
async def list_ongoing_assessments() -> dict[str, Any]:
    """Return active and recently-finished AI analysis progress snapshots."""
    try:
        # This variable stores live and recent terminal progress used for UI completion toasts.
        recent_progress = get_recent_analysis_progress(retain_seconds=120)
        if not recent_progress:
            return _success({"count": 0, "items": []})

        pool = get_pool()
        if pool is None:
            return _error("Database not available")

        # This variable stores upload IDs to fetch upload metadata in one query.
        progress_upload_ids = [str(item.get("upload_id")) for item in recent_progress if item.get("upload_id")]
        progress_upload_ids = [upload_id for upload_id in progress_upload_ids if upload_id]
        if not progress_upload_ids:
            return _success({"count": 0, "items": []})

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, original_filename, file_type, lat, lon, worker_name, field_note, status, uploaded_at
                FROM uploads
                WHERE id = ANY($1::text[])
                """,
                progress_upload_ids,
            )

        # This variable maps upload ID to metadata row for fast response building.
        upload_meta_by_id = {str(row["id"]): dict(row) for row in rows}
        items: list[dict[str, Any]] = []
        for progress_item in recent_progress:
            upload_id = str(progress_item.get("upload_id") or "")
            if not upload_id:
                continue
            upload_meta = upload_meta_by_id.get(upload_id, {})
            items.append(
                {
                    "upload_id": upload_id,
                    "original_filename": upload_meta.get("original_filename"),
                    "file_type": upload_meta.get("file_type"),
                    "lat": upload_meta.get("lat"),
                    "lon": upload_meta.get("lon"),
                    "worker_name": upload_meta.get("worker_name"),
                    "field_note": upload_meta.get("field_note"),
                    "status": progress_item.get("status") or upload_meta.get("status") or "processing",
                    "progress_percent": progress_item.get("progress_percent"),
                    "stage": progress_item.get("stage"),
                    "thought": progress_item.get("thought"),
                    "is_active": progress_item.get("is_active"),
                    "assessment_id": progress_item.get("assessment_id"),
                    "error_message": progress_item.get("error_message"),
                    "updated_at": progress_item.get("updated_at"),
                    "uploaded_at": upload_meta.get("uploaded_at"),
                }
            )

        # This sort keeps latest progress events first so frontend can detect terminal transitions quickly.
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)

        return _success({"count": len(items), "items": items})
    except Exception as exc:
        return _error(f"Failed to fetch ongoing assessments: {exc}")


# ── GET /uploads/{upload_id} ────────────────────────────────────────────────

@router.get("/{upload_id}", summary="Get a single upload record")
async def get_upload(upload_id: str) -> dict[str, Any]:
    """Fetch one upload record by its ID."""
    try:
        pool = get_pool()
        if pool is None:
            return _error("Database not available")

        sql = (
            "SELECT id, original_filename, saved_path, file_type, mime_type, "
            "file_size_bytes, lat, lon, location_source, gps_accuracy_m, "
            "status, is_analyzed, duration_seconds, frames_extracted, "
            "is_georeferenced, cog_path, bounds_west, bounds_south, bounds_east, bounds_north, "
            "assessment_id, batch_id, parent_upload_id, "
            "error_message, retry_count, worker_name, field_note, "
            "uploaded_at, processing_started_at, processing_done_at, updated_at "
            "FROM uploads WHERE id = $1"
        )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, upload_id)

        if row is None:
            return _error(f"Upload '{upload_id}' not found")

        return _success(_row_to_upload(row).model_dump())

    except Exception as exc:
        return _error(f"Failed to fetch upload: {exc}")


# ── DELETE /uploads/{upload_id} ─────────────────────────────────────────────

@router.delete("/{upload_id}", summary="Delete an upload record")
async def delete_upload(
    upload_id: str,
    delete_file: bool = Query(False, description="Also delete the file from disk"),
) -> dict[str, Any]:
    """Remove an upload record from the DB, and optionally its file from disk."""
    try:
        pool = get_pool()
        if pool is None:
            return _error("Database not available")

        # Fetch saved_path before deletion if we need to remove the file
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT saved_path FROM uploads WHERE id = $1", upload_id
            )
            if row is None:
                return _error(f"Upload '{upload_id}' not found")

            await conn.execute("DELETE FROM uploads WHERE id = $1", upload_id)

        file_deleted = False
        if delete_file and row["saved_path"]:
            try:
                file_path = UPLOAD_ROOT.parent / row["saved_path"]
                if file_path.exists():
                    await asyncio.to_thread(file_path.unlink)
                    file_deleted = True
            except Exception:
                pass  # best-effort file deletion

        return _success({
            "upload_id": upload_id,
            "record_deleted": True,
            "file_deleted": file_deleted,
        })

    except Exception as exc:
        return _error(f"Failed to delete upload: {exc}")


# ── POST /uploads/{upload_id}/analyze ───────────────────────────────────────

@router.post("/{upload_id}/analyze", summary="Trigger manual analysis for an upload")
async def trigger_analysis(upload_id: str) -> dict[str, Any]:
    """Trigger the Gemma 4 pipeline for this upload immediately."""
    try:
        pool = get_pool()
        if pool is None:
            return _error("Database not available")

        # Check if it exists and is unanalyzed
        sql = "SELECT id, is_analyzed, status FROM uploads WHERE id = $1"
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, upload_id)

        if not row:
            return _error(f"Upload '{upload_id}' not found")
        if row["is_analyzed"]:
            return _error(f"Upload '{upload_id}' is already analyzed")
        # This variable tracks whether in-memory progress still marks upload as active.
        active_progress = get_analysis_progress_for_upload(upload_id)
        is_progress_active = bool(active_progress and active_progress.get("is_active"))

        # This variable tracks whether an asyncio background task is actively running.
        is_task_active = is_upload_analysis_task_active(upload_id)

        if row["status"] == "done":
            return _error(f"Upload '{upload_id}' is already done")
        if is_task_active or is_progress_active:
            return _error(f"Upload '{upload_id}' is already processing")

        # Trigger in background so endpoint returns quickly.
        started = trigger_upload_analysis_task(upload_id)
        if not started:
            return _error(f"Upload '{upload_id}' is already processing")

        return _success({
            "upload_id": upload_id,
            "status": "processing",
            "message": "Analysis started in background"
        })

    except Exception as exc:
        return _error(f"Failed to trigger analysis: {exc}")


# ── POST /uploads/analyze-location ───────────────────────────────────────────

@router.post("/analyze-location", summary="Trigger grouped analysis for one location")
async def trigger_location_analysis(payload: LocationAnalyzeRequest) -> dict[str, Any]:
    """Start one background AI analysis for multiple uploads from the same location."""
    try:
        if not payload.upload_ids:
            return _error("upload_ids is required")

        # This variable deduplicates incoming IDs while preserving order.
        deduped_upload_ids = list(dict.fromkeys([upload_id for upload_id in payload.upload_ids if upload_id]))
        if not deduped_upload_ids:
            return _error("No valid upload IDs provided")

        pool = get_pool()
        if pool is None:
            return _error("Database not available")

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, is_analyzed, status
                FROM uploads
                WHERE id = ANY($1::text[])
                """,
                deduped_upload_ids,
            )

        if not rows:
            return _error("No matching uploads found")

        # This variable stores only uploads that are safe to start in grouped AI analysis.
        actionable_upload_ids = []
        for row in rows:
            upload_id = str(row["id"])
            if row["is_analyzed"]:
                continue
            if row["status"] == "done":
                continue

            # This variable tracks whether upload is truly active in task/progress state.
            active_progress = get_analysis_progress_for_upload(upload_id)
            is_progress_active = bool(active_progress and active_progress.get("is_active"))
            is_task_active = is_upload_analysis_task_active(upload_id)
            if is_progress_active or is_task_active:
                continue
            actionable_upload_ids.append(upload_id)

        if not actionable_upload_ids:
            return _error("No actionable uploads found. They may already be processing or completed.")

        started_upload_ids = trigger_upload_group_analysis_task(actionable_upload_ids)
        if not started_upload_ids:
            return _error("No actionable uploads found. They may already be processing or completed.")

        return _success(
            {
                "status": "processing",
                "started": len(started_upload_ids),
                "upload_ids": started_upload_ids,
                "message": "Grouped analysis started in background",
            }
        )
    except Exception as exc:
        return _error(f"Failed to trigger location analysis: {exc}")


# ── POST /uploads/{upload_id}/cancel-analysis ───────────────────────────────

@router.post("/{upload_id}/cancel-analysis", summary="Cancel running analysis for an upload")
async def cancel_upload_analysis(upload_id: str) -> dict[str, Any]:
    """Cancel running analysis task and mark affected uploads as failed/canceled."""
    try:
        pool = get_pool()
        if pool is None:
            return _error("Database not available")

        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM uploads WHERE id = $1", upload_id)
        if not row:
            return _error(f"Upload '{upload_id}' not found")

        # This variable lists uploads tied to the same running grouped task.
        affected_upload_ids = cancel_analysis_task_for_upload(upload_id)
        if not affected_upload_ids:
            return _error(f"Upload '{upload_id}' has no active analysis task")

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE uploads
                SET status = 'failed',
                    error_message = 'Analysis canceled by user.',
                    processing_done_at = NOW()
                WHERE id = ANY($1::text[])
                """,
                affected_upload_ids,
            )

        return _success(
            {
                "status": "canceled",
                "upload_id": upload_id,
                "affected_upload_ids": affected_upload_ids,
                "message": "Analysis cancellation requested",
            }
        )
    except Exception as exc:
        return _error(f"Failed to cancel analysis: {exc}")


# ── POST /uploads/{upload_id}/retry-analysis ────────────────────────────────

@router.post("/{upload_id}/retry-analysis", summary="Retry analysis for an upload")
async def retry_upload_analysis(upload_id: str) -> dict[str, Any]:
    """Reset one upload state and trigger analysis again in background."""
    try:
        pool = get_pool()
        if pool is None:
            return _error("Database not available")

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, is_analyzed, status
                FROM uploads
                WHERE id = $1
                """,
                upload_id,
            )

        if not row:
            return _error(f"Upload '{upload_id}' not found")
        if row["is_analyzed"]:
            return _error(f"Upload '{upload_id}' is already analyzed")

        # This variable checks whether a true active background task already exists.
        active_progress = get_analysis_progress_for_upload(upload_id)
        is_progress_active = bool(active_progress and active_progress.get("is_active"))
        is_task_active = is_upload_analysis_task_active(upload_id)
        if is_progress_active or is_task_active:
            return _error(f"Upload '{upload_id}' is already processing")

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE uploads
                SET status = 'uploaded',
                    error_message = NULL,
                    processing_started_at = NULL,
                    processing_done_at = NULL
                WHERE id = $1
                """,
                upload_id,
            )

        started = trigger_upload_analysis_task(upload_id)
        if not started:
            return _error(f"Upload '{upload_id}' is already processing")

        return _success(
            {
                "upload_id": upload_id,
                "status": "processing",
                "message": "Analysis retry started in background",
            }
        )
    except Exception as exc:
        return _error(f"Failed to retry analysis: {exc}")
