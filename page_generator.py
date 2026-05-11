#!/usr/bin/env python3
"""
Waypoint Visualization Module — ATC display style.

Generates a standalone HTML file with a Leaflet + Proj4Leaflet map styled to
mirror an en-route ATC situation display:
  - Dark navy background, no land/ocean differentiation
  - Muted country borders, brighter sector outlines
  - Triangle fix symbols (filled = IFR, open = VFR, amber = conflict)
  - Permanent monospace labels next to each fix
  - Morocco Lambert Conformal Conic CRS
  - Runtime cropping of border features outside waypoint extent + padding

Pure stdlib — no external Python dependencies.

Usage:
    python visualize_waypoints.py waypoints.geojson [output.html] [borders.geojson] [sectors.geojson]
"""

import json
import sys
from pathlib import Path

# ─── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_BORDERS_PATH = "borders.geojson"
DEFAULT_SECTORS_PATH = "sectors.geojson"

# Map view padding around waypoint extent (degrees)
VIEW_PADDING_DEG = 1.5
# Border features beyond this distance from waypoint extent are dropped
BORDERS_CROP_PADDING_DEG = 8.0


# ─── GeoJSON loading & geometry helpers ────────────────────────────────────

def load_geojson_optional(path):
    """Load a GeoJSON file if it exists, trying CWD then script dir. Returns None if missing."""
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        p = Path(__file__).parent / path
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def load_geojson_required(path):
    """Load a GeoJSON file or raise FileNotFoundError."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"GeoJSON file not found: {path}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def waypoint_bbox(features, fallback=(-18, 20, 0, 37)):
    """Compute (min_lon, min_lat, max_lon, max_lat) of Point features."""
    lats, lons = [], []
    for feat in features:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        c = geom.get("coordinates") or []
        if len(c) < 2:
            continue
        lons.append(c[0])
        lats.append(c[1])
    if not lats:
        return fallback
    return (min(lons), min(lats), max(lons), max(lats))


def _ring_bbox(ring):
    """Bbox of a single linear ring (list of [x, y] pairs). Returns None if empty."""
    if not ring:
        return None
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def bboxes_intersect(a, b):
    """True if two (minx, miny, maxx, maxy) bboxes intersect."""
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _clip_polygon(polygon_rings, keep_box):
    """Keep only rings whose bbox intersects keep_box. Each ring is treated as
    an independent shape (single-ring polygon). This is robust to datasets like
    Natural Earth that encode multipart countries as a single Polygon with many
    'rings' — one per island — rather than as a proper MultiPolygon. For
    border-line rendering the visual result is the same."""
    return [ring for ring in polygon_rings
            if (bb := _ring_bbox(ring)) is not None
            and bboxes_intersect(bb, keep_box)]


def _clip_geometry(geom, keep_box):
    """Filter sub-parts of a Polygon/MultiPolygon by bbox against keep_box.
    Returns a new geometry dict or None if nothing survives."""
    if not geom:
        return None
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []

    if gtype == "Polygon":
        kept_rings = _clip_polygon(coords, keep_box)
        if not kept_rings:
            return None
        # Emit each surviving ring as its own polygon part. Safe for line
        # rendering (which is all we use borders for) and avoids guessing
        # which rings are holes vs. independent islands.
        if len(kept_rings) == 1:
            return {"type": "Polygon", "coordinates": [kept_rings[0]]}
        return {"type": "MultiPolygon",
                "coordinates": [[r] for r in kept_rings]}

    if gtype == "MultiPolygon":
        kept_polys = []
        for poly in coords:
            kept_rings = _clip_polygon(poly, keep_box)
            if kept_rings:
                kept_polys.extend([r] for r in kept_rings)
        if not kept_polys:
            return None
        if len(kept_polys) == 1:
            return {"type": "Polygon", "coordinates": kept_polys[0]}
        return {"type": "MultiPolygon", "coordinates": kept_polys}

    return None


def crop_geojson_to_bbox(geo, bbox, padding=0.0):
    """Return a new FeatureCollection containing only geometry parts whose bbox
    intersects bbox±padding. Operates at the ring/polygon-part level so
    countries with scattered overseas territories shed their distant parts
    instead of being kept wholesale."""
    if not geo:
        return None
    keep_box = (bbox[0] - padding, bbox[1] - padding,
                bbox[2] + padding, bbox[3] + padding)
    kept = []
    for feat in geo.get("features", []):
        clipped = _clip_geometry(feat.get("geometry"), keep_box)
        if clipped is None:
            continue
        kept.append({
            "type": "Feature",
            "properties": feat.get("properties") or {},
            "geometry": clipped,
        })
    return {"type": "FeatureCollection", "features": kept}


def compute_stats(features):
    """Return dict of counts, treating conflict as a status (orthogonal to flight type)."""
    ifr = vfr = conflict = 0
    for f in features:
        props = f.get("properties") or {}
        ft = props.get("flight_type")
        if ft == "IFR":
            ifr += 1
        elif ft == "VFR":
            vfr += 1
        if props.get("has_conflict"):
            conflict += 1
    return {"total": len(features), "ifr": ifr, "vfr": vfr, "conflict": conflict}


# ─── HTML template ─────────────────────────────────────────────────────────
# A static HTML template with a single `__PAYLOAD__` placeholder. Using a
# placeholder rather than an f-string means CSS/JS braces don't need to be
# doubled — a far less error-prone approach for templates of this size.

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Morocco AIP ENR 4.4 — Significant Points</title>

    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">

    <style>
        :root {
            --bg: #060d18;
            --bg-soft: #0a1727;
            --border: #2a4a5e;
            --sector: #3eb5d4;
            --sector-soft: rgba(62, 181, 212, 0.08);
            --fix-ifr: #5fd0e8;
            --fix-vfr: #7dd16f;
            --fix-conflict: #f4b942;
            --conflict-glow: rgba(244, 185, 66, 0.55);
            --label: #c8d8e8;
            --label-dim: #6a829a;
            --panel-bg: rgba(10, 23, 39, 0.88);
            --panel-border: #1f3a52;
            --text: #d8e4f0;
            --text-dim: #7a92aa;
            --accent: #5fd0e8;
            --rbl: #d4a72c;
            --rbl-bright: #ffd24a;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg);
            color: var(--text);
            overflow: hidden;
        }

        #map {
            width: 100vw;
            height: 100vh;
            background: var(--bg);
            outline: none;
        }

        /* Smooth Leaflet zoom rendering */
        .leaflet-fade-anim .leaflet-tile,
        .leaflet-zoom-anim .leaflet-zoom-animated {
            will-change: transform;
        }

        .leaflet-container {
            background: var(--bg);
            font-family: 'Inter', sans-serif;
        }

        /* ─── Fix markers ─────────────────────────────────────────── */
        .atc-marker-wrapper { background: none !important; border: none !important; }

        .atc-fix {
            position: relative;
            width: 14px;
            height: 14px;
            transition: transform 0.18s ease-out;
            cursor: pointer;
        }
        .atc-fix:hover { transform: scale(1.5); z-index: 1000; }

        .atc-fix svg { display: block; overflow: visible; }

        .atc-fix.ifr path {
            fill: var(--fix-ifr);
            stroke: rgba(255, 255, 255, 0.25);
            stroke-width: 0.5;
            filter: drop-shadow(0 0 3px rgba(95, 208, 232, 0.35));
        }

        .atc-fix.vfr path {
            fill: none;
            stroke: var(--fix-vfr);
            stroke-width: 1.4;
            filter: drop-shadow(0 0 3px rgba(125, 209, 111, 0.4));
        }

        .atc-fix.conflict path {
            fill: var(--fix-conflict);
            stroke: #ff5a5a;
            stroke-width: 1;
            filter: drop-shadow(0 0 5px var(--conflict-glow));
            animation: conflict-pulse 1.8s ease-in-out infinite;
        }

        @keyframes conflict-pulse {
            0%, 100% { filter: drop-shadow(0 0 4px var(--conflict-glow)); }
            50%      { filter: drop-shadow(0 0 9px var(--conflict-glow)); }
        }

        .atc-label {
            position: absolute;
            left: 11px;
            top: -3px;
            font-family: 'JetBrains Mono', 'Roboto Mono', Menlo, monospace;
            font-size: 10.5px;
            color: var(--label);
            white-space: nowrap;
            pointer-events: auto;
            cursor: pointer;
            text-shadow: 0 0 3px rgba(0, 0, 0, 0.95),
                         0 0 2px rgba(0, 0, 0, 0.95),
                         0 0 1px rgba(0, 0, 0, 0.95);
            letter-spacing: 0.4px;
            opacity: 0.88;
            transition: opacity 0.2s, color 0.2s;
        }
        .atc-label:hover { opacity: 1; color: var(--accent); }
        .atc-label.vfr { color: var(--fix-vfr); opacity: 0.78; }
        .atc-label.vfr:hover { opacity: 1; color: var(--fix-vfr); }
        .atc-fix:hover + .atc-label,
        .atc-fix:hover .atc-label { opacity: 1; }
        .labels-hidden .atc-label { display: none; }

        /* ─── RBL mode ────────────────────────────────────────────── */
        .atc-fix.rbl-selected path,
        .atc-fix.rbl-hover path {
            fill: var(--rbl) !important;
            stroke: var(--rbl) !important;
            stroke-width: 1 !important;
            filter: drop-shadow(0 0 4px rgba(212, 167, 44, 0.55)) !important;
            animation: none !important;
        }
        .atc-label.rbl-selected,
        .atc-label.rbl-hover {
            color: var(--rbl) !important;
            opacity: 1 !important;
        }

        .rbl-label {
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            color: var(--rbl-bright);
            text-align: center;
            line-height: 1.25;
            letter-spacing: 0.6px;
            white-space: nowrap;
            padding: 2px 6px;
            background: rgba(10, 23, 39, 0.78);
            border: 1px solid rgba(255, 210, 74, 0.35);
            border-radius: 2px;
            cursor: pointer;
            pointer-events: auto;
            text-shadow: 0 0 2px rgba(0, 0, 0, 0.9);
            transition: background 0.15s, border-color 0.15s;
        }
        .rbl-label:hover {
            background: rgba(10, 23, 39, 0.95);
            border-color: var(--rbl-bright);
        }

        .rbl-clear-popup .leaflet-popup-content { margin: 0; min-width: 0; }
        .rbl-clear-popup .leaflet-popup-content-wrapper { padding: 0; }
        .rbl-clear-btn {
            display: block;
            padding: 6px 14px;
            background: transparent;
            border: none;
            color: var(--text);
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            letter-spacing: 1.2px;
            text-transform: uppercase;
            cursor: pointer;
            transition: color 0.15s, background 0.15s;
        }
        .rbl-clear-btn:hover { color: var(--rbl-bright); background: rgba(255, 210, 74, 0.08); }

        /* Sector labels */
        .sector-label {
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            font-weight: 500;
            color: var(--sector);
            text-shadow: 0 0 4px rgba(0,0,0,0.95), 0 0 2px rgba(0,0,0,0.95);
            letter-spacing: 1.5px;
            text-transform: uppercase;
            opacity: 0.72;
            background: none !important;
            border: none !important;
            pointer-events: none;
        }

        /* ─── Panels ─────────────────────────────────────────────── */
        .panel {
            position: fixed;
            z-index: 1000;
            background: var(--panel-bg);
            border: 1px solid var(--panel-border);
            border-radius: 3px;
            padding: 11px 14px;
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            font-size: 12px;
            color: var(--text);
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
            animation: fade-in 0.4s ease-out;
        }
        @keyframes fade-in {
            from { opacity: 0; transform: translateY(-4px); }
            to   { opacity: 1; transform: translateY(0); }
        }

        .panel h4 {
            font-size: 9.5px;
            text-transform: uppercase;
            letter-spacing: 1.4px;
            color: var(--text-dim);
            font-weight: 600;
            margin-bottom: 8px;
        }

        .title-box {
            top: 12px;
            left: 12px;
            min-width: 240px;
        }
        .title-box .title {
            font-size: 12px;
            font-weight: 600;
            color: var(--accent);
            letter-spacing: 1.2px;
            text-transform: uppercase;
        }
        .title-box .subtitle {
            font-size: 10.5px;
            color: var(--text-dim);
            margin-top: 3px;
            letter-spacing: 0.4px;
        }
        .title-box .stats {
            margin-top: 9px;
            padding-top: 9px;
            border-top: 1px solid var(--panel-border);
            display: flex;
            gap: 14px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 10.5px;
        }
        .stats .stat-block { display: flex; flex-direction: column; }
        .stats .stat-label {
            color: var(--text-dim);
            font-size: 8.5px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .stats .stat-value { color: var(--text); font-weight: 500; }
        .stats .stat-value.ifr      { color: var(--fix-ifr); }
        .stats .stat-value.vfr      { color: var(--fix-vfr); }
        .stats .stat-value.conflict { color: var(--fix-conflict); }

        /* ─── Control box (mode + display + speed) ────────────────── */
        .control-box {
            top: 120px;
            left: 12px;
            min-width: 240px;
        }
        .control-box h4 { margin-bottom: 6px; }
        .control-box h4.spaced { margin-top: 12px; }

        .seg-toggle {
            display: flex;
            background: rgba(10, 23, 39, 0.55);
            border: 1px solid var(--panel-border);
            border-radius: 3px;
            padding: 2px;
            margin: 4px 0;
        }
        .seg-option {
            flex: 1;
            padding: 5px 8px;
            background: transparent;
            border: none;
            color: var(--text-dim);
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            letter-spacing: 0.8px;
            text-transform: uppercase;
            cursor: pointer;
            border-radius: 2px;
            transition: color 0.15s, background 0.15s;
        }
        .seg-option:hover { color: var(--text); }
        .seg-option.active {
            background: rgba(95, 208, 232, 0.18);
            color: var(--accent);
        }

        .speed-input {
            width: 100%;
            background: rgba(10, 23, 39, 0.55);
            border: 1px solid var(--panel-border);
            border-radius: 3px;
            padding: 6px 10px;
            color: var(--text);
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
            letter-spacing: 0.8px;
            margin: 4px 0;
            transition: border-color 0.15s, box-shadow 0.15s;
        }
        .speed-input:focus {
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 2px rgba(95, 208, 232, 0.15);
        }
        .speed-input::-webkit-outer-spin-button,
        .speed-input::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
        .speed-input { -moz-appearance: textfield; }

        .filter-box { top: 12px; right: 12px; min-width: 200px; }
        .filter-item {
            display: flex;
            align-items: center;
            margin: 5px 0;
            font-size: 12px;
            cursor: pointer;
            user-select: none;
            transition: color 0.15s;
        }
        .filter-item:hover { color: var(--accent); }
        .filter-item input { margin-right: 9px; accent-color: var(--accent); }
        .filter-item .count {
            margin-left: auto;
            font-family: 'JetBrains Mono', monospace;
            font-size: 10.5px;
            color: var(--text-dim);
        }

        .search-box { top: 12px; left: 50%; transform: translateX(-50%); padding: 0; }
        .search-box input {
            background: var(--panel-bg);
            border: 1px solid var(--panel-border);
            border-radius: 3px;
            padding: 9px 14px;
            color: var(--text);
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
            letter-spacing: 1.2px;
            text-transform: uppercase;
            width: 220px;
            transition: border-color 0.15s, box-shadow 0.15s;
        }
        .search-box input:focus {
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 2px rgba(95, 208, 232, 0.15);
        }
        .search-box input::placeholder {
            color: var(--text-dim);
            letter-spacing: 1px;
            text-transform: none;
        }

        .legend { bottom: 14px; left: 12px; }
        .legend-item {
            display: flex;
            align-items: center;
            margin: 5px 0;
            font-size: 11.5px;
            color: var(--text);
        }
        .legend-marker { width: 16px; height: 14px; margin-right: 9px; display: flex; align-items: center; justify-content: center; }
        .legend-marker svg { overflow: visible; }

        .coords-box {
            bottom: 14px;
            right: 12px;
            padding: 7px 11px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 10.5px;
            color: var(--text-dim);
            letter-spacing: 0.6px;
            min-width: 180px;
            text-align: right;
        }
        .coords-box .coord-value { color: var(--text); }

        /* ─── Popups ─────────────────────────────────────────────── */
        .leaflet-popup-content-wrapper {
            background: var(--panel-bg);
            color: var(--text);
            border: 1px solid var(--panel-border);
            border-radius: 3px;
            backdrop-filter: blur(8px);
            box-shadow: 0 4px 20px rgba(0,0,0,0.5);
        }
        .leaflet-popup-tip { background: var(--panel-bg); border: 1px solid var(--panel-border); }
        .leaflet-popup-content { margin: 12px 14px; min-width: 220px; }
        .leaflet-popup-close-button { color: var(--text-dim) !important; }

        .waypoint-popup h3 {
            font-family: 'JetBrains Mono', monospace;
            font-size: 14px;
            font-weight: 500;
            color: var(--accent);
            letter-spacing: 1.5px;
            margin-bottom: 4px;
        }
        .waypoint-popup .badge {
            display: inline-block;
            padding: 1px 7px;
            border-radius: 2px;
            font-size: 9.5px;
            font-weight: 500;
            letter-spacing: 1.2px;
            margin-bottom: 9px;
            text-transform: uppercase;
        }
        .badge-ifr { background: rgba(95, 208, 232, 0.18); color: var(--fix-ifr); border: 1px solid var(--fix-ifr); }
        .badge-vfr { background: rgba(125, 209, 111, 0.18); color: var(--fix-vfr); border: 1px solid var(--fix-vfr); }

        .waypoint-popup table { width: 100%; font-size: 11px; border-collapse: collapse; }
        .waypoint-popup td { padding: 3px 0; vertical-align: top; font-family: 'JetBrains Mono', monospace; }
        .waypoint-popup td:first-child {
            color: var(--text-dim);
            width: 75px;
            text-transform: uppercase;
            font-size: 9.5px;
            letter-spacing: 0.8px;
            padding-top: 5px;
        }

        .conflict-warning {
            margin-top: 10px;
            padding: 7px 9px;
            background: rgba(244, 185, 66, 0.1);
            border: 1px solid var(--fix-conflict);
            border-radius: 2px;
            font-size: 10.5px;
            color: var(--fix-conflict);
            font-family: 'JetBrains Mono', monospace;
        }

        /* Zoom controls */
        .leaflet-control-zoom a {
            background: var(--panel-bg) !important;
            color: var(--text) !important;
            border: 1px solid var(--panel-border) !important;
            backdrop-filter: blur(8px);
        }
        .leaflet-control-zoom a:hover { color: var(--accent) !important; }

        /* Attribution */
        .leaflet-control-attribution {
            background: rgba(10, 23, 39, 0.6) !important;
            color: var(--text-dim) !important;
            font-size: 9px !important;
        }
        .leaflet-control-attribution a { color: var(--text-dim) !important; }
    </style>
</head>
<body>
    <div id="map"></div>

    <div class="panel title-box">
        <div class="title" id="title-text">Loading…</div>
        <div class="subtitle" id="subtitle-text"></div>
        <div class="stats" id="stats"></div>
    </div>

    <div class="panel control-box">
        <h4>Mode</h4>
        <div class="seg-toggle" id="toggle-rbl-mode">
            <button class="seg-option active" type="button" data-value="view">View Mode</button>
            <button class="seg-option" type="button" data-value="rbl">RBL Mode</button>
        </div>
        <div class="seg-toggle" id="toggle-display">
            <button class="seg-option active" type="button" data-value="rbl">Show RBL</button>
            <button class="seg-option" type="button" data-value="time">Show Time Est</button>
        </div>
        <h4 class="spaced">Aircraft Speed</h4>
        <input type="number" class="speed-input" id="speed-input" value="0.78" step="0.01" min="0" />
        <div class="seg-toggle" id="toggle-speed-unit">
            <button class="seg-option active" type="button" data-value="M">M</button>
            <button class="seg-option" type="button" data-value="kts">kts</button>
        </div>
    </div>

    <div class="panel search-box">
        <input type="text" id="search" placeholder="Search waypoint…" autocomplete="off" />
    </div>

    <div class="panel filter-box">
        <h4>Filters</h4>
        <label class="filter-item">
            <input type="checkbox" id="filter-ifr" checked />
            <span>IFR Fixes</span>
            <span class="count" id="count-ifr">0</span>
        </label>
        <label class="filter-item">
            <input type="checkbox" id="filter-vfr" checked />
            <span>VFR Fixes</span>
            <span class="count" id="count-vfr">0</span>
        </label>
        <label class="filter-item">
            <input type="checkbox" id="filter-labels" checked />
            <span>Fix Name</span>
        </label>
    </div>

    <div class="panel legend">
        <h4>Legend</h4>
        <div class="legend-item">
            <span class="legend-marker"><svg width="14" height="14" viewBox="-7 -7 14 14"><path d="M 0,-5.5 L 4.8,3.5 L -4.8,3.5 Z" fill="#5fd0e8" stroke="rgba(255,255,255,0.25)" stroke-width="0.5"/></svg></span>
            IFR Reporting Point
        </div>
        <div class="legend-item">
            <span class="legend-marker"><svg width="14" height="14" viewBox="-7 -7 14 14"><path d="M 0,-5.5 L 4.8,3.5 L -4.8,3.5 Z" fill="none" stroke="#7dd16f" stroke-width="1.4"/></svg></span>
            VFR Reporting Point
        </div>
    </div>

    <div class="panel coords-box">
        <span class="coord-value" id="cursor-coords">— —</span>
    </div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/proj4js/2.9.0/proj4.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/proj4leaflet/1.0.2/proj4leaflet.min.js"></script>

    <script id="payload" type="application/json">__PAYLOAD__</script>

    <script>
    (function() {
        'use strict';

        // ─── Load payload ──────────────────────────────────────────────
        const PAYLOAD = JSON.parse(document.getElementById('payload').textContent);
        const waypoints = PAYLOAD.waypoints;
        const borders   = PAYLOAD.borders;
        const sectors   = PAYLOAD.sectors;
        const view      = PAYLOAD.view;
        const stats     = PAYLOAD.stats;
        const title     = PAYLOAD.title;
        const subtitle  = PAYLOAD.subtitle;

        // ─── Header + counts ───────────────────────────────────────────
        document.getElementById('title-text').textContent = title;
        document.getElementById('subtitle-text').textContent = subtitle;
        document.getElementById('count-ifr').textContent = stats.ifr.toString().padStart(3, '0');
        document.getElementById('count-vfr').textContent = stats.vfr.toString().padStart(3, '0');

        const statsHtml = [
            ['total',    stats.total, ''],
            ['ifr',      stats.ifr,   'ifr'],
            ['vfr',      stats.vfr,   'vfr'],
        ].concat(stats.conflict > 0 ? [['conflict', stats.conflict, 'conflict']] : [])
         .map(([label, val, cls]) =>
            `<div class="stat-block"><span class="stat-label">${label}</span>` +
            `<span class="stat-value ${cls}">${String(val).padStart(3, '0')}</span></div>`
         ).join('');
        document.getElementById('stats').innerHTML = statsHtml;

        // ─── Utilities ─────────────────────────────────────────────────
        function escapeHtml(s) {
            if (s == null) return '';
            return String(s).replace(/[&<>"']/g, c => ({
                '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
            }[c]));
        }

        function safeUpper(s) { return (s == null ? '' : String(s).toUpperCase()); }

        function polygonCentroid(coords) {
            // Coords = [ring], use the outer ring only. Simple area-weighted centroid.
            const ring = coords[0];
            let cx = 0, cy = 0, a = 0;
            for (let i = 0, n = ring.length - 1; i < n; i++) {
                const [x0, y0] = ring[i];
                const [x1, y1] = ring[i + 1];
                const f = x0 * y1 - x1 * y0;
                a += f;
                cx += (x0 + x1) * f;
                cy += (y0 + y1) * f;
            }
            a *= 0.5;
            if (Math.abs(a) < 1e-9) {
                // Fallback to vertex mean
                const n = ring.length - 1;
                return [ring.slice(0, n).reduce((s, p) => s + p[0], 0) / n,
                        ring.slice(0, n).reduce((s, p) => s + p[1], 0) / n];
            }
            return [cx / (6 * a), cy / (6 * a)];
        }

        // ─── Morocco Lambert Conformal Conic CRS ───────────────────────
        const moroccoLambert = new L.Proj.CRS(
            'EPSG:102191',
            '+proj=lcc +lat_1=29.05 +lat_2=34.5833 +lat_0=33.0693 +lon_0=-6.0663 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs',
            {
                resolutions: [8192, 4096, 2048, 1024, 512, 256, 128, 64, 32, 16, 8, 4, 2, 1, 0.5],
                origin: [0, 0],
                bounds: L.bounds([-2000000, -2000000], [2000000, 2000000])
            }
        );

        // ─── Map init ──────────────────────────────────────────────────
        const map = L.map('map', {
            crs: moroccoLambert,
            center: [(view.min_lat + view.max_lat) / 2, (view.min_lon + view.max_lon) / 2],
            zoom: 5,
            zoomControl: true,
            zoomSnap: 0.25,
            zoomDelta: 0.5,
            wheelPxPerZoomLevel: 100,
            zoomAnimation: true,
            fadeAnimation: true,
            inertia: true,
            inertiaDeceleration: 2500,
            preferCanvas: false,
            attributionControl: true
        });
        map.attributionControl.setPrefix('');

        // Fit to view bounds without animation (avoids initial flash)
        map.fitBounds(
            [[view.min_lat, view.min_lon], [view.max_lat, view.max_lon]],
            { animate: false }
        );

        // ─── Borders layer ─────────────────────────────────────────────
        if (borders) {
            L.geoJSON(borders, {
                style: {
                    color: '#2a4a5e',
                    weight: 0.8,
                    fill: false,
                    opacity: 0.85,
                    lineJoin: 'round'
                },
                interactive: false
            }).addTo(map);
        }

        // ─── Sectors layer (+ centroid labels) ─────────────────────────
        if (sectors) {
            L.geoJSON(sectors, {
                style: {
                    color: '#3eb5d4',
                    weight: 1.2,
                    fillColor: '#3eb5d4',
                    fillOpacity: 0.04,
                    opacity: 0.55,
                    dashArray: '3,4',
                    lineJoin: 'round'
                },
                interactive: false
            }).addTo(map);

            // Sector name labels
            sectors.features.forEach(feat => {
                const name = (feat.properties || {}).name;
                if (!name) return;
                const geom = feat.geometry || {};
                let coords = null;
                if (geom.type === 'Polygon') coords = geom.coordinates;
                else if (geom.type === 'MultiPolygon') coords = geom.coordinates[0];
                if (!coords) return;
                const [cx, cy] = polygonCentroid(coords);
                L.marker([cy, cx], {
                    icon: L.divIcon({
                        html: `<div class="sector-label">${escapeHtml(name)}</div>`,
                        className: 'atc-marker-wrapper',
                        iconSize: [120, 14],
                        iconAnchor: [60, 7]
                    }),
                    interactive: false,
                    keyboard: false
                }).addTo(map);
            });
        }

        // ─── Waypoint markers ──────────────────────────────────────────
        const TRIANGLE_PATH = 'M 0,-5.5 L 4.8,3.5 L -4.8,3.5 Z';
        const markers = [];

        function buildIcon(props) {
            const ft        = (props.flight_type || '').toUpperCase();
            const isVFR     = ft === 'VFR';
            const isConflict = !!props.has_conflict;
            const cls = isConflict ? 'conflict' : (isVFR ? 'vfr' : 'ifr');
            const labelCls = isVFR ? 'atc-label vfr' : 'atc-label';
            const name = escapeHtml(props.name || '????');
            const html =
                `<div class="atc-fix ${cls}">` +
                `<svg width="14" height="14" viewBox="-7 -7 14 14"><path d="${TRIANGLE_PATH}"/></svg>` +
                `</div>` +
                `<span class="${labelCls}">${name}</span>`;
            return L.divIcon({
                html: html,
                className: 'atc-marker-wrapper',
                iconSize: [14, 14],
                iconAnchor: [7, 7]
            });
        }

        function buildPopup(props, coords) {
            const ft = (props.flight_type || '').toUpperCase();
            const ftLower = ft.toLowerCase();
            const name = escapeHtml(props.name || '????');
            const rawCoords = escapeHtml(props.raw_coordinates || '');
            let html = `<div class="waypoint-popup"><h3>${name}</h3>`;
            if (ft) html += `<span class="badge badge-${ftLower}">${ft}</span>`;
            html += '<table>';
            if (rawCoords) {
                html += `<tr><td>Sexagesimal</td><td>${rawCoords}</td></tr>`;
            }
            html += `<tr><td>WGS84</td><td>${coords[1].toFixed(4)}°, ${coords[0].toFixed(4)}°</td></tr>`;
            if (ft === 'IFR' && props.routes) {
                html += `<tr><td>Remarks</td><td>${escapeHtml(props.routes)}</td></tr>`;
            }
            if (ft === 'IFR' && props.fra_relevance) {
                html += `<tr><td>FRA</td><td>${escapeHtml(props.fra_relevance)}</td></tr>`;
            }
            if (ft === 'VFR' && props.location) {
                html += `<tr><td>Location</td><td>${escapeHtml(props.location)}</td></tr>`;
            }
            html += '</table>';
            if (props.has_conflict) {
                const detail = escapeHtml(props.conflict_details || 'Extraction mismatch detected');
                html += `<div class="conflict-warning">⚠ ${detail}</div>`;
            }
            html += '</div>';
            return html;
        }

        const ifrLayer = L.layerGroup().addTo(map);
        const vfrLayer = L.layerGroup().addTo(map);

        waypoints.features.forEach(feat => {
            const geom = feat.geometry || {};
            if (geom.type !== 'Point') return;
            const coords = geom.coordinates;
            const props  = feat.properties || {};
            const ft     = (props.flight_type || '').toUpperCase();
            const isVFR  = ft === 'VFR';

            const marker = L.marker([coords[1], coords[0]], {
                icon: buildIcon(props),
                riseOnHover: true,
                keyboard: false
            });

            const record = {
                marker: marker,
                name: (props.name || '').toUpperCase(),
                category: isVFR ? 'vfr' : 'ifr',
                hasConflict: !!props.has_conflict,
                props: props,
                coords: coords
            };
            markers.push(record);

            marker.on('click', (e) => {
                if (document.body.classList.contains('rbl-active')) {
                    L.DomEvent.stopPropagation(e);
                    handleRblClick(record);
                } else {
                    L.popup({ closeButton: true, maxWidth: 320 })
                        .setLatLng(marker.getLatLng())
                        .setContent(buildPopup(props, coords))
                        .openOn(map);
                }
            });

            marker.on('mouseover', () => {
                if (document.body.classList.contains('rbl-active') && record !== rblFirstFix) {
                    setRblHover(record, true);
                }
            });
            marker.on('mouseout', () => { setRblHover(record, false); });

            (isVFR ? vfrLayer : ifrLayer).addLayer(marker);
        });

        // ─── RBL mode (range & bearing line) ───────────────────────────
        const rblLayer    = L.layerGroup().addTo(map);
        const rblLines    = [];   // each: { line, labelMarker, distNM, magBrg }
        let rblFirstFix   = null;
        let displayMode   = 'rbl';   // 'rbl' | 'time'
        let speedUnit     = 'M';     // 'M'   | 'kts'
        let speedValue    = 0.78;

        // Static 2026 magnetic declination for NW Africa / Morocco region.
        // Linear fit to WMM-2025 projected to 2026; accurate to ~1° within
        // the dataset extent. Westerly declination is negative.
        function magneticDeclination(lat, lon) {
            return -1.0 + 0.4 * (lon + 5) + 0.1 * (35 - lat);
        }

        function greatCircleNM(lat1, lon1, lat2, lon2) {
            const R = 3440.065; // Earth radius, NM
            const toRad = d => d * Math.PI / 180;
            const p1 = toRad(lat1), p2 = toRad(lat2);
            const dp = toRad(lat2 - lat1), dl = toRad(lon2 - lon1);
            const a = Math.sin(dp/2)**2 +
                      Math.cos(p1) * Math.cos(p2) * Math.sin(dl/2)**2;
            return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
        }

        function trueBearing(lat1, lon1, lat2, lon2) {
            const toRad = d => d * Math.PI / 180;
            const p1 = toRad(lat1), p2 = toRad(lat2);
            const dl = toRad(lon2 - lon1);
            const y = Math.sin(dl) * Math.cos(p2);
            const x = Math.cos(p1) * Math.sin(p2) -
                      Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
            const brg = Math.atan2(y, x) * 180 / Math.PI;
            return (brg + 360) % 360;
        }

        function setRblClass(rec, cls, on) {
            if (!rec || !rec.marker) return;
            const el = rec.marker.getElement();
            if (!el) return;
            const fix = el.querySelector('.atc-fix');
            const lbl = el.querySelector('.atc-label');
            if (fix) fix.classList.toggle(cls, on);
            if (lbl) lbl.classList.toggle(cls, on);
        }
        function setRblSelected(rec, on) { setRblClass(rec, 'rbl-selected', on); }
        function setRblHover(rec, on)    { setRblClass(rec, 'rbl-hover',    on); }

        function labelHtml(distNM, magBrg) {
            if (displayMode === 'time') {
                const mins = (speedUnit === 'M')
                    ? Math.round(distNM / (10 * speedValue))
                    : Math.round(distNM / speedValue * 60);
                const safe = (isFinite(mins) && mins >= 0) ? mins : '---';
                const txt  = (safe === '---') ? '---' : String(safe).padStart(3, '0');
                return `<div class="rbl-label">T ${txt}</div>`;
            }
            const brgStr  = String(Math.round(magBrg) % 360).padStart(3, '0');
            const distStr = distNM.toFixed(1);
            return `<div class="rbl-label">B ${brgStr}<br>R ${distStr}</div>`;
        }

        function makeLabelIcon(distNM, magBrg) {
            return L.divIcon({
                html: labelHtml(distNM, magBrg),
                className: 'atc-marker-wrapper',
                iconSize: [62, 32],
                iconAnchor: [31, 16]
            });
        }

        function renderRblLabels() {
            rblLines.forEach(item => {
                item.labelMarker.setIcon(makeLabelIcon(item.distNM, item.magBrg));
            });
        }

        function drawRblLine(p1, p2, magBrg, distNM) {
            const line = L.polyline([p1, p2], {
                color: '#ffd24a',
                weight: 1.5,
                opacity: 0.95,
                interactive: false
            }).addTo(rblLayer);

            const midLat = (p1.lat + p2.lat) / 2;
            const midLng = (p1.lng + p2.lng) / 2;

            const labelMarker = L.marker([midLat, midLng], {
                icon: makeLabelIcon(distNM, magBrg),
                keyboard: false
            }).addTo(rblLayer);

            const item = { line, labelMarker, distNM, magBrg };
            rblLines.push(item);

            labelMarker.bindPopup(
                '<button class="rbl-clear-btn" type="button">Clear</button>',
                { closeButton: false, className: 'rbl-clear-popup', offset: [0, -6] }
            );
            labelMarker.on('popupopen', e => {
                const btn = e.popup.getElement().querySelector('.rbl-clear-btn');
                if (!btn) return;
                btn.onclick = () => {
                    rblLayer.removeLayer(line);
                    rblLayer.removeLayer(labelMarker);
                    const idx = rblLines.indexOf(item);
                    if (idx >= 0) rblLines.splice(idx, 1);
                };
            });
        }

        function handleRblClick(rec) {
            // First click: select.
            if (!rblFirstFix) {
                rblFirstFix = rec;
                setRblHover(rec, false);
                setRblSelected(rec, true);
                return;
            }
            // Second click on the same fix: cancel.
            if (rec === rblFirstFix) {
                setRblSelected(rblFirstFix, false);
                rblFirstFix = null;
                return;
            }
            // Successful second click: compute, briefly hold the highlight,
            // then draw the line + midpoint label.
            const p1 = rblFirstFix.marker.getLatLng();
            const p2 = rec.marker.getLatLng();
            const dist = greatCircleNM(p1.lat, p1.lng, p2.lat, p2.lng);
            const tBrg = trueBearing(p1.lat, p1.lng, p2.lat, p2.lng);
            const decl = magneticDeclination((p1.lat + p2.lat) / 2,
                                             (p1.lng + p2.lng) / 2);
            // Mag = True − declination (east-positive convention).
            const mBrg = ((tBrg - decl) % 360 + 360) % 360;

            setRblHover(rec, false);
            setRblSelected(rec, true);
            const first = rblFirstFix;
            rblFirstFix = null;
            setTimeout(() => {
                setRblSelected(first, false);
                setRblSelected(rec,   false);
            }, 50);
            drawRblLine(p1, p2, mBrg, dist);
        }

        // Map click while a first fix is selected = the "second click missed
        // a fix" case → revert to the pre-first-click state.
        map.on('click', () => {
            if (document.body.classList.contains('rbl-active') && rblFirstFix) {
                setRblSelected(rblFirstFix, false);
                rblFirstFix = null;
            }
        });

        // ─── Segmented toggles + speed input ───────────────────────────
        function setupSegToggle(id, onChange) {
            const root = document.getElementById(id);
            root.addEventListener('click', e => {
                const btn = e.target.closest('.seg-option');
                if (!btn || btn.classList.contains('active')) return;
                root.querySelectorAll('.seg-option').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                onChange(btn.dataset.value);
            });
        }

        const speedInput = document.getElementById('speed-input');

        setupSegToggle('toggle-rbl-mode', v => {
            const on = (v === 'rbl');
            document.body.classList.toggle('rbl-active', on);
            if (!on) {
                if (rblFirstFix) {
                    setRblSelected(rblFirstFix, false);
                    rblFirstFix = null;
                }
                markers.forEach(rec => setRblHover(rec, false));
            }
        });

        setupSegToggle('toggle-display', v => {
            displayMode = v;
            renderRblLabels();
        });

        setupSegToggle('toggle-speed-unit', v => {
            speedUnit = v;
            if (v === 'M') {
                speedValue = 0.78;
                speedInput.value = '0.78';
                speedInput.step  = '0.01';
            } else {
                speedValue = 480;
                speedInput.value = '480';
                speedInput.step  = '1';
            }
            renderRblLabels();
        });

        speedInput.addEventListener('input', () => {
            const v = parseFloat(speedInput.value);
            if (isFinite(v) && v > 0) {
                speedValue = v;
                renderRblLabels();
            }
        });

        // ─── Filters ───────────────────────────────────────────────────
        const filterIfr    = document.getElementById('filter-ifr');
        const filterVfr    = document.getElementById('filter-vfr');
        const filterLabels = document.getElementById('filter-labels');
        const searchInput  = document.getElementById('search');

        function applyFilters() {
            const showIfr    = filterIfr.checked;
            const showVfr    = filterVfr.checked;
            const search     = searchInput.value.toUpperCase().trim();

            markers.forEach(rec => {
                const categoryOk = (rec.category === 'ifr' && showIfr) ||
                                   (rec.category === 'vfr' && showVfr);
                const searchOk = !search || rec.name.includes(search);
                const layer = rec.category === 'vfr' ? vfrLayer : ifrLayer;
                if (categoryOk && searchOk) {
                    if (!layer.hasLayer(rec.marker)) layer.addLayer(rec.marker);
                } else {
                    if (layer.hasLayer(rec.marker)) layer.removeLayer(rec.marker);
                }
            });
        }

        function applyLabelVisibility() {
            document.body.classList.toggle('labels-hidden', !filterLabels.checked);
        }

        filterIfr.addEventListener('change',    applyFilters);
        filterVfr.addEventListener('change',    applyFilters);
        filterLabels.addEventListener('change', applyLabelVisibility);
        searchInput.addEventListener('input',   applyFilters);

        // Enter to zoom to first match
        searchInput.addEventListener('keypress', e => {
            if (e.key !== 'Enter') return;
            const term = searchInput.value.toUpperCase().trim();
            if (!term) return;
            const exact = markers.find(m => m.name === term);
            const partial = exact || markers.find(m => m.name.includes(term));
            if (partial) {
                map.flyTo(partial.marker.getLatLng(), 9, { duration: 0.6 });
                if (!document.body.classList.contains('rbl-active')) {
                    L.popup({ closeButton: true, maxWidth: 320 })
                        .setLatLng(partial.marker.getLatLng())
                        .setContent(buildPopup(partial.props, partial.coords))
                        .openOn(map);
                }
            }
        });

        // ─── Cursor coordinates readout ────────────────────────────────
        const coordsEl = document.getElementById('cursor-coords');
        function fmtCoord(deg, posChar, negChar) {
            const abs = Math.abs(deg);
            const d = Math.floor(abs);
            const m = Math.floor((abs - d) * 60);
            const s = ((abs - d - m / 60) * 3600).toFixed(0).padStart(2, '0');
            return `${d.toString().padStart(2, '0')}°${m.toString().padStart(2,'0')}'${s}" ${deg >= 0 ? posChar : negChar}`;
        }
        map.on('mousemove', e => {
            const { lat, lng } = e.latlng;
            coordsEl.textContent = `${fmtCoord(lat, 'N', 'S')}  ${fmtCoord(lng, 'E', 'W')}`;
        });
        map.on('mouseout', () => { coordsEl.textContent = '— —'; });
    })();
    </script>
</body>
</html>
"""


