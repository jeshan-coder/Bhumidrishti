"""Report generation service for site and building markdown reports."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
from fpdf import FPDF
import httpx
from PIL import ImageDraw
from ollama import AsyncClient
from staticmap import CircleMarker, Line, StaticMap

from models.report import ReportGenerateRequest
from prompts.base_system_prompt import BHUMIDRISHTI_BASE_SYSTEM_PROMPT
from prompts.report_system_prompt import REPORT_GENERATION_SYSTEM_PROMPT
from services.gis import query_osrm_route
from services.tools import ALL_TOOLS, REPORT_TOOLS

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
ollama_client = AsyncClient(host=OLLAMA_HOST)
# Force report generation model to Gemma 4 26B.
REPORT_MODEL = os.getenv("REPORT_MODEL", "gemma4:26b")

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/app/data/uploads")).resolve()
REPORTS_DIR = UPLOAD_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
BACKEND_PUBLIC_URL = os.getenv("BACKEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")
OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "http://osrm:5000").rstrip("/")

# Tool schemas are now centralized in services/tools.py.
# REPORT_TOOLS = report-specific fetchers (imported above)
# ALL_TOOLS    = every tool the report agent can use (imported above)


def _safe_json(data: Any) -> str:
    try:
        return json.dumps(data, default=str, ensure_ascii=False)
    except Exception:
        return str(data)


def _log_report_event(stage: str, payload: dict[str, Any]) -> None:
    logger.info("report_stage=%s payload=%s", stage, _safe_json(payload))


@dataclass
class SiteStats:
    """Aggregated site report statistics."""

    total_buildings: int
    sev5: int
    sev4: int
    sev3: int
    signs_of_life: int
    flood_count: int
    blocked_roads_count: int
    avg_confidence: float | None
    estimated_people: int


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except Exception:
            return default
    return default


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except Exception:
            return None
    return None


def _extract_occupants(raw_value: Any) -> int:
    """Estimate occupants from strings like '6-8' or '12'."""
    if raw_value is None:
        return 0
    if isinstance(raw_value, (int, float)):
        return max(0, int(round(float(raw_value))))
    if not isinstance(raw_value, str):
        return 0
    nums = re.findall(r"\d+", raw_value)
    if not nums:
        return 0
    values = [int(token) for token in nums]
    if len(values) == 1:
        return max(0, values[0])
    return max(0, int(round(sum(values) / len(values))))


def _format_timestamp(value: datetime | None = None) -> str:
    source = value or datetime.now(timezone.utc)
    return source.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _chunk_text(text: str, chunk_size: int = 120) -> list[str]:
    if not text:
        return []
    return [text[idx : idx + chunk_size] for idx in range(0, len(text), chunk_size)]


def _route_minutes(seconds: float | None) -> int | None:
    if seconds is None:
        return None
    return max(1, int(round(seconds / 60.0)))


def _centroid_from_rows(rows: list[asyncpg.Record]) -> tuple[float, float] | None:
    points = []
    for row in rows:
        lat = _safe_float(row.get("lat"))
        lon = _safe_float(row.get("lon"))
        if lat is None or lon is None:
            continue
        points.append((lat, lon))
    if not points:
        return None
    return (sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points))


async def _fetch_site_boundary(
    conn: asyncpg.Connection,
    request: ReportGenerateRequest,
) -> list[list[tuple[float, float]]]:
    if request.site_id is not None:
        row = await conn.fetchrow(
            "SELECT ST_AsGeoJSON(boundary)::text AS boundary_geojson FROM sites WHERE id = $1 LIMIT 1",
            request.site_id,
        )
    else:
        row = await conn.fetchrow(
            "SELECT ST_AsGeoJSON(boundary)::text AS boundary_geojson FROM sites WHERE LOWER(name) = LOWER($1) LIMIT 1",
            request.site_name or "",
        )
    if not row:
        return []
    raw_geojson = row.get("boundary_geojson")
    if not isinstance(raw_geojson, str):
        return []
    try:
        parsed = json.loads(raw_geojson)
    except Exception:
        return []
    geom_type = str(parsed.get("type") or "")
    coords = parsed.get("coordinates")
    polygons: list[list[tuple[float, float]]] = []
    if geom_type == "Polygon" and isinstance(coords, list) and coords:
        rings = [coords[0]]
    elif geom_type == "MultiPolygon" and isinstance(coords, list):
        rings = [poly[0] for poly in coords if isinstance(poly, list) and poly]
    else:
        rings = []
    for ring in rings:
        if not isinstance(ring, list):
            continue
        points: list[tuple[float, float]] = []
        for pair in ring:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            try:
                lon = float(pair[0])
                lat = float(pair[1])
                points.append((lat, lon))
            except Exception:
                continue
        if len(points) >= 3:
            polygons.append(points)
    return polygons


async def _fetch_nearest_shelter(
    conn: asyncpg.Connection,
    centroid: tuple[float, float] | None,
) -> tuple[float, float, str] | None:
    if centroid is None:
        return None
    lat, lon = centroid
    row = await conn.fetchrow(
        """
        SELECT
            ST_Y(ST_Centroid(geom)) AS lat,
            ST_X(ST_Centroid(geom)) AS lon,
            COALESCE(NULLIF(name, ''), 'Shelter') AS name
        FROM turkey_points
        WHERE amenity IN ('shelter', 'hospital', 'school', 'clinic', 'community_centre')
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint($1, $2), 4326)
        LIMIT 1
        """,
        lon,
        lat,
    )
    if not row:
        return None
    shelter_lat = _safe_float(row.get("lat"))
    shelter_lon = _safe_float(row.get("lon"))
    shelter_name = str(row.get("name") or "Shelter")
    if shelter_lat is None or shelter_lon is None:
        return None
    return (shelter_lat, shelter_lon, shelter_name)


async def _fetch_osrm_steps(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> list[str]:
    route_url = f"{OSRM_BASE_URL}/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}"
    params = {"overview": "false", "steps": "true", "geometries": "geojson"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(route_url, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return []
    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        return []
    legs = routes[0].get("legs")
    if not isinstance(legs, list) or not legs:
        return []
    steps = legs[0].get("steps")
    if not isinstance(steps, list):
        return []
    results: list[str] = []
    for step in steps[:25]:
        if not isinstance(step, dict):
            continue
        maneuver = step.get("maneuver") if isinstance(step.get("maneuver"), dict) else {}
        instruction = maneuver.get("instruction") or maneuver.get("type") or "Continue"
        distance = step.get("distance")
        distance_m = f"{int(round(float(distance)))}m" if isinstance(distance, (int, float)) else ""
        results.append(f"{instruction} {distance_m}".strip())
    return results

def _language_name(language_code: str) -> str:
    names = {"en": "English", "tr": "Turkish", "ar": "Arabic", "fr": "French", "ne": "Nepali"}
    return names.get(language_code.lower(), language_code)


def _parse_frames(raw_frames: Any) -> list[str]:
    if isinstance(raw_frames, list):
        return [str(item).strip() for item in raw_frames if isinstance(item, str) and item.strip()]
    if isinstance(raw_frames, str) and raw_frames.strip():
        try:
            parsed = json.loads(raw_frames)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if isinstance(item, str) and item.strip()]
        except Exception:
            return []
    return []


def _to_media_url(path_value: Any) -> str | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    normalized = path_value.replace("\\", "/").strip()
    if normalized.startswith("http://") or normalized.startswith("https://"):
        return normalized
    if normalized.startswith("/media/uploads/"):
        return f"{BACKEND_PUBLIC_URL}{normalized}"
    if normalized.startswith("media/uploads/"):
        return f"{BACKEND_PUBLIC_URL}/{normalized}"
    if normalized.startswith("uploads/"):
        return f"{BACKEND_PUBLIC_URL}/media/{normalized}"
    return None


def _pick_visual_images(row: asyncpg.Record) -> tuple[str | None, str | None]:
    pre_image = _to_media_url(row.get("pre_chip_path"))
    post_candidates = [
        _to_media_url(row.get("chip_path")),
        _to_media_url(row.get("photo_path")),
    ]
    frame_candidates = [_to_media_url(item) for item in _parse_frames(row.get("drone_frames"))]
    post_image = next((img for img in [*post_candidates, *frame_candidates] if isinstance(img, str) and img), None)
    return pre_image, post_image


def _image_pair_markdown(left_url: str | None, right_url: str | None) -> str:
    if not left_url and not right_url:
        return ""
    left_cell = f"![Pre image]({left_url})" if left_url else "_No pre image available_"
    right_cell = f"![Post image]({right_url})" if right_url else "_No post/ground/frame image available_"
    return (
        "\n| Pre-earthquake / Reference | Post-earthquake / Ground / Frame |\n"
        "| --- | --- |\n"
        f"| {left_cell} | {right_cell} |\n"
    )


def _resolve_media_local_path(image_url: str) -> Path | None:
    normalized = image_url.replace("\\", "/")
    marker = "/media/uploads/"
    marker_idx = normalized.find(marker)
    if marker_idx != -1:
        relative = normalized[marker_idx + len(marker) :]
        candidate = UPLOAD_DIR / relative
        if candidate.exists():
            return candidate
    if normalized.startswith("uploads/"):
        candidate = Path("/app/data") / normalized
        if candidate.exists():
            return candidate
    if normalized.startswith(str(UPLOAD_DIR).replace("\\", "/")):
        candidate = Path(normalized)
        if candidate.exists():
            return candidate
    return None


def _extract_image_urls(markdown_text: str) -> list[str]:
    if not markdown_text:
        return []
    matches = re.findall(r"!\[[^\]]*]\(([^)]+)\)", markdown_text)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in matches:
        image_url = str(item).strip()
        if image_url and image_url not in seen:
            seen.add(image_url)
            deduped.append(image_url)
    return deduped


def _markdown_to_text_lines(markdown_text: str) -> list[str]:
    raw = markdown_text
    raw = re.sub(r"!\[[^\]]*]\(([^)]+)\)", "", raw)
    raw = re.sub(r"`([^`]+)`", r"\1", raw)
    raw = re.sub(r"\*\*([^*]+)\*\*", r"\1", raw)
    raw = re.sub(r"#+\s*", "", raw)
    raw = re.sub(r"\|\s*---\s*\|", "", raw)
    return [line.rstrip() for line in raw.splitlines()]


def _render_pdf(report_id: str, markdown_text: str) -> str:
    output_path = REPORTS_DIR / f"{report_id}.pdf"
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)

    content_width = pdf.w - pdf.l_margin - pdf.r_margin
    for line in _markdown_to_text_lines(markdown_text):
        if not line.strip():
            pdf.ln(2)
            continue
        wrapped = textwrap.wrap(line, width=105) or [line]
        for wrapped_line in wrapped:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(content_width, 6, wrapped_line)

    image_urls = _extract_image_urls(markdown_text)
    if image_urls:
        pdf.add_page()
        pdf.set_font("Helvetica", style="B", size=13)
        pdf.cell(0, 8, "Image Annex", ln=1)
        pdf.set_font("Helvetica", size=10)
        for idx in range(0, len(image_urls), 2):
            left_url = image_urls[idx]
            right_url = image_urls[idx + 1] if idx + 1 < len(image_urls) else None
            left_path = _resolve_media_local_path(left_url)
            right_path = _resolve_media_local_path(right_url) if right_url else None

            y_start = pdf.get_y()
            if left_path is not None:
                pdf.image(str(left_path), x=10, y=y_start + 2, w=90, h=65, keep_aspect_ratio=True)
            if right_path is not None:
                pdf.image(str(right_path), x=110, y=y_start + 2, w=90, h=65, keep_aspect_ratio=True)
            pdf.ln(72)
            if pdf.get_y() > 260:
                pdf.add_page()

    pdf.output(str(output_path))
    return f"uploads/reports/{report_id}.pdf"


def _severity_color(severity: int) -> str:
    if severity >= 5:
        return "#7f1d1d"
    if severity == 4:
        return "#dc2626"
    if severity == 3:
        return "#f59e0b"
    if severity == 2:
        return "#eab308"
    return "#16a34a"


def _draw_map_overlays(image) -> None:
    draw = ImageDraw.Draw(image)
    width, height = image.size

    # North arrow
    arrow_x = width - 45
    arrow_y = 55
    draw.polygon([(arrow_x, arrow_y - 22), (arrow_x - 10, arrow_y + 8), (arrow_x + 10, arrow_y + 8)], fill="black")
    draw.text((arrow_x - 4, arrow_y + 12), "N", fill="black")

    # Legend
    legend_x = 20
    legend_y = height - 180
    draw.rectangle((legend_x, legend_y, legend_x + 260, legend_y + 150), fill="white", outline="black", width=2)
    draw.text((legend_x + 10, legend_y + 8), "Legend", fill="black")
    legend_items = [
        ("Severity 5", "#7f1d1d"),
        ("Severity 4", "#dc2626"),
        ("Severity 3", "#f59e0b"),
        ("Severity 2", "#eab308"),
        ("Severity 1", "#16a34a"),
        ("Shelter", "#0f9d58"),
    ]
    for idx, (label, color) in enumerate(legend_items):
        y = legend_y + 30 + idx * 18
        draw.rectangle((legend_x + 10, y, legend_x + 22, y + 12), fill=color, outline="black")
        draw.text((legend_x + 30, y), label, fill="black")

    # Scale bar (visual)
    scale_x = width - 220
    scale_y = height - 45
    draw.rectangle((scale_x, scale_y, scale_x + 160, scale_y + 8), fill="black")
    draw.text((scale_x, scale_y - 18), "Approx scale", fill="black")


async def create_site_static_map(
    *,
    pool: asyncpg.Pool,
    request: ReportGenerateRequest,
    report_id: str,
    rows: list[asyncpg.Record],
) -> tuple[str | None, str | None, list[str]]:
    centroid = _centroid_from_rows(rows)
    if centroid is None:
        return None, None, []

    async with pool.acquire() as conn:
        boundaries = await _fetch_site_boundary(conn, request)
        shelter = await _fetch_nearest_shelter(conn, centroid)

    static_map = StaticMap(1400, 950, url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png")

    for boundary in boundaries:
        polyline = [(point[1], point[0]) for point in boundary]
        static_map.add_line(Line(polyline, "#111827", 3))

    for row in rows[:250]:
        lat = _safe_float(row.get("lat"))
        lon = _safe_float(row.get("lon"))
        if lat is None or lon is None:
            continue
        severity = _safe_int(row.get("severity"), 1)
        static_map.add_marker(CircleMarker((lon, lat), _severity_color(severity), 10 + (severity * 2)))

    route_steps: list[str] = []
    if shelter is not None:
        shelter_lat, shelter_lon, _ = shelter
        static_map.add_marker(CircleMarker((shelter_lon, shelter_lat), "#0f9d58", 14))
        route = await query_osrm_route(
            start_lat=centroid[0],
            start_lon=centroid[1],
            end_lat=shelter_lat,
            end_lon=shelter_lon,
            profile="driving",
        )
        if route.found and isinstance(route.geometry_geojson, dict):
            coordinates = route.geometry_geojson.get("coordinates")
            if isinstance(coordinates, list) and coordinates:
                path = []
                for pair in coordinates:
                    if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                        continue
                    try:
                        path.append((float(pair[0]), float(pair[1])))
                    except Exception:
                        continue
                if len(path) > 1:
                    static_map.add_line(Line(path, "#2563eb", 4))
        route_steps = await _fetch_osrm_steps(centroid[0], centroid[1], shelter_lat, shelter_lon)

    image = static_map.render()
    _draw_map_overlays(image)
    filename = f"{report_id}_map_site.png"
    absolute_path = REPORTS_DIR / filename
    await asyncio.to_thread(image.save, absolute_path)
    relative_path = f"uploads/reports/{filename}"
    url = f"{BACKEND_PUBLIC_URL}/media/uploads/reports/{filename}"
    return relative_path, url, route_steps

def _site_stats(rows: list[asyncpg.Record]) -> SiteStats:
    total = len(rows)
    sev5 = 0
    sev4 = 0
    sev3 = 0
    signs = 0
    flood = 0
    blocked = 0
    confidence_values: list[float] = []
    people = 0

    for row in rows:
        severity = _safe_int(row.get("severity"), 0)
        if severity >= 5:
            sev5 += 1
        if severity == 4:
            sev4 += 1
        if severity == 3:
            sev3 += 1
        if str(row.get("occupant_status") or "").lower() in {"trapped", "evacuated"}:
            signs += 1
        if bool(row.get("flood_zone")):
            flood += 1
        if str(row.get("road_access") or "").lower() == "blocked":
            blocked += 1
        confidence = _safe_float(row.get("confidence"))
        if confidence is not None:
            confidence_values.append(confidence)
        people += _extract_occupants(row.get("estimated_occupants"))

    avg_conf = None
    if confidence_values:
        avg_conf = round(sum(confidence_values) / len(confidence_values), 3)

    return SiteStats(
        total_buildings=total,
        sev5=sev5,
        sev4=sev4,
        sev3=sev3,
        signs_of_life=signs,
        flood_count=flood,
        blocked_roads_count=blocked,
        avg_confidence=avg_conf,
        estimated_people=people,
    )


async def _query_site_rows_by_ref(
    db: asyncpg.Connection | asyncpg.Pool,
    *,
    site_id: int | None,
    site_name: str | None,
    limit: int = 500,
) -> list[asyncpg.Record]:
    safe_limit = max(1, min(limit, 1000))

    # When a site_id is given, also resolve its canonical name from the sites table.
    # Many assessments pre-date the site_id FK migration so they are only linked by
    # site_name. We match on BOTH to cover old and new data.
    resolved_name: str | None = site_name
    if site_id is not None:
        try:
            name_row = await db.fetchrow("SELECT name FROM sites WHERE id = $1", site_id)
            if name_row:
                resolved_name = str(name_row["name"])
        except Exception:
            pass

    # Build a WHERE clause that matches by site_id FK where it exists AND by name.
    # This guarantees we find rows regardless of whether the FK was populated.
    where_parts: list[str] = []
    args: list[Any] = []

    if site_id is not None:
        # Match rows that carry the site_id FK (new data)
        where_parts.append(
            f"(a.site_id = ${len(args)+1} OR b.site_id = ${len(args)+1})"
        )
        args.append(site_id)

    if resolved_name:
        # Match rows that carry only the site_name text (old data without FK)
        where_parts.append(
            f"LOWER(COALESCE(NULLIF(a.site_name, ''), NULLIF(b.site_name, ''), '')) = LOWER(${len(args)+1})"
        )
        args.append(resolved_name)

    if not where_parts:
        return []

    # Combine with OR so either condition is sufficient
    where_clause = "AND (" + " OR ".join(where_parts) + ")"

    query = f"""
        SELECT
            a.id,
            a.severity,
            a.damage_type,
            a.damage_description,
            a.building_type,
            a.estimated_occupants,
            a.occupant_status,
            a.flood_zone,
            a.road_access,
            a.nearest_road,
            a.lat,
            a.lon,
            a.nearest_shelter,
            a.shelter_distance_m,
            a.shelter_type,
            a.confidence,
            a.recommended_action,
            a.reasoning,
            a.warnings,
            a.action_priority,
            a.pre_chip_path,
            a.chip_path,
            a.photo_path,
            a.drone_frames,
            a.province,
            a.district,
            COALESCE(NULLIF(a.site_name, ''), NULLIF(b.site_name, ''), 'Unknown') AS resolved_site_name
        FROM assessments a
        LEFT JOIN batches b ON a.batch_id = b.id
        WHERE LOWER(COALESCE(a.status, '')) <> 'false_positive'
          {where_clause}
        ORDER BY COALESCE(a.action_priority, 999), COALESCE(a.severity, 0) DESC, a.created_at DESC
        LIMIT {safe_limit}
    """
    return await db.fetch(query, *args)


async def create_report_record(pool: asyncpg.Pool, request: ReportGenerateRequest) -> str:
    """Insert initial reports row with generating status."""
    report_id = f"RPT-{uuid4().hex[:8].upper()}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO reports (
                id, report_type, site_id, assessment_id, team_name, language,
                file_path, status, created_by, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, NULL, 'generating', $7, NOW())
            """,
            report_id,
            request.report_type,
            str(request.site_id) if request.site_id is not None else request.site_name,
            request.assessment_id,
            request.team_name,
            request.language.lower(),
            request.created_by or "coordinator",
        )
    return report_id


