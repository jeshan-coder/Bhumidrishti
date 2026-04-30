"""Background worker to dispatch uploads to the Gemma 4 pipeline."""

import asyncio
import logging
import os
import uuid
from typing import Any
from pathlib import Path

from db.postgres import get_pool
from services.gemma_pipeline import run_assessment_agent

logger = logging.getLogger(__name__)

# Polling interval in seconds
POLL_INTERVAL = 10

# Configure root upload path as in routers
UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR", "/app/data/uploads")).resolve()


async def generate_assessment_id() -> str:
    """Generate a unique ID for an assessment."""
    return f"ASS-{str(uuid.uuid4().int)[:6]}"


async def process_upload(upload_row: dict[str, Any], pool) -> None:
    """Process a single upload through Gemma 4."""
    upload_id = upload_row["id"]
    file_type = upload_row["file_type"]
    saved_path = upload_row["saved_path"]
    lat = upload_row["lat"]
    lon = upload_row["lon"]
    field_note = upload_row["field_note"]

    logger.info(f"Starting processing for upload {upload_id}")

    absolute_path = str(UPLOAD_ROOT.parent / saved_path) if saved_path else ""
    if not os.path.exists(absolute_path):
        logger.error(f"File not found on disk: {absolute_path}")
        await set_upload_status(pool, upload_id, "failed", error_message="File not found on disk")
        return

    # To do video, we'd need multiple frames. For now, we process 1 image per upload if ground_photo.
    # If video, maybe we just pass the video if Ollama supports it, or if it doesn't, we fail gracefully. 
    # Actually, LLaVa/Gemma vision typically requires images not videos.
    image_paths = [absolute_path] if file_type in ("ground_photo", "drone_image", "video_frame") else []

    if not image_paths:
        await set_upload_status(pool, upload_id, "failed", error_message="No images extracted or supported for analysis.")
        return

    try:
        # Run agent loop
        assessment_data = await run_assessment_agent(
            image_paths=image_paths,
            lat=lat,
            lon=lon,
            input_type=file_type,
            db=pool,
            field_note=field_note,
        )

        assessment_id = await generate_assessment_id()

        # Insert into assessments
        sql_insert = """
            INSERT INTO assessments (
                id, lat, lon, input_type, photo_path, 
                severity, damage_type, damage_description, structural_risk,
                building_type, building_floors, building_material, 
                estimated_occupants, occupant_status, recommended_action, 
                action_priority, flood_zone, elevation_m, slope_degrees, 
                slope_risk, nearest_shelter, shelter_distance_m, shelter_type, 
                road_access, nearest_road, road_distance_m, reasoning, 
                confidence, turkish_summary, field_note, status
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10, $11, $12,
                $13, $14, $15,
                $16, $17, $18, $19,
                $20, $21, $22, $23,
                $24, $25, $26, $27,
                $28, $29, $30, 'pending'
            )
        """
        async with pool.acquire() as conn:
            await conn.execute(
                sql_insert,
                assessment_id, lat, lon, file_type, saved_path,
                assessment_data.get("severity"),
                assessment_data.get("damage_type"),
                assessment_data.get("damage_description"),
                assessment_data.get("structural_risk"),
                assessment_data.get("building_type"),
                assessment_data.get("building_floors"),
                assessment_data.get("building_material"),
                assessment_data.get("estimated_occupants"),
                assessment_data.get("occupant_status"),
                assessment_data.get("recommended_action"),
                assessment_data.get("action_priority"),
                assessment_data.get("flood_zone", False),
                assessment_data.get("elevation_m"),
                assessment_data.get("slope_degrees"),
                assessment_data.get("slope_risk"),
                assessment_data.get("nearest_shelter"),
                assessment_data.get("shelter_distance_m"),
                assessment_data.get("shelter_type"),
                assessment_data.get("road_access"),
                assessment_data.get("nearest_road"),
                assessment_data.get("road_distance_m"),
                assessment_data.get("reasoning"),
                assessment_data.get("confidence"),
                assessment_data.get("turkish_summary"),
                field_note,
            )

        # Update uploads table
        sql_update = """
            UPDATE uploads 
            SET status = 'done', is_analyzed = TRUE, assessment_id = $1, processing_done_at = NOW() 
            WHERE id = $2
        """
        async with pool.acquire() as conn:
            await conn.execute(sql_update, assessment_id, upload_id)

        logger.info(f"Successfully processed upload {upload_id} -> Assessment {assessment_id}")

    except Exception as e:
        logger.error(f"Error processing upload {upload_id}: {e}")
        await set_upload_status(pool, upload_id, "failed", error_message=str(e))


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
    try:
        pool = get_pool()
        if not pool:
            logger.error("No database pool available.")
            return

        query = """
            SELECT id, file_type, saved_path, lat, lon, field_note 
            FROM uploads 
            WHERE id = $1 AND is_analyzed = FALSE
        """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, upload_id)
        
        if row:
            upload_dict = dict(row)
            
            # Mark as processing
            async with pool.acquire() as conn:
                await conn.execute("UPDATE uploads SET status = 'processing', processing_started_at = NOW() WHERE id = $1", upload_id)

            await process_upload(upload_dict, pool)
        else:
            logger.warning(f"Upload {upload_id} not found or already analyzed.")

    except Exception as e:
        logger.error(f"Error in manual analysis for upload {upload_id}: {e}")
