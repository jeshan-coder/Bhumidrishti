"""Extract building chips from a GeoTIFF orthophoto and draw building outlines."""

import logging
import os
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from PIL import Image, ImageDraw
from rasterio.crs import CRS
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_bounds as warp_transform_bounds
from rasterio.windows import from_bounds

logger = logging.getLogger(__name__)

_WGS84 = CRS.from_epsg(4326)


def _is_wgs84(crs: CRS | None) -> bool:
    """Return True if the CRS is WGS84 geographic (EPSG:4326)."""
    if crs is None:
        return True  # assume geographic if unknown
    try:
        return crs.to_epsg() == 4326
    except Exception:
        return False

# Expand bounding box by this fraction on each side before chipping.
CHIP_EXPANSION_FACTOR = 0.25

# Minimum physical context padding on EACH side of the building bbox, in metres.
# Ensures that even tiny buildings get a meaningful surrounding area in the chip.
# At 0.3 m/px, 80 m = ~262 native pixels before upscale → acceptable quality.
CHIP_MIN_CONTEXT_M = 80.0

# Output chip resolution (pixels).
CHIP_SIZE = 1024

# JPEG quality for saved chips.
JPEG_QUALITY = 85


class ChipExtractionError(Exception):
    """Raised when chip extraction fails unrecoverably for a building."""


class BuildingOutsideOrthoError(ChipExtractionError):
    """Raised when the building bbox has no overlap with the orthophoto."""


def _expand_bbox(west: float, south: float, east: float, north: float) -> tuple[float, float, float, float]:
    """
    Expand a bounding box around the building so the chip has enough visual context.

    Two rules, whichever gives the larger padding wins per axis:
      1. Percentage rule  — CHIP_EXPANSION_FACTOR × building dimension
      2. Minimum absolute — CHIP_MIN_CONTEXT_M metres on each side

    Coordinates are WGS84 (degrees).  The metre→degree conversion uses a
    centre-latitude approximation (111 000 m ≈ 1° lat; cos(lat) for lon).
    """
    width = east - west
    height = north - south

    # Percentage-based padding.
    pad_x_pct = width * CHIP_EXPANSION_FACTOR
    pad_y_pct = height * CHIP_EXPANSION_FACTOR

    # Absolute minimum padding converted to degrees.
    center_lat = (south + north) / 2.0
    m_per_deg_lat = 111_000.0
    m_per_deg_lon = 111_000.0 * abs(float(np.cos(np.radians(center_lat))))
    pad_x_min = CHIP_MIN_CONTEXT_M / m_per_deg_lon
    pad_y_min = CHIP_MIN_CONTEXT_M / m_per_deg_lat

    pad_x = max(pad_x_pct, pad_x_min)
    pad_y = max(pad_y_pct, pad_y_min)

    return (
        west - pad_x,
        south - pad_y,
        east + pad_x,
        north + pad_y,
    )


def _read_chip(
    dataset: rasterio.DatasetReader,
    bbox: tuple[float, float, float, float],
) -> np.ndarray:
    """Read RGB window from a rasterio dataset and return HxWx3 uint8 array."""
    west, south, east, north = bbox
    window = from_bounds(west, south, east, north, transform=dataset.transform)

    # Rasterio may have up to 4 bands (RGBA) or just 1-3.
    band_count = min(dataset.count, 3)
    bands = dataset.read(
        list(range(1, band_count + 1)),
        window=window,
        out_shape=(band_count, CHIP_SIZE, CHIP_SIZE),
        resampling=rasterio.enums.Resampling.lanczos,
        boundless=True,
        fill_value=0,
    )

    if band_count == 1:
        # Grayscale — replicate to RGB
        rgb = np.stack([bands[0], bands[0], bands[0]], axis=0)
    elif band_count == 2:
        rgb = np.stack([bands[0], bands[1], bands[0]], axis=0)
    else:
        rgb = bands[:3]

    return np.transpose(rgb, (1, 2, 0)).astype(np.uint8)


def _check_dark_chip(chip_array: np.ndarray) -> tuple[bool, list[str]]:
    """Return (is_dark, warnings) for a chip array."""
    mean_value = float(chip_array.mean())
    zero_fraction = float((chip_array == 0).mean())
    warnings: list[str] = []
    is_dark = mean_value < 10 or zero_fraction > 0.30
    if is_dark:
        warnings.append("image_not_visible")
        logger.warning(
            "chip_dark_detected mean=%.2f zero_fraction=%.2f",
            mean_value,
            zero_fraction,
        )
    return is_dark, warnings