# ─── HTML emission ─────────────────────────────────────────────────────────

def create_map(geojson_data, output_path="waypoints_map.html",
               borders_data=None, sectors_data=None,
               title="Morocco AIP ENR 4.4", subtitle="Significant Points"):
    """Create a standalone HTML map file from waypoint + borders + sectors GeoJSON."""
    features = geojson_data.get("features", [])
    bbox = waypoint_bbox(features)
    view = {
        "min_lon": bbox[0] - VIEW_PADDING_DEG,
        "min_lat": bbox[1] - VIEW_PADDING_DEG,
        "max_lon": bbox[2] + VIEW_PADDING_DEG,
        "max_lat": bbox[3] + VIEW_PADDING_DEG,
    }
    stats = compute_stats(features)

    # Crop borders to waypoint extent + padding to keep the embedded payload small
    cropped_borders = crop_geojson_to_bbox(borders_data, bbox, padding=BORDERS_CROP_PADDING_DEG)

    payload = {
        "waypoints": geojson_data,
        "borders":   cropped_borders,
        "sectors":   sectors_data,
        "view":      view,
        "stats":     stats,
        "title":     title,
        "subtitle":  subtitle,
    }

    # Embed payload safely inside a <script type="application/json"> tag.
    # The only sequence that could break out is "</script>", which we escape.
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = HTML_TEMPLATE.replace("__PAYLOAD__", payload_json)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return {
        "output_path": output_path,
        "waypoints": len(features),
        "borders_before": len((borders_data or {}).get("features", [])),
        "borders_after":  len((cropped_borders or {}).get("features", [])),
        "sectors":   len((sectors_data or {}).get("features", [])),
        "view":      view,
        "stats":     stats,
    }