async def update_report_record(
    pool: asyncpg.Pool,
    report_id: str,
    *,
    status: str,
    file_path: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update report status and file path after generation."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE reports
            SET status = $2,
                file_path = COALESCE($3, file_path),
                error_message = $4
            WHERE id = $1
            """,
            report_id,
            status,
            file_path,
            error_message,
        )


async def load_site_rows(pool: asyncpg.Pool, request: ReportGenerateRequest) -> list[asyncpg.Record]:
    """Fetch site-level assessment rows used in report generation."""
    async with pool.acquire() as conn:
        return await _query_site_rows_by_ref(
            conn,
            site_id=request.site_id,
            site_name=request.site_name,
            limit=500,
        )


async def load_building_row(pool: asyncpg.Pool, assessment_id: str) -> asyncpg.Record | None:
    """Fetch one assessment row for a single-building report."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT
                a.id,
                a.site_name,
                a.province,
                a.district,
                a.lat,
                a.lon,
                a.severity,
                a.damage_type,
                a.damage_description,
                a.structural_risk,
                a.building_type,
                a.building_floors,
                a.building_material,
                a.estimated_occupants,
                a.occupant_status,
                a.recommended_action,
                a.action_priority,
                a.flood_zone,
                a.elevation_m,
                a.slope_degrees,
                a.slope_risk,
                a.nearest_road,
                a.road_distance_m,
                a.road_access,
                a.nearest_shelter,
                a.shelter_type,
                a.shelter_distance_m,
                a.confidence,
                a.reasoning,
                a.warnings,
                a.turkish_summary,
                a.model_used,
                a.photo_path,
                a.chip_path,
                a.pre_chip_path,
                a.drone_frames,
                COALESCE(NULLIF(b.site_name, ''), NULLIF(a.site_name, ''), 'Unknown') AS resolved_site_name
            FROM assessments a
            LEFT JOIN batches b ON a.batch_id = b.id
            WHERE a.id = $1
            LIMIT 1
            """,
            assessment_id,
        )


async def _fetch_building_row_by_id(
    db: asyncpg.Connection | asyncpg.Pool,
    assessment_id: str,
) -> asyncpg.Record | None:
    query = """
        SELECT
            a.id,
            a.site_name,
            a.province,
            a.district,
            a.lat,
            a.lon,
            a.severity,
            a.damage_type,
            a.damage_description,
            a.structural_risk,
            a.building_type,
            a.building_floors,
            a.building_material,
            a.estimated_occupants,
            a.occupant_status,
            a.recommended_action,
            a.action_priority,
            a.flood_zone,
            a.elevation_m,
            a.slope_degrees,
            a.slope_risk,
            a.nearest_road,
            a.road_distance_m,
            a.road_access,
            a.nearest_shelter,
            a.shelter_type,
            a.shelter_distance_m,
            a.confidence,
            a.reasoning,
            a.warnings,
            a.turkish_summary,
            a.model_used,
            a.photo_path,
            a.chip_path,
            a.pre_chip_path,
            a.drone_frames,
            COALESCE(NULLIF(b.site_name, ''), NULLIF(a.site_name, ''), 'Unknown') AS resolved_site_name
        FROM assessments a
        LEFT JOIN batches b ON a.batch_id = b.id
        WHERE a.id = $1
        LIMIT 1
    """
    if isinstance(db, asyncpg.Pool):
        async with db.acquire() as conn:
            return await conn.fetchrow(query, assessment_id)
    return await db.fetchrow(query, assessment_id)


async def get_site_report_data(tool_args: dict[str, Any], db: asyncpg.Connection | asyncpg.Pool | None) -> dict[str, Any]:
    if db is None:
        return {"success": False, "error": "Database unavailable"}
    site_id_raw = tool_args.get("site_id")
    site_name_raw = str(tool_args.get("site_name") or "").strip() or None
    limit = int(tool_args.get("limit") or 500)
    site_id = int(site_id_raw) if site_id_raw is not None else None
    rows = await _query_site_rows_by_ref(db, site_id=site_id, site_name=site_name_raw, limit=limit)
    stats = _site_stats(rows)
    building_items = [_site_row_context(row) for row in rows[:40]]
    resolved_site_name = (
        str(rows[0].get("resolved_site_name"))
        if rows
        else (site_name_raw or (f"site-{site_id}" if site_id is not None else "unknown-site"))
    )
    return {
        "success": True,
        "site_name": resolved_site_name,
        "total_records": len(rows),
        "stats": {
            "total_buildings": stats.total_buildings,
            "severity_5": stats.sev5,
            "severity_4": stats.sev4,
            "severity_3": stats.sev3,
            "signs_of_life": stats.signs_of_life,
            "flood_zone_count": stats.flood_count,
            "blocked_roads_count": stats.blocked_roads_count,
            "avg_confidence": stats.avg_confidence,
            "estimated_people": stats.estimated_people,
        },
        "priority_buildings": building_items,
    }


async def get_building_report_data(
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    if db is None:
        return {"success": False, "error": "Database unavailable"}
    assessment_id = str(tool_args.get("assessment_id") or "").strip()
    if not assessment_id:
        return {"success": False, "error": "assessment_id is required"}
    row = await _fetch_building_row_by_id(db, assessment_id)
    if row is None:
        return {"success": False, "error": f"assessment not found: {assessment_id}"}
    return {"success": True, "building": dict(row)}


async def get_building_route(
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    from_lat = _safe_float(tool_args.get("from_lat"))
    from_lon = _safe_float(tool_args.get("from_lon"))
    to_lat = _safe_float(tool_args.get("to_lat"))
    to_lon = _safe_float(tool_args.get("to_lon"))
    profile = str(tool_args.get("profile") or "driving")
    if None in {from_lat, from_lon, to_lat, to_lon}:
        return {"success": False, "error": "from/to coordinates required"}
    route = await query_osrm_route(
        start_lat=float(from_lat),
        start_lon=float(from_lon),
        end_lat=float(to_lat),
        end_lon=float(to_lon),
        profile=profile,
    )
    steps = await _fetch_osrm_steps(float(from_lat), float(from_lon), float(to_lat), float(to_lon))
    return {
        "success": route.found,
        "profile": route.profile,
        "distance_m": route.distance_m,
        "duration_s": route.duration_s,
        "geometry_geojson": route.geometry_geojson,
        "steps": steps,
        "warnings": route.warnings,
        "error": route.error,
    }


async def generate_situation_summary(
    tool_args: dict[str, Any],
    _db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    language = str(tool_args.get("language") or "en")
    context = tool_args.get("context")
    context_json = json.dumps(context, ensure_ascii=False) if isinstance(context, dict) else str(context or "")
    summary_prompt = (
        "Write 3-4 sentence field coordination situation summary.\n"
        f"Language: {_language_name(language)} ({language}).\n"
        f"Context:\n{context_json}"
    )

    async def _noop_token(_: str) -> None:
        return None

    text = await stream_ai_summary(
        prompt=summary_prompt,
        on_token=_noop_token,
        system_prompt=BHUMIDRISHTI_BASE_SYSTEM_PROMPT,
        db=None,
        tools=None,
    )
    return {"success": True, "summary": text}


async def generate_building_note(
    tool_args: dict[str, Any],
    _db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    language = str(tool_args.get("language") or "en")
    context = tool_args.get("context")
    context_json = json.dumps(context, ensure_ascii=False) if isinstance(context, dict) else str(context or "")
    note_prompt = (
        "Write 3-5 sentence tactical building note for field team.\n"
        f"Language: {_language_name(language)} ({language}).\n"
        f"Context:\n{context_json}"
    )

    async def _noop_token(_: str) -> None:
        return None

    text = await stream_ai_summary(
        prompt=note_prompt,
        on_token=_noop_token,
        system_prompt=BHUMIDRISHTI_BASE_SYSTEM_PROMPT,
        db=None,
        tools=None,
    )
    return {"success": True, "note": text}


async def _dispatch_report_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    """Delegate to the centralized dispatcher in services.tools.

    The central dispatcher handles all 15 tools.  Report-specific tools
    (get_site_report_data, get_building_report_data, get_building_route) are
    lazily imported from this module inside tools.dispatch_tool to avoid
    circular imports.
    """
    from services.tools import dispatch_tool as _central_dispatch  # noqa: PLC0415
    started_at = time.perf_counter()
    _log_report_event(
        "tool_dispatch_started",
        {"tool_name": tool_name, "tool_args": tool_args, "db_available": db is not None},
    )
    if db is None and tool_name not in {"get_building_route"}:
        result: dict[str, Any] = {"error": f"Tool requires database: {tool_name}"}
    else:
        result = await _central_dispatch(tool_name, tool_args, db)
    _log_report_event(
        "tool_dispatch_completed",
        {
            "tool_name": tool_name,
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "result_preview": _safe_json(result)[:1200],
        },
    )
    return result


async def stream_ai_summary(
    *,
    prompt: str,
    on_token,
    on_thinking=None,
    on_tool_call=None,
    on_tool_result=None,
    system_prompt: str | None = None,
    db: asyncpg.Connection | asyncpg.Pool | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> str:
    """Run tool-capable report agent loop and stream final content."""
    try:
        stream_started_at = time.perf_counter()
        messages: list[dict[str, Any]] = []
        active_system_prompt = system_prompt if isinstance(system_prompt, str) and system_prompt.strip() else BHUMIDRISHTI_BASE_SYSTEM_PROMPT
        messages.append({"role": "system", "content": active_system_prompt})
        messages.append({"role": "user", "content": prompt})
        _log_report_event(
            "report_stream_started",
            {
                "model": REPORT_MODEL,
                "prompt_preview": prompt[:1200],
                "prompt_chars": len(prompt),
                "has_system_prompt": True,
                "system_prompt_chars": len(active_system_prompt),
                "tool_count": len(tools or []),
                "tool_names": [
                    t.get("function", {}).get("name")
                    for t in (tools or [])
                    if isinstance(t, dict)
                ],
            },
        )

        iteration = 0
        max_iterations = 12
        any_tool_called = False  # track whether the model has called at least one tool
        while iteration < max_iterations:
            iteration += 1
            has_tool_context = any(str(message.get("role")) == "tool" for message in messages)
            _log_report_event(
                "report_stream_iteration_started",
                {
                    "iteration": iteration,
                    "max_iterations": max_iterations,
                    "message_count": len(messages),
                    "has_tool_context": has_tool_context,
                },
            )

            # Once tool context exists, force final answer generation with live token streaming.
            if has_tool_context:
                final_started_at = time.perf_counter()
                _log_report_event(
                    "report_stream_final_generation_started",
                    {"iteration": iteration, "message_count": len(messages)},
                )
                final_stream = await ollama_client.chat(
                    model=REPORT_MODEL,
                    messages=messages,
                    tools=None,
                    options={"temperature": 0.2},
                    stream=True,
                )
                final_parts: list[str] = []
                final_token_count = 0
                async for chunk in final_stream:
                    message_block = chunk.get("message") if isinstance(chunk, dict) else getattr(chunk, "message", None)
                    if message_block is None:
                        continue
                    thinking = message_block.get("thinking") if isinstance(message_block, dict) else getattr(message_block, "thinking", None)
                    if isinstance(thinking, str) and thinking and on_thinking is not None:
                        await on_thinking(thinking)
                    token = message_block.get("content") if isinstance(message_block, dict) else getattr(message_block, "content", None)
                    if isinstance(token, str) and token:
                        final_parts.append(token)
                        final_token_count += 1
                        await on_token(token)
                final_content = "".join(final_parts).strip()
                _log_report_event(
                    "report_stream_final_generation_completed",
                    {
                        "iteration": iteration,
                        "final_tokens": final_token_count,
                        "final_chars": len(final_content),
                        "elapsed_ms": round((time.perf_counter() - final_started_at) * 1000, 2),
                    },
                )
                if final_content:
                    _log_report_event(
                        "report_stream_completed",
                        {
                            "iterations": iteration,
                            "final_chars": len(final_content),
                            "total_elapsed_ms": round((time.perf_counter() - stream_started_at) * 1000, 2),
                        },
                    )
                    return final_content
                raise ValueError("Model returned empty final report after tool context")

            iteration_started_at = time.perf_counter()
            stream = await ollama_client.chat(
                model=REPORT_MODEL,
                messages=messages,
                tools=tools if tools else None,
                options={"temperature": 0.2},
                stream=True,
            )

            assistant_content_parts: list[str] = []
            assistant_tool_calls: list[dict[str, Any]] = []
            assistant_token_count = 0
            # Accumulate pre-tool reasoning text so it can be sent live to the thinking panel.
            iteration_thinking_buf: list[str] = []

            async for chunk in stream:
                message_block = chunk.get("message") if isinstance(chunk, dict) else getattr(chunk, "message", None)
                if message_block is None:
                    continue
                thinking = message_block.get("thinking") if isinstance(message_block, dict) else getattr(message_block, "thinking", None)
                if isinstance(thinking, str) and thinking and on_thinking is not None:
                    await on_thinking(thinking)

                token = message_block.get("content") if isinstance(message_block, dict) else getattr(message_block, "content", None)
                if isinstance(token, str) and token:
                    assistant_content_parts.append(token)
                    assistant_token_count += 1
                    # This is the tool-calling iteration — any content the model emits here
                    # is reasoning/confirmation, NOT report text.  Route it to the thinking
                    # panel so it never leaks into the report preview.
                    if on_thinking is not None:
                        iteration_thinking_buf.append(token)
                        await on_thinking("".join(iteration_thinking_buf))

                tool_calls_chunk = message_block.get("tool_calls") if isinstance(message_block, dict) else getattr(message_block, "tool_calls", None)
                if isinstance(tool_calls_chunk, list) and tool_calls_chunk:
                    normalized_tool_calls: list[dict[str, Any]] = []
                    for raw_call in tool_calls_chunk:
                        function_block = raw_call.get("function", {}) if isinstance(raw_call, dict) else getattr(raw_call, "function", None)
                        if isinstance(function_block, dict):
                            tool_name = function_block.get("name")
                            raw_args = function_block.get("arguments")
                        elif function_block is not None:
                            tool_name = getattr(function_block, "name", None)
                            raw_args = getattr(function_block, "arguments", None)
                        else:
                            tool_name = None
                            raw_args = None
                        if not isinstance(tool_name, str) or not tool_name:
                            continue
                        if isinstance(raw_args, dict):
                            parsed_args = raw_args
                        elif isinstance(raw_args, str):
                            try:
                                loaded = json.loads(raw_args)
                                parsed_args = loaded if isinstance(loaded, dict) else {}
                            except json.JSONDecodeError:
                                parsed_args = {}
                        else:
                            parsed_args = {}
                        normalized_tool_calls.append(
                            {"function": {"name": tool_name, "arguments": parsed_args}}
                        )
                    if normalized_tool_calls:
                        assistant_tool_calls = normalized_tool_calls

            assistant_content = "".join(assistant_content_parts).strip()
            _log_report_event(
                "report_stream_iteration_received",
                {
                    "iteration": iteration,
                    "assistant_token_count": assistant_token_count,
                    "assistant_chars": len(assistant_content),
                    "tool_call_count": len(assistant_tool_calls),
                    "elapsed_ms": round((time.perf_counter() - iteration_started_at) * 1000, 2),
                },
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": assistant_tool_calls,
                }
            )

            if assistant_tool_calls:
                any_tool_called = True
                for tool_call in assistant_tool_calls:
                    function_block = tool_call.get("function", {})
                    tool_name = function_block.get("name")
                    tool_args = function_block.get("arguments", {})
                    if not isinstance(tool_name, str):
                        continue
                    if not isinstance(tool_args, dict):
                        tool_args = {}
                    if on_tool_call is not None:
                        await on_tool_call(tool_name, tool_args)
                    _log_report_event(
                        "report_stream_tool_call_detected",
                        {
                            "iteration": iteration,
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                        },
                    )
                    tool_result = await _dispatch_report_tool(tool_name, tool_args, db)
                    if on_tool_result is not None:
                        await on_tool_result(tool_name, tool_result)
                    messages.append({"role": "tool", "content": json.dumps(tool_result, ensure_ascii=False)})
                continue

            # Model returned text but no tool calls.
            # If it has never called a tool yet, it is still in "acknowledgement" mode —
            # ignore the text and inject a hard nudge so the next iteration calls the tool.
            if not any_tool_called and tools:
                _log_report_event(
                    "report_stream_nudge_tool_call",
                    {"iteration": iteration, "assistant_chars": len(assistant_content)},
                )
                tool_names = [t.get("function", {}).get("name") for t in tools if isinstance(t, dict)]
                first_tool = tool_names[0] if tool_names else "get_site_report_data"
                messages.append({
                    "role": "user",
                    "content": (
                        f"You have not called any tool yet. "
                        f"Call {first_tool} RIGHT NOW with the arguments already provided. "
                        f"Do not write any more text. Just call the tool."
                    ),
                })
                continue

            if assistant_content:
                _log_report_event(
                    "report_stream_completed_without_tool_context",
                    {
                        "iterations": iteration,
                        "final_chars": len(assistant_content),
                        "total_elapsed_ms": round((time.perf_counter() - stream_started_at) * 1000, 2),
                    },
                )
                return assistant_content

        raise ValueError(f"Report agent exceeded {max_iterations} iterations without final output")
    except Exception as exc:
        logger.exception("report.ai_stream.failed error=%s", exc)
        return ""


def _site_summary_prompt(
    *,
    site_name: str,
    language: str,
    stats: SiteStats,
    nearest_shelter: str | None,
    nearest_shelter_distance: float | None,
) -> str:
    return (
        "Generate a short disaster site situation summary for field coordination.\n"
        f"Language: {_language_name(language)} ({language}).\n"
        "Write exactly 3-4 concise sentences.\n"
        f"Site: {site_name}\n"
        f"Total buildings: {stats.total_buildings}\n"
        f"Extreme severity (5): {stats.sev5}\n"
        f"Critical severity (4): {stats.sev4}\n"
        f"Moderate severity (3): {stats.sev3}\n"
        f"Signs of life count: {stats.signs_of_life}\n"
        f"Blocked roads count: {stats.blocked_roads_count}\n"
        f"Flood zone buildings: {stats.flood_count}\n"
        f"Estimated affected people: {stats.estimated_people}\n"
        f"Nearest shelter: {nearest_shelter or 'unknown'}\n"
        f"Distance to shelter in meters: {nearest_shelter_distance if nearest_shelter_distance is not None else 'unknown'}\n"
        "Do not use markdown headings, only plain narrative text."
    )


def _building_note_prompt(*, language: str, row: asyncpg.Record) -> str:
    warnings = row.get("warnings")
    warnings_text = ", ".join(warnings) if isinstance(warnings, list) else str(warnings or "none")
    return (
        "Generate a practical field-worker building action note.\n"
        f"Language: {_language_name(language)} ({language}).\n"
        "Write 3-5 short sentences and keep it operational.\n"
        f"Assessment ID: {row.get('id')}\n"
        f"Severity: {row.get('severity')}\n"
        f"Damage type: {row.get('damage_type')}\n"
        f"Damage description: {row.get('damage_description')}\n"
        f"Occupant status: {row.get('occupant_status')}\n"
        f"Road access: {row.get('road_access')}\n"
        f"Flood zone: {row.get('flood_zone')}\n"
        f"Nearest shelter: {row.get('nearest_shelter')} ({row.get('shelter_distance_m')}m)\n"
        f"Recommended action: {row.get('recommended_action')}\n"
        f"Warnings: {warnings_text}\n"
        "No bullet points."
    )


def _site_row_context(row: asyncpg.Record) -> dict[str, Any]:
    pre_image, post_image = _pick_visual_images(row)
    warnings = row.get("warnings")
    warnings_list = warnings if isinstance(warnings, list) else []
    return {
        "assessment_id": row.get("id"),
        "severity": row.get("severity"),
        "damage_type": row.get("damage_type"),
        "damage_description": row.get("damage_description"),
        "building_type": row.get("building_type"),
        "estimated_occupants": row.get("estimated_occupants"),
        "occupant_status": row.get("occupant_status"),
        "flood_zone": bool(row.get("flood_zone")),
        "road_access": row.get("road_access"),
        "nearest_road": row.get("nearest_road"),
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "recommended_action": row.get("recommended_action"),
        "warnings": warnings_list,
        "pre_image_url": pre_image,
        "post_image_url": post_image,
    }


def _site_report_prompt(
    *,
    request: ReportGenerateRequest,
    site_name: str,
    province: str | None,
    district: str | None,
    stats: SiteStats,
    map_image_url: str | None,
    route_steps: list[str],
    building_items: list[dict[str, Any]],
) -> str:
    payload = {
        "site_name": site_name,
        "province": province,
        "district": district,
        "team_name": request.team_name or "Unassigned",
        "language": request.language,
        "stats": {
            "total_buildings": stats.total_buildings,
            "severity_5": stats.sev5,
            "severity_4": stats.sev4,
            "severity_3": stats.sev3,
            "signs_of_life": stats.signs_of_life,
            "flood_zone_count": stats.flood_count,
            "blocked_roads_count": stats.blocked_roads_count,
            "avg_confidence": stats.avg_confidence,
            "estimated_people": stats.estimated_people,
        },
        "map_image_url": map_image_url,
        "evacuation_route_steps": route_steps,
        "priority_buildings": building_items,
    }
    return (
        "Generate the full field-operational site report in markdown.\n"
        f"Language: {_language_name(request.language)} ({request.language}).\n"
        "Output requirements:\n"
        "- Produce complete report narrative; do not say data is missing if fields are present.\n"
        "- Include concise actionable guidance for field teams.\n"
        "- Include a map section and embed this markdown image if available: ![Site map](map_image_url).\n"
        "- For each priority building, include pre/post photo evidence side-by-side using markdown table with image URLs.\n"
        "- Use clear headings and bullet lists for readability.\n"
        "- Keep report practical and not generic.\n\n"
        f"Context JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _building_report_prompt(
    *,
    request: ReportGenerateRequest,
    row: asyncpg.Record,
    route_steps: list[str],
) -> str:
    pre_image, post_image = _pick_visual_images(row)
    warnings = row.get("warnings")
    warnings_list = warnings if isinstance(warnings, list) else []
    payload = {
        "assessment_id": row.get("id"),
        "site_name": row.get("resolved_site_name"),
        "team_name": request.team_name or "Unassigned",
        "language": request.language,
        "severity": row.get("severity"),
        "damage_type": row.get("damage_type"),
        "damage_description": row.get("damage_description"),
        "recommended_action": row.get("recommended_action"),
        "occupant_status": row.get("occupant_status"),
        "flood_zone": row.get("flood_zone"),
        "coordinates": {"lat": row.get("lat"), "lon": row.get("lon")},
        "nearest_shelter": row.get("nearest_shelter"),
        "shelter_type": row.get("shelter_type"),
        "shelter_distance_m": row.get("shelter_distance_m"),
        "warnings": warnings_list,
        "reasoning": row.get("reasoning"),
        "turkish_summary": row.get("turkish_summary"),
        "pre_image_url": pre_image,
        "post_image_url": post_image,
        "route_steps": route_steps,
    }
    return (
        "Generate complete single-building report markdown for field workers.\n"
        f"Language: {_language_name(request.language)} ({request.language}).\n"
        "Output requirements:\n"
        "- Include one decisive action summary at top.\n"
        "- Include image evidence table with pre/post images using provided URLs.\n"
        "- Include route-to-shelter step-by-step section using provided route_steps.\n"
        "- Include detailed but concise damage and safety narrative.\n"
        "- Use headings and bullet lists.\n\n"
        f"Context JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _site_header_markdown(
    *,
    site_name: str,
    province: str | None,
    district: str | None,
    request: ReportGenerateRequest,
) -> str:
    return (
        "# BhumiDrishti Site Report\n\n"
        f"- Generated at: {_format_timestamp()}\n"
        f"- Site: {site_name}\n"
        f"- Province: {province or 'Unknown'}\n"
        f"- District: {district or 'Unknown'}\n"
        f"- Team: {request.team_name or 'Unassigned'}\n"
        f"- Language: {_language_name(request.language)} ({request.language})\n\n"
        "## Situation Summary\n\n"
    )


def _site_stats_markdown(stats: SiteStats, nearest_shelter: str | None, shelter_type: str | None, shelter_distance: float | None) -> str:
    confidence_text = f"{stats.avg_confidence:.3f}" if stats.avg_confidence is not None else "N/A"
    shelter_distance_text = f"{shelter_distance:.1f} m" if shelter_distance is not None else "N/A"
    return (
        "\n\n## Key Statistics\n\n"
        f"- Total buildings assessed: {stats.total_buildings}\n"
        f"- Severity 5 (extreme): {stats.sev5}\n"
        f"- Severity 4 (critical): {stats.sev4}\n"
        f"- Severity 3 (moderate): {stats.sev3}\n"
        f"- Estimated affected people: {stats.estimated_people}\n"
        f"- Buildings with signs of life: {stats.signs_of_life}\n"
        f"- Buildings in flood zone: {stats.flood_count}\n"
        f"- Blocked roads count: {stats.blocked_roads_count}\n"
        f"- Average confidence score: {confidence_text}\n\n"
        "## Nearest Shelter For Site\n\n"
        f"- Name: {nearest_shelter or 'Unknown'}\n"
        f"- Type: {shelter_type or 'Unknown'}\n"
        f"- Distance from site center: {shelter_distance_text}\n"
    )


def _priority_buildings_markdown(rows: list[asyncpg.Record]) -> str:
    lines = ["\n\n## Priority Building List\n"]
    for idx, row in enumerate(rows[:15], start=1):
        warnings = row.get("warnings")
        warnings_text = ", ".join(warnings) if isinstance(warnings, list) and warnings else "None"
        pre_image, post_image = _pick_visual_images(row)
        lines.append(
            (
                f"\n### Building #{idx} - {row.get('id')}\n"
                f"- Severity: {row.get('severity') or 'N/A'}\n"
                f"- Damage: {row.get('damage_type') or 'Unknown'}\n"
                f"- Description: {row.get('damage_description') or 'N/A'}\n"
                f"- Building type: {row.get('building_type') or 'Unknown'}\n"
                f"- Estimated occupants: {row.get('estimated_occupants') or 'N/A'}\n"
                f"- Occupant status: {row.get('occupant_status') or 'unknown'}\n"
                f"- Flood zone: {'yes' if row.get('flood_zone') else 'no'}\n"
                f"- Road access: {row.get('road_access') or 'unknown'}\n"
                f"- Coordinates: {row.get('lat')}, {row.get('lon')}\n"
                f"- Recommended action: {row.get('recommended_action') or 'N/A'}\n"
                f"- Warnings: {warnings_text}\n"
            )
        )
        image_block = _image_pair_markdown(pre_image, post_image)
        if image_block:
            lines.append(image_block)
    if len(rows) > 15:
        lines.append(f"\n- Additional buildings omitted in this first-pass markdown report: {len(rows) - 15}\n")
    return "".join(lines)


def _building_header_markdown(request: ReportGenerateRequest, row: asyncpg.Record) -> str:
    return (
        "# BhumiDrishti Building Report\n\n"
        f"- Assessment ID: {row.get('id')}\n"
        f"- Generated at: {_format_timestamp()}\n"
        f"- Team: {request.team_name or 'Unassigned'}\n"
        f"- Language: {_language_name(request.language)} ({request.language})\n"
        f"- Site: {row.get('resolved_site_name')}\n\n"
    )


def _building_sections_markdown(row: asyncpg.Record) -> str:
    warnings = row.get("warnings")
    warnings_text = ", ".join(warnings) if isinstance(warnings, list) and warnings else "None"
    confidence = row.get("confidence")
    confidence_text = f"{float(confidence):.3f}" if isinstance(confidence, (int, float)) else "N/A"
    pre_image, post_image = _pick_visual_images(row)
    media_pair = _image_pair_markdown(pre_image, post_image)
    media_section = media_pair if media_pair else "- No images available for this building.\n"
    return (
        "## Photo and Severity\n\n"
        f"{media_section}"
        f"- Severity: {row.get('severity') or 'N/A'}\n"
        f"- Recommended action: {row.get('recommended_action') or 'N/A'}\n\n"
        "## Damage Detail\n\n"
        f"- Damage type: {row.get('damage_type') or 'Unknown'}\n"
        f"- Damage description: {row.get('damage_description') or 'N/A'}\n"
        f"- Structural risk: {row.get('structural_risk') or 'unknown'}\n"
        f"- Building type: {row.get('building_type') or 'Unknown'}\n"
        f"- Floors: {row.get('building_floors') or 'Unknown'}\n"
        f"- Material: {row.get('building_material') or 'Unknown'}\n"
        f"- Occupant status: {row.get('occupant_status') or 'unknown'}\n"
        f"- Warnings: {warnings_text}\n\n"
        "## Location and Spatial Context\n\n"
        f"- Coordinates: {row.get('lat')}, {row.get('lon')}\n"
        f"- Province: {row.get('province') or 'Unknown'}\n"
        f"- District: {row.get('district') or 'Unknown'}\n"
        f"- Flood zone: {'yes' if row.get('flood_zone') else 'no'}\n"
        f"- Elevation (m): {row.get('elevation_m') or 'N/A'}\n"
        f"- Slope (degrees): {row.get('slope_degrees') or 'N/A'}\n"
        f"- Slope risk: {row.get('slope_risk') or 'unknown'}\n"
        f"- Nearest road: {row.get('nearest_road') or 'Unknown'} ({row.get('road_distance_m') or 'N/A'} m)\n"
        f"- Road access: {row.get('road_access') or 'unknown'}\n\n"
        "## Route to Shelter\n\n"
        f"- Shelter: {row.get('nearest_shelter') or 'Unknown'}\n"
        f"- Shelter type: {row.get('shelter_type') or 'Unknown'}\n"
        f"- Distance to shelter: {row.get('shelter_distance_m') or 'N/A'} m\n\n"
        "## Assessment Notes\n\n"
        f"- Reasoning: {row.get('reasoning') or 'N/A'}\n"
        f"- Turkish summary: {row.get('turkish_summary') or 'N/A'}\n"
        f"- Confidence: {confidence_text}\n\n"
        "## Footer\n\n"
        f"- Generated by: BhumiDrishti\n"
        f"- Model used: {row.get('model_used') or REPORT_MODEL}\n"
    )


async def save_markdown_report(report_id: str, markdown_text: str) -> str:
    """Persist markdown report text to uploads/reports directory."""
    output_path = REPORTS_DIR / f"{report_id}.md"
    await asyncio.to_thread(output_path.write_text, markdown_text, encoding="utf-8")
    return f"uploads/reports/{report_id}.md"


async def save_pdf_report(report_id: str, markdown_text: str) -> str:
    """Persist PDF report (final download artifact) under uploads/reports."""
    return await asyncio.to_thread(_render_pdf, report_id, markdown_text)


async def generate_site_markdown(
    *,
    pool: asyncpg.Pool,
    request: ReportGenerateRequest,
    report_id: str,
    on_progress,
    on_token,
    on_thinking=None,
    on_tool_call=None,
    on_tool_result=None,
) -> str:
    _log_report_event(
        "generate_site_report_started",
        {
            "report_id": report_id,
            "site_id": request.site_id,
            "site_name": request.site_name,
            "language": request.language,
            "team_name": request.team_name,
            "model": REPORT_MODEL,
        },
    )
    await on_progress("Fetching assessment data...")
    rows = await load_site_rows(pool, request)
    if not rows:
        _log_report_event(
            "generate_site_report_no_rows",
            {"report_id": report_id, "site_id": request.site_id, "site_name": request.site_name},
        )
        raise ValueError("No assessments found for the selected site")

    site_name = str(rows[0].get("resolved_site_name") or request.site_name or f"site-{request.site_id}")
    province = rows[0].get("province")
    district = rows[0].get("district")
    stats = _site_stats(rows)

    nearest_row = next((row for row in rows if row.get("shelter_distance_m") is not None), None)
    nearest_shelter = nearest_row.get("nearest_shelter") if nearest_row else None
    nearest_shelter_type = nearest_row.get("shelter_type") if nearest_row else None
    nearest_shelter_distance = _safe_float(nearest_row.get("shelter_distance_m")) if nearest_row else None

    await on_progress("Generating static site map...")
    map_relative_path = None
    map_image_url = None
    route_steps: list[str] = []
    try:
        map_relative_path, map_image_url, route_steps = await create_site_static_map(
            pool=pool,
            request=request,
            report_id=report_id,
            rows=rows,
        )
        if map_relative_path and map_image_url:
            logger.info("report.map.created path=%s", map_relative_path)
    except Exception as exc:
        logger.warning("report.map.failed error=%s", exc)
        _log_report_event("generate_site_map_failed", {"report_id": report_id, "error": str(exc)})

    await on_progress("Generating full AI report...")
    # Terse, imperative user message — no pleasantries, just a direct tool invocation command.
    # Long explanatory messages cause the model to acknowledge instead of act.
    prompt = (
        f"Call get_site_report_data now. "
        f"site_id={request.site_id} language={request.language} team={request.team_name or 'Unassigned'}. "
        f"After the tool returns, generate the full HTML report."
    )
    ai_report = await stream_ai_summary(
        prompt=prompt,
        on_token=on_token,
        on_thinking=on_thinking,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
        system_prompt=REPORT_GENERATION_SYSTEM_PROMPT,
        db=pool,
        tools=ALL_TOOLS,
    )
    if ai_report:
        _log_report_event(
            "generate_site_report_completed_ai",
            {"report_id": report_id, "chars": len(ai_report)},
        )
        return ai_report

    # Fallback if streaming model fails.
    fallback = _site_header_markdown(
        site_name=site_name,
        province=province if isinstance(province, str) else None,
        district=district if isinstance(district, str) else None,
        request=request,
    )
    if map_image_url:
        fallback += f"\n## Site Map\n\n![Site map]({map_image_url})\n"
    fallback += _site_stats_markdown(
        stats,
        nearest_shelter if isinstance(nearest_shelter, str) else None,
        nearest_shelter_type if isinstance(nearest_shelter_type, str) else None,
        nearest_shelter_distance,
    )
    fallback += _priority_buildings_markdown(rows)
    _log_report_event(
        "generate_site_report_completed_fallback",
        {"report_id": report_id, "chars": len(fallback)},
    )
    return fallback


async def generate_building_markdown(
    *,
    pool: asyncpg.Pool,
    request: ReportGenerateRequest,
    report_id: str,
    on_progress,
    on_token,
    on_thinking=None,
    on_tool_call=None,
    on_tool_result=None,
) -> str:
    _log_report_event(
        "generate_building_report_started",
        {
            "report_id": report_id,
            "assessment_id": request.assessment_id,
            "language": request.language,
            "team_name": request.team_name,
            "model": REPORT_MODEL,
        },
    )
    await on_progress("Fetching assessment data...")
    row = await load_building_row(pool, request.assessment_id or "")
    if row is None:
        _log_report_event(
            "generate_building_report_not_found",
            {"report_id": report_id, "assessment_id": request.assessment_id},
        )
        raise ValueError("Assessment not found for building report")

    await on_progress("Generating full AI report...")
    # Terse, imperative user message — no pleasantries, just a direct tool invocation command.
    prompt = (
        f"Call get_building_report_data now. "
        f"assessment_id={request.assessment_id} language={request.language} team={request.team_name or 'Unassigned'}. "
        f"After the tool returns, generate the full HTML report."
    )
    ai_report = await stream_ai_summary(
        prompt=prompt,
        on_token=on_token,
        on_thinking=on_thinking,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
        system_prompt=REPORT_GENERATION_SYSTEM_PROMPT,
        db=pool,
        tools=ALL_TOOLS,
    )
    if ai_report:
        _log_report_event(
            "generate_building_report_completed_ai",
            {"report_id": report_id, "chars": len(ai_report)},
        )
        return ai_report

    # Fallback report if model fails.
    fallback = _building_header_markdown(request, row) + "\n" + _building_sections_markdown(row)
    _log_report_event(
        "generate_building_report_completed_fallback",
        {"report_id": report_id, "chars": len(fallback)},
    )
    return fallback

