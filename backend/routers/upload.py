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
import os
import random
import string
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from PIL import Image

from db.postgres import get_pool
from models.upload import Upload, UploadCreate, UploadListResponse
from services.pipeline_worker import analyze_upload

router = APIRouter(prefix="/uploads", tags=["uploads"])

# ── Storage config ──────────────────────────────────────────────────────────

UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR", "/app/data/uploads")).resolve()
MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

ALLOWED_GROUND_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png"}
ALLOWED_ORTHO_EXTENSIONS: set[str] = {".tif", ".tiff", ".geotiff", ".jpg", ".jpeg", ".png"}
ALLOWED_VIDEO_EXTENSIONS: set[str] = {".mp4", ".mov"}

MIME_MAP: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".geotiff": "image/tiff",
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
            gps_accuracy_m, worker_name, field_note,
            is_georeferenced, bounds_west, bounds_south,
            bounds_east, bounds_north, batch_id, parent_upload_id
        ) VALUES (
            $1,  $2,  $3,  $4,  $5,
            $6,  $7,  $8,  $9,
            $10, $11, $12,
            $13, $14, $15,
            $16, $17, $18, $19
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
        return _error(f"Ground photo upload failed: {exc}")
    except Exception as exc:
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
        latitude = _parse_coord(lat, -90.0, 90.0)
        longitude = _parse_coord(lon, -180.0, 180.0)

        ext = _ext(file.filename)
        if ext not in ALLOWED_ORTHO_EXTENSIONS:
            return _error(f"Unsupported extension '{ext}'. Allowed: {ALLOWED_ORTHO_EXTENSIONS}")

        file_bytes = await file.read()
        if not file_bytes:
            return _error("Uploaded file is empty")
        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            return _error("File exceeds 2 GB limit")

        image_meta = await asyncio.to_thread(_validate_image, file_bytes)
        upload_id = _generate_upload_id()

        # Determine if ground_photo extension or actual orthophoto
        file_type: str = "drone_orthophoto" if ext in {".tif", ".tiff", ".geotiff"} else "drone_image"
        relative_path, absolute_path = await _save_file(file_bytes, FOLDER_MAP["drone_orthophoto"], ext, upload_id)

        geo = is_georeferenced.strip().lower() in ("true", "1", "yes")

        def _opt_float(v: str | None) -> float | None:
            return float(v.strip()) if v and v.strip() else None

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
            bounds_west=_opt_float(bounds_west),
            bounds_south=_opt_float(bounds_south),
            bounds_east=_opt_float(bounds_east),
            bounds_north=_opt_float(bounds_north),
            batch_id=batch_id,
        )
        await _insert_upload(payload)

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
        return _error(f"Orthophoto upload failed: {exc}")
    except Exception as exc:
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
        if len(file_bytes) > MAX_FILE_SIZE_BYTES:
            return _error("File exceeds 2 GB limit")

        upload_id = _generate_upload_id()
        relative_path, absolute_path = await _save_file(file_bytes, FOLDER_MAP["video"], ext, upload_id)

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
            worker_name=worker_name,
            field_note=field_note,
        )
        await _insert_upload(payload)

        return _success({
            "upload_id": upload_id,
            "file_type": "video",
            "original_filename": file.filename,
            "saved_path": relative_path,
            "stored_at": absolute_path,
            "size_bytes": len(file_bytes),
            "lat": latitude,
            "lon": longitude,
            "worker_name": worker_name,
            "status": "uploaded",
        })

    except ValueError as exc:
        return _error(f"Video upload failed: {exc}")
    except Exception as exc:
        return _error(f"Video upload failed: {exc}")


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
    try:
        pool = get_pool()
        if pool is None:
            return _error("Database not available")

        statuses = ("uploaded", "queued", "processing", "failed")
        
        # Use PostGIS to cluster uploads by proximity
        # ST_ClusterDBSCAN assigns cluster IDs to points within radius
        sql = """
            WITH unfinished AS (
                SELECT 
                    id, original_filename, saved_path, file_type, mime_type,
                    file_size_bytes, lat, lon, location_source, gps_accuracy_m,
                    status, is_analyzed, duration_seconds, frames_extracted,
                    is_georeferenced, cog_path, bounds_west, bounds_south, bounds_east, bounds_north,
                    assessment_id, batch_id, parent_upload_id,
                    error_message, retry_count, worker_name, field_note,
                    uploaded_at, processing_started_at, processing_done_at, updated_at,
                    ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography AS geom
                FROM uploads
                WHERE status = ANY($1::text[])
                  AND COALESCE(is_analyzed, FALSE) = FALSE
                  AND assessment_id IS NULL
                  AND lat IS NOT NULL 
                  AND lon IS NOT NULL
            ),
            clustered AS (
                SELECT 
                    *,
                    ST_ClusterDBSCAN(geom, $2, 1) OVER () AS cluster_id
                FROM unfinished
            )
            SELECT 
                cluster_id,
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
            FROM clustered
            GROUP BY cluster_id
            ORDER BY upload_count DESC, MAX(uploaded_at) DESC
            LIMIT $3
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, list(statuses), radius_meters, limit)

        # Format response
        location_groups = []
        for row in rows:
            cluster_id = row["cluster_id"]
            uploads = row["uploads"]
            
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
        if row["status"] in ("processing", "done"):
            return _error(f"Upload '{upload_id}' is already {row['status']}")

        # Trigger in background so endpoint returns quickly
        asyncio.create_task(analyze_upload(upload_id))

        return _success({
            "upload_id": upload_id,
            "status": "processing",
            "message": "Analysis started in background"
        })

    except Exception as exc:
        return _error(f"Failed to trigger analysis: {exc}")
