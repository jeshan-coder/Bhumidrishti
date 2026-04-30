"""Assessment upload endpoints for ground photo, orthophoto, and video inputs."""

import asyncio
import os
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, Form, UploadFile, Query
from PIL import Image
from db.postgres import get_pool

# This variable defines the API router for assessment-related endpoints.
router = APIRouter(prefix="/assessments", tags=["assessments"])

# This variable defines where uploaded assessment files are stored locally.
UPLOAD_ROOT = Path(os.getenv("ASSESSMENT_UPLOAD_DIR", "/app/data/uploads")).resolve()

# This variable defines the storage folder for ground photos.
PHOTO_UPLOAD_FOLDER = "photo"

# This variable defines the storage folder for orthophoto and drone image uploads.
ORTHOPHOTO_DRONE_UPLOAD_FOLDER = "orthophoto_drone"

# This variable defines the storage folder for video uploads.
VIDEO_UPLOAD_FOLDER = "video"

# This variable defines the maximum upload size per file.
MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024

# This variable defines allowed file extensions for ground photo uploads.
ALLOWED_GROUND_EXTENSIONS = {".jpg", ".jpeg", ".png"}

# This variable defines allowed file extensions for orthophoto uploads.
ALLOWED_ORTHO_EXTENSIONS = {".tif", ".tiff", ".geotiff", ".jpg", ".jpeg", ".png"}

# This variable defines allowed file extensions for video uploads.
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov"}


# This function builds the standard API success response envelope.
def _success_response(data: Any) -> dict[str, Any]:
    return {
        "success": True,
        "data": data,
        "error": None,
    }


# This function builds the standard API error response envelope.
def _error_response(message: str) -> dict[str, Any]:
    return {
        "success": False,
        "data": None,
        "error": message,
    }


# This function extracts and normalizes the extension from an uploaded filename.
def _get_extension(filename: str | None) -> str:
    if not filename:
        return ""
    return Path(filename).suffix.lower()


# This function validates optional coordinate text and returns a float value.
def _parse_coordinate(raw_value: str | None, min_value: float, max_value: float) -> float | None:
    if raw_value is None:
        return None

    stripped_value = raw_value.strip()
    if not stripped_value:
        return None

    parsed_value = float(stripped_value)
    if parsed_value < min_value or parsed_value > max_value:
        raise ValueError(f"Coordinate out of range: {parsed_value}")
    return parsed_value


# This function performs basic image validation and metadata extraction.
def _validate_image_bytes(file_bytes: bytes) -> dict[str, Any]:
    with Image.open(BytesIO(file_bytes)) as image:
        image.verify()

    with Image.open(BytesIO(file_bytes)) as verified_image:
        image_width, image_height = verified_image.size
        image_format = verified_image.format or "UNKNOWN"

    return {
        "format": image_format,
        "width": image_width,
        "height": image_height,
    }


# This function writes uploaded bytes to disk in a background thread.
def _write_file_bytes(target_path: Path, file_bytes: bytes) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("wb") as output_file:
        output_file.write(file_bytes)


# This function validates common upload properties and returns file bytes.
async def _read_and_validate_upload(
    upload_file: UploadFile,
    allowed_extensions: set[str],
) -> tuple[bytes, str]:
    extension = _get_extension(upload_file.filename)
    if extension not in allowed_extensions:
        raise ValueError(f"Unsupported file extension: {extension or 'unknown'}")

    file_bytes = await upload_file.read()
    if not file_bytes:
        raise ValueError("Uploaded file is empty")

    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise ValueError("Uploaded file exceeds 2GB limit")

    return file_bytes, extension


# This function stores uploaded file bytes and returns relative and absolute paths.
async def _store_upload(file_bytes: bytes, category: str, extension: str) -> tuple[str, str]:
    filename = f"{uuid4().hex}{extension}"
    relative_path = f"uploads/{category}/{filename}"
    absolute_path = UPLOAD_ROOT / category / filename

    await asyncio.to_thread(_write_file_bytes, absolute_path, file_bytes)

    return relative_path, str(absolute_path)


# This endpoint uploads and validates a ground photo assessment input.
@router.post("/upload/ground-photo")
async def upload_ground_photo(
    file: UploadFile = File(...),
    lat: str = Form(...),
    lon: str = Form(...),
    worker_name: str | None = Form(None),
    note: str | None = Form(None),
) -> dict[str, Any]:
    try:
        latitude = _parse_coordinate(lat, -90.0, 90.0)
        longitude = _parse_coordinate(lon, -180.0, 180.0)
        if latitude is None or longitude is None:
            return _error_response("Latitude and longitude are required for ground photo uploads")

        file_bytes, extension = await _read_and_validate_upload(file, ALLOWED_GROUND_EXTENSIONS)
        image_metadata = await asyncio.to_thread(_validate_image_bytes, file_bytes)
        relative_path, absolute_path = await _store_upload(file_bytes, PHOTO_UPLOAD_FOLDER, extension)

        return _success_response(
            {
                "input_type": "ground_photo",
                "file_name": file.filename,
                "file_path": relative_path,
                "stored_at": absolute_path,
                "size_bytes": len(file_bytes),
                "lat": latitude,
                "lon": longitude,
                "worker_name": worker_name,
                "note": note,
                "image": image_metadata,
                "processing_status": "ready_for_ground_photo_processing",
            }
        )
    except ValueError as exc:
        return _error_response(f"Ground photo upload failed: {exc}")
    except Exception as exc:
        return _error_response(f"Ground photo upload failed: {exc}")


