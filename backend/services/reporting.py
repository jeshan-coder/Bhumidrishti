"""Report generation service for site and building markdown reports."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import textwrap
import time
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime, timezone
from html.parser import HTMLParser
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
# Tool dispatch is called lazily inside _dispatch_report_tool to avoid circular imports.

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
ollama_client = AsyncClient(host=OLLAMA_HOST)
from services.ai_runtime import ACTIVE_GEMMA_MODEL as _ACTIVE_MODEL  # noqa: E402
REPORT_MODEL = os.getenv("REPORT_MODEL") or _ACTIVE_MODEL

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/app/data/uploads")).resolve()
REPORTS_DIR = UPLOAD_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
BACKEND_PUBLIC_URL = os.getenv("BACKEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")
OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "http://osrm:5000").rstrip("/")
TILE_SERVER_URL = os.getenv("TILE_SERVER_URL", "http://tileserver:8080/styles/basic/{z}/{x}/{y}.png")

# Tool dispatch is called lazily inside _dispatch_report_tool to avoid circular imports.


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


@dataclass
class _Paragraph:
    """Structured text unit for PDF rendering."""
    text: str
    style: str  # "h1" | "h2" | "h3" | "body" | "bullet"


class _HtmlTextExtractor(HTMLParser):
    """Extract structured paragraphs from AI-generated HTML reports."""

    _HEADING: dict[str, str] = {"h1": "h1", "h2": "h2", "h3": "h3", "h4": "h3", "h5": "h3"}
    _BLOCK = frozenset({"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "li", "article", "section"})
    _SKIP = frozenset({"script", "style", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._paragraphs: list[_Paragraph] = []
        self._buf: list[str] = []
        self._stack: list[str] = []
        self._skip_depth = 0

    def _current_style(self) -> str:
        for tag in reversed(self._stack):
            if tag in self._HEADING:
                return self._HEADING[tag]
            if tag == "li":
                return "bullet"
        return "body"

    def _flush(self) -> None:
        text = "".join(self._buf).strip()
        self._buf.clear()
        if text:
            self._paragraphs.append(_Paragraph(text, self._current_style()))

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        if self._skip_depth:
            return
        self._stack.append(tag)
        if tag in self._BLOCK:
            self._flush()
        if tag == "li":
            self._buf.append("• ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        if self._skip_depth:
            return
        if tag in self._BLOCK:
            self._flush()
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i] == tag:
                self._stack.pop(i)
                break

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._buf.append(data)

    def get_paragraphs(self) -> list[_Paragraph]:
        self._flush()
        return self._paragraphs


def _markdown_to_paragraphs(text: str) -> list[_Paragraph]:
    """Convert markdown text to structured paragraphs for PDF rendering."""
    paragraphs: list[_Paragraph] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            paragraphs.append(_Paragraph(stripped[4:].strip(), "h3"))
        elif stripped.startswith("## "):
            paragraphs.append(_Paragraph(stripped[3:].strip(), "h2"))
        elif stripped.startswith("# "):
            paragraphs.append(_Paragraph(stripped[2:].strip(), "h1"))
        elif stripped.startswith("- ") or stripped.startswith("* "):
            clean = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped[2:])
            clean = re.sub(r"`(.+?)`", r"\1", clean)
            clean = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", clean)
            clean = clean.strip()
            if clean:
                paragraphs.append(_Paragraph("• " + clean, "bullet"))
        elif re.match(r"^\|[\s\-:|]+\|", stripped):
            # Skip markdown table separator rows
            continue
        elif stripped.startswith("| "):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            row_text = " | ".join(c for c in cells if c)
            if row_text:
                paragraphs.append(_Paragraph(row_text, "body"))
        else:
            clean = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped)
            clean = re.sub(r"`(.+?)`", r"\1", clean)
            clean = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", clean)
            clean = clean.strip()
            if clean:
                paragraphs.append(_Paragraph(clean, "body"))
    return paragraphs


def _extract_image_urls_html(html_content: str) -> list[str]:
    """Extract <img src="..."> URLs from HTML content (deduped)."""
    if not html_content:
        return []
    matches = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_content, re.IGNORECASE)
    deduped: list[str] = []
    seen: set[str] = set()
    for url in matches:
        url = url.strip()
        if url and url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _inject_media_into_html(
    html_content: str,
    map_image_url: str | None,
    photo_lookup: dict[str, tuple[str | None, str | None]] | None = None,
) -> str:
    """Replace AI-generated placeholder divs with actual <img> tags.

    The AI report system prompt uses two placeholder conventions:
      <div class="map-placeholder">...</div>        → replaced with the site map image
      <div class="img-placeholder" data-type="pre|post" data-id="{id}">...</div>
                                                    → replaced with pre/post assessment photos
    """
    # Replace map placeholder with real image tag
    if map_image_url:
        map_img_tag = (
            f'<img src="{map_image_url}" alt="Site map" '
            f'style="max-width:100%;height:auto;display:block;margin:0.5rem 0;" />'
        )
        html_content = re.sub(
            r'<div[^>]*class=["\'][^"\']*map-placeholder[^"\']*["\'][^>]*>.*?</div>',
            map_img_tag,
            html_content,
            flags=re.IGNORECASE | re.DOTALL,
        )

    # Replace per-assessment img placeholders
    if photo_lookup:
        def _replace_img_placeholder(m: re.Match) -> str:
            tag_full = m.group(0)
            data_type_m = re.search(r'data-type=["\']([^"\']+)["\']', tag_full)
            data_id_m = re.search(r'data-id=["\']([^"\']+)["\']', tag_full)
            if not data_id_m:
                return tag_full
            assessment_id = data_id_m.group(1)
            data_type = data_type_m.group(1) if data_type_m else "post"
            pre_url, post_url = photo_lookup.get(assessment_id, (None, None))
            img_url = pre_url if data_type == "pre" else post_url
            if not img_url:
                return tag_full
            return (
                f'<img src="{img_url}" alt="{data_type} image {assessment_id}" '
                f'style="max-width:100%;height:auto;display:block;margin:0.5rem 0;" />'
            )

        html_content = re.sub(
            r'<div[^>]*class=["\'][^"\']*img-placeholder[^"\']*["\'][^>]*>.*?</div>',
            _replace_img_placeholder,
            html_content,
            flags=re.IGNORECASE | re.DOTALL,
        )

    return html_content


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


def _render_pdf(report_id: str, content: str) -> str:
    """Delegate PDF rendering to the WeasyPrint-based pdf_renderer module."""
    from services.pdf_renderer import render_report_pdf  # noqa: PLC0415
    return render_report_pdf(report_id, content)


def _severity_color(severity: int) -> str:
    if severity >= 5:
        return "#7f0000"   # deep maroon — extreme
    if severity == 4:
        return "#cc0000"   # vivid red — critical
    if severity == 3:
        return "#e65c00"   # deep burnt orange — moderate
    if severity == 2:
        return "#b09000"   # dark gold — low
    return "#1a6e2e"       # deep forest green — minimal


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


def _compute_scale_bar_metres(zoom: int, centre_lat: float, px_length: int = 160) -> tuple[float, int]:
    """Return (nice_metres, pixel_width) for a human-readable map scale bar."""
    mpp = 156543.03 * math.cos(math.radians(centre_lat)) / (2 ** max(zoom, 1))
    raw_metres = mpp * px_length
    if raw_metres <= 0:
        return 100.0, px_length
    magnitude = 10 ** math.floor(math.log10(max(raw_metres, 1)))
    for factor in (1, 2, 5, 10):
        nice = factor * magnitude
        if nice >= raw_metres * 0.6:
            nice_px = max(10, min(int(round(nice / mpp)), px_length * 2))
            return float(nice), nice_px
    return float(raw_metres), px_length


def _draw_map_overlays_professional(
    image,
    *,
    zoom: int,
    centre_lat: float,
    legend_items: list[tuple[str, str]] | None = None,
    show_epsg: bool = True,
) -> None:
    """Draw north arrow, real scale bar, EPSG note, and colour legend on a PIL image."""
    draw = ImageDraw.Draw(image)
    width, height = image.size

    # ── North arrow (top-right corner) ───────────────────────────
    ax, ay = width - 52, 60
    draw.ellipse((ax - 20, ay - 28, ax + 20, ay + 28), fill="white", outline="#374151", width=1)
    draw.polygon([(ax, ay - 22), (ax - 8, ay + 2), (ax + 8, ay + 2)], fill="#111827")
    draw.polygon([(ax, ay + 22), (ax - 8, ay - 2), (ax + 8, ay - 2)], fill="#d1d5db")
    draw.text((ax - 4, ay - 30), "N", fill="#111827")

    # ── Real scale bar (bottom-right) ────────────────────────────
    nice_m, nice_px = _compute_scale_bar_metres(zoom, centre_lat)
    nice_px = min(nice_px, width - 60)
    sx = width - nice_px - 22
    sy = height - 36
    draw.rectangle((sx - 5, sy - 18, sx + nice_px + 5, sy + 14), fill="white")
    draw.rectangle((sx, sy, sx + nice_px, sy + 8), fill="#374151")
    draw.line([(sx, sy - 5), (sx, sy + 13)], fill="#374151", width=2)
    draw.line([(sx + nice_px, sy - 5), (sx + nice_px, sy + 13)], fill="#374151", width=2)
    scale_label = f"{nice_m / 1000:.1f} km" if nice_m >= 1000 else f"{int(nice_m)} m"
    lx = sx + nice_px // 2 - len(scale_label) * 3
    draw.text((lx, sy - 17), scale_label, fill="#111827")

    # ── EPSG note (bottom-left) ───────────────────────────────────
    if show_epsg:
        draw.rectangle((6, height - 21, 82, height - 5), fill="white")
        draw.text((8, height - 19), "EPSG:4326", fill="#6b7280")

    # ── Colour legend (left side, above EPSG) ────────────────────
    default_legend: list[tuple[str, str]] = [
        ("Severity 5", "#7f0000"),
        ("Severity 4", "#cc0000"),
        ("Severity 3", "#e65c00"),
        ("Severity 2", "#b09000"),
        ("Severity 1", "#1a6e2e"),
        ("Shelter", "#0f6e56"),
    ]
    items = legend_items if legend_items is not None else default_legend
    lg_x = 12
    lg_item_h = 19
    lg_h = len(items) * lg_item_h + 28
    lg_y = height - lg_h - 38
    draw.rectangle((lg_x, lg_y, lg_x + 165, lg_y + lg_h), fill="white", outline="#d1d5db", width=1)
    draw.text((lg_x + 8, lg_y + 6), "Legend", fill="#374151")
    for i, (lbl, clr) in enumerate(items):
        iy = lg_y + 24 + i * lg_item_h
        draw.rectangle((lg_x + 8, iy, lg_x + 22, iy + 11), fill=clr, outline="#6b7280", width=1)
        draw.text((lg_x + 26, iy), lbl, fill="#374151")


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

    static_map = StaticMap(1400, 950, url_template=TILE_SERVER_URL)

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
    _draw_map_overlays_professional(
        image,
        zoom=static_map.zoom,
        centre_lat=centroid[0],
    )
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
            a.worker_name,
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


async def _dispatch_report_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    db: asyncpg.Connection | asyncpg.Pool | None,
) -> dict[str, Any]:
    """Delegate to the centralized dispatcher in services.tools."""
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
                final_thinking_buf: list[str] = []
                final_token_count = 0
                async for chunk in final_stream:
                    message_block = chunk.get("message") if isinstance(chunk, dict) else getattr(chunk, "message", None)
                    if message_block is None:
                        continue
                    thinking = message_block.get("thinking") if isinstance(message_block, dict) else getattr(message_block, "thinking", None)
                    if isinstance(thinking, str) and thinking and on_thinking is not None:
                        final_thinking_buf.append(thinking)
                        await on_thinking("".join(final_thinking_buf))
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
                # Accumulate thinking deltas into the same buffer so the full growing
                # text is sent each time — produces smooth streaming in the UI.
                thinking = message_block.get("thinking") if isinstance(message_block, dict) else getattr(message_block, "thinking", None)
                if isinstance(thinking, str) and thinking and on_thinking is not None:
                    iteration_thinking_buf.append(thinking)
                    await on_thinking("".join(iteration_thinking_buf))

                token = message_block.get("content") if isinstance(message_block, dict) else getattr(message_block, "content", None)
                if isinstance(token, str) and token:
                    assistant_content_parts.append(token)
                    assistant_token_count += 1
                    # Tool-calling iteration — content is pre-tool reasoning, not report text.
                    # Route to thinking panel so it never leaks into the report preview.
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
    map_line = (
        f"- Embed the site map in the map section as: <img src=\"{map_image_url}\" alt=\"Site map\">.\n"
        if map_image_url
        else "- Include a map section (no map image available).\n"
    )
    return (
        "Generate the full field-operational site report in markdown.\n"
        f"Language: {_language_name(request.language)} ({request.language}).\n"
        "Output requirements:\n"
        "- Produce complete report narrative; do not say data is missing if fields are present.\n"
        "- Include concise actionable guidance for field teams.\n"
        + map_line
        + "- For each priority building, include pre/post photo evidence side-by-side using markdown table with image URLs.\n"
        "- Use clear headings and bullet lists for readability.\n"
        "- Keep report practical and not generic.\n\n"
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


async def _save_report_meta(
    report_id: str,
    *,
    report_type: str,
    title: str,
    subtitle: str = "",
    team_name: str | None = None,
    language: str = "en",
    generated_at: str | None = None,
    stats: SiteStats | None = None,
) -> None:
    """Write a JSON sidecar with cover metadata for the PDF renderer."""
    meta: dict[str, Any] = {
        "title": title,
        "subtitle": subtitle,
        "report_type": report_type,
        "team_name": team_name or "Unassigned",
        "language": (language or "en").upper(),
        "generated_at": generated_at or _format_timestamp(),
    }
    if stats is not None:
        meta["stats"] = {
            "total": stats.total_buildings,
            "sev5": stats.sev5,
            "sev4": stats.sev4,
            "sev3": stats.sev3,
            "signs_of_life": stats.signs_of_life,
            "estimated_people": stats.estimated_people,
            "flood_count": stats.flood_count,
        }
    meta_path = REPORTS_DIR / f"{report_id}_meta.json"
    try:
        await asyncio.to_thread(
            meta_path.write_text, json.dumps(meta, ensure_ascii=False), "utf-8"
        )
    except Exception as exc:
        logger.warning("report.meta.save_failed report_id=%s error=%s", report_id, exc)


async def _save_report_chart(report_id: str, stats: SiteStats) -> None:
    """Generate a severity bar chart and save as a PNG sidecar for the PDF renderer."""
    from services.pdf_renderer import generate_severity_chart_b64  # noqa: PLC0415

    stats_dict = {
        "total_buildings": stats.total_buildings,
        "sev5": stats.sev5,
        "sev4": stats.sev4,
        "sev3": stats.sev3,
    }
    try:
        chart_b64 = await asyncio.to_thread(generate_severity_chart_b64, stats_dict)
        if chart_b64:
            chart_path = REPORTS_DIR / f"{report_id}_chart.png"
            await asyncio.to_thread(chart_path.write_bytes, base64.b64decode(chart_b64))
            logger.info("report.chart.saved report_id=%s", report_id)
    except Exception as exc:
        logger.warning("report.chart.save_failed report_id=%s error=%s", report_id, exc)


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
    # Embed all fetched data directly in the prompt — no tool calls needed.
    building_items = [_site_row_context(row) for row in rows[:40]]
    prompt = _site_report_prompt(
        request=request,
        site_name=site_name,
        province=province if isinstance(province, str) else None,
        district=district if isinstance(district, str) else None,
        stats=stats,
        map_image_url=map_image_url,
        route_steps=route_steps,
        building_items=building_items,
    )
    try:
        ai_report = await asyncio.wait_for(
            stream_ai_summary(
                prompt=prompt,
                on_token=on_token,
                on_thinking=on_thinking,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
                system_prompt=REPORT_GENERATION_SYSTEM_PROMPT,
                db=pool,
                tools=None,
            ),
            timeout=360.0,
        )
    except asyncio.TimeoutError:
        logger.warning("report.site.ai_timeout report_id=%s — falling back to markdown", report_id)
        ai_report = ""
    if ai_report:
        # Replace AI-generated placeholder divs with real image tags
        photo_lookup: dict[str, tuple[str | None, str | None]] = {
            str(row.get("id")): _pick_visual_images(row)
            for row in rows
            if row.get("id")
        }
        ai_report = _inject_media_into_html(ai_report, map_image_url, photo_lookup)
        _log_report_event(
            "generate_site_report_completed_ai",
            {"report_id": report_id, "chars": len(ai_report)},
        )
        final_content = ai_report
    else:
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
        final_content = fallback

    # Save PDF sidecar files (meta JSON + severity chart PNG)
    province_str = province if isinstance(province, str) else None
    district_str = district if isinstance(district, str) else None
    subtitle_parts = [p for p in (province_str, district_str) if p]
    await _save_report_meta(
        report_id,
        report_type="SITE REPORT",
        title=f"Site Report — {site_name}",
        subtitle=" · ".join(subtitle_parts),
        team_name=request.team_name,
        language=request.language,
        stats=stats,
    )
    await on_progress("Generating severity chart...")
    await _save_report_chart(report_id, stats)

    return final_content


# ═══════════════════════════════════════════════════════════════════
# V2 REPORT — DB query helpers
# ═══════════════════════════════════════════════════════════════════

async def _fetch_building_footprint(
    conn: asyncpg.Connection,
    lat: float,
    lon: float,
) -> dict[str, Any] | None:
    """Return GeoJSON polygon for the building at (lat, lon) from turkey_buildings."""
    try:
        row = await conn.fetchrow(
            """
            SELECT ST_AsGeoJSON(geom)::text AS geojson
            FROM turkey_buildings
            WHERE ST_DWithin(geom::geography, ST_MakePoint($1, $2)::geography, 60)
            ORDER BY ST_Distance(geom::geography, ST_MakePoint($1, $2)::geography)
            LIMIT 1
            """,
            lon,
            lat,
        )
        if row and isinstance(row.get("geojson"), str):
            return json.loads(row["geojson"])
    except Exception:
        pass
    return None


async def _fetch_shelter_coords_by_name(
    conn: asyncpg.Connection,
    shelter_name: str | None,
    centroid_lat: float,
    centroid_lon: float,
) -> tuple[float, float] | None:
    """Resolve a shelter name to (lat, lon) from turkey_points using ILIKE + proximity."""
    if not shelter_name or not shelter_name.strip():
        return None
    try:
        row = await conn.fetchrow(
            """
            SELECT ST_Y(ST_Centroid(geom)) AS lat, ST_X(ST_Centroid(geom)) AS lon
            FROM turkey_points
            WHERE amenity IN ('shelter', 'hospital', 'school', 'clinic', 'community_centre')
              AND name ILIKE $1
            ORDER BY geom <-> ST_SetSRID(ST_MakePoint($2, $3), 4326)
            LIMIT 1
            """,
            f"%{shelter_name.strip()}%",
            centroid_lon,
            centroid_lat,
        )
        if row:
            lat = _safe_float(row.get("lat"))
            lon = _safe_float(row.get("lon"))
            if lat is not None and lon is not None:
                return (lat, lon)
    except Exception:
        pass
    return None


async def _fetch_osrm_route_full(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> tuple[dict | None, list[str], float | None, float | None]:
    """Return (geometry_geojson, steps, distance_m, duration_s) from OSRM."""
    route = await query_osrm_route(
        start_lat=start_lat,
        start_lon=start_lon,
        end_lat=end_lat,
        end_lon=end_lon,
        profile="driving",
    )
    steps = await _fetch_osrm_steps(start_lat, start_lon, end_lat, end_lon)
    if route.found:
        return route.geometry_geojson, steps, route.distance_m, route.duration_s
    return None, steps, None, None


# ═══════════════════════════════════════════════════════════════════
# V2 REPORT — Per-building map generators
# ═══════════════════════════════════════════════════════════════════

def _tiles_loaded(image) -> bool:
    """Return True if tile background pixels rendered (not a blank white canvas).
    Samples the top-left 40×40 corner — should be non-white if tiles loaded."""
    try:
        from PIL import ImageStat  # noqa: PLC0415
        corner = image.crop((0, 0, 40, 40))
        mean = ImageStat.Stat(corner.convert("L")).mean[0]
        return mean < 242
    except Exception:
        return True  # assume loaded to avoid spurious fallback


def _apply_styled_basemap(image, *, draw_marker_at: tuple[int, int] | None = None, severity: int = 3) -> None:
    """Overwrite image pixels with a neutral beige map-like background and redraw marker."""
    from PIL import ImageDraw as PilDraw  # noqa: PLC0415
    draw = PilDraw.Draw(image)
    w, h = image.size
    # Background fill
    draw.rectangle([0, 0, w - 1, h - 1], fill="#e8e0d5")
    # Subtle grid to suggest street pattern
    for x in range(0, w, 72):
        draw.line([(x, 0), (x, h)], fill="#cfc9be", width=1)
    for y in range(0, h, 72):
        draw.line([(0, y), (w, y)], fill="#cfc9be", width=1)
    # Slightly wider lines every 3rd grid line to suggest main roads
    for x in range(0, w, 216):
        draw.line([(x, 0), (x, h)], fill="#b8b0a2", width=2)
    for y in range(0, h, 216):
        draw.line([(0, y), (w, y)], fill="#b8b0a2", width=2)
    # Redraw building marker if position known
    if draw_marker_at:
        cx, cy = draw_marker_at
        color_hex = _severity_color(severity)
        r = int(color_hex[1:3], 16)
        g = int(color_hex[3:5], 16)
        b = int(color_hex[5:7], 16)
        draw.ellipse([cx - 11, cy - 11, cx + 11, cy + 11], fill=(r, g, b), outline="#ffffff", width=3)


async def create_building_location_map(
    *,
    report_id: str,
    assessment_id: str,
    lat: float,
    lon: float,
    footprint_geojson: dict | None,
    severity: int,
) -> tuple[str | None, str | None]:
    """Generate a 600×600 building location map and return (rel_path, url)."""
    try:
        from PIL import Image as PilImage  # noqa: PLC0415

        static_map = StaticMap(600, 600, url_template=TILE_SERVER_URL)

        # Collect ring coords for post-render polygon fill
        ring: list | None = None
        if isinstance(footprint_geojson, dict):
            geom_type = str(footprint_geojson.get("type") or "")
            coords = footprint_geojson.get("coordinates")
            if geom_type == "Polygon" and isinstance(coords, list) and coords:
                ring = coords[0]
            elif geom_type == "MultiPolygon" and isinstance(coords, list) and coords and coords[0]:
                ring = coords[0][0]

        # Always add a marker — staticmap requires ≥1 feature before render()
        # The polygon is drawn via PIL *after* render, so marker is the anchor
        static_map.add_marker(CircleMarker((lon, lat), _severity_color(severity), 16))

        image = static_map.render(zoom=17)

        # If tile server is unavailable, replace blank white background with styled fallback
        if not _tiles_loaded(image):
            logger.info("create_building_location_map.tile_fallback assessment_id=%s", assessment_id)
            _apply_styled_basemap(image, draw_marker_at=(300, 300), severity=severity)

        # Draw filled polygon using PIL after render (precise pixel coords from Mercator projection)
        if ring is not None and len(ring) >= 3:
            zoom_used = static_map.zoom
            x_c = static_map.x_center
            y_c = static_map.y_center
            img_w, img_h = image.size

            def _to_px(pt: list | tuple) -> tuple[int, int]:
                lon_p, lat_p = float(pt[0]), float(pt[1])
                n = 2.0 ** zoom_used
                tx = (lon_p + 180.0) / 360.0 * n
                ty = (1.0 - math.log(math.tan(math.radians(lat_p)) + 1.0 / math.cos(math.radians(lat_p))) / math.pi) / 2.0 * n
                return (int((tx - x_c) * 256 + img_w / 2), int((ty - y_c) * 256 + img_h / 2))

            pixels = [
                _to_px(p) for p in ring
                if isinstance(p, (list, tuple)) and len(p) >= 2
            ]
            if len(pixels) >= 3:
                color_hex = _severity_color(severity)
                r = int(color_hex[1:3], 16)
                g = int(color_hex[3:5], 16)
                b = int(color_hex[5:7], 16)
                overlay = PilImage.new("RGBA", image.size, (0, 0, 0, 0))
                ov_draw = ImageDraw.Draw(overlay)
                ov_draw.polygon(pixels, fill=(r, g, b, 110))
                ov_draw.line(pixels + [pixels[0]], fill=(r, g, b, 240), width=4)
                image = PilImage.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

        _draw_map_overlays_professional(
            image,
            zoom=static_map.zoom,
            centre_lat=lat,
            legend_items=[(f"Severity {severity}", _severity_color(severity)), ("Building footprint", _severity_color(severity))],
            show_epsg=True,
        )

        filename = f"{report_id}_bmap_{assessment_id}.png"
        absolute_path = REPORTS_DIR / filename
        await asyncio.to_thread(image.save, absolute_path)
        rel_path = f"uploads/reports/{filename}"
        url = f"{BACKEND_PUBLIC_URL}/media/uploads/reports/{filename}"
        return rel_path, url
    except Exception as exc:
        logger.warning("create_building_location_map.failed assessment_id=%s error=%s", assessment_id, exc)
        return None, None


async def create_building_route_map(
    *,
    report_id: str,
    assessment_id: str,
    building_lat: float,
    building_lon: float,
    shelter_lat: float,
    shelter_lon: float,
    route_geometry_geojson: dict | None,
    severity: int,
) -> tuple[str | None, str | None]:
    """Generate an 800×500 route-to-shelter map and return (rel_path, url)."""
    try:
        static_map = StaticMap(800, 500, url_template=TILE_SERVER_URL)

        if isinstance(route_geometry_geojson, dict):
            coords = route_geometry_geojson.get("coordinates")
            if isinstance(coords, list) and len(coords) >= 2:
                path = [
                    (float(p[0]), float(p[1]))
                    for p in coords
                    if isinstance(p, (list, tuple)) and len(p) >= 2
                ]
                if len(path) >= 2:
                    static_map.add_line(Line(path, "#2563eb", 4))

        static_map.add_marker(CircleMarker((building_lon, building_lat), _severity_color(severity), 14))
        static_map.add_marker(CircleMarker((shelter_lon, shelter_lat), "#0f9d58", 14))

        image = static_map.render()

        if not _tiles_loaded(image):
            logger.info("create_building_route_map.tile_fallback assessment_id=%s", assessment_id)
            # Estimate building center pixel (may not be exactly 400, 250 due to bbox fitting)
            cx = int(image.size[0] / 2)
            cy = int(image.size[1] / 2)
            _apply_styled_basemap(image, draw_marker_at=(cx, cy), severity=severity)
            # Re-draw shelter marker (green)
            from PIL import ImageDraw as _PD  # noqa: PLC0415
            _d = _PD.Draw(image)
            _d.ellipse([image.size[0] - 40, image.size[1] // 2 - 11,
                        image.size[0] - 18, image.size[1] // 2 + 11],
                       fill="#0f9d58", outline="#ffffff", width=2)

        _draw_map_overlays_professional(
            image,
            zoom=static_map.zoom,
            centre_lat=(building_lat + shelter_lat) / 2,
            legend_items=[
                (f"Severity {severity}", _severity_color(severity)),
                ("Shelter", "#0f9d58"),
                ("Route", "#2563eb"),
            ],
            show_epsg=False,
        )

        filename = f"{report_id}_rmap_{assessment_id}.png"
        absolute_path = REPORTS_DIR / filename
        await asyncio.to_thread(image.save, absolute_path)
        rel_path = f"uploads/reports/{filename}"
        url = f"{BACKEND_PUBLIC_URL}/media/uploads/reports/{filename}"
        return rel_path, url
    except Exception as exc:
        logger.warning("create_building_route_map.failed assessment_id=%s error=%s", assessment_id, exc)
        return None, None


# ═══════════════════════════════════════════════════════════════════
# V2 REPORT — AI narrative helpers
# ═══════════════════════════════════════════════════════════════════

async def _generate_site_summary_narrative(
    *,
    site_name: str,
    language: str,
    stats: SiteStats,
    shelter_name: str | None,
    shelter_distance_m: float | None,
    on_token,
    on_thinking=None,
) -> str:
    from prompts.report_system_prompt import SITE_SUMMARY_SYSTEM_PROMPT  # noqa: PLC0415

    dist_text = f"{shelter_distance_m:.0f}m" if shelter_distance_m is not None else "unknown"
    lang_name = _language_name(language)
    prompt = (
        f"IMPORTANT: Write entirely in {lang_name}. Do not use English at all.\n"
        f"Site: {site_name}\n"
        f"Language: {lang_name} ({language})\n"
        f"Total buildings assessed: {stats.total_buildings}\n"
        f"Extreme severity (5): {stats.sev5}\n"
        f"Critical severity (4): {stats.sev4}\n"
        f"Moderate severity (3): {stats.sev3}\n"
        f"Buildings with signs of life: {stats.signs_of_life}\n"
        f"Blocked roads: {stats.blocked_roads_count}\n"
        f"Flood zone buildings: {stats.flood_count}\n"
        f"Estimated people affected: {stats.estimated_people}\n"
        f"Nearest shelter: {shelter_name or 'unknown'}\n"
        f"Distance to shelter: {dist_text}\n"
        "Write a 3-4 sentence field coordination situation summary. Plain text only."
    )
    return await stream_ai_summary(
        prompt=prompt,
        on_token=on_token,
        on_thinking=on_thinking,
        system_prompt=SITE_SUMMARY_SYSTEM_PROMPT,
        db=None,
        tools=None,
    )


async def _generate_building_narrative(
    *,
    language: str,
    damage_description: str,
    reasoning: str,
    severity: int,
    damage_type: str,
    structural_risk: str,
    warnings: list[str],
    on_token=None,
    on_thinking=None,
) -> str:
    from prompts.report_system_prompt import BUILDING_NARRATIVE_SYSTEM_PROMPT  # noqa: PLC0415

    async def _noop(_: str) -> None:
        return None

    lang_name = _language_name(language)
    prompt = (
        f"IMPORTANT: Write entirely in {lang_name}. Do not use English at all.\n"
        f"Language: {lang_name} ({language})\n"
        f"Severity: {severity}/5\n"
        f"Damage type: {damage_type}\n"
        f"Structural risk: {structural_risk or 'unknown'}\n"
        f"Damage description: {damage_description}\n"
        f"AI reasoning: {reasoning[:800]}\n"
        f"Warnings: {', '.join(warnings) if warnings else 'none'}\n"
        "Write 3-4 sentences explaining WHY this severity was assigned. Plain text only."
    )
    return await stream_ai_summary(
        prompt=prompt,
        on_token=on_token or _noop,
        on_thinking=on_thinking,
        system_prompt=BUILDING_NARRATIVE_SYSTEM_PROMPT,
        db=None,
        tools=None,
    )


# ═══════════════════════════════════════════════════════════════════
# V2 REPORT — BuildingMapSet dataclass + processing pipeline
# ═══════════════════════════════════════════════════════════════════

def _action_label_class(action_raw: str) -> tuple[str, str]:
    """Return (canonical_english_label, css_class). Always returns a canonical label so translations can match."""
    low = action_raw.lower()
    if "search" in low or "rescue" in low:
        return "IMMEDIATE SEARCH AND RESCUE", "urgent"
    if "urgent" in low and "evacuat" in low:
        return "URGENT EVACUATION", "high"
    if "evacuat" in low:
        return "EVACUATE AND SECURE", "medium"
    if "structural" in low or "assessment" in low:
        return "STRUCTURAL ASSESSMENT", "low"
    return "MONITOR", "none"


# ── AI-driven label translation ───────────────────────────────────────────

_TRANSLATE_KEYS: list[str] = [
    # Data field labels
    "Severity", "Damage Type", "Description", "Building Type",
    "Estimated Occupants", "Occupant Status", "Flood Zone", "Road Access",
    "Nearest Road", "Nearest Shelter", "Shelter Distance", "Recommended Action",
    "Action Priority", "Confidence", "Assigned Worker", "Coordinates",
    # Section / map headings
    "Site Overview Map", "Nearest Evacuation Point", "Evacuation Route to Shelter",
    "Priority Building Assessments", "Respond to buildings in priority order",
    "Building Location Map", "Imagery", "Ground Photo", "Before Earthquake",
    "After Earthquake", "Drone Frame", "Route to Shelter", "Signs of Life Detected",
    "km driving", "min driving",
    # Site header meta row
    "FIELD REPORT", "Team", "Language", "Generated", "Report ID",
    # Stat badge labels
    "Total", "Extreme", "Critical", "Moderate", "Signs of Life", "Affected",
    # Building card
    "PRIORITY RESPONSE",
    # Action label fallbacks
    "IMMEDIATE SEARCH AND RESCUE", "URGENT EVACUATION", "EVACUATE AND SECURE",
    "STRUCTURAL ASSESSMENT", "MONITOR",
    # Report footer
    "All processing local · No data transmitted externally",
    "Generated by BhumiDrishti",
    # Field enum values (damage type)
    "no_visible_damage", "structural_damage", "partial_collapse", "full_collapse",
    "moderate_damage", "severe_damage",
    # Field enum values (road access / occupant status)
    "passable", "blocked", "limited", "unknown", "Unknown",
    "trapped", "evacuated", "sheltered",
    # Field enum values (recommended action)
    "immediate_search_rescue", "urgent_evacuation", "evacuate_secure",
    "structural_assessment", "monitor",
    # Common field fallbacks
    "Unassigned", "N/A", "Yes", "No",
]


async def _translate_labels_ai(language: str) -> dict[str, str]:
    """Single Gemma batch call to translate all UI label keys into the target language.
    Falls back gracefully — missing keys render as their English name."""
    if not language or language == "en":
        return {}
    lang_name = _language_name(language)
    prompt = (
        f"Translate each value in the following JSON into {lang_name}. "
        "Return ONLY valid JSON with identical keys and translated string values. "
        "No markdown fences, no extra text.\n"
        + json.dumps({k: k for k in _TRANSLATE_KEYS}, ensure_ascii=False)
    )
    try:
        resp = await ollama_client.chat(
            model=REPORT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        raw = (resp.message.content or "").strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(raw[start:end])
            return {k: str(v) for k, v in parsed.items() if isinstance(v, (str, int, float))}
    except Exception as exc:
        logger.warning("translate_labels_ai.failed lang=%s error=%s", language, exc)
    return {}


async def _translate_route_steps_ai(steps: list[str], language: str) -> dict[str, str]:
    """Translate a deduplicated list of OSRM route-step strings in one AI batch call."""
    if not steps or not language or language == "en":
        return {}
    lang_name = _language_name(language)
    unique = list(dict.fromkeys(s for s in steps if s))
    if not unique:
        return {}
    prompt = (
        f"Translate each value in the following JSON into {lang_name}. "
        "Keep distance values like '100m', '1.2km', street names, and proper nouns unchanged. "
        "Return ONLY valid JSON with identical keys and translated string values. "
        "No markdown fences, no extra text.\n"
        + json.dumps({s: s for s in unique}, ensure_ascii=False)
    )
    try:
        resp = await ollama_client.chat(
            model=REPORT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        raw = (resp.message.content or "").strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(raw[start:end])
            return {k: str(v) for k, v in parsed.items() if isinstance(v, (str, int, float))}
    except Exception as exc:
        logger.warning("translate_route_steps_ai.failed lang=%s error=%s", language, exc)
    return {}


def _build_building_data_fields(row: asyncpg.Record, translations: dict[str, str] | None = None) -> list[dict[str, Any]]:
    sev = _safe_int(row.get("severity"), 0)
    occ_status = str(row.get("occupant_status") or "unknown")
    flood = "YES" if bool(row.get("flood_zone")) else "No"
    road = str(row.get("road_access") or "unknown")
    conf_raw = _safe_float(row.get("confidence"))
    conf_text = f"{conf_raw:.0%}" if conf_raw is not None else "N/A"
    shelter_dist = row.get("shelter_distance_m")
    shelter_dist_text = f"{shelter_dist:.0f} m" if shelter_dist else "N/A"

    tr = translations or {}
    t = lambda k: tr.get(k, k)  # noqa: E731
    damage_type = t(str(row.get("damage_type") or "Unknown"))
    occ_status_tr = t(occ_status)
    flood_tr = t(flood)
    road_tr = t(road)
    action_raw = str(row.get("recommended_action") or "N/A")
    action_tr = t(action_raw)
    worker_tr = t(str(row.get("worker_name") or "Unassigned"))
    return [
        {"label": t("Severity"), "value": f"SEV {sev}", "css_class": "bd-data-critical" if sev >= 4 else ""},
        {"label": t("Damage Type"), "value": damage_type, "css_class": ""},
        {"label": t("Description"), "value": str(row.get("damage_description") or "N/A"), "css_class": ""},
        {"label": t("Building Type"), "value": str(row.get("building_type") or "Unknown"), "css_class": ""},
        {"label": t("Estimated Occupants"), "value": str(row.get("estimated_occupants") or "N/A"), "css_class": ""},
        {"label": t("Occupant Status"), "value": occ_status_tr, "css_class": "bd-data-critical" if occ_status.lower() == "trapped" else ""},
        {"label": t("Flood Zone"), "value": flood_tr, "css_class": "bd-data-critical" if flood == "YES" else ""},
        {"label": t("Road Access"), "value": road_tr, "css_class": "bd-data-blocked" if road.lower() == "blocked" else ""},
        {"label": t("Nearest Road"), "value": str(row.get("nearest_road") or "N/A"), "css_class": ""},
        {"label": t("Nearest Shelter"), "value": str(row.get("nearest_shelter") or "N/A"), "css_class": ""},
        {"label": t("Shelter Distance"), "value": shelter_dist_text, "css_class": ""},
        {"label": t("Recommended Action"), "value": action_tr, "css_class": ""},
        {"label": t("Action Priority"), "value": str(row.get("action_priority") or "N/A"), "css_class": ""},
        {"label": t("Confidence"), "value": conf_text, "css_class": ""},
        {"label": t("Assigned Worker"), "value": worker_tr, "css_class": ""},
        {"label": t("Coordinates"), "value": f"{row.get('lat')}°N  {row.get('lon')}°E", "css_class": ""},
    ]


@dataclass
class BuildingMapSet:
    assessment_id: str
    severity: int
    lat: float
    lon: float
    action_label: str
    action_class: str
    location_map_url: str | None
    route_map_url: str | None
    route_steps: list[str] = field(default_factory=list)
    route_distance_m: float | None = None
    route_duration_s: float | None = None
    shelter_lat: float | None = None
    shelter_lon: float | None = None
    narrative: str = ""
    data_fields: list[dict] = field(default_factory=list)
    photo_url: str | None = None
    pre_url: str | None = None
    post_url: str | None = None
    drone_frame_urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    signs_of_life: bool = False


async def _process_building(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
    report_id: str,
    shelter_lat: float | None,
    shelter_lon: float | None,
    language: str,
) -> BuildingMapSet:
    assessment_id = str(row.get("id") or "")
    severity = _safe_int(row.get("severity"), 1)
    lat = _safe_float(row.get("lat")) or 0.0
    lon = _safe_float(row.get("lon")) or 0.0
    action_raw = str(row.get("recommended_action") or "")
    action_label, action_class = _action_label_class(action_raw)
    signs_of_life = str(row.get("occupant_status") or "").lower() in {"trapped", "evacuated"}
    warnings_raw = row.get("warnings")
    warnings = warnings_raw if isinstance(warnings_raw, list) else []
    data_fields = _build_building_data_fields(row)

    # Media URLs
    pre_url = _to_media_url(row.get("pre_chip_path"))
    post_chip = _to_media_url(row.get("chip_path"))
    photo = _to_media_url(row.get("photo_path"))
    post_url = post_chip or photo
    photo_url = photo
    drone_frame_urls = [
        u for u in (_to_media_url(f) for f in _parse_frames(row.get("drone_frames")))
        if u is not None
    ]

    # Step 1: Building footprint
    footprint: dict | None = None
    try:
        footprint = await _fetch_building_footprint(conn, lat, lon)
    except Exception as exc:
        logger.debug("footprint.failed assessment_id=%s error=%s", assessment_id, exc)

    # Step 2: Location map
    _, location_map_url = await create_building_location_map(
        report_id=report_id,
        assessment_id=assessment_id,
        lat=lat, lon=lon,
        footprint_geojson=footprint,
        severity=severity,
    )

    # Step 3 & 4: Route to shelter
    route_geojson: dict | None = None
    route_steps: list[str] = []
    route_distance_m: float | None = None
    route_duration_s: float | None = None
    route_map_url: str | None = None

    if shelter_lat is not None and shelter_lon is not None:
        try:
            route_geojson, route_steps, route_distance_m, route_duration_s = await _fetch_osrm_route_full(
                lat, lon, shelter_lat, shelter_lon
            )
        except Exception as exc:
            logger.debug("route.failed assessment_id=%s error=%s", assessment_id, exc)

        _, route_map_url = await create_building_route_map(
            report_id=report_id,
            assessment_id=assessment_id,
            building_lat=lat, building_lon=lon,
            shelter_lat=shelter_lat, shelter_lon=shelter_lon,
            route_geometry_geojson=route_geojson,
            severity=severity,
        )

    # Step 5: AI narrative (fire-and-forget style, result stored)
    narrative = ""
    try:
        narrative = await asyncio.wait_for(
            _generate_building_narrative(
                language=language,
                damage_description=str(row.get("damage_description") or ""),
                reasoning=str(row.get("reasoning") or ""),
                severity=severity,
                damage_type=str(row.get("damage_type") or ""),
                structural_risk="",
                warnings=warnings,
            ),
            timeout=240.0,
        )
    except Exception as exc:
        logger.debug("building_narrative.failed assessment_id=%s error=%s", assessment_id, exc)

    return BuildingMapSet(
        assessment_id=assessment_id,
        severity=severity,
        lat=lat,
        lon=lon,
        action_label=action_label,
        action_class=action_class,
        location_map_url=location_map_url,
        route_map_url=route_map_url,
        route_steps=route_steps,
        route_distance_m=route_distance_m,
        route_duration_s=route_duration_s,
        shelter_lat=shelter_lat,
        shelter_lon=shelter_lon,
        narrative=narrative,
        data_fields=data_fields,
        photo_url=photo_url,
        pre_url=pre_url,
        post_url=post_url,
        drone_frame_urls=drone_frame_urls,
        warnings=warnings,
        signs_of_life=signs_of_life,
    )


async def _process_buildings_concurrent(
    pool: asyncpg.Pool,
    rows: list[asyncpg.Record],
    report_id: str,
    shelter_lat: float | None,
    shelter_lon: float | None,
    language: str,
    on_progress,
) -> list[BuildingMapSet]:
    sem = asyncio.Semaphore(3)

    async def _one(idx: int, row: asyncpg.Record) -> BuildingMapSet | None:
        async with sem:
            try:
                await on_progress(f"Processing building {idx}/{len(rows)}…")
                async with pool.acquire() as conn:
                    return await _process_building(conn, row, report_id, shelter_lat, shelter_lon, language)
            except Exception as exc:
                logger.warning("process_building.failed idx=%d error=%s", idx, exc)
                return None

    raw = await asyncio.gather(*[_one(i + 1, r) for i, r in enumerate(rows)], return_exceptions=True)
    return [r for r in raw if isinstance(r, BuildingMapSet)]


async def _process_building_data_only(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
    report_id: str,
    shelter_lat: float | None,
    shelter_lon: float | None,
    translations: dict[str, str] | None = None,
) -> BuildingMapSet:
    """Same as _process_building but skips AI narrative — runs sequentially in orchestrator."""
    assessment_id = str(row.get("id") or "")
    severity = _safe_int(row.get("severity"), 1)
    lat = _safe_float(row.get("lat")) or 0.0
    lon = _safe_float(row.get("lon")) or 0.0
    action_raw = str(row.get("recommended_action") or "")
    action_label, action_class = _action_label_class(action_raw)
    if translations:
        action_label = translations.get(action_label, action_label)
    signs_of_life = str(row.get("occupant_status") or "").lower() in {"trapped", "evacuated"}
    warnings_raw = row.get("warnings")
    warnings = warnings_raw if isinstance(warnings_raw, list) else []
    data_fields = _build_building_data_fields(row, translations)

    pre_url = _to_media_url(row.get("pre_chip_path"))
    post_chip = _to_media_url(row.get("chip_path"))
    photo = _to_media_url(row.get("photo_path"))
    post_url = post_chip or photo
    drone_frame_urls = [
        u for u in (_to_media_url(f) for f in _parse_frames(row.get("drone_frames")))
        if u is not None
    ]

    footprint: dict | None = None
    try:
        footprint = await _fetch_building_footprint(conn, lat, lon)
    except Exception as exc:
        logger.debug("footprint.failed assessment_id=%s error=%s", assessment_id, exc)

    _, location_map_url = await create_building_location_map(
        report_id=report_id,
        assessment_id=assessment_id,
        lat=lat, lon=lon,
        footprint_geojson=footprint,
        severity=severity,
    )

    route_geojson: dict | None = None
    route_steps: list[str] = []
    route_distance_m: float | None = None
    route_duration_s: float | None = None
    route_map_url: str | None = None

    if shelter_lat is not None and shelter_lon is not None:
        try:
            route_geojson, route_steps, route_distance_m, route_duration_s = await _fetch_osrm_route_full(
                lat, lon, shelter_lat, shelter_lon
            )
        except Exception as exc:
            logger.debug("route.failed assessment_id=%s error=%s", assessment_id, exc)

        _, route_map_url = await create_building_route_map(
            report_id=report_id,
            assessment_id=assessment_id,
            building_lat=lat, building_lon=lon,
            shelter_lat=shelter_lat, shelter_lon=shelter_lon,
            route_geometry_geojson=route_geojson,
            severity=severity,
        )

    return BuildingMapSet(
        assessment_id=assessment_id,
        severity=severity,
        lat=lat,
        lon=lon,
        action_label=action_label,
        action_class=action_class,
        location_map_url=location_map_url,
        route_map_url=route_map_url,
        route_steps=route_steps,
        route_distance_m=route_distance_m,
        route_duration_s=route_duration_s,
        shelter_lat=shelter_lat,
        shelter_lon=shelter_lon,
        narrative="",
        data_fields=data_fields,
        photo_url=photo,
        pre_url=pre_url,
        post_url=post_url,
        drone_frame_urls=drone_frame_urls,
        warnings=warnings,
        signs_of_life=signs_of_life,
    )


# ═══════════════════════════════════════════════════════════════════
# V2 REPORT — Jinja2 HTML template + renderer
# ═══════════════════════════════════════════════════════════════════

REPORT_HTML_TEMPLATE = """
<div class="bd-content">

