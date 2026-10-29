#!/usr/bin/env python3
"""
Waypoint Visualization Module — ATC display style.

Generates a standalone HTML file with a Leaflet + Proj4Leaflet map styled to
mirror an en-route ATC situation display:
  - Dark navy background, no land/ocean differentiation
  - Muted country borders, brighter sector outlines
  - Triangle fix symbols (filled = IFR, open = VFR)
  - Hexagon navaid symbols (VOR / DME / NDB), labelled with the ident
  - Thin ATS route lines (hover = highlight route, click = segment details)
  - Permanent monospace labels next to each fix
  - Morocco Lambert Conformal Conic CRS
  - Runtime cropping of border features outside waypoint extent + padding

Pure stdlib — no external Python dependencies.

Usage:
    python page_generator.py waypoints.geojson [output.html] [borders.geojson] [sectors.geojson] [navaids.geojson] [routes.geojson]
"""

import json
import re
import sys
from pathlib import Path

# ─── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_BORDERS_PATH = "borders.geojson"
DEFAULT_SECTORS_PATH = "sectors.geojson"
DEFAULT_NAVAIDS_PATH = "navaids.geojson"
DEFAULT_ROUTES_PATH = "routes.geojson"

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
    """Return dict of IFR / VFR / total counts."""
    ifr = vfr = 0
    for f in features:
        props = f.get("properties") or {}
        ft = props.get("flight_type")
        if ft == "IFR":
            ifr += 1
        elif ft == "VFR":
            vfr += 1
    return {"total": len(features), "ifr": ifr, "vfr": vfr}


# ─── Label priority ────────────────────────────────────────────────────────
# When labels overlap, the lower-priority one is hidden. Lower number wins:
#   sector transfer points → navaids (by type) → IFR fixes (FIR boundary →
#   on a route → TMA only / other → approach only) → VFR fixes.

NAVAID_PRIORITY = {"DVOR/DME": 0, "VOR/DME": 0, "VOR": 1, "DME": 2,
                   "NDB": 3, "Locator": 4}
IFR_PRIORITY = {"transfer": -1, "fir": 10, "route": 11, "tma": 12, "approach": 13}
# IFR classes whose label may move above the symbol when the right side is
# taken (navaids always may). TMA-only, approach-only and VFR fixes may not.
MOVE_UP_CLASSES = ("transfer", "fir", "route")
# Words that mark a fix as approach-only (hidden by default on the map)
APPROACH_WORDS = re.compile(r"\b(IAF|IF|FAF|STAR|SID|RWY|DEP|ARR)\b")
VFR_PRIORITY = 20

# Marine NDBs are beacons for ships, not aviation aids: left off the map
HIDDEN_NAVAID_TYPES = ("NDB (marin)",)
# Map label for merged navaids (the popup still shows the full ident)
NAVAID_LABEL_OVERRIDES = {"CBA/SAK": "SAK"}
# Navaids whose label goes above the symbol instead of to the right, because
# a neighbouring navaid's symbol sits right where the label would be
NAVAID_LABEL_ABOVE = {"FES", "ADM"}

# Route designators like UN858, G/UG5, A411, J744 (and the odd "GUG 5")
ROUTE_RE = re.compile(r"\b(?:[A-Z]/?)?U?[A-Z]\s?\d{1,4}[A-Z]?\b(?![.,]\d)")


def ifr_class(routes_text, fra_relevance=""):
    """Classify an IFR fix by its 'routes' text:
    transfer, fir, route, tma (also anything unrecognised) or approach."""
    text = routes_text or ""
    if "transfert secteur" in text:
        return "transfer"
    if "FIR" in text or "BDRY" in text:
        return "fir"
    if ROUTE_RE.search(text) or fra_relevance:
        return "route"
    if "TMA" not in text and APPROACH_WORDS.search(text):
        return "approach"
    return "tma"


def prepare_waypoints(geo):
    """Copy waypoint features, adding _class and _priority for the page."""
    out = []
    for feat in (geo or {}).get("features", []):
        props = dict(feat.get("properties") or {})
        if (props.get("flight_type") or "").upper() == "VFR":
            props["_class"] = "vfr"
            props["_priority"] = VFR_PRIORITY
        else:
            cls = ifr_class(props.get("routes"), props.get("fra_relevance"))
            props["_class"] = "approach" if cls == "approach" else "ifr"
            # More route designators = slightly more important within a tier
            n_routes = len(ROUTE_RE.findall(props.get("routes") or ""))
            props["_priority"] = IFR_PRIORITY[cls] - min(n_routes, 9) / 10
            # High-level fixes may move their label above the symbol
            props["_move_up"] = cls in MOVE_UP_CLASSES
        out.append({**feat, "properties": props})
    return {"type": "FeatureCollection", "features": out}


def prepare_navaids(geo):
    """Copy navaid features minus hidden types, adding _label and _priority."""
    if not geo:
        return None
    out = []
    for feat in geo.get("features", []):
        props = dict(feat.get("properties") or {})
        nav_type = props.get("type") or ""
        if nav_type in HIDDEN_NAVAID_TYPES:
            continue
        # Merged navaids ("DME + NDB") rank by their most important part
        parts = [t.strip() for t in nav_type.split("+")]
        props["_priority"] = min(NAVAID_PRIORITY.get(t, 5) for t in parts)
        props["_label"] = NAVAID_LABEL_OVERRIDES.get(props.get("id"), props.get("id"))
        if props.get("id") in NAVAID_LABEL_ABOVE:
            props["_label_pos"] = "above"
        out.append({**feat, "properties": props})
    return {"type": "FeatureCollection", "features": out}


# ─── HTML template ─────────────────────────────────────────────────────────
# A static HTML template with a single `__PAYLOAD__` placeholder. Using a
# placeholder rather than an f-string means CSS/JS braces don't need to be
# doubled — a far less error-prone approach for templates of this size.

# Per-route fields shown in the segment popup
ROUTE_FIELDS = ("route", "from", "to", "track_mag", "track_inherited", "distance_nm",
                "layers", "width_nm", "odd", "even", "control", "tma", "notes", "issues",
                "route_notes", "source", "nav_spec")


def add_route_point_details(routes_geo, *fix_collections):
    """Copy ENR 3.1/3.2 point details (FIR boundary pair, RDL or VOR/DME fix
    reference) onto the
    matching waypoint / navaid features, by name or navaid ident."""
    details = {}
    for f in (routes_geo or {}).get("features", []):
        p = f["properties"]
        if f["geometry"]["type"] != "Point":
            continue
        d = {k: p[k] for k in ("fir_boundary", "rdl", "fix_ref") if p.get(k)}
        if d:
            details[str(p["name"]).upper()] = d
            if p.get("navaid"):
                details[str(p["navaid"]).upper()] = d
    for geo in fix_collections:
        for f in (geo or {}).get("features", []):
            p = f["properties"]
            keys = [str(p.get("name", "")).upper()] + \
                   [str(c["id"]).upper() for c in p.get("components", [])]
            d = next((details[k] for k in keys if k in details), None)
            if d:
                p["_fir_boundary"] = d.get("fir_boundary")
                p["_rdl"] = d.get("rdl")
                p["_fix_ref"] = d.get("fix_ref")


