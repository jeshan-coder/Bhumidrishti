"""Pydantic models for the uploads table.

uploads tracks every file written to disk by field workers before
Gemma 4 analysis begins.  Assessments are the *output*; uploads are
the *input*.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ── Type aliases ────────────────────────────────────────────────────────────

FileType = Literal[
    "ground_photo",
    "drone_image",
    "drone_orthophoto",
    "video",
    "video_frame",
]

UploadStatus = Literal[
    "uploaded",    # file saved to disk, not yet queued
    "queued",      # added to processing queue
    "processing",  # Gemma 4 currently analyzing
    "done",        # analysis complete, assessment created
    "failed",      # processing failed, see error_message
    "skipped",     # deliberately skipped (e.g. duplicate)
]

LocationSource = Literal["device_gps", "exif", "manual", "unknown"]


# ── Full upload record ──────────────────────────────────────────────────────

class Upload(BaseModel):
    """Complete upload record as stored in the uploads table."""

    # Identity
    id: str | None = None

    # File info
    original_filename: str
    saved_path: str
    file_type: FileType
    mime_type: str | None = None
    file_size_bytes: int | None = None

    # Location
    lat: float | None = None
    lon: float | None = None
    location_source: LocationSource = "device_gps"
    gps_accuracy_m: float | None = None

    # Processing status
    status: UploadStatus = "uploaded"
    is_analyzed: bool = False

    # Video-specific
    duration_seconds: float | None = None
    frames_extracted: int | None = None

    # Orthophoto-specific
    is_georeferenced: bool = False
    cog_path: str | None = None
    bounds_west: float | None = None
    bounds_south: float | None = None
    bounds_east: float | None = None
    bounds_north: float | None = None

    # Relationships
    assessment_id: str | None = None
    batch_id: str | None = None
    parent_upload_id: str | None = None

    # Error handling
    error_message: str | None = None
    retry_count: int = 0

    # Field worker
    worker_name: str | None = None
    field_note: str | None = None

    # Timestamps
    uploaded_at: datetime | None = None
    processing_started_at: datetime | None = None
    processing_done_at: datetime | None = None
    updated_at: datetime | None = None


# ── Request models ──────────────────────────────────────────────────────────

class UploadCreate(BaseModel):
    """Internal helper used by the router after the file is saved to disk.

    The router fills all required fields from the uploaded file and form
    data, then passes this model to the DB insert helper.
    """

    id: str
    original_filename: str
    saved_path: str
    file_type: FileType
    mime_type: str | None = None
    file_size_bytes: int | None = None
    lat: float | None = None
    lon: float | None = None
    location_source: LocationSource = "device_gps"
    gps_accuracy_m: float | None = None
    worker_name: str | None = None
    field_note: str | None = None
    # Orthophoto extras (optional)
    is_georeferenced: bool = False
    bounds_west: float | None = None
    bounds_south: float | None = None
    bounds_east: float | None = None
    bounds_north: float | None = None
    batch_id: str | None = None
    parent_upload_id: str | None = None


# ── Response models ─────────────────────────────────────────────────────────

class UploadListResponse(BaseModel):
    """Paginated list of upload records."""

    uploads: list[Upload]
    total_count: int
    page: int
    page_size: int