def _polygon_geojson_to_pixel_coords(
    polygon_geojson: dict[str, Any],
    expanded_bbox: tuple[float, float, float, float],
    ds_crs: "CRS | None" = None,
) -> list[list[tuple[int, int]]]:
    """
    Convert GeoJSON polygon rings from WGS84 lon/lat to CHIP_SIZE pixel space.

    The expanded bbox is in dataset CRS coordinates (may be UTM or WGS84).
    expanded_bbox maps to the 1024×1024 output chip so we do a linear
    transform: x_coord → x pixel, y_coord → y pixel (north/top is top).

    If ds_crs is provided and is not WGS84, polygon coordinates (always WGS84
    lon/lat) are reprojected to the dataset CRS before pixel mapping.
    """
    west, south, east, north = expanded_bbox
    x_range = east - west
    y_range = north - south

    rings: list[list[tuple[int, int]]] = []

    geometry = polygon_geojson.get("geometry") or polygon_geojson
    geo_type = geometry.get("type", "")
    coordinates = geometry.get("coordinates", [])

    if geo_type == "Polygon":
        ring_list = coordinates
    elif geo_type == "MultiPolygon":
        ring_list = [ring for poly in coordinates for ring in poly]
    else:
        ring_list = []

    need_reproject = ds_crs is not None and not _is_wgs84(ds_crs)

    for ring in ring_list:
        if not ring:
            continue
        lons = [pt[0] for pt in ring]
        lats = [pt[1] for pt in ring]

        if need_reproject:
            try:
                xs, ys = warp_transform(_WGS84, ds_crs, lons, lats)
            except Exception:
                xs, ys = lons, lats
        else:
            xs, ys = lons, lats

        pixel_ring: list[tuple[int, int]] = []
        for x, y in zip(xs, ys):
            px = int((x - west) / x_range * CHIP_SIZE)
            py = int((north - y) / y_range * CHIP_SIZE)
            px = max(0, min(CHIP_SIZE - 1, px))
            py = max(0, min(CHIP_SIZE - 1, py))
            pixel_ring.append((px, py))
        if pixel_ring:
            rings.append(pixel_ring)

    return rings


def _draw_building_outline(
    chip_array: np.ndarray,
    polygon_geojson: dict[str, Any],
    expanded_bbox: tuple[float, float, float, float],
    osm_id: int | str,
    ds_crs: "CRS | None" = None,
) -> Image.Image:
    """Draw green building outline on a chip array, return PIL Image."""
    img = Image.fromarray(chip_array).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    rings = _polygon_geojson_to_pixel_coords(polygon_geojson, expanded_bbox, ds_crs=ds_crs)

    for ring in rings:
        if len(ring) < 3:
            continue
        # Outline only — no fill, so the building interior stays visible.
        draw.line(ring + [ring[0]], fill=(0, 255, 0, 255), width=4)

    img = Image.alpha_composite(img, overlay).convert("RGB")
    draw_rgb = ImageDraw.Draw(img)

    if rings:
        all_x = [pt[0] for ring in rings for pt in ring]
        all_y = [pt[1] for ring in rings for pt in ring]
        if all_x and all_y:
            # ── Crosshair at building centroid ────────────────────────────────
            cx = int(sum(all_x) / len(all_x))
            cy = int(sum(all_y) / len(all_y))
            arm = 14  # crosshair arm length in pixels
            draw_rgb.line([(cx - arm, cy), (cx + arm, cy)], fill=(255, 255, 0), width=3)
            draw_rgb.line([(cx, cy - arm), (cx, cy + arm)], fill=(255, 255, 0), width=3)

    return img


def _bbox_overlap_percent(
    bbox_a: tuple[float, float, float, float],
    bbox_b: tuple[float, float, float, float],
) -> float:
    """Compute overlap percentage (IoU × 100) between two WGS84 bounding boxes."""
    # This variable stores the intersected west coordinate.
    inter_west = max(bbox_a[0], bbox_b[0])
    # This variable stores the intersected south coordinate.
    inter_south = max(bbox_a[1], bbox_b[1])
    # This variable stores the intersected east coordinate.
    inter_east = min(bbox_a[2], bbox_b[2])
    # This variable stores the intersected north coordinate.
    inter_north = min(bbox_a[3], bbox_b[3])

    if inter_east <= inter_west or inter_north <= inter_south:
        return 0.0

    # This variable stores the intersection area in degree-space.
    inter_area = (inter_east - inter_west) * (inter_north - inter_south)
    # This variable stores bbox A area in degree-space.
    area_a = max(0.0, (bbox_a[2] - bbox_a[0]) * (bbox_a[3] - bbox_a[1]))
    # This variable stores bbox B area in degree-space.
    area_b = max(0.0, (bbox_b[2] - bbox_b[0]) * (bbox_b[3] - bbox_b[1]))
    # This variable stores the union area in degree-space.
    union_area = area_a + area_b - inter_area

    if union_area <= 0:
        return 0.0
    return float((inter_area / union_area) * 100.0)


