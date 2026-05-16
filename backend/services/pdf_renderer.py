"""WeasyPrint + Matplotlib PDF renderer for BhumiDrishti field reports.

Responsibilities
----------------
* `generate_severity_chart_b64`  — horizontal bar chart as base-64 PNG
* `generate_building_gauge_b64`  — semicircle severity gauge as base-64 PNG
* `render_report_pdf`            — builds a polished PDF from HTML/markdown
                                   using WeasyPrint; falls back to fpdf2 if
                                   WeasyPrint is not available in the container.

Sidecar convention (written by reporting.py before calling render_report_pdf)
------------------------------------------------------------------------------
  REPORTS_DIR/{report_id}_meta.json   — title, subtitle, team, stats, …
  REPORTS_DIR/{report_id}_chart.png   — pre-rendered severity bar chart
  REPORTS_DIR/{report_id}_gauge.png   — pre-rendered building gauge
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import textwrap
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/app/data/uploads")).resolve()
REPORTS_DIR = UPLOAD_DIR / "reports"
BACKEND_PUBLIC_URL = os.getenv("BACKEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")


# ---------------------------------------------------------------------------
# CSS for WeasyPrint
# ---------------------------------------------------------------------------

_WEASYPRINT_CSS = """
@page {
    size: A4;
    margin: 14mm 14mm 20mm 14mm;
    @bottom-left {
        content: "BhumiDrishti — Field Operations — Confidential";
        font-family: Helvetica, sans-serif;
        font-size: 7pt;
        color: #9ca3af;
    }
    @bottom-right {
        content: "Page " counter(page) " / " counter(pages);
        font-family: Helvetica, sans-serif;
        font-size: 7pt;
        color: #9ca3af;
    }
}
@page :first {
    margin-top: 0;
    @bottom-left  { content: ""; }
    @bottom-right { content: ""; }
}

* { box-sizing: border-box; }

body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 9.5pt;
    color: #1f2937;
    line-height: 1.55;
}

/* ── Cover band ──────────────────────────────────────────────────────────── */

.bd-cover {
    background-color: #0c4a2f;
    color: white;
    padding: 18px 14mm 15px;
    margin: 0 -14mm 16px -14mm;
}
.bd-cover-logo {
    font-size: 7.5pt;
    font-weight: 700;
    letter-spacing: 2.5px;
    text-transform: uppercase;
    opacity: 0.65;
    margin-bottom: 7px;
}
.bd-cover-logo-badge {
    display: inline-block;
    background: #f59e0b;
    color: #1a1a1a;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 6.5pt;
    vertical-align: middle;
    margin-left: 7px;
    letter-spacing: 0.5px;
    font-weight: 700;
}
.bd-cover-title {
    font-size: 21pt;
    font-weight: 700;
    line-height: 1.15;
    margin-bottom: 6px;
}
.bd-cover-subtitle {
    font-size: 10.5pt;
    opacity: 0.72;
    margin-bottom: 10px;
}
.bd-cover-meta-row {
    display: flex;
    gap: 0;
    flex-wrap: wrap;
    font-size: 8pt;
    opacity: 0.85;
    border-top: 1px solid rgba(255,255,255,0.18);
    padding-top: 8px;
}
.bd-cover-meta-item { margin-right: 20px; }
.bd-cover-meta-label {
    display: block;
    font-size: 6pt;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    opacity: 0.6;
    margin-bottom: 1px;
}
.bd-cover-meta-value { font-weight: 600; }

/* ── Stat cards ──────────────────────────────────────────────────────────── */