def prepare_routes(routes_geo, waypoints_geo, navaids_geo):
    """ATS route segments from routes.geojson (made by route_parser.py).

    Lower and upper routes usually share the same segment (A 411 / UA 411), so
    segments joining the same two positions are drawn once and list every
    route on them. Route points that no other layer has (e.g. ARENA, CABEL,
    outside the FIR) are returned too, so the lines don't end in empty space.
    """
    if not routes_geo:
        return None
    segments, by_key = [], {}
    designators, types = set(), {}
    extra_points = []

    known = set()
    for f in (waypoints_geo or {}).get("features", []):
        known.add(str(f["properties"].get("name", "")).upper())
    for f in (navaids_geo or {}).get("features", []):
        p = f["properties"]
        known.update(str(x).upper() for x in str(p.get("id", "")).split("/"))
        known.update(str(c["id"]).upper() for c in p.get("components", []))

    points_by_route = {}   # designator -> names/idents of its points (for search)
    for f in routes_geo.get("features", []):
        geom, p = f["geometry"], f["properties"]
        if geom["type"] == "Point":
            for d in p.get("routes", []):
                names = points_by_route.setdefault(d, set())
                names.add(str(p["name"]).upper())
                if p.get("navaid"):
                    names.add(str(p["navaid"]).upper())
            if p.get("kind") == "route_point" and \
                    str(p["name"]).upper() not in known and \
                    str(p.get("navaid") or "").upper() not in known:
                extra_points.append(f)
            continue
        if geom["type"] != "LineString":
            continue
        # same two points = same line, whichever document or direction
        key = tuple(sorted((p["from"], p["to"])))
        seg = by_key.get(key)
        if seg is None:
            seg = by_key[key] = {"coords": geom["coordinates"], "routes": []}
            segments.append(seg)
        seg["routes"].append({k: p.get(k) for k in ROUTE_FIELDS})
        designators.add(p["route"])
        types[p["route"]] = "rnav" if p.get("nav_spec") else "conventional"

    # Shaped like IFR waypoints so they share the fix symbol, popup and declutter
    extra = [{"type": "Feature", "geometry": f["geometry"], "properties": {
                "name": f["properties"]["name"], "flight_type": "IFR",
                "routes": "On " + ", ".join(f["properties"].get("routes", []))
                          + " (from ENR 3.1; not listed in ENR 4.4)",
                "_class": "ifr", "_priority": IFR_PRIORITY["route"], "_move_up": True}}
             for f in extra_points]
    return {"segments": segments, "count": len(designators), "extra_points": extra, "types": types,
            "points": {d: sorted(n) for d, n in points_by_route.items()}}


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Infrastructure</title>

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
            --fix-navaid: #9db4c8;
            --label: #c8d8e8;
            --label-dim: #6a829a;
            --panel-bg: rgba(10, 23, 39, 0.88);
            --panel-border: #1f3a52;
            --text: #d8e4f0;
            --text-dim: #7a92aa;
            --accent: #5fd0e8;
            --rbl: #d4a72c;
            --rbl-bright: #ffd24a;
            --route: #5d7790;
            --route-hl: #5fd0e8;
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

        .atc-fix.navaid path {
            fill: rgba(157, 180, 200, 0.15);
            stroke: var(--fix-navaid);
            stroke-width: 1.4;
            filter: drop-shadow(0 0 3px rgba(157, 180, 200, 0.4));
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
        .atc-label.navaid { color: var(--fix-navaid); }
        .atc-label.above { left: 7px; top: -15px; transform: translateX(-50%); }
        .atc-label.below { left: 7px; top: 15px;  transform: translateX(-50%); }
        .atc-label.navaid:hover { opacity: 1; color: var(--fix-navaid); }
        .atc-fix:hover + .atc-label,
        .atc-fix:hover .atc-label { opacity: 1; }
        .labels-hidden .atc-label:not(.route-label) { display: none; }
        /* Airway designator in the middle of a route segment */
        .atc-label.route-label {
            left: 7px;
            top: 7px;
            transform: translate(-50%, -50%);
            font-size: 9px;
            letter-spacing: 0.5px;
            color: #8fa7be;
            background: rgba(6, 13, 24, 0.88);
            padding: 0 3px;
            border-radius: 2px;
            opacity: 0.9;
            pointer-events: none;
            cursor: default;
        }
        .atc-label.route-label.route-label-hl { color: var(--route-hl); opacity: 1; }
        /* Decluttering: lower-priority labels that overlap are hidden; hovering
           a fix reveals its label and hides the ones it overlaps meanwhile. */
        .atc-label.declutter-hidden,
        .atc-label.hover-suppressed { visibility: hidden; }
        .atc-label.hover-reveal { visibility: visible !important; }

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
        .stats .stat-value.navaid   { color: var(--fix-navaid); }

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

        .filter-box {
            top: 12px; right: 12px; min-width: 200px;
            max-height: calc(100vh - 110px);   /* stays clear of the zoom buttons */
            overflow-y: auto;
        }
        .filter-box h4.spaced { margin-top: 12px; display: flex; align-items: baseline; }
        .sector-clear {
            margin-left: auto;
            background: none;
            border: none;
            padding: 0;
            color: var(--text-dim);
            font: inherit;
            letter-spacing: inherit;
            text-transform: uppercase;
            cursor: pointer;
        }
        .sector-clear:hover { color: var(--accent); }
        .sector-clear[hidden] { display: none; }
        .sector-hint { font-size: 10.5px; color: var(--text-dim); margin: 2px 0 4px; }
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
        .filter-item.filter-sub { padding-left: 18px; }
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
        .badge-navaid { background: rgba(157, 180, 200, 0.18); color: var(--fix-navaid); border: 1px solid var(--fix-navaid); }
        .badge-route  { background: rgba(93, 119, 144, 0.22); color: var(--text); border: 1px solid var(--route); }

        /* ─── ATS routes ─────────────────────────────────────────── */
        .route-popup .badge { margin-bottom: 8px; }
        .route-popup .badge + .badge { margin-left: 5px; }
        .route-popup .variant + .variant { margin-top: 3px; }
        .route-popup .route-tag {
            margin-left: 6px;
            color: var(--text-dim);
            font-size: 9px;
            letter-spacing: 0.6px;
        }
        .route-popup td:last-child { white-space: nowrap; }
        .route-popup td:first-child { padding-right: 12px; }
        .route-popup .note { color: var(--text-dim); font-size: 10px; }
        .route-popup .route-notes {
            max-width: 300px;
            margin-top: 8px;
            padding-top: 6px;
            border-top: 1px solid var(--panel-border);
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            line-height: 1.45;
        }
        .route-popup .route-notes div + div { margin-top: 4px; }
        .route-popup h5 {
            font-size: 8.5px;
            font-weight: 600;
            letter-spacing: 1.2px;
            text-transform: uppercase;
            color: var(--text-dim);
            margin-bottom: 3px;
        }
        .route-popup .source {
            max-width: 300px;
            margin-top: 8px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 9px;
            color: var(--text-dim);
            letter-spacing: 0.4px;
        }
        .route-popup .remark {
            max-width: 300px;
            margin-top: 7px;
            padding-top: 6px;
            border-top: 1px solid var(--panel-border);
            font-family: 'JetBrains Mono', monospace;
            font-size: 10px;
            line-height: 1.45;
            color: var(--text);
        }
        .route-popup .dim  { color: var(--text-dim); }
        .leaflet-tooltip.route-tooltip {
            background: var(--panel-bg);
            border: 1px solid var(--panel-border);
            border-radius: 2px;
            color: var(--route-hl);
            font-family: 'JetBrains Mono', monospace;
            font-size: 10.5px;
            letter-spacing: 0.8px;
            padding: 2px 7px;
            box-shadow: none;
        }
        .leaflet-tooltip.route-tooltip::before { display: none; }
        .rbl-active .route-hit { cursor: crosshair; }
        path.route-hit:focus { outline: none; }

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

        /* Zoom controls */
        .leaflet-control-zoom a {
            background: var(--panel-bg) !important;
            color: var(--text) !important;
            border: 1px solid var(--panel-border) !important;
            backdrop-filter: blur(8px);
        }
        .leaflet-control-zoom a:hover { color: var(--accent) !important; }
        /* sits above the cursor-coordinates box */
        .leaflet-bottom.leaflet-right .leaflet-control-zoom { margin: 0 12px 54px 0; }

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
        <input type="text" id="search" placeholder="Search waypoint or route…" autocomplete="off" />
    </div>

    <div class="panel filter-box">
        <h4>Filters</h4>
        <label class="filter-item">
            <input type="checkbox" autocomplete="off" id="filter-ifr" checked />
            <span>IFR Fixes</span>
            <span class="count" id="count-ifr">0</span>
        </label>
        <label class="filter-item filter-sub">
            <input type="checkbox" autocomplete="off" id="filter-approach" />
            <span>Approach only</span>
            <span class="count" id="count-approach">0</span>
        </label>
        <label class="filter-item">
            <input type="checkbox" autocomplete="off" id="filter-vfr" checked />
            <span>VFR Fixes</span>
            <span class="count" id="count-vfr">0</span>
        </label>
        <label class="filter-item">
            <input type="checkbox" autocomplete="off" id="filter-navaid" checked />
            <span>Navaids</span>
            <span class="count" id="count-navaid">0</span>
        </label>
        <label class="filter-item">
            <input type="checkbox" autocomplete="off" id="filter-routes" checked />
            <span>ATS Routes</span>
            <span class="count" id="count-routes">0</span>
        </label>
        <label class="filter-item filter-sub">
            <input type="checkbox" autocomplete="off" id="filter-routes-lower" checked />
            <span>Lower</span>
        </label>
        <label class="filter-item filter-sub">
            <input type="checkbox" autocomplete="off" id="filter-routes-upper" checked />
            <span>Upper (U…)</span>
        </label>
        <label class="filter-item filter-sub">
            <input type="checkbox" autocomplete="off" id="filter-routes-conv" checked />
            <span>Conventional</span>
        </label>
        <label class="filter-item filter-sub">
            <input type="checkbox" autocomplete="off" id="filter-routes-rnav" checked />
            <span>RNAV</span>
        </label>
        <label class="filter-item">
            <input type="checkbox" autocomplete="off" id="filter-labels" checked />
            <span>Fix Name</span>
        </label>
        <label class="filter-item">
            <input type="checkbox" autocomplete="off" id="filter-route-names" checked />
            <span>Route Name</span>
        </label>
        <div id="sector-filter-box" hidden>
            <h4 class="spaced">Sectors
                <button class="sector-clear" id="sector-clear" type="button" hidden>Show all</button>
            </h4>
            <div class="sector-hint" id="sector-hint">Tick sectors to show only what's inside</div>
            <div id="sector-filters"></div>
        </div>
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
        <div class="legend-item">
            <span class="legend-marker"><svg width="14" height="14" viewBox="-7 -7 14 14"><path d="M -5,0 L -2.5,-4.33 L 2.5,-4.33 L 5,0 L 2.5,4.33 L -2.5,4.33 Z" fill="rgba(157,180,200,0.15)" stroke="#9db4c8" stroke-width="1.4"/></svg></span>
            Navaid (VOR / DME / NDB)
        </div>
        <div class="legend-item">
            <span class="legend-marker"><svg width="16" height="14" viewBox="0 -7 16 14"><line x1="0" y1="0" x2="16" y2="0" stroke="#5d7790" stroke-width="1.2"/></svg></span>
            ATS Route
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
        const navaids   = PAYLOAD.navaids;
        const routes    = PAYLOAD.routes;
        const view      = PAYLOAD.view;
        const stats     = PAYLOAD.stats;
        const title     = PAYLOAD.title;
        const subtitle  = PAYLOAD.subtitle;

        // ─── Header + counts ───────────────────────────────────────────
        document.getElementById('title-text').textContent = title;
        document.getElementById('subtitle-text').textContent = subtitle;
        document.getElementById('count-ifr').textContent = (stats.ifr - stats.approach).toString().padStart(3, '0');
        document.getElementById('count-approach').textContent = stats.approach.toString().padStart(3, '0');
        document.getElementById('count-vfr').textContent = stats.vfr.toString().padStart(3, '0');
        document.getElementById('count-navaid').textContent = stats.navaid.toString().padStart(3, '0');
        document.getElementById('count-routes').textContent = (stats.routes || 0).toString().padStart(3, '0');

        const statsHtml = [
            ['total',    stats.total,  ''],
            ['ifr',      stats.ifr,    'ifr'],
            ['vfr',      stats.vfr,    'vfr'],
            ['navaid',   stats.navaid, 'navaid'],
        ].map(([label, val, cls]) =>
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
            zoomControl: false,   // added bottom-right below, clear of the panels
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
        L.control.zoom({ position: 'bottomright' }).addTo(map);

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
        const SECTOR_STYLE     = { color: '#3eb5d4', weight: 1.2, fillColor: '#3eb5d4', fillOpacity: 0.04,
                                   opacity: 0.55, dashArray: '3,4', lineJoin: 'round' };
        const SECTOR_STYLE_SEL = { ...SECTOR_STYLE, weight: 1.8, fillOpacity: 0.10, opacity: 0.95, dashArray: null };
        const SECTOR_STYLE_OFF = { ...SECTOR_STYLE, fillOpacity: 0, opacity: 0.25 };
        const sectorList = [];   // { name, short, rings, layer, label }

        if (sectors) {
            sectors.features.forEach(feat => {
                const geom = feat.geometry || {};
                const polys = geom.type === 'Polygon' ? [geom.coordinates]
                            : geom.type === 'MultiPolygon' ? geom.coordinates : [];
                if (!polys.length) return;
                const name = (feat.properties || {}).name || `Sector ${sectorList.length + 1}`;
                const rec = {
                    name,
                    short: name.split(' / ')[0].replace(/\s+Sector$/i, ''),   // "Oceanic North"
                    polys,
                    layer: L.geoJSON(feat, { style: SECTOR_STYLE, interactive: false }).addTo(map),
                    label: null
                };
                const [cx, cy] = polygonCentroid(polys[0]);
                rec.label = L.marker([cy, cx], {
                    icon: L.divIcon({
                        html: `<div class="sector-label">${escapeHtml(name)}</div>`,
                        className: 'atc-marker-wrapper',
                        iconSize: [120, 14],
                        iconAnchor: [60, 7]
                    }),
                    interactive: false,
                    keyboard: false
                }).addTo(map);
                sectorList.push(rec);
            });
        }

        // ─── Sector geometry (which sector a fix / route lies in) ──────
        // Lon/lat are scaled to NM around each test point, fine at sector size.
        const SECTOR_EDGE_NM = 2;   // fixes this close to a boundary count as inside

        function inRing(lon, lat, ring) {
            let inside = false;
            for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
                const [xi, yi] = ring[i], [xj, yj] = ring[j];
                if ((yi > lat) !== (yj > lat) &&
                    lon < (xj - xi) * (lat - yi) / (yj - yi) + xi) inside = !inside;
            }
            return inside;
        }

        function inPolys(lon, lat, polys) {
            // outer ring in, no hole contains it
            return polys.some(p => inRing(lon, lat, p[0]) && !p.slice(1).some(h => inRing(lon, lat, h)));
        }

        function nmToEdge(lon, lat, polys) {
            const kx = 60 * Math.cos(lat * Math.PI / 180), ky = 60;
            let best = Infinity;
            polys.forEach(p => p.forEach(ring => {
                for (let i = 1; i < ring.length; i++) {
                    const ax = (ring[i - 1][0] - lon) * kx, ay = (ring[i - 1][1] - lat) * ky;
                    const bx = (ring[i][0] - lon) * kx,     by = (ring[i][1] - lat) * ky;
                    const dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy;
                    const t = len2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2)) : 0;
                    best = Math.min(best, Math.hypot(ax + t * dx, ay + t * dy));
                }
            }));
            return best;
        }

        // Indexes of the sectors a fix belongs to (boundary fixes belong to both)
        function sectorsOfPoint(lon, lat) {
            const out = [];
            sectorList.forEach((s, i) => {
                if (inPolys(lon, lat, s.polys) || nmToEdge(lon, lat, s.polys) <= SECTOR_EDGE_NM) out.push(i);
            });
            return out;
        }

        // Indexes of the sectors a route line passes through: the line is
        // sampled every ~2 NM, so a segment that only touches a corner or ends
        // on the boundary does not count.
        function sectorsOfLine(coords) {
            const hit = new Set();
            for (let k = 1; k < coords.length; k++) {
                const [x0, y0] = coords[k - 1], [x1, y1] = coords[k];
                const nm = Math.hypot((x1 - x0) * 60 * Math.cos(y0 * Math.PI / 180), (y1 - y0) * 60);
                const n = Math.max(2, Math.ceil(nm / 2));
                for (let s = 1; s < n; s++) {
                    const t = s / n, lon = x0 + t * (x1 - x0), lat = y0 + t * (y1 - y0);
                    sectorList.forEach((sec, i) => { if (!hit.has(i) && inPolys(lon, lat, sec.polys)) hit.add(i); });
                }
            }
            return [...hit];
        }

        // ─── Waypoint markers ──────────────────────────────────────────
        const TRIANGLE_PATH = 'M 0,-5.5 L 4.8,3.5 L -4.8,3.5 Z';
        const markers = [];

        function buildIcon(props) {
            const ft        = (props.flight_type || '').toUpperCase();
            const isVFR     = ft === 'VFR';
            const cls = isVFR ? 'vfr' : 'ifr';
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
            html += enr31Rows(props);
            if (ft === 'VFR' && props.location) {
                html += `<tr><td>Location</td><td>${escapeHtml(props.location)}</td></tr>`;
            }
            html += '</table>';
            html += '</div>';
            return html;
        }

        // ─── Navaid markers (hexagon, labelled with the ident) ─────────
        const HEXAGON_PATH = 'M -5,0 L -2.5,-4.33 L 2.5,-4.33 L 5,0 L 2.5,4.33 L -2.5,4.33 Z';

        function buildNavaidIcon(props) {
            const ident = escapeHtml(props._label || props.id || '???');
            const html =
                `<div class="atc-fix navaid">` +
                `<svg width="14" height="14" viewBox="-7 -7 14 14"><path d="${HEXAGON_PATH}"/></svg>` +
                `</div>` +
                `<span class="atc-label navaid${props._label_pos === 'above' ? ' above' : ''}">${ident}</span>`;
            return L.divIcon({
                html: html,
                className: 'atc-marker-wrapper',
                iconSize: [14, 14],
                iconAnchor: [7, 7]
            });
        }

        function buildNavaidPopup(props, coords) {
            const ident = escapeHtml(props.id || '???');
            const station = escapeHtml(props.name || '');
            const rawCoords = escapeHtml(props.raw_coordinates || '');
            let html = `<div class="waypoint-popup"><h3>${ident}</h3>`;
            const navType = escapeHtml(props.type || 'NAVAID');
            html += `<span class="badge badge-navaid">${navType}</span><table>`;
            if (station) html += `<tr><td>Station</td><td>${station}</td></tr>`;
            if (props.type) html += `<tr><td>Type</td><td>${navType}</td></tr>`;
            if (rawCoords) html += `<tr><td>Sexagesimal</td><td>${rawCoords}</td></tr>`;
            html += `<tr><td>WGS84</td><td>${coords[1].toFixed(4)}°, ${coords[0].toFixed(4)}°</td></tr>`;
            html += enr31Rows(props);
            html += '</table></div>';
            return html;
        }

        // Point details printed in the route tables (ENR 3.1 / 3.2)
        function enr31Rows(props) {
            let html = '';
            if (props._fir_boundary) html += `<tr><td>FIR bdy</td><td>${escapeHtml(props._fir_boundary)}</td></tr>`;
            const refs = [];
            if (props._rdl) refs.push(escapeHtml(props._rdl));
            const f = props._fix_ref;
            if (f) refs.push(`${escapeHtml(f.navaid)} ${String(f.bearing).padStart(3, '0')}° / ${f.distance_nm} NM`);
            if (refs.length) html += `<tr><td>Fix ref</td><td>${refs.join('<br>')}</td></tr>`;
            return html;
        }

        const layers = {
            ifr:    L.layerGroup().addTo(map),
            vfr:      L.layerGroup().addTo(map),
            approach: L.layerGroup().addTo(map),
            navaid:   L.layerGroup().addTo(map),
        };

        // Shared marker setup for waypoints and navaids: popup, RBL, search.
        function addFix(coords, icon, record, popupHtml) {
            const marker = L.marker([coords[1], coords[0]], {
                icon: icon,
                riseOnHover: true,
                keyboard: false
            });
            record.marker = marker;
            record.coords = coords;
            record.sectors = sectorsOfPoint(coords[0], coords[1]);
            record.popupHtml = popupHtml;
            markers.push(record);

            marker.on('click', (e) => {
                if (document.body.classList.contains('rbl-active')) {
                    L.DomEvent.stopPropagation(e);
                    handleRblClick(record);
                } else {
                    L.popup({ closeButton: true, maxWidth: 320 })
                        .setLatLng(marker.getLatLng())
                        .setContent(popupHtml)
                        .openOn(map);
                }
            });

            marker.on('mouseover', () => {
                if (document.body.classList.contains('rbl-active') && record !== rblFirstFix) {
                    setRblHover(record, true);
                }
                revealLabel(record);
            });
            marker.on('mouseout', () => {
                setRblHover(record, false);
                unrevealLabel(record);
            });

            layers[record.category].addLayer(marker);
        }

        waypoints.features.forEach(feat => {
            const geom = feat.geometry || {};
            if (geom.type !== 'Point') return;
            const props = feat.properties || {};
            const name  = (props.name || '').toUpperCase();
            addFix(geom.coordinates, buildIcon(props), {
                name: name,
                search: name,
                category: props._class || 'ifr',
                priority: props._priority != null ? props._priority : 15,
                canMoveUp: !!props._move_up,
                props: props
            }, buildPopup(props, geom.coordinates));
        });

        ((navaids && navaids.features) || []).forEach(feat => {
            const geom = feat.geometry || {};
            if (geom.type !== 'Point') return;
            const props = feat.properties || {};
            const ident = (props.id || '').toUpperCase();
            addFix(geom.coordinates, buildNavaidIcon(props), {
                name: ident,
                // Search by ident (incl. each part of merged ones like CBA/SAK) or station name
                search: ident + ' ' + safeUpper(props.name),
                idents: ident.split('/'),
                category: 'navaid',
                priority: props._priority != null ? props._priority : 5,
                canMoveUp: true,
                fixedAbove: props._label_pos === 'above',
                props: props
            }, buildNavaidPopup(props, geom.coordinates));
        });

        // ─── ATS routes ────────────────────────────────────────────────
        // One thin line per segment (lower/upper twins share it). An invisible
        // wider line on top makes the thin one easy to hover and click.
        // Hover = highlight every segment of the route(s); click = details.
        const ROUTE_STYLE    = { color: '#5d7790', weight: 1.1, opacity: 0.75 };
        const ROUTE_STYLE_HL = { color: '#5fd0e8', weight: 2.2, opacity: 1 };
        const routesLayer = L.layerGroup().addTo(map);
        const routeSegs   = [];
        const segsByRoute = {};
        const routeHover  = new Set();   // designators highlighted by the mouse
        let routeSearch   = null;        // designator matched by the search box
        const routePoints = (routes && routes.points) || {};
        const normRoute    = s => String(s || '').toUpperCase().replace(/\s+/g, '');
        const isUpperRoute = d => /^U/.test(d);
        const pad3 = n => String(n).padStart(3, '0');

        function directionText(r, key) {
            if (r[key] === 'forward')  return `${escapeHtml(r.from)} → ${escapeHtml(r.to)}`;
            if (r[key] === 'backward') return `${escapeHtml(r.to)} → ${escapeHtml(r.from)}`;
            return '<span class="dim">—</span>';
        }

        // Flags already shown another way in the popup
        const SHOWN_ISSUES = /^(levels cell is blank|no cruising-level arrows)/;

        // A route's data seen in the segment's own direction (A → B). A twin
        // listed the other way round gets its tracks and odd/even swapped.
        function orient(r, A, B) {
            const flip = r.from !== A;
            const dir = d => flip ? ({ forward: 'backward', backward: 'forward' }[d] || d) : d;
            return { ...r, from: A, to: B,
                     track_mag: r.track_mag && (flip ? [r.track_mag[1], r.track_mag[0]] : r.track_mag),
                     odd: dir(r.odd), even: dir(r.even) };
        }

        function levelValue(l) {
            if (!l) return -1;
            if (/^FL/.test(l)) return +l.slice(2) * 100;
            if (/FT$/.test(l)) return parseInt(l, 10);
            return l === 'UNL' ? 1e6 : 0;
        }

        const routeTag = names => `<span class="route-tag">${names.map(escapeHtml).join(' · ')}</span>`;

        // One row per field. When every route agrees the value is shown once;
        // otherwise each distinct value gets a line labelled with its route(s).
        function mergedRow(label, rs, valueOf) {
            const groups = new Map();
            rs.forEach(r => {
                const v = valueOf(r);
                if (v == null || v === '') return;
                if (!groups.has(v)) groups.set(v, []);
                groups.get(v).push(r.route);
            });
            if (!groups.size) return '';
            const shared = groups.size === 1 && [...groups.values()][0].length === rs.length;
            const cell = shared ? [...groups.keys()][0]
                : [...groups].map(([v, names]) => `<div class="variant">${v} ${routeTag(names)}</div>`).join('');
            return `<tr><td>${label}</td><td>${cell}</td></tr>`;
        }

        // Same idea on one line: "230°M" or "230°M A 600 · 231°M UA 600"
        function inlineVariants(rs, valueOf) {
            const groups = new Map();
            rs.forEach(r => {
                const v = valueOf(r);
                if (v == null) return;
                if (!groups.has(v)) groups.set(v, []);
                groups.get(v).push(r.route);
            });
            if (groups.size === 1 && [...groups.values()][0].length === rs.length) return [...groups.keys()][0];
            return [...groups].map(([v, names]) => `${v}${routeTag(names)}`).join(' · ');
        }

        // Texts printed by several routes, each once; texts that differ only in
        // capitals or spacing count as the same. Returns [[text, routes], ...]
        function groupTexts(rs, listOf) {
            const groups = new Map();
            rs.forEach(r => (listOf(r) || []).forEach(t => {
                const key = t.toLowerCase().replace(/[\s,.;:\/]+/g, '');
                if (!groups.has(key)) groups.set(key, [t, []]);
                const names = groups.get(key)[1];
                if (!names.includes(r.route)) names.push(r.route);
            }));
            return [...groups.values()];
        }
        const tagIfPartial = (names, rs) => names.length < rs.length ? routeTag(names) : '';

        // All layers of all routes as one stack, highest first
        function levelsRow(rs) {
            const layers = new Map();
            rs.forEach(r => (r.layers || []).forEach(l => {
                const key = `${l.upper}|${l.lower}|${l['class']}`;
                if (!layers.has(key)) layers.set(key, { l, routes: [] });
                layers.get(key).routes.push(r.route);
            }));
            const lines = [...layers.values()]
                .sort((a, b) => levelValue(b.l.upper) - levelValue(a.l.upper) ||
                                levelValue(b.l.lower) - levelValue(a.l.lower))
                .map(({ l, routes }) =>
                    `<div class="variant">${escapeHtml(l.upper)} – ${escapeHtml(l.lower)}` +
                    (l['class'] ? ` · Class ${escapeHtml(l['class'])}` : '') +
                    (routes.length < rs.length ? ' ' + routeTag(routes) : '') + '</div>');
            const missing = rs.filter(r => !(r.layers || []).length).map(r => r.route);
            if (missing.length) {
                lines.push(`<div class="variant dim">not published in AIP Maroc` +
                           (missing.length < rs.length ? ' ' + routeTag(missing) : '') + '</div>');
            }
            return `<tr><td>Levels</td><td>${lines.join('')}</td></tr>`;
        }

        function buildRoutePopup(seg) {
            const A = seg.routes[0].from, B = seg.routes[0].to;
            const rs = seg.routes.map(r => orient(r, A, B));
            const a = escapeHtml(A), b = escapeHtml(B);
            let html = `<div class="waypoint-popup route-popup"><h3>${a} – ${b}</h3>` +
                       [...new Set(rs.map(r => r.route))]
                           .map(n => `<span class="badge badge-route">${escapeHtml(n)}</span>`).join('') +
                       '<table>';
            if (rs.some(r => r.track_mag)) {
                const dirLine = (to, i) => `<div class="variant">→ ${to}&nbsp; ` +
                    inlineVariants(rs, r => r.track_mag ? pad3(r.track_mag[i]) + '°M' : null) + '</div>';
                html += `<tr><td>Track</td><td>${dirLine(b, 0)}${dirLine(a, 1)}</td></tr>`;
            }
            if (rs.some(r => r.nav_spec)) {
                html += mergedRow('Nav spec', rs, r => r.nav_spec || 'Conventional');
            }
            html += mergedRow('Distance', rs, r => r.distance_nm != null ? `${r.distance_nm.toFixed(1)} NM` : null);
            html += levelsRow(rs);
            html += mergedRow('Lateral', rs, r => r.width_nm != null ? `${r.width_nm} NM` : null);
            html += mergedRow('Odd FL', rs, r => (r.odd || r.even) ? directionText(r, 'odd') : null);
            html += mergedRow('Even FL', rs, r => (r.odd || r.even) ? directionText(r, 'even') : null);
            html += mergedRow('Control', rs, r => (r.control || []).map(c =>
                `${escapeHtml(c.unit)}` +
                (c.freq ? ` ${escapeHtml(c.freq)}` : '') +
                (c.qualifier ? ` <span class="dim">(${escapeHtml(c.qualifier)})</span>` : '') +
                (c.backup ? `<br><span class="dim">backup ${escapeHtml(c.backup)}</span>` : '')).join('<br>'));
            html += mergedRow('TMA', rs, r => (r.tma || []).map(t =>
                `${escapeHtml(t.name)}<br><span class="dim">` +
                [t.upper && t.lower ? `${escapeHtml(t.upper)} – ${escapeHtml(t.lower)}` : null,
                 t['class'] ? `Class ${escapeHtml(t['class'])}` : null,
                 t.app_freq ? `APP ${escapeHtml(t.app_freq)}` : null].filter(Boolean).join(' · ') +
                '</span>').join('<br>'));
            html += '</table>';
            // remarks as printed (CDR hours, alternate routes…), each once
            groupTexts(rs, r => r.notes).forEach(([n, names]) => {
                html += `<div class="remark">${escapeHtml(n)}${tagIfPartial(names, rs)}</div>`;
            });
            // notes, each once, labelled when only some routes have it
            groupTexts(rs, r => (r.issues || []).filter(i => !SHOWN_ISSUES.test(i))).forEach(([i, names]) => {
                html += `<div class="note">⚠ ${escapeHtml(i)}${tagIfPartial(names, rs)}</div>`;
            });
            // notes printed for the whole route (continuation, CDR hours…)
            const routeNotes = groupTexts(rs, r => r.route_notes);
            if (routeNotes.length) {
                html += '<div class="route-notes"><h5>Route notes</h5>' +
                        routeNotes.map(([n, names]) => `<div>${escapeHtml(n)}${tagIfPartial(names, rs)}</div>`).join('') +
                        '</div>';
            }
            // where it comes from: page, page date, AIRAC amendment
            const sources = groupTexts(rs, r => r.source ? [
                `${r.source.doc || 'ENR 3.1'}-${r.source.page}` + (r.source.date ? ` · ${r.source.date}` : '') +
                (r.source.amdt ? ` · AIRAC AMDT ${r.source.amdt}` : '')] : []);
            if (sources.length) {
                html += '<div class="source">' +
                        sources.map(([t, names]) => `<div>${escapeHtml(t)}${tagIfPartial(names, rs)}</div>`).join('') +
                        '</div>';
            }
            return html + '</div>';
        }

        function refreshRouteStyles() {
            routeSegs.forEach(rec => {
                const hl = rec.names.some(n => routeHover.has(n) || n === routeSearch);
                if (hl === rec.hl) return;
                rec.hl = hl;
                rec.line.setStyle(hl ? ROUTE_STYLE_HL : ROUTE_STYLE);
                if (hl) rec.line.bringToFront();
                const lbl = rec.label && labelEl(rec.label);
                if (lbl) lbl.classList.toggle('route-label-hl', hl);
            });
        }

        // Airway labels: shown from one zoom step past the overview, placed by
        // the label declutter after all fix names (so they never cover one).
        const ROUTE_LABEL_MIN_ZOOM = map.getZoom() + 1;
        const ROUTE_LABEL_PRIORITY = 30;   // fixes are 0–20
        const routeLabelLayer = L.layerGroup().addTo(map);
        const routeLabels = [];

        ((routes && routes.segments) || []).forEach(seg => {
            const ll = seg.coords.map(c => [c[1], c[0]]);
            const line = L.polyline(ll, { ...ROUTE_STYLE, interactive: false });
            const hit  = L.polyline(ll, { color: '#000', opacity: 0, weight: 10, className: 'route-hit' });
            const names = [...new Set(seg.routes.map(r => r.route))];
            const rec = { names, line, hit, group: L.layerGroup([line, hit]),
                          popupHtml: buildRoutePopup(seg), hl: false,
                          sectors: sectorsOfLine(seg.coords) };
            hit.bindTooltip(names.join(' · '), {
                sticky: true, direction: 'top', offset: [0, -8], className: 'route-tooltip'
            });
            hit.on('mouseover', () => { names.forEach(n => routeHover.add(n)); refreshRouteStyles(); });
            hit.on('mouseout',  () => { names.forEach(n => routeHover.delete(n)); refreshRouteStyles(); });
            hit.on('click', e => {
                // In RBL mode a click on a route counts as a click on the map
                if (document.body.classList.contains('rbl-active')) return;
                L.DomEvent.stopPropagation(e);
                // keep the popup clear of the search box at the top
                L.popup({ closeButton: true, maxWidth: 360, autoPanPaddingTopLeft: [20, 70] })
                    .setLatLng(e.latlng)
                    .setContent(rec.popupHtml)
                    .openOn(map);
            });
            names.forEach(n => (segsByRoute[n] = segsByRoute[n] || []).push(rec));
            routeSegs.push(rec);

            const mid = [(ll[0][0] + ll[ll.length - 1][0]) / 2, (ll[0][1] + ll[ll.length - 1][1]) / 2];
            rec.label = {
                seg: rec, center: true, priority: ROUTE_LABEL_PRIORITY, name: names.join(' '), text: null,
                marker: L.marker(mid, {
                    icon: L.divIcon({ html: '<span class="atc-label route-label"></span>',
                                      className: 'atc-marker-wrapper', iconSize: [14, 14], iconAnchor: [7, 7] }),
                    interactive: false, keyboard: false
                })
            };
            routeLabels.push(rec.label);
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
            // Highlighted (yellow) fixes always show their label
            rec[cls === 'rbl-selected' ? 'rblSelected' : 'rblHover'] = on;
            scheduleDeclutter();
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
        const filterNavaid = document.getElementById('filter-navaid');
        const filterApproach = document.getElementById('filter-approach');
        const filterLabels = document.getElementById('filter-labels');
        const filterRoutes      = document.getElementById('filter-routes');
        const filterRoutesLower = document.getElementById('filter-routes-lower');
        const filterRoutesUpper = document.getElementById('filter-routes-upper');
        const filterRoutesConv  = document.getElementById('filter-routes-conv');
        const filterRoutesRnav  = document.getElementById('filter-routes-rnav');
        const routeTypes = (routes && routes.types) || {};
        const filterRouteNames  = document.getElementById('filter-route-names');

        function routeShownByFilters(n) {
            return n === routeSearch || (filterRoutes.checked &&
                (isUpperRoute(n) ? filterRoutesUpper.checked : filterRoutesLower.checked) &&
                (routeTypes[n] === 'rnav' ? filterRoutesRnav.checked : filterRoutesConv.checked));
        }

        // Label text = the visible routes on that segment, e.g. "A857 · UN857"
        function updateRouteLabels() {
            const on = filterRouteNames.checked && map.getZoom() >= ROUTE_LABEL_MIN_ZOOM;
            routeLabels.forEach(rec => {
                const text = on && routesLayer.hasLayer(rec.seg.group)
                    ? rec.seg.names.filter(routeShownByFilters).map(n => n.replace(/\s+/g, '')).join(' · ')
                    : '';
                if (text) {
                    if (!routeLabelLayer.hasLayer(rec.marker)) routeLabelLayer.addLayer(rec.marker);
                    const lbl = labelEl(rec);
                    if (lbl && rec.text !== text) { lbl.textContent = text; rec.text = text; }
                    if (lbl) lbl.classList.toggle('route-label-hl', rec.seg.hl);
                } else if (routeLabelLayer.hasLayer(rec.marker)) {
                    routeLabelLayer.removeLayer(rec.marker);
                    rec.text = null;
                }
            });
        }
        const searchInput  = document.getElementById('search');

        // ─── Sector filter: tick sectors → only what lies inside them ──
        // Nothing ticked = no sector filter. A searched route is still shown whole.
        const selectedSectors = new Set();
        const sectorClear = document.getElementById('sector-clear');
        const sectorHint  = document.getElementById('sector-hint');
        const inSelectedSectors = rec => !selectedSectors.size || rec.sectors.some(i => selectedSectors.has(i));

        function refreshSectorStyles() {
            sectorList.forEach((s, i) => {
                const on = selectedSectors.has(i);
                s.layer.setStyle(!selectedSectors.size ? SECTOR_STYLE : on ? SECTOR_STYLE_SEL : SECTOR_STYLE_OFF);
                s.label.setOpacity(!selectedSectors.size || on ? 1 : 0.35);
            });
            sectorClear.hidden = !selectedSectors.size;
            sectorHint.hidden  = !!selectedSectors.size;
        }

        if (sectorList.length) {
            document.getElementById('sector-filter-box').hidden = false;
            const box = document.getElementById('sector-filters');
            sectorList.forEach((s, i) => {
                const label = document.createElement('label');
                label.className = 'filter-item';
                label.title = s.name;
                label.innerHTML = `<input type="checkbox" autocomplete="off" /><span>${escapeHtml(s.short)}</span>`;
                const cb = label.querySelector('input');
                cb.addEventListener('change', () => {
                    if (cb.checked) selectedSectors.add(i); else selectedSectors.delete(i);
                    refreshSectorStyles();
                    applyFilters();
                });
                s.checkbox = cb;
                box.appendChild(label);
            });
            sectorClear.addEventListener('click', () => clearSectorFilter());
        }

        function clearSectorFilter() {
            selectedSectors.clear();
            sectorList.forEach(s => { s.checkbox.checked = false; });
            refreshSectorStyles();
            applyFilters();
        }

        function applyFilters() {
            const show = {
                ifr:    filterIfr.checked,
                vfr:      filterVfr.checked,
                approach: filterApproach.checked,
                navaid:   filterNavaid.checked,
            };
            const search = searchInput.value.toUpperCase().trim();

            // A search for a route designator ("UG5", "a 600 f") highlights
            // that route and keeps the fixes along it visible.
            routeSearch = search ? (Object.keys(segsByRoute).find(d => normRoute(d) === normRoute(search)) || null) : null;
            const onRoute = new Set(routeSearch ? routePoints[routeSearch] || [] : []);
            refreshRouteStyles();
            routeSegs.forEach(rec => {
                const visible = rec.names.includes(routeSearch) ||
                                (rec.names.some(routeShownByFilters) && inSelectedSectors(rec));
                if (visible && !routesLayer.hasLayer(rec.group)) routesLayer.addLayer(rec.group);
                if (!visible && routesLayer.hasLayer(rec.group)) routesLayer.removeLayer(rec.group);
            });
            updateRouteLabels();

            markers.forEach(rec => {
                const categoryOk = show[rec.category];
                const alongRoute = onRoute.has(rec.name) || (rec.idents || []).some(i => onRoute.has(i));
                const searchOk = !search || rec.search.includes(search) || alongRoute;
                const sectorOk = alongRoute || inSelectedSectors(rec);
                const layer = layers[rec.category];
                if (categoryOk && searchOk && sectorOk) {
                    if (!layer.hasLayer(rec.marker)) layer.addLayer(rec.marker);
                } else {
                    if (layer.hasLayer(rec.marker)) layer.removeLayer(rec.marker);
                }
            });
            scheduleDeclutter();
        }

        function applyLabelVisibility() {
            document.body.classList.toggle('labels-hidden', !filterLabels.checked);
            scheduleDeclutter();
        }

        filterIfr.addEventListener('change',    applyFilters);
        filterVfr.addEventListener('change',    applyFilters);
        filterNavaid.addEventListener('change', applyFilters);
        filterApproach.addEventListener('change', applyFilters);
        filterRoutes.addEventListener('change', applyFilters);
        filterRoutesLower.addEventListener('change', applyFilters);
        filterRoutesUpper.addEventListener('change', applyFilters);
        filterRouteNames.addEventListener('change', () => { updateRouteLabels(); scheduleDeclutter(); });
        filterRoutesConv.addEventListener('change', applyFilters);
        filterRoutesRnav.addEventListener('change', applyFilters);
        filterLabels.addEventListener('change', applyLabelVisibility);
        searchInput.addEventListener('input',   applyFilters);

        // Enter to zoom to first match
        searchInput.addEventListener('keypress', e => {
            if (e.key !== 'Enter') return;
            const term = searchInput.value.toUpperCase().trim();
            if (!term) return;
            const exact = markers.find(m => m.name === term ||
                                            (m.idents && m.idents.includes(term)));
            // A route designator zooms to the whole route
            if (!exact && routeSearch) {
                const bounds = L.latLngBounds([]);
                segsByRoute[routeSearch].forEach(rec => bounds.extend(rec.line.getBounds()));
                map.flyToBounds(bounds, { padding: [60, 60], duration: 0.6 });
                return;
            }
            const partial = exact || markers.find(m => m.search.includes(term));
            if (partial) {
                // If the fix's category is filtered out (e.g. approach-only),
                // switch that filter on so the fix is actually visible
                const box = {
                    ifr: filterIfr, vfr: filterVfr,
                    approach: filterApproach, navaid: filterNavaid
                }[partial.category];
                if (box && !box.checked) {
                    box.checked = true;
                    applyFilters();
                }
                // Same if it lies outside the ticked sectors
                if (!inSelectedSectors(partial)) clearSectorFilter();
                map.flyTo(partial.marker.getLatLng(), 9, { duration: 0.6 });
                if (!document.body.classList.contains('rbl-active')) {
                    L.popup({ closeButton: true, maxWidth: 320 })
                        .setLatLng(partial.marker.getLatLng())
                        .setContent(partial.popupHtml)
                        .openOn(map);
                }
            }
        });

        // ─── Label decluttering ────────────────────────────────────────
        // Labels are placed in priority order (transfer points → navaids → IFR
        // → VFR). A label goes to the right of its symbol; if that overlaps a
        // label already placed, high-level fixes try above, then below the
        // symbol. Once a label has moved it keeps that spot at every zoom level
        // (while it fits). If no spot is free the label is hidden (the symbol
        // always stays). Symbols are not obstacles. Re-run after every zoom.
        function labelEl(rec) {
            const el = rec.marker.getElement();
            return el ? el.querySelector('.atc-label') : null;
        }

        function rectsOverlap(a, b, pad = 2) {
            return !(a.right + pad < b.left || b.right + pad < a.left ||
                     a.bottom + pad < b.top || b.bottom + pad < a.top);
        }

        // Screen rect of a label in a given position, from the 14px icon box
        // (matches the CSS: right = left 11px / top -3px; above / below =
        // centred, top -15px / +15px).
        const LABEL_TOP = { right: -3, above: -15, below: 15 };
        function labelRectAt(box, w, h, pos) {
            if (pos === 'center') {   // airway labels sit centred on the icon point
                const left = box.left + 7 - w / 2, top = box.top + 7 - h / 2;
                return { left: left, top: top, right: left + w, bottom: top + h };
            }
            const left = pos === 'right' ? box.left + 11 : box.left + 7 - w / 2;
            const top  = box.top + LABEL_TOP[pos];
            return { left: left, top: top, right: left + w, bottom: top + h };
        }

        // Positions to try, in order
        function candidatePositions(rec) {
            if (rec.center) return ['center'];
            if (rec.fixedAbove) return ['above'];
            if (!rec.canMoveUp) return ['right'];
            const all = ['right', 'above', 'below'];
            // A label that moved before tries its last spot first
            return rec.stickyPos ? [rec.stickyPos, ...all.filter(p => p !== rec.stickyPos)] : all;
        }

        // Part of a screen segment inside the map view (Liang–Barsky clipping)
        function clipToView(a, b, pad) {
            const size = map.getSize();
            const [x0, y0, x1, y1] = [pad, pad, size.x - pad, size.y - pad];
            const dx = b.x - a.x, dy = b.y - a.y;
            let t0 = 0, t1 = 1;
            for (const [p, q] of [[-dx, a.x - x0], [dx, x1 - a.x], [-dy, a.y - y0], [dy, y1 - a.y]]) {
                if (p === 0) { if (q < 0) return null; continue; }
                const t = q / p;
                if (p < 0) { if (t > t1) return null; if (t > t0) t0 = t; }
                else       { if (t < t0) return null; if (t < t1) t1 = t; }
            }
            return [L.point(a.x + t0 * dx, a.y + t0 * dy), L.point(a.x + t1 * dx, a.y + t1 * dy)];
        }

        // Each airway label goes to the middle of the visible part of its line
        function placeRouteLabels() {
            routeLabels.forEach(rec => {
                if (!routeLabelLayer.hasLayer(rec.marker)) return;
                const ll = rec.seg.line.getLatLngs();
                const part = clipToView(map.latLngToContainerPoint(ll[0]),
                                        map.latLngToContainerPoint(ll[ll.length - 1]), 20);
                rec.visibleLength = part ? part[0].distanceTo(part[1]) : 0;
                if (part) rec.marker.setLatLng(map.containerPointToLatLng(part[0].add(part[1]).divideBy(2)));
            });
        }

        function declutter() {
            placeRouteLabels();
            const fixes = markers.filter(r => layers[r.category].hasLayer(r.marker));
            const visible = (filterLabels.checked ? fixes : [])
                .concat(routeLabels.filter(r => routeLabelLayer.hasLayer(r.marker)));
            if (!visible.length) return;
            // Write phase: reset classes. Read phase: measure. Then decide.
            visible.forEach(rec => {
                const lbl = labelEl(rec);
                if (lbl) lbl.classList.remove('declutter-hidden', 'hover-suppressed', 'hover-reveal');
            });
            visible.forEach(rec => {
                const el = rec.marker.getElement();
                const lbl = labelEl(rec);
                if (!el || !lbl) { rec.measure = null; return; }
                const r = lbl.getBoundingClientRect();
                rec.measure = { box: el.getBoundingClientRect(), w: r.width, h: r.height };
            });
            // Fixes highlighted in RBL mode (yellow) come first and always show
            const isHighlighted = r => !!(r.rblSelected || r.rblHover);
            const rank = r => isHighlighted(r) ? -Infinity : r.priority;
            visible.sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
            const placed = [];
            // airway labels also keep clear of fix symbols
            const symbols = fixes.map(r => r.marker.getElement())
                .filter(Boolean).map(el => el.querySelector('.atc-fix').getBoundingClientRect());
            visible.forEach(rec => {
                const m = rec.measure;
                if (!m) { rec.labelRect = null; return; }
                if (rec.center) {
                    const rect = labelRectAt(m.box, m.w, m.h, 'center');
                    // visible part too short for its label, or something already there
                    if (rec.visibleLength < m.w + 30 || placed.some(r => rectsOverlap(r, rect)) ||
                        symbols.some(r => rectsOverlap(r, rect, 1))) {
                        labelEl(rec).classList.add('declutter-hidden');
                        rec.labelRect = null;
                    } else {
                        placed.push(rect);
                        rec.labelRect = rect;
                    }
                    return;
                }
                const options = candidatePositions(rec);
                let pos = isHighlighted(rec) ? options[0] : null;
                if (!pos) {
                    pos = options.find(p => !placed.some(r =>
                        rectsOverlap(r, labelRectAt(m.box, m.w, m.h, p))));
                }
                const lbl = labelEl(rec);
                // Hidden labels keep their preferred spot, used when hovered
                const shownPos = pos || options[0];
                lbl.classList.toggle('above', shownPos === 'above');
                lbl.classList.toggle('below', shownPos === 'below');
                rec.labelRect = labelRectAt(m.box, m.w, m.h, shownPos);
                if (pos) {
                    placed.push(rec.labelRect);
                    if (pos !== 'right' && !rec.fixedAbove) rec.stickyPos = pos;
                } else {
                    lbl.classList.add('declutter-hidden');
                }
            });
        }

        let declutterQueued = false;
        function scheduleDeclutter() {
            if (declutterQueued) return;
            declutterQueued = true;
            requestAnimationFrame(() => { declutterQueued = false; declutter(); });
        }

        // Hovering a fix whose label was hidden: show it, and hide the labels
        // it overlaps until the mouse leaves.
        function revealLabel(rec) {
            const lbl = labelEl(rec);
            if (!lbl || !rec.labelRect || !lbl.classList.contains('declutter-hidden')) return;
            lbl.classList.add('hover-reveal');
            rec.suppressed = markers.filter(o => {
                if (o === rec || !o.labelRect || !layers[o.category].hasLayer(o.marker)) return false;
                const ol = labelEl(o);
                return ol && !ol.classList.contains('declutter-hidden') &&
                       rectsOverlap(o.labelRect, rec.labelRect);
            });
            rec.suppressed.forEach(o => labelEl(o).classList.add('hover-suppressed'));
        }

        function unrevealLabel(rec) {
            const lbl = labelEl(rec);
            if (lbl) lbl.classList.remove('hover-reveal');
            (rec.suppressed || []).forEach(o => {
                const ol = labelEl(o);
                if (ol) ol.classList.remove('hover-suppressed');
            });
            rec.suppressed = null;
        }

        map.on('zoomend', () => { updateRouteLabels(); scheduleDeclutter(); });
        map.on('moveend', scheduleDeclutter);   // airway labels follow the visible part of their line
        window.addEventListener('resize', scheduleDeclutter);
        // Initial pass: apply the filters and the Fix Name box as they are,
        // then declutter again once the label font has loaded (label widths
        // change when JetBrains Mono replaces the fallback font).
        applyLabelVisibility();
        applyFilters();
        if (document.fonts && document.fonts.ready) {
            document.fonts.ready.then(scheduleDeclutter);
        }
        window.addEventListener('load', scheduleDeclutter);

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
               borders_data=None, sectors_data=None, navaids_data=None,
               routes_data=None, title="Infrastructure", subtitle="FIR"):
    """Create a standalone HTML map file from waypoint + borders + sectors + navaids + routes GeoJSON."""
    features = geojson_data.get("features", [])
    bbox = waypoint_bbox(features)
    view = {
        "min_lon": bbox[0] - VIEW_PADDING_DEG,
        "min_lat": bbox[1] - VIEW_PADDING_DEG,
        "max_lon": bbox[2] + VIEW_PADDING_DEG,
        "max_lat": bbox[3] + VIEW_PADDING_DEG,
    }
    stats = compute_stats(features)
    waypoints_out = prepare_waypoints(geojson_data)
    navaids_out = prepare_navaids(navaids_data)
    routes_out = prepare_routes(routes_data, geojson_data, navaids_data)
    if routes_out:
        waypoints_out["features"] += routes_out.pop("extra_points")
    add_route_point_details(routes_data, waypoints_out, navaids_out)
    stats["navaid"] = len((navaids_out or {}).get("features", []))
    stats["routes"] = routes_out["count"] if routes_out else 0
    stats["approach"] = sum(1 for f in waypoints_out["features"]
                            if f["properties"]["_class"] == "approach")

    # Crop borders to waypoint extent + padding to keep the embedded payload small
    cropped_borders = crop_geojson_to_bbox(borders_data, bbox, padding=BORDERS_CROP_PADDING_DEG)

    payload = {
        "waypoints": waypoints_out,
        "borders":   cropped_borders,
        "sectors":   sectors_data,
        "navaids":   navaids_out,
        "routes":    routes_out,
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
        "navaids":   stats["navaid"],
        "routes":    stats["routes"],
        "route_segments": len(routes_out["segments"]) if routes_out else 0,
        "view":      view,
        "stats":     stats,
    }


# ─── CLI ───────────────────────────────────────────────────────────────────

def main(geojson_path, output_path="waypoints_map.html",
         borders_path=None, sectors_path=None, navaids_path=None, routes_path=None):
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

    navaids_data = load_geojson_optional(navaids_path or DEFAULT_NAVAIDS_PATH)
    if navaids_data:
        print(f"Loaded navaids:    {len(navaids_data.get('features', []))} features")
    else:
        print("Loaded navaids:    none")

    routes_data = load_geojson_optional(routes_path or DEFAULT_ROUTES_PATH)
    if routes_data:
        print(f"Loaded routes:     {len(routes_data.get('features', []))} features")
    else:
        print("Loaded routes:     none (run route_parser.py to make routes.geojson)")

    print(f"\nGenerating map → {output_path}")
    result = create_map(geojson_data, output_path, borders_data, sectors_data, navaids_data,
                        routes_data)

    print()
    print(f"  Stats:     IFR={result['stats']['ifr']}  "
          f"VFR={result['stats']['vfr']}  "
          f"Total={result['stats']['total']}")
    print(f"  View:      lat [{result['view']['min_lat']:.2f}, {result['view']['max_lat']:.2f}], "
          f"lon [{result['view']['min_lon']:.2f}, {result['view']['max_lon']:.2f}]")
    print(f"  Borders:   {result['borders_after']}/{result['borders_before']} kept after "
          f"{BORDERS_CROP_PADDING_DEG}° crop")
    print(f"  Sectors:   {result['sectors']}")
    print(f"  Navaids:   {result['navaids']} (marine NDBs left off)")
    print(f"  Routes:    {result['routes']} ATS routes on {result['route_segments']} drawn segments")
    print(f"  Approach:  {result['stats']['approach']} approach-only fixes (hidden by default)")
    print()
    print(f"Output: {output_path}")
    print("─" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python page_generator.py <waypoints.geojson> "
              "[output.html] [borders.geojson] [sectors.geojson] [navaids.geojson] [routes.geojson]")
        sys.exit(1)

    waypoints_arg = sys.argv[1]
    output_arg    = sys.argv[2] if len(sys.argv) > 2 else "waypoints_map.html"
    borders_arg   = sys.argv[3] if len(sys.argv) > 3 else None
    sectors_arg   = sys.argv[4] if len(sys.argv) > 4 else None
    navaids_arg   = sys.argv[5] if len(sys.argv) > 5 else None
    routes_arg    = sys.argv[6] if len(sys.argv) > 6 else None

    if not Path(waypoints_arg).exists():
        print(f"Error: waypoints file not found: {waypoints_arg}")
        sys.exit(1)

    main(waypoints_arg, output_arg, borders_arg, sectors_arg, navaids_arg, routes_arg)