# ─── CLI ───────────────────────────────────────────────────────────────────

def main(geojson_path, output_path="waypoints_map.html",
         borders_path=None, sectors_path=None):
    print("─" * 60)
    print("ATC Display Map Generator")
    print("─" * 60)

    print(f"Loading waypoints: {geojson_path}")
    geojson_data = load_geojson_required(geojson_path)
    print(f"  {len(geojson_data.get('features', []))} waypoints")

    borders_data = load_geojson_optional(borders_path or DEFAULT_BORDERS_PATH)
    if borders_data:
        print(f"Loaded borders:    {len(borders_data.get('features', []))} features")
    else:
        print("Loaded borders:    none (will render without)")

    sectors_data = load_geojson_optional(sectors_path or DEFAULT_SECTORS_PATH)
    if sectors_data:
        print(f"Loaded sectors:    {len(sectors_data.get('features', []))} features")
    else:
        print("Loaded sectors:    none")

    print(f"\nGenerating map → {output_path}")
    result = create_map(geojson_data, output_path, borders_data, sectors_data)

    print()
    print(f"  Stats:     IFR={result['stats']['ifr']}  "
          f"VFR={result['stats']['vfr']}  "
          f"Conflicts={result['stats']['conflict']}  "
          f"Total={result['stats']['total']}")
    print(f"  View:      lat [{result['view']['min_lat']:.2f}, {result['view']['max_lat']:.2f}], "
          f"lon [{result['view']['min_lon']:.2f}, {result['view']['max_lon']:.2f}]")
    print(f"  Borders:   {result['borders_after']}/{result['borders_before']} kept after "
          f"{BORDERS_CROP_PADDING_DEG}° crop")
    print(f"  Sectors:   {result['sectors']}")
    print()
    print(f"Output: {output_path}")
    print("─" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python visualize_waypoints.py <waypoints.geojson> "
              "[output.html] [borders.geojson] [sectors.geojson]")
        sys.exit(1)

    waypoints_arg = sys.argv[1]
    output_arg    = sys.argv[2] if len(sys.argv) > 2 else "waypoints_map.html"
    borders_arg   = sys.argv[3] if len(sys.argv) > 3 else None
    sectors_arg   = sys.argv[4] if len(sys.argv) > 4 else None

    if not Path(waypoints_arg).exists():
        print(f"Error: waypoints file not found: {waypoints_arg}")
        sys.exit(1)

    main(waypoints_arg, output_arg, borders_arg, sectors_arg)