.bd-stats {
    display: flex;
    gap: 7px;
    margin: 0 0 14px;
    flex-wrap: wrap;
}
.bd-stat {
    flex: 1;
    min-width: 62px;
    border-radius: 7px;
    padding: 9px 8px 7px;
    text-align: center;
    color: white;
}
.bd-stat-num {
    font-size: 15pt;
    font-weight: 700;
    line-height: 1.1;
}
.bd-stat-label {
    font-size: 6pt;
    margin-top: 3px;
    opacity: 0.85;
    text-transform: uppercase;
    letter-spacing: 0.3px;
}
.bd-stat-gray  { background-color: #374151; }
.bd-stat-dark  { background-color: #7f1d1d; }
.bd-stat-red   { background-color: #dc2626; }
.bd-stat-amber { background-color: #d97706; }
.bd-stat-pink  { background-color: #9f1239; }
.bd-stat-blue  { background-color: #1e40af; }
.bd-stat-green { background-color: #166534; }
.bd-stat-teal  { background-color: #0f6e56; }

/* ── Visuals block (two-col: chart + map) ────────────────────────────────── */

.bd-two-col {
    display: flex;
    gap: 12px;
    margin-bottom: 14px;
    align-items: flex-start;
}
.bd-two-col > div { flex: 1; }

.bd-chart, .bd-map, .bd-gauge { page-break-inside: avoid; }
.bd-chart img, .bd-map img, .bd-gauge img {
    max-width: 100%;
    height: auto;
    display: block;
}
.bd-map img {
    border: 1px solid #d1fae5;
    border-radius: 5px;
}
.bd-section-caption {
    font-size: 6.5pt;
    color: #9ca3af;
    text-align: center;
    margin-top: 4px;
}
.bd-section-rule {
    border: none;
    border-top: 1px solid #e5e7eb;
    margin: 12px 0;
}

/* ── Report body ─────────────────────────────────────────────────────────── */

.bd-content h1 {
    font-size: 13pt; font-weight: 700; color: #0c4a2f;
    margin: 0 0 9px; padding-bottom: 5px;
    border-bottom: 2px solid #d1fae5;
    page-break-after: avoid;
}
.bd-content h2 {
    font-size: 10.5pt; font-weight: 700; color: #064e35;
    margin: 12px 0 5px; padding-bottom: 2px;
    border-bottom: 1px solid #e5e7eb;
    page-break-after: avoid;
}
.bd-content h3 {
    font-size: 9.5pt; font-weight: 600; color: #1f2937;
    margin: 9px 0 4px;
    page-break-after: avoid;
}
.bd-content h4 {
    font-size: 9pt; font-weight: 600; color: #374151;
    margin: 7px 0 3px;
}
.bd-content p  { margin: 0 0 6px; }
.bd-content ul, .bd-content ol { margin: 3px 0 7px 15px; }
.bd-content li { margin-bottom: 2px; }
.bd-content strong { font-weight: 600; color: #111827; }
.bd-content em { font-style: italic; }
.bd-content a  { color: #0f6e56; }
.bd-content hr { border: none; border-top: 1px solid #e5e7eb; margin: 10px 0; }

.bd-content table {
    width: 100%; border-collapse: collapse;
    margin: 6px 0; font-size: 8pt;
    page-break-inside: avoid;
}
.bd-content th {
    background: #f0fdf4; font-weight: 600; text-align: left;
    padding: 5px 8px; border: 1px solid #d1fae5; color: #064e35;
}
.bd-content td {
    padding: 4px 8px; border: 1px solid #e5e7eb; vertical-align: top;
}
.bd-content tr:nth-child(even) td { background: #fafafa; }

.bd-content img {
    max-width: 100%; height: auto;
    border: 1px solid #e5e7eb; border-radius: 4px;
    page-break-inside: avoid; display: block; margin: 4px 0;
}
.bd-content code {
    font-size: 8pt; background: #f3f4f6;
    padding: 1px 4px; border-radius: 3px;
}
.bd-content pre {
    background: #f8f9fa; border: 1px solid #e5e7eb;
    border-radius: 4px; padding: 8px; font-size: 8pt;
    overflow-x: auto; margin: 6px 0;
}

/* Building cards */
.bd-content .building-card {
    border: 1px solid #e5e7eb; border-radius: 5px;
    padding: 9px 12px; margin-bottom: 9px;
    page-break-inside: avoid;
}
.bd-content .sev-border-5 { border-left: 4px solid #7f1d1d; }
.bd-content .sev-border-4 { border-left: 4px solid #dc2626; }
.bd-content .sev-border-3 { border-left: 4px solid #d97706; }
.bd-content .sev-border-2 { border-left: 4px solid #ca8a04; }
.bd-content .sev-border-1 { border-left: 4px solid #16a34a; }
.bd-content .sev {
    display: inline-block; padding: 1px 5px; border-radius: 3px;
    font-weight: 600; font-size: 7.5pt;
}
.bd-content .sev-5 { background: #7f1d1d; color: white; }
.bd-content .sev-4 { background: #dc2626; color: white; }
.bd-content .sev-3 { background: #d97706; color: white; }
.bd-content .sev-2 { background: #ca8a04; color: white; }
.bd-content .sev-1 { background: #16a34a; color: white; }

/* Stats badges inside AI content */
.bd-content .stats-row { display: flex; flex-wrap: wrap; gap: 6px; margin: 5px 0 9px; }
.bd-content .stat-badge {
    border-radius: 6px; padding: 7px 10px;
    text-align: center; min-width: 62px;
}
.bd-content .stat-num { font-size: 12pt; font-weight: 700; line-height: 1.1; }
.bd-content .stat-label { font-size: 6pt; margin-top: 2px; opacity: 0.85; }
.bd-content .stat-total   { background: #f3f4f6; color: #1f2937; }
.bd-content .stat-extreme { background: #7f1d1d; color: white; }
.bd-content .stat-critical{ background: #dc2626; color: white; }
.bd-content .stat-moderate{ background: #d97706; color: white; }
.bd-content .stat-life    { background: #9f1239; color: white; }
.bd-content .stat-flood   { background: #1d4ed8; color: white; }

/* Tags, banners, notes */
.bd-content .warn-tag {
    background: #fef3c7; color: #92400e;
    border: 1px solid #fcd34d; padding: 1px 5px;
    border-radius: 3px; font-size: 7.5pt;
    display: inline-block; margin-right: 3px; margin-bottom: 2px;
}
.bd-content .life-banner {
    background: #fef2f2; border: 1px solid #fecaca;
    color: #991b1b; padding: 5px 9px;
    border-radius: 4px; font-weight: 600; margin: 5px 0;
}
.bd-content .action-block {
    background: #fffbeb; border: 1px solid #fde68a;
    border-radius: 5px; padding: 7px 10px; margin: 5px 0;
}
.bd-content .gemma-note {
    background: #f0fdf4; border-left: 3px solid #16a34a;
    padding: 6px 9px; margin: 5px 0;
    border-radius: 0 4px 4px 0; font-style: italic; font-size: 8pt;
}

/* Data rows */
.bd-content .data-row { display: flex; border-bottom: 1px solid #f3f4f6; font-size: 8.5pt; }
.bd-content .data-row.alt { background: #f9fafb; }
.bd-content .data-key { width: 38%; padding: 3px 6px; color: #6b7280; font-weight: 500; }
.bd-content .data-val { width: 62%; padding: 3px 6px; }

/* Route steps */
.bd-content .route-step {
    display: flex; gap: 6px; padding: 3px 0;
    align-items: baseline; font-size: 8.5pt;
}
.bd-content .step-num {
    background: #0c4a2f; color: white; border-radius: 50%;
    width: 15px; height: 15px; display: flex;
    align-items: center; justify-content: center;
    font-size: 5.5pt; font-weight: 700; flex-shrink: 0;
}

/* Hide elements that duplicate info from the cover */
.bd-content .report-footer,
.bd-content .report-header { display: none; }

/* Placeholders (when images didn't inject) */
.bd-content .map-placeholder,
.bd-content .img-placeholder {
    background: #f9fafb; border: 1px dashed #e5e7eb;
    border-radius: 4px; padding: 8px; text-align: center;
    color: #d1d5db; font-size: 7.5pt; margin: 4px 0;
}

/* Page break helpers */
.bd-content .page-break { page-break-after: always; }
.bd-page-break { page-break-after: always; height: 0; }

/* Pre-post image rows */
.bd-content .pre-post-row { display: flex; gap: 8px; }
.bd-content .pre-post-row > div { flex: 1; }
.bd-content .img-label { font-size: 7pt; color: #6b7280; font-weight: 500; margin-bottom: 3px; }

/* ── V2 report layout ────────────────────────────────────────────── */

.bd-v2-header {
    text-align: center; padding: 10px 0 14px;
    border-bottom: 2px solid #d1fae5; margin-bottom: 14px;
}
.bd-v2-logo {
    font-size: 7pt; font-weight: 700; letter-spacing: 2px;
    text-transform: uppercase; color: #6b7280; margin-bottom: 6px;
}
.bd-logo-badge {
    display: inline-block; background: #f59e0b; color: #1a1a1a;
    padding: 1px 5px; border-radius: 3px; font-size: 6pt;
    vertical-align: middle; margin-left: 6px; font-weight: 700;
}
.bd-v2-title { font-size: 20pt; font-weight: 700; color: #0c4a2f; margin: 0 0 4px; }
.bd-v2-subtitle { font-size: 9pt; color: #6b7280; margin-bottom: 6px; }
.bd-v2-meta-row {
    display: flex; gap: 16px; justify-content: center; flex-wrap: wrap;
    font-size: 7.5pt; color: #9ca3af;
}

.bd-shelter-block {
    background: #f0fdf4; border: 1px solid #bbf7d0;
    border-radius: 6px; padding: 7px 12px; margin: 10px 0;
}
.bd-shelter-name { font-weight: 600; color: #166534; font-size: 9pt; }
.bd-shelter-meta { font-size: 8pt; color: #4b7c5b; }

.bd-narrative-block {
    background: #f0fdf4; border-left: 3px solid #16a34a;
    padding: 8px 12px; margin: 10px 0;
    border-radius: 0 5px 5px 0; font-size: 9pt; line-height: 1.55;
}

.bd-map-section { margin: 10px 0; page-break-inside: avoid; }
.bd-map-section img {
    max-width: 100%; height: auto; display: block;
    border-radius: 5px; border: 1px solid #e5e7eb;
}
.bd-map-site { width: 100%; }
.bd-map-building { max-height: 320px; width: auto; }
.bd-map-route { width: 100%; }
.bd-map-caption { font-size: 6.5pt; color: #9ca3af; text-align: center; margin-top: 3px; }

.bd-building-header {
    display: flex; align-items: baseline; gap: 8px;
    flex-wrap: wrap; margin-bottom: 7px;
}
.bd-assessment-id { font-size: 10.5pt; font-weight: 700; color: #0c4a2f; }
.bd-building-coords { font-size: 7pt; color: #9ca3af; margin-left: auto; }

.bd-action-tag {
    display: inline-block; padding: 2px 7px; border-radius: 3px;
    font-size: 6.5pt; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px;
}
.bd-action-urgent { background: #7f1d1d; color: white; }
.bd-action-high   { background: #dc2626; color: white; }
.bd-action-medium { background: #d97706; color: white; }
.bd-action-low    { background: #ca8a04; color: white; }
.bd-action-none   { background: #6b7280; color: white; }

.bd-data-table { width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 8.5pt; }
.bd-data-key {
    width: 34%; padding: 3px 7px; color: #6b7280; font-weight: 500;
    background: #f9fafb; border-bottom: 1px solid #f3f4f6; vertical-align: top;
}
.bd-data-val { width: 66%; padding: 3px 7px; border-bottom: 1px solid #f3f4f6; }
.bd-data-alt .bd-data-key, .bd-data-alt .bd-data-val { background: #fafafa; }
.bd-data-critical { font-weight: 700; color: #991b1b; }
.bd-data-blocked  { font-weight: 700; color: #b45309; }

.bd-media-section { margin: 10px 0; }
.bd-subsection-heading {
    font-size: 8.5pt; font-weight: 600; color: #374151; margin: 7px 0 5px;
}
.bd-media-row { display: flex; gap: 7px; flex-wrap: wrap; }
.bd-media-item { flex: 1; min-width: 100px; max-width: 200px; }
.bd-media-item img {
    max-width: 100%; height: auto; border: 1px solid #e5e7eb;
    border-radius: 4px; display: block;
}
.bd-media-label { font-size: 6.5pt; color: #6b7280; margin-bottom: 3px; }

.bd-route-section { margin: 10px 0; page-break-inside: avoid; }
.bd-route-meta { font-size: 7.5pt; color: #6b7280; margin: 4px 0 5px; }
.bd-route-directions { margin-top: 5px; }
.bd-route-step { display: flex; gap: 8px; padding: 3px 0; align-items: baseline; font-size: 8pt; }
.bd-step-num {
    background: #0c4a2f; color: white; border-radius: 50%;
    width: 15px; height: 15px; display: inline-flex; min-width: 15px;
    align-items: center; justify-content: center;
    font-size: 5pt; font-weight: 700; flex-shrink: 0; text-align: center;
}
.bd-step-text { flex: 1; }

.bd-warnings-row { margin-top: 7px; }
.bd-warn-tag {
    background: #fef3c7; color: #92400e; border: 1px solid #fcd34d;
    padding: 1px 6px; border-radius: 3px; font-size: 7.5pt;
    display: inline-block; margin-right: 4px; margin-bottom: 3px;
}

.bd-part-heading {
    font-size: 13pt; font-weight: 700; color: #0c4a2f;
    margin: 16px 0 8px; padding-bottom: 5px;
    border-bottom: 2px solid #d1fae5; page-break-after: avoid;
}
.bd-section-heading {
    font-size: 10pt; font-weight: 700; color: #064e35;
    margin: 12px 0 5px; padding-bottom: 2px;
    border-bottom: 1px solid #e5e7eb; page-break-after: avoid;
}
"""


# ---------------------------------------------------------------------------
# Matplotlib chart generators
# ---------------------------------------------------------------------------

def generate_severity_chart_b64(stats_dict: dict[str, Any]) -> str | None:
    """Generate a horizontal severity distribution bar chart.

    Parameters
    ----------
    stats_dict : keys total_buildings, sev5, sev4, sev3 (integers)

    Returns
    -------
    Base-64 encoded PNG string, or None on any error.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        total = int(stats_dict.get("total_buildings", 0) or 0)
        sev5  = int(stats_dict.get("sev5",  0) or 0)
        sev4  = int(stats_dict.get("sev4",  0) or 0)
        sev3  = int(stats_dict.get("sev3",  0) or 0)
        sev_other = max(0, total - sev5 - sev4 - sev3)

        categories = ["Extreme (5)", "Critical (4)", "Moderate (3)", "Minor / Safe"]
        values     = [sev5, sev4, sev3, sev_other]
        colors     = ["#7f1d1d", "#dc2626", "#d97706", "#16a34a"]

        fig, ax = plt.subplots(figsize=(7.2, 2.1), facecolor="white")
        ax.set_facecolor("white")

        bars = ax.barh(
            categories, values, color=colors,
            height=0.52, edgecolor="white", linewidth=0.4,
        )
        max_val = max(values + [1])
        for bar, val in zip(bars, values):
            if val > 0:
                ax.text(
                    bar.get_width() + max_val * 0.012,
                    bar.get_y() + bar.get_height() / 2,
                    str(int(val)),
                    va="center", ha="left",
                    fontsize=9, fontweight="bold", color="#374151",
                )

        ax.set_xlim(0, max_val * 1.22)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        ax.spines["left"].set_color("#f3f4f6")
        ax.spines["bottom"].set_color("#f3f4f6")
        ax.tick_params(axis="y", pad=5,  labelsize=9,   colors="#374151")
        ax.tick_params(axis="x", pad=3,  labelsize=7.5, colors="#9ca3af")
        ax.xaxis.set_tick_params(size=0)
        ax.yaxis.set_tick_params(size=0)
        ax.set_xlabel("Buildings", fontsize=7.5, color="#9ca3af", labelpad=3)

        plt.tight_layout(pad=0.4)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=140, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")
    except Exception as exc:
        logger.warning("pdf_renderer.chart.failed error=%s", exc)
        return None


def generate_building_gauge_b64(severity: int, damage_type: str | None = None) -> str | None:
    """Generate a semicircle severity gauge for a single building.

    Returns base-64 encoded PNG or None on error.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        sev = max(1, min(5, int(severity or 1)))
        sev_colors = ["#16a34a", "#ca8a04", "#d97706", "#dc2626", "#7f1d1d"]
        sev_labels = ["Low", "Minor", "Moderate", "Critical", "Extreme"]
        active_color = sev_colors[sev - 1]
        active_label = sev_labels[sev - 1]

        fig, ax = plt.subplots(figsize=(4.0, 2.2), facecolor="white")
        ax.set_facecolor("white")
        ax.set_aspect("equal")
        ax.axis("off")

        angles = np.linspace(np.pi, 0, 6)
        for i, (c, a0, a1) in enumerate(zip(sev_colors, angles[:-1], angles[1:])):
            theta = np.linspace(a0, a1, 60)
            alpha = 1.0 if (i + 1) == sev else 0.15
            x_o, y_o = np.cos(theta), np.sin(theta)
            x_i, y_i = 0.54 * np.cos(theta), 0.54 * np.sin(theta)
            ax.fill(
                np.concatenate([x_o, x_i[::-1]]),
                np.concatenate([y_o, y_i[::-1]]),
                color=c, alpha=alpha, linewidth=0,
            )

        # Needle
        needle_angle = np.pi - (sev - 1) / 4.0 * np.pi
        ax.annotate(
            "",
            xy=(0.70 * np.cos(needle_angle), 0.70 * np.sin(needle_angle)),
            xytext=(0.0, 0.0),
            arrowprops=dict(arrowstyle="->", color="#111827", lw=2.2),
        )
        ax.plot(0, 0, "o", color="#111827", markersize=5.5, zorder=5)

        ax.text(0, -0.11, f"SEV {sev}", ha="center", va="center",
                fontsize=11, fontweight="bold", color=active_color)
        ax.text(0, -0.37, active_label.upper(), ha="center", va="center",
                fontsize=7.5, color=active_color, alpha=0.82)
        if damage_type:
            ax.text(0, -0.60, str(damage_type), ha="center", va="center",
                    fontsize=7, color="#6b7280")

        ax.set_xlim(-1.15, 1.15)
        ax.set_ylim(-0.82, 1.05)
        plt.tight_layout(pad=0)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=140, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")
    except Exception as exc:
        logger.warning("pdf_renderer.gauge.failed error=%s", exc)
        return None


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _markdown_to_html_fallback(text: str) -> str:
    """Minimal regex markdown→HTML when the `markdown` package is absent."""
    html = re.sub(r"^#### (.+)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    html = re.sub(r"^### (.+)$",  r"<h3>\1</h3>",  html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$",   r"<h2>\1</h2>",   html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$",    r"<h1>\1</h1>",    html, flags=re.MULTILINE)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*",     r"<em>\1</em>",         html)
    html = re.sub(r"`(.+?)`",       r"<code>\1</code>",     html)
    html = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img src="\2" alt="\1">', html)
    html = re.sub(r"^\s*[-*] (.+)$", r"<li>\1</li>", html, flags=re.MULTILINE)
    html = re.sub(
        r"(<li>.*?</li>\n?)+",
        lambda m: f"<ul>{m.group()}</ul>",
        html, flags=re.DOTALL,
    )
    paragraphs = []
    for block in html.split("\n\n"):
        b = block.strip()
        if b and not b.startswith("<"):
            paragraphs.append(f"<p>{b}</p>")
        elif b:
            paragraphs.append(b)
    return "\n".join(paragraphs)


def _content_to_html(content: str) -> str:
    """Convert HTML-or-markdown content to HTML."""
    if content.lstrip()[:1] == "<":
        return content          # already HTML
    try:
        import markdown as md_lib
        return md_lib.markdown(
            content,
            extensions=["tables", "nl2br", "sane_lists", "fenced_code"],
        )
    except ImportError:
        return _markdown_to_html_fallback(content)


def _rewrite_media_urls_for_weasyprint(html: str) -> str:
    """Convert absolute HTTP media URLs to ``file://`` so WeasyPrint can load them.

    The FastAPI mount is: /media/uploads → UPLOAD_DIR  (e.g. /app/data/uploads)
    So  http://host/media/uploads/reports/x.png  → UPLOAD_DIR/reports/x.png
    """
    backend = BACKEND_PUBLIC_URL.rstrip("/")
    # The mount prefix used in main.py: app.mount("/media/uploads", StaticFiles(directory=UPLOAD_DIR))
    MEDIA_UPLOADS_PREFIX = "/media/uploads/"

    def _replace(m: re.Match) -> str:
        url = m.group(1)
        # Strip backend host prefix to get the URL path
        if url.startswith(backend):
            path_part = url[len(backend):]
        else:
            idx = url.find("/media/uploads/")
            if idx == -1:
                return m.group(0)
            path_part = url[idx:]

        # path_part is now like /media/uploads/reports/xxx.png
        if path_part.startswith(MEDIA_UPLOADS_PREFIX):
            # Strip the /media/uploads/ mount prefix — what remains is relative to UPLOAD_DIR
            relative = path_part[len(MEDIA_UPLOADS_PREFIX):]
        elif path_part.startswith("/media/"):
            # Fallback: strip only /media/ and keep whatever follows
            relative = path_part[len("/media/"):]
        elif path_part.startswith("/uploads/"):
            relative = path_part[1:]
        else:
            return m.group(0)

        local = UPLOAD_DIR / relative.lstrip("/")
        if local.exists():
            return f'src="file://{local.as_posix()}"'
        return m.group(0)

    return re.sub(r'src="(https?://[^"]+)"', _replace, html)


def _build_cover_html(meta: dict[str, Any]) -> str:
    """Return the styled green cover band HTML from report metadata."""
    title       = meta.get("title", "Field Report")
    subtitle    = meta.get("subtitle") or ""
    team        = meta.get("team_name") or "Unassigned"
    language    = (meta.get("language") or "EN").upper()
    generated   = meta.get("generated_at", "")
    rtype       = (meta.get("report_type") or "REPORT").upper()

    subtitle_html = (
        f'<div class="bd-cover-subtitle">{subtitle}</div>' if subtitle else ""
    )

    # ---- stat cards (site reports only) ----------------------------------
    stats = meta.get("stats") or {}
    stats_html = ""
    if stats and int(stats.get("total", 0) or 0) > 0:
        cards = [
            ("bd-stat-gray",  stats.get("total", 0),         "Buildings"),
            ("bd-stat-dark",  stats.get("sev5",  0),         "Extreme"),
            ("bd-stat-red",   stats.get("sev4",  0),         "Critical"),
            ("bd-stat-amber", stats.get("sev3",  0),         "Moderate"),
            ("bd-stat-pink",  stats.get("signs_of_life", 0), "Signs of Life"),
            ("bd-stat-blue",  stats.get("estimated_people", 0), "Affected"),
        ]
        inner = "".join(
            f'<div class="bd-stat {cls}">'
            f'<div class="bd-stat-num">{val}</div>'
            f'<div class="bd-stat-label">{lbl}</div>'
            f'</div>'
            for cls, val, lbl in cards
        )
        stats_html = f'<div class="bd-stats">{inner}</div>'

    return (
        f'<div class="bd-cover">'
        f'<div class="bd-cover-logo">BhumiDrishti'
        f' <span class="bd-cover-logo-badge">{rtype}</span></div>'
        f'<div class="bd-cover-title">{title}</div>'
        f'{subtitle_html}'
        f'<div class="bd-cover-meta-row">'
        f'<div class="bd-cover-meta-item">'
        f'<span class="bd-cover-meta-label">Team</span>'
        f'<span class="bd-cover-meta-value">{team}</span></div>'
        f'<div class="bd-cover-meta-item">'
        f'<span class="bd-cover-meta-label">Language</span>'
        f'<span class="bd-cover-meta-value">{language}</span></div>'
        f'<div class="bd-cover-meta-item">'
        f'<span class="bd-cover-meta-label">Generated</span>'
        f'<span class="bd-cover-meta-value">{generated}</span></div>'
        f'</div>'
        f'</div>'
        f'{stats_html}'
    )


def _weasyprint_html_shell(body: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"UTF-8\">\n"
        f"<title>BhumiDrishti Report</title>\n"
        f"<style>{_WEASYPRINT_CSS}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>"
    )


# ---------------------------------------------------------------------------
# fpdf2 fallback (minimal, text-only)
# ---------------------------------------------------------------------------

def _render_pdf_fpdf2(report_id: str, content: str) -> str:
    """Simple fpdf2 fallback when WeasyPrint is unavailable."""
    from fpdf import FPDF  # type: ignore[import]

    output_path = REPORTS_DIR / f"{report_id}.pdf"
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    content_w = pdf.w - pdf.l_margin - pdf.r_margin

    is_html = content.lstrip()[:1] == "<"
    if is_html:
        # Strip HTML tags to plain text
        plain = re.sub(r"<[^>]+>", " ", content)
        plain = re.sub(r"\s{2,}", " ", plain)
        lines = [plain]
    else:
        lines = content.splitlines()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            pdf.ln(2)
            continue
        wrapped = textwrap.wrap(line, width=110) or [line]
        for wl in wrapped:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(content_w, 6, wl)

    pdf.output(str(output_path))
    return f"uploads/reports/{report_id}.pdf"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def render_report_pdf(report_id: str, content: str) -> str:
    """Build a polished PDF for *report_id* from *content* (HTML or markdown).

    Reads optional sidecar files produced by ``reporting.py``:
    - ``{report_id}_meta.json``   → cover, stat cards
    - ``{report_id}_chart.png``   → severity bar chart
    - ``{report_id}_gauge.png``   → building gauge
    - ``{report_id}_map_site.png``→ static site map

    Falls back to fpdf2 if WeasyPrint is not installed.
    """
    output_path = REPORTS_DIR / f"{report_id}.pdf"

    # ── Load sidecars ──────────────────────────────────────────────────────
    meta: dict[str, Any] = {}
    meta_path = REPORTS_DIR / f"{report_id}_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _load_img_b64(path: Path) -> str | None:
        if path.exists():
            try:
                return base64.b64encode(path.read_bytes()).decode("utf-8")
            except Exception:
                pass
        return None

    chart_b64 = _load_img_b64(REPORTS_DIR / f"{report_id}_chart.png")
    gauge_b64 = _load_img_b64(REPORTS_DIR / f"{report_id}_gauge.png")
    map_b64   = _load_img_b64(REPORTS_DIR / f"{report_id}_map_site.png")

    # ── Build document ─────────────────────────────────────────────────────
    cover_html = _build_cover_html(meta) if meta else ""

    # Two-column visuals: chart / gauge on left, map on right
    visual_html = ""
    left_img  = chart_b64 or gauge_b64
    left_cap  = "Severity Distribution" if chart_b64 else "Building Severity"
    right_img = map_b64

    if left_img and right_img:
        visual_html = (
            '<div class="bd-two-col">'
            f'<div class="bd-chart">'
            f'<img src="data:image/png;base64,{left_img}" alt="{left_cap}"/>'
            f'<p class="bd-section-caption">{left_cap}</p>'
            f'</div>'
            f'<div class="bd-map">'
            f'<img src="data:image/png;base64,{right_img}" alt="Site map"/>'
            f'<p class="bd-section-caption">Site Overview Map</p>'
            f'</div>'
            '</div>'
        )
    elif left_img:
        visual_html = (
            f'<div class="bd-chart">'
            f'<img src="data:image/png;base64,{left_img}" alt="{left_cap}"/>'
            f'<p class="bd-section-caption">{left_cap}</p>'
            f'</div>'
        )
    elif right_img:
        visual_html = (
            f'<div class="bd-map">'
            f'<img src="data:image/png;base64,{right_img}" alt="Site map"/>'
            f'<p class="bd-section-caption">Site Overview Map</p>'
            f'</div>'
        )

    body_html = _content_to_html(content)

    # Embed V2 per-building PNG sidecars (bmap + rmap) as base64 data URIs so
    # WeasyPrint never needs to make HTTP requests for them.
    def _embed_sidecar_pngs(html: str, rid: str) -> str:
        """Replace http:// URLs for known sidecar PNGs with data: URIs."""
        def _sub(m: re.Match) -> str:
            url = m.group(1)
            # Match only sidecar filenames produced by generate_site_report_v2
            for prefix in (f"{rid}_bmap_", f"{rid}_rmap_", f"{rid}_map_"):
                if prefix in url:
                    # Extract just the filename
                    fname = url.rsplit("/", 1)[-1]
                    fpath = REPORTS_DIR / fname
                    if fpath.exists():
                        b64 = base64.b64encode(fpath.read_bytes()).decode()
                        return f'src="data:image/png;base64,{b64}"'
            return m.group(0)
        return re.sub(r'src="(https?://[^"]+\.png)"', _sub, html)

    body_html = _embed_sidecar_pngs(body_html, report_id)
    body_html = _rewrite_media_urls_for_weasyprint(body_html)

    full_body = (
        cover_html
        + visual_html
        + ('<hr class="bd-section-rule">' if cover_html or visual_html else "")
        + f'<div class="bd-content">{body_html}</div>'
    )

    full_html = _weasyprint_html_shell(full_body)

    # ── WeasyPrint ─────────────────────────────────────────────────────────
    try:
        from weasyprint import HTML as WeasyHTML  # type: ignore[import]
        WeasyHTML(string=full_html, base_url=str(UPLOAD_DIR)).write_pdf(str(output_path))
        logger.info("pdf_renderer.weasyprint.ok report_id=%s size=%d", report_id, output_path.stat().st_size)
        return f"uploads/reports/{report_id}.pdf"
    except ImportError:
        logger.warning("pdf_renderer.weasyprint_missing — falling back to fpdf2")
    except Exception as exc:
        logger.error("pdf_renderer.weasyprint.error report_id=%s error=%s", report_id, exc)

    # ── fpdf2 fallback ─────────────────────────────────────────────────────
    return _render_pdf_fpdf2(report_id, content)