def extract_building_chips(
    post_cog_path: str,
    pre_cog_path: str | None,
    building_geojson: dict[str, Any],
    osm_id: int | str,
    batch_id: str,
    output_dir: Path,
) -> dict[str, Any]:
    """
    Extract pre/post chips for one building and return metadata.

    Returns a dict with keys:
      post_chip_path, pre_chip_path, expanded_bbox,
      warnings, is_dark, pre_available,
      area_m2, width_m, height_m
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    geometry = building_geojson.get("geometry") or building_geojson
    coordinates = geometry.get("coordinates", [])
    if not coordinates:
        raise ChipExtractionError(f"Building {osm_id} has empty geometry coordinates")

    # Build bounding box from polygon coordinates — handle Polygon and MultiPolygon.
    geo_type = geometry.get("type", "")
    if geo_type == "Polygon":
        # coordinates = [ring, ring, ...]  where ring = [[lon, lat], ...]
        all_coords = [pt for ring in coordinates for pt in ring]
    elif geo_type == "MultiPolygon":
        # coordinates = [polygon, ...]  where polygon = [ring, ...]
        all_coords = [pt for polygon in coordinates for ring in polygon for pt in ring]
    else:
        all_coords = []
    if not all_coords:
        raise ChipExtractionError(f"Building {osm_id} has no coordinate data (type={geo_type})")

    lons = [c[0] for c in all_coords]
    lats = [c[1] for c in all_coords]
    bbox_west = min(lons)
    bbox_east = max(lons)
    bbox_south = min(lats)
    bbox_north = max(lats)

    exp_west, exp_south, exp_east, exp_north = _expand_bbox(bbox_west, bbox_south, bbox_east, bbox_north)

    # Approximate building dimensions.
    width_deg = bbox_east - bbox_west
    height_deg = bbox_north - bbox_south
    center_lat = (bbox_south + bbox_north) / 2.0
    lat_m_per_deg = 111000.0
    lon_m_per_deg = 111000.0 * abs(float(np.cos(np.radians(center_lat))))
    width_m = width_deg * lon_m_per_deg
    height_m = height_deg * lat_m_per_deg

    # Rough area estimate (rectangular approximation; PostGIS gives accurate value).
    area_m2 = width_m * height_m

    warnings: list[str] = []

    # This variable stores post-chip clipped bbox converted to WGS84 for overlap checks.
    post_clipped_wgs84: tuple[float, float, float, float] | None = None
    # This variable stores pre-chip clipped bbox converted to WGS84 for overlap checks.
    pre_clipped_wgs84: tuple[float, float, float, float] | None = None

    # ── Post-earthquake chip ──────────────────────────────────────────────────
    with rasterio.open(post_cog_path) as post_ds:
        post_crs = post_ds.crs
        bounds = post_ds.bounds

        # Reproject expanded WGS84 bbox to dataset CRS if needed.
        if not _is_wgs84(post_crs):
            try:
                exp_west_ds, exp_south_ds, exp_east_ds, exp_north_ds = warp_transform_bounds(
                    _WGS84, post_crs, exp_west, exp_south, exp_east, exp_north
                )
            except Exception as exc:
                raise ChipExtractionError(
                    f"Building {osm_id} CRS transform failed: {exc}"
                ) from exc
        else:
            exp_west_ds, exp_south_ds, exp_east_ds, exp_north_ds = exp_west, exp_south, exp_east, exp_north

        # Check overlap in dataset CRS.
        if (
            exp_east_ds < bounds.left
            or exp_west_ds > bounds.right
            or exp_north_ds < bounds.bottom
            or exp_south_ds > bounds.top
        ):
            raise BuildingOutsideOrthoError(
                f"Building {osm_id} bbox has no overlap with post orthophoto"
            )

        # Clip expanded bbox to dataset bounds (in dataset CRS).
        clipped_bbox = (
            max(exp_west_ds, bounds.left),
            max(exp_south_ds, bounds.bottom),
            min(exp_east_ds, bounds.right),
            min(exp_north_ds, bounds.top),
        )

        post_array = _read_chip(post_ds, clipped_bbox)

        if _is_wgs84(post_crs):
            post_clipped_wgs84 = clipped_bbox
        else:
            post_clipped_wgs84 = warp_transform_bounds(
                post_crs,
                _WGS84,
                clipped_bbox[0],
                clipped_bbox[1],
                clipped_bbox[2],
                clipped_bbox[3],
            )

    is_dark, dark_warnings = _check_dark_chip(post_array)
    warnings.extend(dark_warnings)

    # Save raw post chip.
    post_raw_path = output_dir / f"{osm_id}_post_raw.jpg"
    Image.fromarray(post_array).convert("RGB").save(str(post_raw_path), "JPEG", quality=JPEG_QUALITY)

    # Draw outline on post chip — pass ds_crs so polygon coords are reprojected.
    post_overlay_img = _draw_building_outline(post_array, building_geojson, clipped_bbox, osm_id, ds_crs=post_crs)
    post_overlay_path = output_dir / f"{osm_id}_post_overlay.jpg"
    post_overlay_img.save(str(post_overlay_path), "JPEG", quality=JPEG_QUALITY)

    # ── Pre-earthquake chip (optional) ───────────────────────────────────────
    pre_overlay_path: Path | None = None
    pre_available = False

    if pre_cog_path:
        try:
            with rasterio.open(pre_cog_path) as pre_ds:
                pre_crs = pre_ds.crs
                pre_bounds = pre_ds.bounds

                # Reproject expanded WGS84 bbox to pre dataset CRS if needed.
                if not _is_wgs84(pre_crs):
                    try:
                        pre_w_ds, pre_s_ds, pre_e_ds, pre_n_ds = warp_transform_bounds(
                            _WGS84, pre_crs, exp_west, exp_south, exp_east, exp_north
                        )
                    except Exception:
                        pre_w_ds, pre_s_ds, pre_e_ds, pre_n_ds = exp_west, exp_south, exp_east, exp_north
                else:
                    pre_w_ds, pre_s_ds, pre_e_ds, pre_n_ds = exp_west, exp_south, exp_east, exp_north

                has_overlap = not (
                    pre_e_ds < pre_bounds.left
                    or pre_w_ds > pre_bounds.right
                    or pre_n_ds < pre_bounds.bottom
                    or pre_s_ds > pre_bounds.top
                )
                if has_overlap:
                    pre_clipped = (
                        max(pre_w_ds, pre_bounds.left),
                        max(pre_s_ds, pre_bounds.bottom),
                        min(pre_e_ds, pre_bounds.right),
                        min(pre_n_ds, pre_bounds.top),
                    )
                    pre_array = _read_chip(pre_ds, pre_clipped)

                    if _is_wgs84(pre_crs):
                        pre_clipped_wgs84 = pre_clipped
                    else:
                        pre_clipped_wgs84 = warp_transform_bounds(
                            pre_crs,
                            _WGS84,
                            pre_clipped[0],
                            pre_clipped[1],
                            pre_clipped[2],
                            pre_clipped[3],
                        )

                    pre_raw_path = output_dir / f"{osm_id}_pre_raw.jpg"
                    Image.fromarray(pre_array).convert("RGB").save(
                        str(pre_raw_path), "JPEG", quality=JPEG_QUALITY
                    )

                    pre_overlay_img = _draw_building_outline(
                        pre_array, building_geojson, pre_clipped, osm_id, ds_crs=pre_crs
                    )
                    pre_overlay_path = output_dir / f"{osm_id}_pre_overlay.jpg"
                    pre_overlay_img.save(str(pre_overlay_path), "JPEG", quality=JPEG_QUALITY)
                    pre_available = True
                else:
                    warnings.append("no_pre_image")
                    logger.info("chip_pre_no_overlap osm_id=%s", osm_id)
        except Exception as exc:
            warnings.append("no_pre_image")
            logger.warning("chip_pre_failed osm_id=%s error=%s", osm_id, exc)
    else:
        warnings.append("no_pre_image")

    # Build relative paths for DB storage (relative to /app/data, i.e. UPLOAD_ROOT parent).
    _app_data = Path(os.getenv("UPLOAD_DIR", "/app/data/uploads")).resolve().parent
    try:
        rel_post = str(post_overlay_path.relative_to(_app_data))
    except ValueError:
        rel_post = str(post_overlay_path)
    try:
        rel_pre = str(pre_overlay_path.relative_to(_app_data)) if pre_overlay_path else None
    except ValueError:
        rel_pre = str(pre_overlay_path) if pre_overlay_path else None

    # This variable stores pre/post chip overlap percentage in WGS84 bbox space.
    pre_post_overlap_pct = 0.0
    if post_clipped_wgs84 and pre_clipped_wgs84:
        pre_post_overlap_pct = _bbox_overlap_percent(post_clipped_wgs84, pre_clipped_wgs84)

    return {
        "post_chip_path": rel_post,
        "pre_chip_path": rel_pre,
        "post_chip_abs": str(post_overlay_path),
        "pre_chip_abs": str(pre_overlay_path) if pre_overlay_path else None,
        "expanded_bbox": clipped_bbox,
        "post_bbox_wgs84": post_clipped_wgs84,
        "pre_bbox_wgs84": pre_clipped_wgs84,
        "pre_post_overlap_pct": pre_post_overlap_pct,
        "warnings": warnings,
        "is_dark": is_dark,
        "pre_available": pre_available,
        "area_m2": area_m2,
        "width_m": width_m,
        "height_m": height_m,
    }