{# ── Part 1: Site overview ─────────────────────────────────────── #}
<div class="bd-v2-header">
  <div class="bd-v2-logo">BhumiDrishti <span class="bd-logo-badge">{{ lbl['FIELD REPORT'] }}</span></div>
  <h1 class="bd-v2-title">{{ site_name }}</h1>
  {% if province or district %}
  <div class="bd-v2-subtitle">{% if province %}{{ province }}{% endif %}{% if province and district %} · {% endif %}{% if district %}{{ district }}{% endif %}</div>
  {% endif %}
  <div class="bd-v2-meta-row">
    <span>{{ lbl['Team'] }}: <b>{{ team_name }}</b></span>
    <span>{{ lbl['Language'] }}: <b>{{ language_name }}</b></span>
    <span>{{ lbl['Generated'] }}: <b>{{ generated_at }}</b></span>
    <span>{{ lbl['Report ID'] }}: <b>{{ report_id }}</b></span>
  </div>
</div>

<div class="bd-stats">
  <div class="bd-stat bd-stat-gray"><div class="bd-stat-num">{{ stats.total_buildings }}</div><div class="bd-stat-label">{{ lbl['Total'] }}</div></div>
  <div class="bd-stat bd-stat-dark"><div class="bd-stat-num">{{ stats.sev5 }}</div><div class="bd-stat-label">{{ lbl['Extreme'] }}</div></div>
  <div class="bd-stat bd-stat-red"><div class="bd-stat-num">{{ stats.sev4 }}</div><div class="bd-stat-label">{{ lbl['Critical'] }}</div></div>
  <div class="bd-stat bd-stat-amber"><div class="bd-stat-num">{{ stats.sev3 }}</div><div class="bd-stat-label">{{ lbl['Moderate'] }}</div></div>
  <div class="bd-stat bd-stat-pink"><div class="bd-stat-num">{{ stats.signs_of_life }}</div><div class="bd-stat-label">{{ lbl['Signs of Life'] }}</div></div>
  <div class="bd-stat bd-stat-blue"><div class="bd-stat-num">{{ stats.estimated_people }}</div><div class="bd-stat-label">{{ lbl['Affected'] }}</div></div>
  <div class="bd-stat bd-stat-teal"><div class="bd-stat-num">{{ stats.flood_count }}</div><div class="bd-stat-label">{{ lbl['Flood Zone'] }}</div></div>
</div>

{% if site_map_url %}
<div class="bd-map-section">
  <img src="{{ site_map_url }}" alt="Site overview map" class="bd-map-img bd-map-site">
  <p class="bd-map-caption">{{ lbl['Site Overview Map'] }} · EPSG:4326 · OpenStreetMap contributors</p>
</div>
{% endif %}

{% if shelter_name %}
<div class="bd-shelter-block">
  <span class="bd-shelter-name">{{ lbl['Nearest Evacuation Point'] }}: {{ shelter_name }}</span>
  {% if shelter_type %}<span class="bd-shelter-meta"> · {{ shelter_type }}</span>{% endif %}
  {% if shelter_distance_m %}<span class="bd-shelter-meta"> · {{ "%.0f"|format(shelter_distance_m) }} m</span>{% endif %}
</div>
{% endif %}

{% if site_summary %}
<div class="bd-narrative-block">{{ site_summary }}</div>
{% endif %}

{% if site_route_steps %}
<h2 class="bd-section-heading">{{ lbl['Evacuation Route to Shelter'] }}</h2>
<div class="bd-route-directions">
  {% for step in site_route_steps %}
  <div class="bd-route-step">
    <span class="bd-step-num">{{ loop.index }}</span>
    <span class="bd-step-text">{{ step }}</span>
  </div>
  {% endfor %}
</div>
{% endif %}

{# ── Part 2: Per-building assessments ─────────────────────────── #}
<h2 class="bd-part-heading">{{ lbl['Priority Building Assessments'] }}</h2>
<p style="font-size:8pt;color:#6b7280;margin-bottom:10px;">{{ lbl['Respond to buildings in priority order'] }}</p>

{% for bms in building_maps %}
<div class="bd-page-break"></div>
<div class="building-card sev-border-{{ bms.severity }}">

  <div class="bd-building-header">
    <span class="bd-assessment-id">#{{ loop.index }} — {{ bms.assessment_id }}</span>
    <span class="sev sev-{{ bms.severity }}" style="margin-left:8px;">SEV {{ bms.severity }}</span>
    <span class="bd-action-tag bd-action-{{ bms.action_class }}" style="margin-left:8px;">{{ bms.action_label }}</span>
    <span class="bd-building-coords">{{ "%.5f"|format(bms.lat) }}°N  {{ "%.5f"|format(bms.lon) }}°E</span>
  </div>

  {% if bms.signs_of_life %}
  <div class="life-banner">⚠ {{ lbl['Signs of Life Detected'] }} — {{ lbl['PRIORITY RESPONSE'] }}</div>
  {% endif %}

  {% if bms.location_map_url %}
  <div class="bd-map-section" style="margin:8px 0;">
    <img src="{{ bms.location_map_url }}" alt="Building location" class="bd-map-img bd-map-building">
    <p class="bd-map-caption">{{ lbl['Building Location Map'] }} · zoom 17 · EPSG:4326</p>
  </div>
  {% endif %}

  <table class="bd-data-table">
    {% for f in bms.data_fields %}
    <tr class="bd-data-row{% if loop.index is even %} bd-data-alt{% endif %}">
      <td class="bd-data-key">{{ f.label }}</td>
      <td class="bd-data-val {{ f.css_class }}">{{ f.value }}</td>
    </tr>
    {% endfor %}
  </table>

  {% if bms.narrative %}
  <div class="gemma-note bd-narrative-block" style="margin-top:8px;">{{ bms.narrative }}</div>
  {% endif %}

  {% set has_media = bms.photo_url or bms.pre_url or bms.post_url or bms.drone_frame_urls %}
  {% if has_media %}
  <div class="bd-media-section">
    <h4 class="bd-subsection-heading">{{ lbl['Imagery'] }}</h4>
    <div class="bd-media-row">
      {% if bms.photo_url %}
      <div class="bd-media-item">
        <div class="bd-media-label">{{ lbl['Ground Photo'] }}</div>
        <img src="{{ bms.photo_url }}" alt="Ground photo">
      </div>
      {% endif %}
      {% if bms.pre_url and bms.pre_url != bms.photo_url %}
      <div class="bd-media-item">
        <div class="bd-media-label">{{ lbl['Before Earthquake'] }}</div>
        <img src="{{ bms.pre_url }}" alt="Pre-earthquake">
      </div>
      {% endif %}
      {% if bms.post_url and bms.post_url != bms.photo_url %}
      <div class="bd-media-item">
        <div class="bd-media-label">{{ lbl['After Earthquake'] }}</div>
        <img src="{{ bms.post_url }}" alt="Post-earthquake">
      </div>
      {% endif %}
      {% for frame_url in bms.drone_frame_urls[:3] %}
      <div class="bd-media-item">
        <div class="bd-media-label">{{ lbl['Drone Frame'] }} {{ loop.index }}</div>
        <img src="{{ frame_url }}" alt="Drone frame {{ loop.index }}">
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  {% if bms.route_map_url %}
  <div class="bd-route-section">
    <h4 class="bd-subsection-heading">{{ lbl['Route to Shelter'] }}</h4>
    <img src="{{ bms.route_map_url }}" alt="Route to shelter" class="bd-map-img bd-map-route">
    {% if bms.route_distance_m or bms.route_duration_s %}
    <p class="bd-route-meta">
      {% if bms.route_distance_m %}{{ "%.1f"|format(bms.route_distance_m / 1000) }} {{ lbl['km driving'] }}{% endif %}
      {% if bms.route_duration_s %} · ~{{ (bms.route_duration_s / 60)|int }} {{ lbl['min driving'] }}{% endif %}
    </p>
    {% endif %}
    {% if bms.route_steps %}
    <div class="bd-route-directions">
      {% for step in bms.route_steps %}
      <div class="bd-route-step">
        <span class="bd-step-num">{{ loop.index }}</span>
        <span class="bd-step-text">{{ step }}</span>
      </div>
      {% endfor %}
    </div>
    {% endif %}
  </div>
  {% endif %}

  {% if bms.warnings %}
  <div class="bd-warnings-row" style="margin-top:7px;">
    {% for w in bms.warnings %}<span class="warn-tag">{{ w }}</span>{% endfor %}
  </div>
  {% endif %}

</div>{# end building-card #}
{% endfor %}

{% if extra_rows %}
<div class="bd-page-break"></div>
<h2 class="bd-part-heading">{{ lbl['Priority Building Assessments'] }} ({{ extra_rows|length }})</h2>
<table class="bd-data-table">
  <tr style="background:#f0fdf4;">
    <th class="bd-data-key">ID</th>
    <th class="bd-data-val">{{ lbl['Severity'] }}</th>
    <th class="bd-data-val">{{ lbl['Recommended Action'] }}</th>
    <th class="bd-data-val">{{ lbl['Estimated Occupants'] }}</th>
    <th class="bd-data-val">{{ lbl['Flood Zone'] }}</th>
    <th class="bd-data-val">{{ lbl['Road Access'] }}</th>
  </tr>
  {% for row in extra_rows %}
  <tr class="bd-data-row{% if loop.index is even %} bd-data-alt{% endif %}">
    <td class="bd-data-key">{{ row.id }}</td>
    <td class="bd-data-val"><span class="sev sev-{{ row.severity }}">{{ row.severity }}</span></td>
    <td class="bd-data-val">{{ row.recommended_action or "N/A" }}</td>
    <td class="bd-data-val">{{ row.estimated_occupants or "N/A" }}</td>
    <td class="bd-data-val {{ "bd-data-critical" if row.flood_zone else "" }}">{{ "YES" if row.flood_zone else "No" }}</td>
    <td class="bd-data-val {{ "bd-data-blocked" if (row.road_access or "")|lower == "blocked" else "" }}">{{ row.road_access or "N/A" }}</td>
  </tr>
  {% endfor %}
</table>
{% endif %}

<div style="margin-top:24px;padding-top:12px;border-top:1px solid #e5e7eb;font-size:7pt;color:#9ca3af;text-align:center;">
  {{ lbl['Generated by BhumiDrishti'] }} · Powered by Gemma 4 via Ollama · Data: OpenStreetMap · {{ lbl['All processing local · No data transmitted externally'] }}
</div>

</div>{# end bd-content #}
""".strip()


def _render_report_html(
    *,
    site_name: str,
    province: str | None,
    district: str | None,
    generated_at: str,
    team_name: str | None,
    language: str,
    report_id: str,
    stats: SiteStats,
    site_map_url: str | None,
    shelter_name: str | None,
    shelter_type: str | None,
    shelter_distance_m: float | None,
    site_summary: str,
    site_route_steps: list[str],
    building_maps: list[BuildingMapSet],
    extra_rows: list[asyncpg.Record],
    translations: dict[str, str] | None = None,
) -> str:
    try:
        from jinja2 import Environment  # noqa: PLC0415

        tr = translations or {}
        lbl = {k: tr.get(k, k) for k in _TRANSLATE_KEYS}
        env = Environment(autoescape=True)
        tmpl = env.from_string(REPORT_HTML_TEMPLATE)
        extra = [dict(r) for r in extra_rows]
        return tmpl.render(
            site_name=site_name,
            province=province or "",
            district=district or "",
            generated_at=generated_at,
            team_name=team_name or "Unassigned",
            language_name=_language_name(language),
            report_id=report_id,
            stats=stats,
            site_map_url=site_map_url,
            shelter_name=shelter_name,
            shelter_type=shelter_type,
            shelter_distance_m=shelter_distance_m,
            site_summary=site_summary,
            site_route_steps=site_route_steps,
            building_maps=building_maps,
            extra_rows=extra,
            lbl=lbl,
        )
    except Exception as exc:
        logger.exception("render_report_html.failed error=%s", exc)
        return f"<div class='bd-content'><h1>{site_name}</h1><p>Report rendering error: {exc}</p></div>"


# ═══════════════════════════════════════════════════════════════════
# V2 REPORT — Per-section templates for live streaming
# ═══════════════════════════════════════════════════════════════════

TIER1_BUILDINGS = 10  # top N buildings get live thinking + token streaming

_SITE_HEADER_TMPL = """
<div class="bd-content">
<div class="bd-v2-header">
  <div class="bd-v2-logo">BhumiDrishti <span class="bd-logo-badge">{{ lbl['FIELD REPORT'] }}</span></div>
  <h1 class="bd-v2-title">{{ site_name }}</h1>
  {% if province or district %}
  <div class="bd-v2-subtitle">{% if province %}{{ province }}{% endif %}{% if province and district %} · {% endif %}{% if district %}{{ district }}{% endif %}</div>
  {% endif %}
  <div class="bd-v2-meta-row">
    <span>{{ lbl['Team'] }}: <b>{{ team_name }}</b></span>
    <span>{{ lbl['Language'] }}: <b>{{ language_name }}</b></span>
    <span>{{ lbl['Generated'] }}: <b>{{ generated_at }}</b></span>
    <span>{{ lbl['Report ID'] }}: <b>{{ report_id }}</b></span>
  </div>
</div>
<div class="bd-stats">
  <div class="bd-stat bd-stat-gray"><div class="bd-stat-num">{{ stats.total_buildings }}</div><div class="bd-stat-label">{{ lbl['Total'] }}</div></div>
  <div class="bd-stat bd-stat-dark"><div class="bd-stat-num">{{ stats.sev5 }}</div><div class="bd-stat-label">{{ lbl['Extreme'] }}</div></div>
  <div class="bd-stat bd-stat-red"><div class="bd-stat-num">{{ stats.sev4 }}</div><div class="bd-stat-label">{{ lbl['Critical'] }}</div></div>
  <div class="bd-stat bd-stat-amber"><div class="bd-stat-num">{{ stats.sev3 }}</div><div class="bd-stat-label">{{ lbl['Moderate'] }}</div></div>
  <div class="bd-stat bd-stat-pink"><div class="bd-stat-num">{{ stats.signs_of_life }}</div><div class="bd-stat-label">{{ lbl['Signs of Life'] }}</div></div>
  <div class="bd-stat bd-stat-blue"><div class="bd-stat-num">{{ stats.estimated_people }}</div><div class="bd-stat-label">{{ lbl['Affected'] }}</div></div>
  <div class="bd-stat bd-stat-teal"><div class="bd-stat-num">{{ stats.flood_count }}</div><div class="bd-stat-label">{{ lbl['Flood Zone'] }}</div></div>
</div>
""".strip()

_SITE_MAP_SECTION_TMPL = """
{% if site_map_url %}
<div class="bd-map-section">
  <img src="{{ site_map_url }}" alt="Site overview map" class="bd-map-img bd-map-site">
  <p class="bd-map-caption">{{ lbl['Site Overview Map'] }} · EPSG:4326 · OpenStreetMap contributors</p>
</div>
{% endif %}
{% if shelter_name %}
<div class="bd-shelter-block">
  <span class="bd-shelter-name">{{ lbl['Nearest Evacuation Point'] }}: {{ shelter_name }}</span>
  {% if shelter_type %}<span class="bd-shelter-meta"> · {{ shelter_type }}</span>{% endif %}
  {% if shelter_distance_m %}<span class="bd-shelter-meta"> · {{ "%.0f"|format(shelter_distance_m) }} m</span>{% endif %}
</div>
{% endif %}
{% if site_route_steps %}
<h2 class="bd-section-heading">{{ lbl['Evacuation Route to Shelter'] }}</h2>
<div class="bd-route-directions">
  {% for step in site_route_steps %}
  <div class="bd-route-step">
    <span class="bd-step-num">{{ loop.index }}</span>
    <span class="bd-step-text">{{ step }}</span>
  </div>
  {% endfor %}
</div>
{% endif %}
""".strip()

_SITE_SUMMARY_SECTION_TMPL = """
{% if site_summary %}
<div class="bd-narrative-block">{{ site_summary }}</div>
{% endif %}
""".strip()

def _section_buildings_heading(translations: dict[str, str] | None = None) -> str:
    tr = translations or {}
    t = lambda k: tr.get(k, k)
    return (
        f'<h2 class="bd-part-heading">{t("Priority Building Assessments")}</h2>'
        f'<p style="font-size:8pt;color:#6b7280;margin-bottom:10px;">'
        f'{t("Respond to buildings in priority order")}'
        "</p>"
    )

_BUILDING_CARD_TMPL = """
<div class="bd-page-break"></div>
<div class="building-card sev-border-{{ bms.severity }}">
  <div class="bd-building-header">
    <span class="bd-assessment-id">#{{ priority_num }} — {{ bms.assessment_id }}</span>
    <span class="sev sev-{{ bms.severity }}" style="margin-left:8px;">SEV {{ bms.severity }}</span>
    <span class="bd-action-tag bd-action-{{ bms.action_class }}" style="margin-left:8px;">{{ bms.action_label }}</span>
    <span class="bd-building-coords">{{ "%.5f"|format(bms.lat) }}°N  {{ "%.5f"|format(bms.lon) }}°E</span>
  </div>
  {% if bms.signs_of_life %}
  <div class="life-banner">⚠ {{ lbl['Signs of Life Detected'] }} — {{ lbl['PRIORITY RESPONSE'] }}</div>
  {% endif %}
  {% if bms.location_map_url %}
  <div class="bd-map-section" style="margin:8px 0;">
    <img src="{{ bms.location_map_url }}" alt="Building location" class="bd-map-img bd-map-building">
    <p class="bd-map-caption">{{ lbl['Building Location Map'] }} · zoom 17 · EPSG:4326</p>
  </div>
  {% endif %}
  <table class="bd-data-table">
    {% for f in bms.data_fields %}
    <tr class="bd-data-row{% if loop.index is even %} bd-data-alt{% endif %}">
      <td class="bd-data-key">{{ f.label }}</td>
      <td class="bd-data-val {{ f.css_class }}">{{ f.value }}</td>
    </tr>
    {% endfor %}
  </table>
  {% if bms.narrative %}
  <div class="gemma-note bd-narrative-block" style="margin-top:8px;">{{ bms.narrative }}</div>
  {% endif %}
  {% set has_media = bms.photo_url or bms.pre_url or bms.post_url or bms.drone_frame_urls %}
  {% if has_media %}
  <div class="bd-media-section">
    <h4 class="bd-subsection-heading">{{ lbl['Imagery'] }}</h4>
    <div class="bd-media-row">
      {% if bms.photo_url %}<div class="bd-media-item"><div class="bd-media-label">{{ lbl['Ground Photo'] }}</div><img src="{{ bms.photo_url }}" alt="Ground photo"></div>{% endif %}
      {% if bms.pre_url and bms.pre_url != bms.photo_url %}<div class="bd-media-item"><div class="bd-media-label">{{ lbl['Before Earthquake'] }}</div><img src="{{ bms.pre_url }}" alt="Pre"></div>{% endif %}
      {% if bms.post_url and bms.post_url != bms.photo_url %}<div class="bd-media-item"><div class="bd-media-label">{{ lbl['After Earthquake'] }}</div><img src="{{ bms.post_url }}" alt="Post"></div>{% endif %}
      {% for frame_url in bms.drone_frame_urls[:3] %}<div class="bd-media-item"><div class="bd-media-label">{{ lbl['Drone Frame'] }} {{ loop.index }}</div><img src="{{ frame_url }}" alt="Drone {{ loop.index }}"></div>{% endfor %}
    </div>
  </div>
  {% endif %}
  {% if bms.route_map_url %}
  <div class="bd-route-section">
    <h4 class="bd-subsection-heading">{{ lbl['Route to Shelter'] }}</h4>
    <img src="{{ bms.route_map_url }}" alt="Route to shelter" class="bd-map-img bd-map-route">
    {% if bms.route_distance_m or bms.route_duration_s %}
    <p class="bd-route-meta">{% if bms.route_distance_m %}{{ "%.1f"|format(bms.route_distance_m / 1000) }} {{ lbl['km driving'] }}{% endif %}{% if bms.route_duration_s %} · ~{{ (bms.route_duration_s / 60)|int }} {{ lbl['min driving'] }}{% endif %}</p>
    {% endif %}
    {% if bms.route_steps %}
    <div class="bd-route-directions">
      {% for step in bms.route_steps %}<div class="bd-route-step"><span class="bd-step-num">{{ loop.index }}</span><span class="bd-step-text">{{ step }}</span></div>{% endfor %}
    </div>
    {% endif %}
  </div>
  {% endif %}
  {% if bms.warnings %}
  <div class="bd-warnings-row" style="margin-top:7px;">
    {% for w in bms.warnings %}<span class="warn-tag">{{ w }}</span>{% endfor %}
  </div>
  {% endif %}
</div>
""".strip()

def _section_report_footer(translations: dict[str, str] | None = None) -> str:
    tr = translations or {}
    t = lambda k: tr.get(k, k)
    return (
        '<div style="margin-top:24px;padding-top:12px;border-top:1px solid #e5e7eb;'
        'font-size:7pt;color:#9ca3af;text-align:center;">'
        f'{t("Generated by BhumiDrishti")} · Powered by Gemma 4 via Ollama · '
        f'Data: OpenStreetMap · {t("All processing local · No data transmitted externally")}'
        "</div></div>"
    )


def _jinja_section(template_str: str, **ctx: Any) -> str:
    try:
        from jinja2 import Environment  # noqa: PLC0415
        return Environment(autoescape=True).from_string(template_str).render(**ctx)
    except Exception as exc:
        logger.warning("jinja_section.failed error=%s", exc)
        return ""


def _section_site_header(*, site_name: str, province: str | None, district: str | None,
                          team_name: str | None, language: str, report_id: str,
                          stats: SiteStats, generated_at: str,
                          translations: dict[str, str] | None = None) -> str:
    tr = translations or {}
    lbl = {k: tr.get(k, k) for k in (
        "FIELD REPORT", "Team", "Language", "Generated", "Report ID",
        "Total", "Extreme", "Critical", "Moderate", "Signs of Life", "Affected", "Flood Zone",
    )}
    return _jinja_section(
        _SITE_HEADER_TMPL,
        site_name=site_name, province=province or "", district=district or "",
        team_name=team_name or "Unassigned",
        language_name=_language_name(language),
        report_id=report_id, stats=stats, generated_at=generated_at, lbl=lbl,
    )


def _section_site_map(*, site_map_url: str | None, shelter_name: str | None,
                       shelter_type: str | None, shelter_distance_m: float | None,
                       site_route_steps: list[str], translations: dict[str, str] | None = None) -> str:
    tr = translations or {}
    lbl = {k: tr.get(k, k) for k in ("Site Overview Map", "Nearest Evacuation Point", "Evacuation Route to Shelter")}
    return _jinja_section(
        _SITE_MAP_SECTION_TMPL,
        site_map_url=site_map_url, shelter_name=shelter_name,
        shelter_type=shelter_type, shelter_distance_m=shelter_distance_m,
        site_route_steps=site_route_steps, lbl=lbl,
    )


def _section_site_summary(*, site_summary: str) -> str:
    return _jinja_section(_SITE_SUMMARY_SECTION_TMPL, site_summary=site_summary)


def _section_building_card(bms: "BuildingMapSet", priority_num: int, translations: dict[str, str] | None = None) -> str:
    tr = translations or {}
    lbl = {k: tr.get(k, k) for k in (
        "Building Location Map", "Imagery", "Ground Photo", "Before Earthquake",
        "After Earthquake", "Drone Frame", "Route to Shelter", "Signs of Life Detected",
        "km driving", "min driving", "PRIORITY RESPONSE",
    )}
    return _jinja_section(_BUILDING_CARD_TMPL, bms=bms, priority_num=priority_num, lbl=lbl)


# ═══════════════════════════════════════════════════════════════════
# V2 REPORT — Orchestrator
# ═══════════════════════════════════════════════════════════════════

async def generate_site_report_v2(
    *,
    pool: asyncpg.Pool,
    request: ReportGenerateRequest,
    report_id: str,
    on_progress,
    on_token,
    on_thinking=None,
    on_tool_call=None,
    on_tool_result=None,
    on_section=None,
) -> str:
    """
    Phased live-streaming report:
      Phase 0 → fetch rows, emit site header immediately
      Phase 1 → site map (background task) + ALL building data sequential one-by-one
      Phase 2 → site summary AI streamed live, emit section
      Phase 3 → Tier 1 top-N: sequential narrative streaming, emit each card live
      Phase 4 → Tier 2 rest: sequential narrative, emit each card live
    Returns the complete assembled HTML for PDF rendering.
    """
    _log_report_event(
        "generate_site_report_v2.started",
        {"report_id": report_id, "site_id": request.site_id, "site_name": request.site_name},
    )

    async def _emit(html: str) -> None:
        if on_section:
            await on_section(html)
            await asyncio.sleep(0)

    async def _tool(name: str, detail: str = "") -> None:
        if on_tool_call:
            await on_tool_call(name, {"detail": detail})
            await asyncio.sleep(0)

    # ── Phase 0: Fetch rows → translate labels → emit header ─────────
    await on_progress("Fetching assessment data…")
    await _tool("query_site_assessments", f"site_id={request.site_id} site_name={request.site_name}")
    rows = await load_site_rows(pool, request)
    if not rows:
        raise ValueError("No assessments found for the selected site")

    site_name = str(rows[0].get("resolved_site_name") or request.site_name or f"site-{request.site_id}").upper()
    province = rows[0].get("province") if isinstance(rows[0].get("province"), str) else None
    district = rows[0].get("district") if isinstance(rows[0].get("district"), str) else None
    stats = _site_stats(rows)
    generated_at = _format_timestamp()

    # Translate all UI labels before any HTML is emitted so the full report
    # (header included) is in the user's chosen language from the start.
    ui_translations: dict[str, str] = {}
    effective_lang = request.language or "en"
    if effective_lang != "en":
        await on_progress("Generating…")
        try:
            ui_translations = await asyncio.wait_for(
                _translate_labels_ai(effective_lang), timeout=150.0
            )
        except Exception as exc:
            logger.warning("ui_translation.failed lang=%s error=%s", effective_lang, exc)

    await _emit(_section_site_header(
        site_name=site_name, province=province, district=district,
        team_name=request.team_name, language=request.language,
        report_id=report_id, stats=stats, generated_at=generated_at,
        translations=ui_translations,
    ))

    # ── Phase 1: Resolve shelter + parallel site map + all building data ─
    centroid = _centroid_from_rows(rows)
    shelter_lat: float | None = None
    shelter_lon: float | None = None
    shelter_name: str | None = None
    shelter_type: str | None = None
    shelter_distance_m: float | None = None

    if centroid:
        await _tool("query_nearest_shelter", f"centroid=({centroid[0]:.4f},{centroid[1]:.4f})")
        async with pool.acquire() as conn:
            shelter_result = await _fetch_nearest_shelter(conn, centroid)
        if shelter_result:
            shelter_lat, shelter_lon, shelter_name = shelter_result
            nearest_row = next((r for r in rows if r.get("shelter_distance_m") is not None), None)
            if nearest_row:
                shelter_type = str(nearest_row.get("shelter_type") or "Shelter")
                shelter_distance_m = _safe_float(nearest_row.get("shelter_distance_m"))

    await on_progress(f"Processing {len(rows)} buildings one by one + site overview map…")

    async def _run_site_map() -> tuple[str | None, list[str]]:
        await _tool("create_site_static_map", f"{len(rows)} buildings, zoom auto")
        try:
            _, url, steps = await create_site_static_map(
                pool=pool, request=request, report_id=report_id, rows=rows
            )
            return url, steps
        except Exception as exc:
            logger.warning("site_map.failed error=%s", exc)
            return None, []

    # Site map runs concurrently with sequential building data collection
    site_map_task = asyncio.create_task(_run_site_map())

    ordered: list[tuple[BuildingMapSet, asyncpg.Record]] = []
    for idx, row in enumerate(rows):
        assessment_id = str(row.get("id") or idx)
        await on_progress(f"Processing building {idx + 1}/{len(rows)} — {assessment_id}…")
        try:
            async with pool.acquire() as conn:
                bms = await _process_building_data_only(
                    conn, row, report_id, shelter_lat, shelter_lon, translations=ui_translations
                )
            ordered.append((bms, row))
        except Exception as exc:
            logger.warning("building_data.failed idx=%d error=%s", idx, exc)

    site_map_url, site_route_steps = await site_map_task

    # Translate route steps (OSRM directions) via one AI batch call
    if effective_lang != "en":
        all_steps = [s for bms, _ in ordered for s in bms.route_steps] + site_route_steps
        try:
            step_tr = await asyncio.wait_for(
                _translate_route_steps_ai(all_steps, effective_lang), timeout=120.0
            )
            if step_tr:
                ordered = [
                    (dc_replace(bms, route_steps=[step_tr.get(s, s) for s in bms.route_steps]), row)
                    for bms, row in ordered
                ]
                site_route_steps = [step_tr.get(s, s) for s in site_route_steps]
        except Exception as exc:
            logger.warning("route_step_translation.failed lang=%s error=%s", effective_lang, exc)

    # Translate damage descriptions (raw DB text, always English) via one AI batch call
    if effective_lang != "en":
        unique_descs = list(dict.fromkeys(
            str(row.get("damage_description") or "")
            for _, row in ordered
            if row.get("damage_description")
        ))
        if unique_descs:
            try:
                desc_tr = await asyncio.wait_for(
                    _translate_route_steps_ai(unique_descs, effective_lang), timeout=150.0
                )
                if desc_tr:
                    description_label = ui_translations.get("Description", "Description")
                    for bms, _ in ordered:
                        for field in bms.data_fields:
                            if field["label"] == description_label:
                                field["value"] = desc_tr.get(field["value"], field["value"])
            except Exception as exc:
                logger.warning("description_translation.failed lang=%s error=%s", effective_lang, exc)

    await _emit(_section_site_map(
        site_map_url=site_map_url,
        shelter_name=shelter_name,
        shelter_type=shelter_type,
        shelter_distance_m=shelter_distance_m,
        site_route_steps=site_route_steps,
        translations=ui_translations,
    ))

    # ── Phase 2: Site summary (AI streamed live) ─────────────────
    await on_progress("Generating situation summary (AI writing live)…")
    await _tool("generate_site_summary", f"language={request.language} stats={stats.total_buildings} buildings")
    site_summary = ""
    try:
        site_summary = await asyncio.wait_for(
            _generate_site_summary_narrative(
                site_name=site_name,
                language=request.language,
                stats=stats,
                shelter_name=shelter_name,
                shelter_distance_m=shelter_distance_m,
                on_token=on_token,
                on_thinking=on_thinking,
            ),
            timeout=240.0,
        )
    except Exception as exc:
        logger.warning("site_summary_narrative.failed error=%s", exc)

    await _emit(_section_site_summary(site_summary=site_summary))
    await _emit(_section_buildings_heading(ui_translations))

    # ── Phase 3: Tier 1 — sequential with live thinking + tokens ─
    tier1 = ordered[:TIER1_BUILDINGS]
    tier2 = ordered[TIER1_BUILDINGS:]
    tier1_complete: list[BuildingMapSet] = []

    for priority_num, (bms, row) in enumerate(tier1, start=1):
        await _tool(
            "generate_building_narrative",
            f"priority={priority_num} id={bms.assessment_id} sev={bms.severity} lang={request.language}",
        )
        narrative = ""
        try:
            narrative = await asyncio.wait_for(
                _generate_building_narrative(
                    language=request.language,
                    damage_description=str(row.get("damage_description") or ""),
                    reasoning=str(row.get("reasoning") or ""),
                    severity=bms.severity,
                    damage_type=str(row.get("damage_type") or ""),
                    structural_risk="",
                    warnings=bms.warnings,
                    on_token=on_token,
                    on_thinking=on_thinking,
                ),
                timeout=240.0,
            )
        except Exception as exc:
            logger.warning("tier1_narrative.failed priority=%d error=%s", priority_num, exc)

        final_bms = dc_replace(bms, narrative=narrative)
        tier1_complete.append(final_bms)
        await _emit(_section_building_card(final_bms, priority_num, translations=ui_translations))

    # ── Phase 4: Tier 2 — sequential narratives ───────────────────
    tier2_complete: list[BuildingMapSet] = []

    for i, (bms, row) in enumerate(tier2):
        priority_num = TIER1_BUILDINGS + i + 1
        narrative = ""
        try:
            narrative = await asyncio.wait_for(
                _generate_building_narrative(
                    language=request.language,
                    damage_description=str(row.get("damage_description") or ""),
                    reasoning=str(row.get("reasoning") or ""),
                    severity=bms.severity,
                    damage_type=str(row.get("damage_type") or ""),
                    structural_risk="",
                    warnings=bms.warnings,
                ),
                timeout=240.0,
            )
        except Exception as exc:
            logger.warning("tier2_narrative.failed idx=%d error=%s", i, exc)
        final_bms = dc_replace(bms, narrative=narrative)
        tier2_complete.append(final_bms)
        await _emit(_section_building_card(final_bms, priority_num, translations=ui_translations))

    await _emit(_section_report_footer(ui_translations))

    # ── Assemble full HTML for PDF (complete, ordered) ────────────
    await on_progress("Assembling full report for PDF…")
    all_building_maps = tier1_complete + tier2_complete
    html = _render_report_html(
        site_name=site_name,
        province=province,
        district=district,
        generated_at=generated_at,
        team_name=request.team_name,
        language=request.language,
        report_id=report_id,
        stats=stats,
        site_map_url=site_map_url,
        shelter_name=shelter_name,
        shelter_type=shelter_type,
        shelter_distance_m=shelter_distance_m,
        site_summary=site_summary,
        site_route_steps=site_route_steps,
        building_maps=all_building_maps,
        extra_rows=[],
        translations=ui_translations,
    )

    # ── Save sidecars ─────────────────────────────────────────────
    subtitle_parts = [p for p in (province, district) if p]
    await _save_report_meta(
        report_id,
        report_type="SITE REPORT",
        title=f"Site Report — {site_name}",
        subtitle=" · ".join(subtitle_parts),
        team_name=request.team_name,
        language=request.language,
        generated_at=generated_at,
        stats=stats,
    )
    await on_progress("Generating severity chart…")
    await _save_report_chart(report_id, stats)

    _log_report_event(
        "generate_site_report_v2.completed",
        {"report_id": report_id, "html_chars": len(html), "buildings_processed": len(all_building_maps)},
    )
    return html