# This endpoint uploads and validates an orthophoto assessment input.
@router.post("/upload/orthophoto")
async def upload_orthophoto(
    file: UploadFile = File(...),
    lat: str | None = Form(None),
    lon: str | None = Form(None),
    note: str | None = Form(None),
) -> dict[str, Any]:
    try:
        latitude = _parse_coordinate(lat, -90.0, 90.0)
        longitude = _parse_coordinate(lon, -180.0, 180.0)

        file_bytes, extension = await _read_and_validate_upload(file, ALLOWED_ORTHO_EXTENSIONS)
        image_metadata = await asyncio.to_thread(_validate_image_bytes, file_bytes)
        relative_path, absolute_path = await _store_upload(file_bytes, ORTHOPHOTO_DRONE_UPLOAD_FOLDER, extension)

        return _success_response(
            {
                "input_type": "orthophoto",
                "file_name": file.filename,
                "file_path": relative_path,
                "stored_at": absolute_path,
                "size_bytes": len(file_bytes),
                "lat": latitude,
                "lon": longitude,
                "note": note,
                "image": image_metadata,
                "processing_status": "ready_for_orthophoto_processing",
            }
        )
    except ValueError as exc:
        return _error_response(f"Orthophoto upload failed: {exc}")
    except Exception as exc:
        return _error_response(f"Orthophoto upload failed: {exc}")


# This endpoint uploads and validates a video assessment input.
@router.post("/upload/video")
async def upload_video(
    file: UploadFile = File(...),
    lat: str = Form(...),
    lon: str = Form(...),
    note: str | None = Form(None),
) -> dict[str, Any]:
    try:
        latitude = _parse_coordinate(lat, -90.0, 90.0)
        longitude = _parse_coordinate(lon, -180.0, 180.0)
        if latitude is None or longitude is None:
            return _error_response("Latitude and longitude are required for video uploads")

        file_bytes, extension = await _read_and_validate_upload(file, ALLOWED_VIDEO_EXTENSIONS)
        relative_path, absolute_path = await _store_upload(file_bytes, VIDEO_UPLOAD_FOLDER, extension)

        return _success_response(
            {
                "input_type": "video",
                "file_name": file.filename,
                "file_path": relative_path,
                "stored_at": absolute_path,
                "size_bytes": len(file_bytes),
                "lat": latitude,
                "lon": longitude,
                "note": note,
                "processing_status": "ready_for_video_processing",
            }
        )
    except ValueError as exc:
        return _error_response(f"Video upload failed: {exc}")
    except Exception as exc:
        return _error_response(f"Video upload failed: {exc}")


@router.get("")
async def list_assessments(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
) -> dict[str, Any]:
    pool = get_pool()
    if not pool:
        return _error_response("Database pool not initialized")
    
    where_clause = "WHERE status = $3" if status else ""
    query = f"""
        SELECT 
            id, lat, lon, input_type, photo_path, severity, damage_type, 
            structural_risk, building_type, recommended_action, action_priority, 
            status, created_at, updated_at
        FROM assessments
        {where_clause}
        ORDER BY created_at DESC
        LIMIT $1 OFFSET $2
    """
    args = [limit, offset, status] if status else [limit, offset]
    
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            
            # Convert values manually for JSON serialization if needed
            # For geometries we don't fetch geom. But datetimes need isoformat
            data = []
            for row in rows:
                d = dict(row)
                if isinstance(d.get("created_at"), object) and hasattr(d["created_at"], "isoformat"):
                    d["created_at"] = d["created_at"].isoformat()
                if isinstance(d.get("updated_at"), object) and hasattr(d["updated_at"], "isoformat"):
                    d["updated_at"] = d["updated_at"].isoformat()
                data.append(d)
                
            return _success_response(data)
    except Exception as exc:
        return _error_response(f"Failed to fetch assessments: {exc}")

@router.get("/{assessment_id}")
async def get_assessment(assessment_id: str) -> dict[str, Any]:
    pool = get_pool()
    if not pool:
        return _error_response("Database pool not initialized")
    
    # We exclude the raw binary geom column
    query = """
        SELECT 
            id, lat, lon, province, district, address_note, input_type,
            photo_path, video_path, ortho_path, chip_path, drone_frames,
            severity, damage_type, damage_description, structural_risk,
            building_type, building_floors, building_material, osm_building_id,
            estimated_occupants, occupant_status, recommended_action, action_priority,
            flood_zone, flood_return_period, elevation_m, slope_degrees, slope_risk,
            nearest_shelter, shelter_distance_m, shelter_type, road_access, nearest_road,
            road_distance_m, reasoning, warnings, confidence, turkish_summary,
            model_used, inference_seconds, worker_name, worker_device, field_note,
            batch_id, batch_building_count, status, verified_by_ground,
            response_team, response_notes, created_at, updated_at, responded_at
        FROM assessments
        WHERE id = $1
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, assessment_id)
            if not row:
                return _error_response("Assessment not found")
                
            d = dict(row)
            for k in ["created_at", "updated_at", "responded_at"]:
                if d.get(k) and hasattr(d[k], "isoformat"):
                    d[k] = d[k].isoformat()
                    
            return _success_response(d)
    except Exception as exc:
        return _error_response(f"Failed to fetch assessment: {exc}")
