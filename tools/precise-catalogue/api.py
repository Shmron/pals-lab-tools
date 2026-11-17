from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import duckdb
import re
import io
import os
import json
import sys
import time
import threading
import smtplib
import hashlib
import gzip
import zipfile
import tempfile
from functools import lru_cache
from email.message import EmailMessage
from collections import defaultdict
sys.path.insert(0, '/home/USER/PRECISE')
import access_db
import accessibility_engine as _access_engine
import pyarrow.ipc as _ipc

app = Flask(__name__)
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB — bounds geodata AOI uploads

DB_PATH        = '/home/USER/PRECISE/precise.duckdb'
SENSOR_DB_PATH = '/home/USER/PRECISE/sensor.duckdb'
FIRE_SMOKE_DB_PATH = '/home/USER/PRECISE/derived_fire_smoke_exceedance.duckdb'
_QUERY_LOG     = '/home/USER/PRECISE/logs/shmron_queries.jsonl'


def _log_query(session_token, cat_user_id, tool, **kwargs):
    """Append one audit entry to the query log. Never raises."""
    try:
        os.makedirs(os.path.dirname(_QUERY_LOG), exist_ok=True)
        session_hash = hashlib.sha256(session_token.encode()).hexdigest()[:12] if session_token else 'anon'
        entry = {
            'ts':          time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'session':     session_hash,
            'cat_user_id': cat_user_id,
            'tool':        tool,
            **kwargs,
        }
        with open(_QUERY_LOG, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass

SMTP_FROM    = 'admin@example.org'
SMTP_PASS    = os.environ.get('SMTP_APP_PASSWORD', '')
ADMIN_EMAILS = [
    'admin@example.org',
    'admin@example.org',
    'admin@example.org',
]
PORTAL_URL   = 'https://portal.placealert.org'


def _send_email(to, subject, body):
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From']    = SMTP_FROM
        msg['To']      = to
        msg.set_content(body)
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(SMTP_FROM, SMTP_PASS)
            s.send_message(msg)
    except Exception:
        pass  # never let email failure break the response

CATALOGUE_ACCESS_CODE = os.environ.get('CATALOGUE_ACCESS_CODE', 'PRECISE2024')

# ── IP rate limiter for the public portal chat ────────────────────────────────
_ip_hits: dict = defaultdict(list)
_RATE_WINDOW = 60    # seconds
_RATE_LIMIT  = 20    # requests per window

def _allow_ip(ip: str) -> bool:
    now  = time.time()
    hits = [t for t in _ip_hits[ip] if now - t < _RATE_WINDOW]
    if len(hits) >= _RATE_LIMIT:
        _ip_hits[ip] = hits
        return False
    hits.append(now)
    _ip_hits[ip] = hits
    return True

# ── Portal chat system prompt (informational only, no DB access) ──────────────
_PORTAL_SYSTEM = """You are the Place Alert Labs (PALs) portal assistant at portal.placealert.org — a friendly, knowledgeable guide for visitors who may know nothing about PALs yet.

STYLE:
- Answer in ONE short paragraph: a sentence of context, then naturally weave in the relevant resource(s) by name with a short, plain-language description of each — not an extensive narrative per item, not a bulleted rundown. Think "here's what's relevant and why," not documentation.
- Only use an actual markdown list if the user is explicitly comparing or scanning a longer set of items (4+). Otherwise keep it prose.
- Keep it tight — a few sentences total covers almost everything. Only go longer if the user is clearly asking for depth on one specific thing.
- Every link MUST be a full absolute URL starting with "https://" written as markdown, e.g. [PALSearth](https://portal.placealert.org/#tool-palsearth).
- CRITICAL — always link to the tool's CARD on the portal page (the anchor URLs given below, e.g. https://portal.placealert.org/#tool-palsearth), never to the tool's own live app/external URL directly. Clicking should take the visitor to the card so they can read about it and click through themselves — it should never open the tool itself.
- Never mention Anthropic, Claude, or that you are an AI model.

ABOUT PALs: Place Alert Labs (PALs) is a research initiative based at Midlands State University (MSU) and CeSHHAR Zimbabwe, advancing geographically precise public health through climate, environmental, and geospatial intelligence. Funded by NIH, Wellcome Trust, Gates Foundation, UKRI/GCRF, Grand Challenges Canada, and Google. In Zimbabwe, the team's geocoding programme produced the Zimbabwe Address Database (ZAD), documentation for which is coming soon.
Team: Prestige Tatenda Makanga (lead, MSU & CeSHHAR), Upenyu Mupfiga (MSU), Liberty Makacha (MSU), Terrence Mushore (MSU), Reason Mlambo (MSU), Bongani Nyoni (MSU), Anotida Chapunza (MSU), Tendai Shangwe (MSU), Cherlynn Dumbura (CeSHHAR), Zororo Chinwadzimba (CeSHHAR), Laura Munthali (CeSHHAR), Perkins Watambwa (CeSHHAR), Rutendo Sibanda (CeSHHAR).

WEBSITE (https://placealert.org) — public-facing organisation website with about, research, team, news, and partners pages.
PORTAL (https://portal.placealert.org) — this open landing page with links to all research tools. No login required to view.

POLICY & DECISION SUPPORT — catalogues and dashboards built for evidence-based decisions:
- HE2AT Environmental Exposure Catalogue (card: https://portal.placealert.org/#policy-decision-support): 35+ climate and environmental exposure variables for heat-health impacts across Sub-Saharan Africa. Covers 9 countries and 74,000+ participants.
- PRECISE Catalogue (card: https://portal.placealert.org/#tool-precise-catalogue): catalogue of Environmental & Social Determinants of Maternal Health across Kenya, Mozambique, and The Gambia. Requires an access code — use the "Request Access" tab on the login screen. Includes an AI research assistant ("Shmron") for querying the participant dataset.
- Landscape Fire Smoke & Pregnancy Risk (card: https://portal.placealert.org/#tool-firesmoke): maps daily wildfire smoke (PM2.5) across Africa and links it to pregnancy risk.
- Africa Road Network Density Map (card: https://portal.placealert.org/#tool-roadnet): H3 hexagon map of road density across Africa (OpenStreetMap-derived) — a proxy for traffic-related air pollution. Has a built-in "Analyze my area" tool (draw a shape or upload a shapefile/GeoJSON, no API key needed) for a one-off population-weighted density value for your own area, and an "API" button for programmatic/repeated use — that same panel has an inline form to request a personal API token, no separate portal card or sign-up page needed.
- Africa Road Proximity Map (card: https://portal.placealert.org/#tool-roadproximity): distance from any location to the nearest highway/major road — doubles as a pollution-exposure and accessibility metric. Same built-in "Analyze my area" tool and in-page API-token request form as the density map, no API key needed just to try it.

TOOLS — hands-on research tools:
- PALSearth (card: https://portal.placealert.org/#tool-palsearth): point-and-extract environmental data (NDVI, temperature, rainfall, soil, air quality, elevation) for any location and date, powered by Google Earth Engine.
- GIPEX (card: https://portal.placealert.org/#tool-gipex): generates satellite-derived environmental exposure indicators across custom grid cells or study areas.
- SPECTRA (card: https://portal.placealert.org/#tool-spectra): turns GPS tracks and wearable-sensor data into personal, minute-by-minute environmental exposure (indoor/outdoor classification).
- HarmonAIze (card: https://portal.placealert.org/#tool-harmonaize): AI-assisted toolkit for harmonising heterogeneous climate and health datasets — variable mapping, unit conversion, schema unification.
- Zimbabwe Address Database (ZAD) (card: https://portal.placealert.org/#tool-zad): a geocoding registry from PALs' Zimbabwe fieldwork. Documentation and open access are coming soon — for now, direct interested users to get in touch via the portal's contact section.

DATA ACCESS — two independent ways to query the same underlying PRECISE Big Table (~3.1M rows), maintained by different team members. Point people to whichever fits their tooling:
- Big Table API Access (card: https://portal.placealert.org/#tool-bigtable-api): request a personal API token for authenticated Postgres queries from any HTTP client or notebook.
- Fast Query Access — DuckDB (card: https://portal.placealert.org/#tool-duckdb-access): request a DuckDB connection snippet (Python or R) for fast in-process analytical queries, ideal for large queries inside the Hub.

WORKFLOW — PALSlab Hub (card: https://portal.placealert.org/#tool-palslab-hub): the shared JupyterHub analysis environment for the research team (Python & R kernels). Sign up by clicking "Sign up" and providing a username, password, and email — an admin must then approve the account (approval email follows). Forgot password: use "Forgot your password?" on the login page. Once in, your home folder has a shared PALS/ directory.

COURSES:
- GeoAI for Climate, Environment & Health (card: https://portal.placealert.org/#tool-geoai-course): a free, self-paced, open-enrolment course on geospatial data science, satellite-derived environmental exposures, and AI-powered data querying, built around real Zimbabwe DHS 2015 birth records. ~3 hours across 6 browser-based Jupyter notebook modules, no local setup or prior GIS experience required. Developed by CeSHHAR, Place Alert Labs, and MSU. Modules: (1) Spatial Health Data — Zimbabwe DHS 2015, (2) Environmental Datasets — Zimbabwe's Climate in Context, (3) Spatial Join — Linking Births to Environmental Data, (4) Exposure Windows — When During Pregnancy Matters, (5) Earth Engine Concepts & Pre-extracted Data, (6) Characterisation, EDA & GeoAI (capstone — builds a logistic regression model and queries the dataset with a natural-language GeoAI assistant). Open to surveyors, GIS analysts, researchers, students, and public health professionals.
- Health Geography — A Short Course (card: https://portal.placealert.org/#tool-health-geography): free course with certificate (80% quiz), ~2 hours, run with PRECISE Network (King's College London) and The Global Health Network Training Centre (Oxford). How geography mediates health outcomes and how to design more precise, locally-tailored public health interventions.
- Geographical Access in Healthcare — A Short Course (card: https://portal.placealert.org/#tool-geo-access-healthcare): free course with certificate (80% quiz), ~50 minutes, same partners as above. How GIS quantifies spatial access to care and supports evidence-based health service planning.
- A few more courses (SPECTRA, GIPEX, Spatial Epidemiology with R, Earth Engine for Health Research) are in development and not yet open for enrolment — mention them only if asked, and don't link them since there's nowhere to click through to yet.
- Additional/future training listings live on the main PALs website's Courses page (https://placealert.org/courses.html) — this one is an external page, not a portal card, so link it directly.

For technical issues or account problems, point the user to the portal's "Get in touch" contact section."""


# ── Security helpers ──────────────────────────────────────────────────────────

def get_api_key():
    """Extract API key from X-API-Key header only (not URL params — avoids nginx log exposure)."""
    return (request.headers.get('X-API-Key') or
            (request.get_json(silent=True) or {}).get('api_key'))


def require_key():
    """Returns (key_info dict, None) or (None, error response).
    Accepts either a researcher DB API key (X-API-Key) or a catalogue
    session token (X-Session-Token) issued by /api/catalogue-login.
    """
    token = request.headers.get('X-Session-Token', '')
    if token and access_db.validate_catalogue_token(token):
        return {'countries': ['Kenya', 'Mozambique', 'Gambia'], 'name': 'catalogue'}, None

    key = get_api_key()
    if not key:
        return None, (jsonify({'error': 'API key required. Include X-API-Key header.'}), 401)
    info = access_db.validate_key(key)
    if not info:
        return None, (jsonify({'error': 'Invalid or revoked API key.'}), 403)
    return info, None


# ── Geodata API (roadnet / euclidean) ──────────────────────────────────────────
# Any valid, active API key (from the existing researcher key system) may use
# these routes — the per-key `countries` ACL is about cohort-data access and
# doesn't apply here: this is public pan-African infrastructure data, not
# participant data.

import h3
from shapely.geometry import shape, Point
from shapely.prepared import prep
from shapely.ops import unary_union

PRECISE_ROOT = '/home/USER/PRECISE'

GEODATA_TOOLS = {
    'roadnet': {
        'cells_dir':  os.path.join(PRECISE_ROOT, 'roadnet', 'data', 'countries'),
        'admin_dir':  os.path.join(PRECISE_ROOT, 'roadnet', 'data', 'pwrnd'),
        'value_fields': ['r'],
    },
    'euclidean': {
        'cells_dir':  os.path.join(PRECISE_ROOT, 'euclidean', 'data', 'countries'),
        'admin_dir':  os.path.join(PRECISE_ROOT, 'euclidean', 'data', 'pwdist'),
        'value_fields': ['hw', 'mr'],
    },
}
_COUNTRY_BOUNDS_PATH = os.path.join(PRECISE_ROOT, 'roadnet', 'data', 'africa_bounds.geojson')
_GEO_MAX_AREA_KM2 = 50_000   # neighborhood/study-area scale, not whole-country dumps
_GEO_MAX_COUNTRIES = 5
_GEO_MAX_ZIP_UNCOMPRESSED = 50 * 1024 * 1024  # 50 MB, protects against zip bombs

_country_bounds_cache = None

def _load_country_bounds():
    """List of {'iso3','name','geom','prepared'} for all 57 covered countries.
    Loaded once from roadnet's africa_bounds.geojson and shared by both tools
    (euclidean covers the identical country set but has no bounds file of its
    own)."""
    global _country_bounds_cache
    if _country_bounds_cache is None:
        with open(_COUNTRY_BOUNDS_PATH) as f:
            gj = json.load(f)
        out = []
        for feat in gj['features']:
            props = feat.get('properties', {})
            geom = shape(feat['geometry'])
            out.append({
                'iso3': props.get('ISO'),
                'name': props.get('name'),
                'geom': geom,
                'prepared': prep(geom),
            })
        _country_bounds_cache = out
    return _country_bounds_cache


def _countries_intersecting(aoi_geom):
    """ISO3 codes of countries whose boundary intersects the AOI, bbox-prefiltered."""
    ax0, ay0, ax1, ay1 = aoi_geom.bounds
    matches = []
    for c in _load_country_bounds():
        if not c['iso3']:
            continue
        cx0, cy0, cx1, cy1 = c['geom'].bounds
        if ax1 < cx0 or ax0 > cx1 or ay1 < cy0 or ay0 > cy1:
            continue
        if c['prepared'].intersects(aoi_geom):
            matches.append(c['iso3'])
    return matches


@lru_cache(maxsize=40)
def _load_country_cells(tool, iso3):
    """Gunzip+parse one country's H3 cell grid. Cached — files are small
    (a few thousand cells) and re-requested often for the same country."""
    path = os.path.join(GEODATA_TOOLS[tool]['cells_dir'], f'{iso3}.json.gz')
    if not os.path.exists(path):
        return {}
    with gzip.open(path) as f:
        return json.load(f)


def _select_cells(tool, iso3, aoi_prepared):
    """Cells whose centroid falls inside the AOI. Centroid-in-polygon is a
    good approximation at H3 res-7 (~5 km² hexes) for neighborhood/study-area
    scale AOIs, and far cheaper than true polygon clipping."""
    cells = _load_country_cells(tool, iso3)
    selected = []
    for cell_id, rec in cells.items():
        lat, lon = h3.cell_to_latlng(cell_id)
        if aoi_prepared.contains(Point(lon, lat)):
            selected.append((cell_id, rec, lat, lon))
    return selected


def _weighted_stats(selected, fields):
    n = len(selected)
    total_pop = sum(float(rec.get('p', 0) or 0) for _, rec, _, _ in selected)
    result = {'n_cells': n, 'total_population': round(total_pop, 1)}
    for f in fields:
        weighted_sum = sum(float(rec.get(f, 0) or 0) * float(rec.get('p', 0) or 0)
                            for _, rec, _, _ in selected)
        plain_sum = sum(float(rec.get(f, 0) or 0) for _, rec, _, _ in selected)
        result[f'{f}_pop_weighted_mean'] = round(weighted_sum / total_pop, 4) if total_pop > 0 else None
        result[f'{f}_unweighted_mean']   = round(plain_sum / n, 4) if n > 0 else None
    return result


def _roadnet_class_breakdown(selected):
    total_pop = sum(float(rec.get('p', 0) or 0) for _, rec, _, _ in selected)
    out = {}
    for cls in ('primary', 'minor', 'local'):
        s = sum(float((rec.get('c') or {}).get(cls, 0) or 0) * float(rec.get('p', 0) or 0)
                for _, rec, _, _ in selected)
        out[f'{cls}_pop_weighted_mean'] = round(s / total_pop, 4) if total_pop > 0 else None
    return out


def _cells_to_geojson(selected):
    features = []
    for cell_id, rec, lat, lon in selected:
        boundary = h3.cell_to_boundary(cell_id)  # tuple of (lat, lon) pairs
        coords = [[lo, la] for la, lo in boundary]
        coords.append(coords[0])
        props = {k: v for k, v in rec.items() if k != 'c'}
        if 'c' in rec:
            props.update({f'c_{k}': v for k, v in rec['c'].items()})
        props['h3'] = cell_id
        features.append({
            'type': 'Feature',
            'properties': props,
            'geometry': {'type': 'Polygon', 'coordinates': [coords]},
        })
    return {'type': 'FeatureCollection', 'features': features}


def _safe_extract_zip(file_storage):
    """Extract an uploaded zip to a fresh temp dir, guarding against zip-slip
    (path traversal) and zip-bomb (decompressed-size) attacks. Caller is
    responsible for cleaning up the returned directory."""
    tmpdir = tempfile.mkdtemp(prefix='pals_aoi_')
    with zipfile.ZipFile(file_storage) as zf:
        total = sum(zi.file_size for zi in zf.infolist())
        if total > _GEO_MAX_ZIP_UNCOMPRESSED:
            raise ValueError('Uploaded archive is too large once decompressed (limit 50 MB).')
        base = os.path.realpath(tmpdir)
        for zi in zf.infolist():
            dest = os.path.realpath(os.path.join(tmpdir, zi.filename))
            if dest != base and not dest.startswith(base + os.sep):
                raise ValueError('Invalid archive: path traversal detected.')
        zf.extractall(tmpdir)
    return tmpdir


def _aoi_area_km2(geom):
    """Approximate equal-area extent via an equal-area projection (EPSG:6933)."""
    import geopandas as gpd
    return gpd.GeoSeries([geom], crs='EPSG:4326').to_crs('EPSG:6933').area.iloc[0] / 1e6


def _parse_aoi_from_request(max_area_km2=None):
    """Builds a shapely Polygon/MultiPolygon (EPSG:4326) from either a
    `geojson` form field or a `shapefile` zip upload. Returns
    (geom, area_km2, notes). Raises ValueError with a user-facing message on
    anything invalid. `max_area_km2` overrides the default cap (callers with
    tighter limits, e.g. the unauthenticated preview endpoints, pass a smaller
    value)."""
    area_cap = _GEO_MAX_AREA_KM2 if max_area_km2 is None else max_area_km2
    notes = []
    geom = None

    geojson_raw = request.form.get('geojson') or (request.get_json(silent=True) or {}).get('geojson')
    shapefile_upload = request.files.get('shapefile')

    if geojson_raw:
        try:
            gj = json.loads(geojson_raw) if isinstance(geojson_raw, str) else geojson_raw
        except json.JSONDecodeError:
            raise ValueError('The "geojson" field is not valid JSON.')
        gj_type = gj.get('type')
        if gj_type == 'FeatureCollection':
            feats = gj.get('features') or []
            if not feats:
                raise ValueError('FeatureCollection has no features.')
            geom = unary_union([shape(f['geometry']) for f in feats])
        elif gj_type == 'Feature':
            geom = shape(gj['geometry'])
        else:
            geom = shape(gj)
    elif shapefile_upload:
        if not (shapefile_upload.filename or '').lower().endswith('.zip'):
            raise ValueError('shapefile upload must be a .zip containing .shp/.dbf/.shx (and ideally .prj).')
        tmpdir = None
        try:
            tmpdir = _safe_extract_zip(shapefile_upload)
            shp_files = [f for f in os.listdir(tmpdir) if f.lower().endswith('.shp')]
            if not shp_files:
                raise ValueError('No .shp file found in the uploaded archive.')
            import geopandas as gpd
            gdf = gpd.read_file(os.path.join(tmpdir, shp_files[0]))
            if gdf.empty:
                raise ValueError('The shapefile contains no features.')
            if gdf.crs is None:
                notes.append('Shapefile had no .prj/CRS defined — assumed EPSG:4326 (lat/lon).')
                gdf = gdf.set_crs('EPSG:4326', allow_override=True)
            elif str(gdf.crs) != 'EPSG:4326':
                gdf = gdf.to_crs('EPSG:4326')
            geom = unary_union(gdf.geometry.values) if len(gdf) > 1 else gdf.geometry.iloc[0]
        finally:
            if tmpdir:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        raise ValueError('Provide either a "geojson" form field or a "shapefile" zip upload.')

    if geom is None or geom.is_empty:
        raise ValueError('Could not extract a geometry from the submitted AOI.')
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.geom_type not in ('Polygon', 'MultiPolygon'):
        raise ValueError(f'AOI must be a Polygon or MultiPolygon, got {geom.geom_type}.')

    area_km2 = _aoi_area_km2(geom)
    if area_km2 > area_cap:
        raise ValueError(
            f'AOI is ~{area_km2:,.0f} km², above the {area_cap:,} km² limit for this '
            f'endpoint (it is meant for neighborhood/study-area scale queries). For whole-country '
            f'or admin-unit stats, use /api/{{roadnet,euclidean}}/admin instead.'
        )
    return geom, area_km2, notes


_geo_key_hits: dict = defaultdict(list)
_GEO_RATE_WINDOW = 60
_GEO_RATE_LIMIT = 30

_PREVIEW_MAX_AREA_KM2 = 5_000    # tighter cap for the unauthenticated in-browser tool
_preview_ip_hits: dict = defaultdict(list)
_PREVIEW_RATE_WINDOW = 60
_PREVIEW_RATE_LIMIT = 10         # requests/min/IP — separate bucket from the portal-chat limiter

def _allow_preview_ip(ip: str) -> bool:
    if not ip:
        return True
    now = time.time()
    hits = [t for t in _preview_ip_hits[ip] if now - t < _PREVIEW_RATE_WINDOW]
    if len(hits) >= _PREVIEW_RATE_LIMIT:
        _preview_ip_hits[ip] = hits
        return False
    hits.append(now)
    _preview_ip_hits[ip] = hits
    return True


def _allow_geo_key(key_str: str) -> bool:
    if not key_str:
        return True
    h = hashlib.sha256(key_str.encode()).hexdigest()[:16]
    now = time.time()
    hits = [t for t in _geo_key_hits[h] if now - t < _GEO_RATE_WINDOW]
    if len(hits) >= _GEO_RATE_LIMIT:
        _geo_key_hits[h] = hits
        return False
    hits.append(now)
    _geo_key_hits[h] = hits
    return True


# ── Accessibility API (live routing/disruption, Valhalla-backed) ───────────────
# Separate rate bucket from the geodata (roadnet/euclidean) one above — a
# route/nearest-facility/disruption call hits the local Valhalla container,
# not the H3 cell files, so it has different (cheaper, faster) cost
# characteristics and shouldn't share a budget with AOI polygon analysis.
_access_key_hits: dict = defaultdict(list)
_ACCESS_RATE_WINDOW = 60
_ACCESS_RATE_LIMIT = 60
_ACCESS_MAX_BLOCKED_POINTS = 20  # bounds how many exclude_polygons one request can ask for


def _allow_access_key(key_str: str) -> bool:
    if not key_str:
        return True
    h = hashlib.sha256(key_str.encode()).hexdigest()[:16]
    now = time.time()
    hits = [t for t in _access_key_hits[h] if now - t < _ACCESS_RATE_WINDOW]
    if len(hits) >= _ACCESS_RATE_LIMIT:
        _access_key_hits[h] = hits
        return False
    hits.append(now)
    _access_key_hits[h] = hits
    return True


def _parse_latlon(obj, field_name):
    if not isinstance(obj, dict) or 'lat' not in obj or 'lon' not in obj:
        raise ValueError(f"'{field_name}' must be an object with 'lat' and 'lon'")
    try:
        return float(obj['lat']), float(obj['lon'])
    except (TypeError, ValueError):
        raise ValueError(f"'{field_name}.lat'/'lon' must be numbers")


def _parse_facilities(raw):
    if not isinstance(raw, list) or not raw:
        raise ValueError("'facilities' must be a non-empty array of {lat, lon, ...} objects")
    if len(raw) > 200:
        raise ValueError("'facilities' supports at most 200 candidates per request")
    out = []
    for i, f in enumerate(raw):
        if not isinstance(f, dict) or 'lat' not in f or 'lon' not in f:
            raise ValueError(f"facilities[{i}] must be an object with 'lat' and 'lon'")
        try:
            fac = dict(f)
            fac['lat'] = float(f['lat'])
            fac['lon'] = float(f['lon'])
        except (TypeError, ValueError):
            raise ValueError(f"facilities[{i}].lat/lon must be numbers")
        out.append(fac)
    return out


def _parse_blocked_points(raw):
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ValueError("'blocked_points' must be a non-empty array of [lat, lon] pairs")
    if len(raw) > _ACCESS_MAX_BLOCKED_POINTS:
        raise ValueError(f"'blocked_points' supports at most {_ACCESS_MAX_BLOCKED_POINTS} points per request")
    out = []
    for i, p in enumerate(raw):
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError(f"blocked_points[{i}] must be a [lat, lon] pair")
        try:
            out.append((float(p[0]), float(p[1])))
        except (TypeError, ValueError):
            raise ValueError(f"blocked_points[{i}] values must be numbers")
    return out


@app.route('/api/accessibility/route', methods=['POST'])
def accessibility_route():
    """Live driving/walking travel time and distance between two arbitrary
    points anywhere in Africa (Valhalla, continent-wide tiles). Optional
    'blocked_points': [[lat,lon], ...] excludes a small area around each
    point from routing for this call only (e.g. a flooded crossing) - no
    tile rebuild, no precomputation. Body: {origin:{lat,lon},
    destination:{lat,lon}, costing?, blocked_points?, buffer_m?}."""
    key_info, err = require_key()
    if err:
        return err
    if not _allow_access_key(get_api_key()):
        return jsonify({'error': f'Rate limit exceeded ({_ACCESS_RATE_LIMIT} requests/{_ACCESS_RATE_WINDOW}s). Please slow down.'}), 429

    body = request.get_json(silent=True) or {}
    try:
        o_lat, o_lon = _parse_latlon(body.get('origin'), 'origin')
        d_lat, d_lon = _parse_latlon(body.get('destination'), 'destination')
        blocked = _parse_blocked_points(body.get('blocked_points'))
        costing = body.get('costing', 'auto')
        buffer_m = float(body.get('buffer_m', 50))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    try:
        result = _access_engine.route(o_lat, o_lon, d_lat, d_lon, costing=costing,
                                       exclude_points=blocked, buffer_m=buffer_m)
    except _access_engine.AccessibilityEngineError as e:
        return jsonify({'error': str(e)}), 502

    _log_query(None, key_info.get('name'), 'accessibility-route',
               origin=[o_lat, o_lon], destination=[d_lat, d_lon], blocked=bool(blocked))
    return jsonify(result)


@app.route('/api/accessibility/nearest-facility', methods=['POST'])
def accessibility_nearest_facility():
    """Nearest reachable facility by travel time, from a list of candidate
    facilities. Body: {origin:{lat,lon}, facilities:[{lat,lon,...}, ...],
    costing?}. Returns the winner plus the full ranked list (runner-ups
    included) so callers aren't limited to just the top result."""
    key_info, err = require_key()
    if err:
        return err
    if not _allow_access_key(get_api_key()):
        return jsonify({'error': f'Rate limit exceeded ({_ACCESS_RATE_LIMIT} requests/{_ACCESS_RATE_WINDOW}s). Please slow down.'}), 429

    body = request.get_json(silent=True) or {}
    try:
        o_lat, o_lon = _parse_latlon(body.get('origin'), 'origin')
        facilities = _parse_facilities(body.get('facilities'))
        costing = body.get('costing', 'auto')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    try:
        result = _access_engine.nearest_facility(o_lat, o_lon, facilities, costing=costing)
    except _access_engine.AccessibilityEngineError as e:
        return jsonify({'error': str(e)}), 502

    _log_query(None, key_info.get('name'), 'accessibility-nearest-facility',
               origin=[o_lat, o_lon], n_facilities=len(facilities))
    return jsonify(result)


@app.route('/api/accessibility/disruption', methods=['POST'])
def accessibility_disruption():
    """Before/after accessibility impact when specific points (e.g. flooded
    crossings) are excluded from routing - live, per-request, no graph
    rebuild. Body: {origin:{lat,lon}, blocked_points:[[lat,lon],...],
    destination:{lat,lon}} OR {..., facilities:[{lat,lon,...},...]} (exactly
    one of destination/facilities), costing?, buffer_m?}."""
    key_info, err = require_key()
    if err:
        return err
    if not _allow_access_key(get_api_key()):
        return jsonify({'error': f'Rate limit exceeded ({_ACCESS_RATE_LIMIT} requests/{_ACCESS_RATE_WINDOW}s). Please slow down.'}), 429

    body = request.get_json(silent=True) or {}
    try:
        o_lat, o_lon = _parse_latlon(body.get('origin'), 'origin')
        blocked = _parse_blocked_points(body.get('blocked_points'))
        if not blocked:
            raise ValueError("'blocked_points' is required and must be non-empty")
        costing = body.get('costing', 'auto')
        buffer_m = float(body.get('buffer_m', 50))

        has_dest = body.get('destination') is not None
        has_fac = body.get('facilities') is not None
        if has_dest == has_fac:
            raise ValueError("Provide exactly one of 'destination' or 'facilities'")

        if has_dest:
            d_lat, d_lon = _parse_latlon(body.get('destination'), 'destination')
            kwargs = {'dest_lat': d_lat, 'dest_lon': d_lon}
        else:
            kwargs = {'facilities': _parse_facilities(body.get('facilities'))}
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    try:
        result = _access_engine.disruption_impact(o_lat, o_lon, blocked, costing=costing,
                                                    buffer_m=buffer_m, **kwargs)
    except _access_engine.AccessibilityEngineError as e:
        return jsonify({'error': str(e)}), 502

    _log_query(None, key_info.get('name'), 'accessibility-disruption',
               origin=[o_lat, o_lon], n_blocked=len(blocked), mode='destination' if has_dest else 'facilities')
    return jsonify(result)


_ACCESS_MAX_REGIONAL_CALLS = 3000  # n_cells x n_facilities x n_scenarios cap - bounds worst-case
                                    # request time; regional_accessibility's thread pool keeps this
                                    # well under 2 minutes at this cap, not tested far beyond it yet.


@app.route('/api/accessibility/regional', methods=['POST'])
def accessibility_regional():
    """Population-weighted accessibility - and, with blocked_points, flood-
    disruption impact - across every populated H3 cell in a selected
    region. Reuses the same AOI/cell-selection machinery as
    /api/roadnet/query and /api/euclidean/query - no separate population
    grid for this endpoint.

    Region selection, exactly one of:
      - {geojson: <GeoJSON Feature/FeatureCollection/geometry>} or a
        'shapefile' zip upload - same AOI parsing as /api/roadnet/query.
      - {district_name: "...", country: "<ISO3>"} - looks up the matching
        adm2 district by name. Country is required because the adm2 data's
        own per-feature iso3166_1 attribute is too sparse to trust directly
        (same caveat already documented on _load_admin_index) - matching is
        instead scoped spatially via _countries_intersecting.

    Also required: facilities:[{lat,lon,...},...]. Optional:
    blocked_points:[[lat,lon],...] (adds the flooded scenario), costing?.
    """
    key_info, err = require_key()
    if err:
        return err
    if not _allow_access_key(get_api_key()):
        return jsonify({'error': f'Rate limit exceeded ({_ACCESS_RATE_LIMIT} requests/{_ACCESS_RATE_WINDOW}s). Please slow down.'}), 429

    body = request.get_json(silent=True) or {}

    try:
        district_name = body.get('district_name')
        if district_name:
            country = body.get('country')
            if not country:
                raise ValueError("'country' (ISO3) is required alongside 'district_name'")
            matches = []
            for _pt, props, geom in _load_admin_index('roadnet', 2):
                if (props.get('name') or '').strip().lower() == district_name.strip().lower():
                    g = shape(geom)
                    if country.upper() in _countries_intersecting(g):
                        matches.append(g)
            if not matches:
                raise ValueError(f"No district named '{district_name}' found in {country}.")
            if len(matches) > 1:
                raise ValueError(f"Multiple districts named '{district_name}' found in {country} — "
                                  f"provide 'geojson' instead for an unambiguous AOI.")
            aoi_geom = matches[0]
            area_km2 = _aoi_area_km2(aoi_geom)
            if area_km2 > _GEO_MAX_AREA_KM2:
                raise ValueError(f"District '{district_name}' is ~{area_km2:,.0f} km², above the "
                                  f"{_GEO_MAX_AREA_KM2:,} km² limit for this endpoint.")
        else:
            aoi_geom, area_km2, _notes = _parse_aoi_from_request()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Could not parse region: {e}'}), 400

    iso_list = _countries_intersecting(aoi_geom)
    if not iso_list:
        return jsonify({'error': 'The selected region does not overlap any of the 57 supported '
                                  'African countries. See /api/geo/countries for coverage.'}), 400
    if len(iso_list) > _GEO_MAX_COUNTRIES:
        return jsonify({'error': f'Region spans {len(iso_list)} countries; this endpoint supports '
                                  f'up to {_GEO_MAX_COUNTRIES} per request.'}), 400

    try:
        facilities = _parse_facilities(body.get('facilities'))
        blocked = _parse_blocked_points(body.get('blocked_points'))
        costing = body.get('costing', 'auto')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    aoi_prepared = prep(aoi_geom)
    cells = []
    for iso3 in iso_list:
        for cell_id, rec, lat, lon in _select_cells('roadnet', iso3, aoi_prepared):
            cells.append((cell_id, float(rec.get('p', 0) or 0), lat, lon))
    if not cells:
        return jsonify({'error': 'No populated H3 cells found inside the selected region.'}), 404

    n_scenarios = 2 if blocked else 1
    n_calls = len(cells) * len(facilities) * n_scenarios
    if n_calls > _ACCESS_MAX_REGIONAL_CALLS:
        return jsonify({'error': f'Region too large for this request: {len(cells)} cells x '
                                  f'{len(facilities)} facilities x {n_scenarios} scenario(s) = '
                                  f'{n_calls:,} routing calls, above the {_ACCESS_MAX_REGIONAL_CALLS:,} '
                                  f'limit. Use a smaller region or fewer facilities.'}), 400

    try:
        result = _access_engine.regional_accessibility(cells, facilities, blocked_points=blocked, costing=costing)
    except _access_engine.AccessibilityEngineError as e:
        return jsonify({'error': str(e)}), 502

    result['aoi_area_km2'] = round(area_km2, 2)
    result['countries'] = iso_list
    _log_query(None, key_info.get('name'), 'accessibility-regional',
               area_km2=round(area_km2, 2), n_cells=len(cells), n_facilities=len(facilities), blocked=bool(blocked))
    return jsonify(result)


@lru_cache(maxsize=8)
def _load_admin_geojson(tool, level):
    path = os.path.join(GEODATA_TOOLS[tool]['admin_dir'], f'adm{level}.geojson')
    with open(path) as f:
        return json.load(f)


@lru_cache(maxsize=8)
def _load_admin_index(tool, level):
    """(representative_point, properties, geometry) per feature, cached —
    the per-feature ISO code is only reliably populated at the country level
    in this data, so country filtering for admin/district-level units is done
    spatially against africa_bounds.geojson rather than trusting the
    feature's own (sparse) iso3166_1 attribute."""
    gj = _load_admin_geojson(tool, level)
    index = []
    for feat in gj['features']:
        try:
            geom = shape(feat['geometry'])
            pt = geom.representative_point()
        except Exception:
            continue
        index.append((pt, feat.get('properties', {}), feat['geometry']))
    return index


@app.route('/api/validate-session', methods=['GET'])
def validate_session():
    """Lightweight token check — returns 200 if valid, 401 if not. No data transferred."""
    token = request.headers.get('X-Session-Token', '')
    if token and access_db.validate_catalogue_token(token):
        return '', 200
    return '', 401


@app.route('/api/catalogue-user-login', methods=['POST'])
def catalogue_user_login():
    """Per-user login for both catalogues — email + password → session token."""
    data      = request.json or {}
    catalogue = data.get('catalogue', 'precise')
    email     = data.get('email', '').strip()
    password  = data.get('password', '')

    if not email or not password:
        return jsonify({'ok': False, 'error': 'Email and password required'}), 400

    # Check status before password so we can give informative messages
    existing = access_db.get_catalogue_user_by_email(catalogue, email)
    if existing:
        status = existing.get('status') or 'approved'
        if status == 'pending':
            return jsonify({'ok': False, 'error': 'Your account is pending admin approval. You will receive an email when approved.'}), 403
        if status == 'rejected':
            return jsonify({'ok': False, 'error': 'Your access request was not approved. Contact the administrator.'}), 403
        if status == 'revoked':
            return jsonify({'ok': False, 'error': 'Your access has been revoked. Contact the administrator.'}), 403

    user = access_db.authenticate_catalogue_user(catalogue, email, password)
    if not user:
        return jsonify({'ok': False, 'error': 'Invalid email or password'}), 401

    token = access_db.issue_user_catalogue_token(user['id'])
    return jsonify({
        'ok':           True,
        'token':        token,
        'name':         user['name'],
        'tokens_used':  user['tokens_used'],
        'token_budget': user['token_budget'],
    })


# ══════════════════════════════════════════════════════════════════════════════
# PORTAL AUTH  (sign-up / login / logout / forgot-password / reset-password)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/portal/signup', methods=['POST'])
def portal_signup():
    d           = request.json or {}
    name        = d.get('name', '').strip()
    email       = d.get('email', '').strip()
    password    = d.get('password', '')
    institution = d.get('institution', '').strip()
    purpose     = d.get('purpose', '').strip()

    if not name or not email or not password:
        return jsonify({'ok': False, 'error': 'Name, email and password are required'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400

    user_id, conflict = access_db.create_portal_user(name, email, password, institution, purpose)
    if user_id is None:
        if conflict == 'pending':
            return jsonify({'ok': False, 'error': 'A request for this email is already pending review.'}), 409
        if conflict == 'approved':
            return jsonify({'ok': False, 'error': 'An account with this email already exists. Please sign in.'}), 409
        return jsonify({'ok': False, 'error': 'This email is already registered.'}), 409

    # Notify all admins
    body = (
        f'A new portal account request requires your approval.\n\n'
        f'Name:        {name}\n'
        f'Email:       {email}\n'
        f'Institution: {institution}\n\n'
        f'Purpose:\n{purpose or "(not provided)"}\n\n'
        f'Review at: https://portal.placealert.org/duckrequest/admin\n'
        f'User ID: {user_id}'
    )
    for addr in ADMIN_EMAILS:
        _send_email(addr, f'[PALs Portal] New account request from {name}', body)

    return jsonify({'ok': True, 'message': 'Your request has been submitted. You will receive an email once approved.'})


@app.route('/api/portal/login', methods=['POST'])
def portal_login():
    d        = request.json or {}
    email    = d.get('email', '').strip()
    password = d.get('password', '')

    if not email or not password:
        return jsonify({'ok': False, 'error': 'Email and password are required'}), 400

    # Check if user exists at all (to give a better pending message)
    existing = access_db.get_portal_user_by_email(email)
    if existing and existing.get('status') == 'pending':
        return jsonify({'ok': False, 'error': 'Your account is pending admin approval. You will receive an email when approved.'}), 403
    if existing and existing.get('status') == 'rejected':
        return jsonify({'ok': False, 'error': 'Your access request was not approved. Contact the administrator.'}), 403
    if existing and existing.get('status') == 'revoked':
        return jsonify({'ok': False, 'error': 'Your account has been revoked. Contact the administrator.'}), 403

    user = access_db.authenticate_portal_user(email, password)
    if not user:
        return jsonify({'ok': False, 'error': 'Invalid email or password'}), 401

    token = access_db.issue_user_catalogue_token(user['id'])
    return jsonify({'ok': True, 'token': token, 'name': user['name']})


@app.route('/api/portal/logout', methods=['POST'])
def portal_logout():
    token = request.headers.get('X-Session-Token', '')
    if token:
        access_db.portal_logout(token)
    return jsonify({'ok': True})


@app.route('/api/portal/validate-session', methods=['GET'])
def portal_validate_session():
    token = request.headers.get('X-Session-Token', '')
    if token and access_db.validate_catalogue_token(token):
        user = access_db.get_catalogue_user_from_token(token)
        if user and user.get('catalogue') == 'portal':
            return jsonify({'ok': True, 'name': user['name']})
    return jsonify({'ok': False}), 401


@app.route('/api/portal/forgot-password', methods=['POST'])
def portal_forgot_password():
    email = (request.json or {}).get('email', '').strip()
    # Always return OK to avoid email enumeration
    if email:
        user = access_db.get_portal_user_by_email(email)
        if user and user.get('status') == 'approved':
            token = access_db.create_user_reset_token(user['id'])
            reset_url = f'{PORTAL_URL}/?reset_token={token}'
            _send_email(
                email,
                '[PALs Portal] Password reset request',
                f'Hi {user["name"]},\n\n'
                f'You requested a password reset for your PALs Portal account.\n\n'
                f'Click the link below to set a new password (valid for 1 hour):\n'
                f'{reset_url}\n\n'
                f'If you did not request this, ignore this email.\n\n'
                f'PALS Lab Team'
            )
    return jsonify({'ok': True, 'message': 'If that email is registered and approved, a reset link has been sent.'})


@app.route('/api/portal/reset-password', methods=['POST'])
def portal_reset_password():
    d        = request.json or {}
    token    = d.get('token', '').strip()
    password = d.get('password', '')

    if not token or not password:
        return jsonify({'ok': False, 'error': 'Token and password are required'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400

    user_id = access_db.consume_user_reset_token(token, password)
    if not user_id:
        return jsonify({'ok': False, 'error': 'Reset link is invalid or has expired'}), 400

    return jsonify({'ok': True, 'message': 'Password updated. You can now sign in.'})


# ══════════════════════════════════════════════════════════════════════════════
# CATALOGUE SELF-SIGNUP  (precise / he2at — users choose their own password)
# ══════════════════════════════════════════════════════════════════════════════

_CATALOGUE_URLS = {
    'precise': 'https://portal.placealert.org/catalogue/',
    'he2at':   'https://portal.placealert.org/heat-catalogue/',
}
_CATALOGUE_LABELS = {
    'precise': 'PRECISE Catalogue',
    'he2at':   'HE²AT Catalogue',
}


@app.route('/api/catalogue/signup', methods=['POST'])
def catalogue_signup():
    d           = request.json or {}
    catalogue   = d.get('catalogue', '').strip()
    name        = d.get('name', '').strip()
    email       = d.get('email', '').strip()
    password    = d.get('password', '')
    institution = d.get('institution', '').strip()
    reason      = d.get('reason', '').strip()

    if catalogue not in ('precise', 'he2at'):
        return jsonify({'ok': False, 'error': 'Unknown catalogue'}), 400
    if not name or not email or not password:
        return jsonify({'ok': False, 'error': 'Name, email and password are required'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400

    user_id, conflict = access_db.create_catalogue_signup_user(
        catalogue, name, email, password, institution, reason
    )
    if user_id is None:
        if conflict == 'pending':
            return jsonify({'ok': False, 'error': 'A request for this email is already pending review.'}), 409
        if conflict == 'approved':
            return jsonify({'ok': False, 'error': 'An account with this email already exists. Please sign in.'}), 409
        return jsonify({'ok': False, 'error': 'This email is already registered.'}), 409

    label = _CATALOGUE_LABELS.get(catalogue, catalogue)
    body = (
        f'A new {label} account request requires your approval.\n\n'
        f'Name:        {name}\n'
        f'Email:       {email}\n'
        f'Institution: {institution}\n\n'
        f'Reason:\n{reason or "(not provided)"}\n\n'
        f'Review at: https://portal.placealert.org/duckrequest/admin\n'
        f'User ID: {user_id}'
    )
    for addr in ADMIN_EMAILS:
        _send_email(addr, f'[{label}] New account request from {name}', body)

    return jsonify({'ok': True, 'message': 'Your request has been submitted. You will receive an email once approved.'})


@app.route('/api/catalogue/forgot-password', methods=['POST'])
def catalogue_forgot_password():
    d         = request.json or {}
    catalogue = d.get('catalogue', '').strip()
    email     = d.get('email', '').strip()

    if email and catalogue in ('precise', 'he2at'):
        user = access_db.get_catalogue_user_by_email(catalogue, email)
        if user and user.get('status') in (None, 'approved'):
            token     = access_db.create_user_reset_token(user['id'])
            reset_url = f"{_CATALOGUE_URLS.get(catalogue, PORTAL_URL)}?reset_token={token}"
            label     = _CATALOGUE_LABELS.get(catalogue, catalogue)
            _send_email(
                email,
                f'[{label}] Password reset request',
                f'Hi {user["name"]},\n\n'
                f'You requested a password reset for your {label} account.\n\n'
                f'Click the link below to set a new password (valid for 1 hour):\n'
                f'{reset_url}\n\n'
                f'If you did not request this, ignore this email.\n\n'
                f'PALS Lab Team'
            )
    return jsonify({'ok': True, 'message': 'If that email is registered and approved, a reset link has been sent.'})


@app.route('/api/catalogue/reset-password', methods=['POST'])
def catalogue_reset_password():
    d        = request.json or {}
    token    = d.get('token', '').strip()
    password = d.get('password', '')

    if not token or not password:
        return jsonify({'ok': False, 'error': 'Token and password are required'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400

    user_id = access_db.consume_user_reset_token(token, password)
    if not user_id:
        return jsonify({'ok': False, 'error': 'Reset link is invalid or has expired'}), 400

    return jsonify({'ok': True, 'message': 'Password updated. You can now sign in.'})


def is_safe_query(sql):
    """Allow SELECT / WITH / SHOW / DESCRIBE / PRAGMA statements."""
    stripped = sql.strip().upper()
    read_only_starts = ('SELECT', 'WITH', 'SHOW', 'DESCRIBE', 'DESC', 'PRAGMA', 'EXPLAIN')
    if not any(stripped.startswith(s) for s in read_only_starts):
        return False, "Only read-only queries are allowed (SELECT, SHOW, DESCRIBE)."
    forbidden = ['DROP', 'DELETE', 'INSERT', 'UPDATE', 'CREATE', 'ALTER', 'TRUNCATE']
    for word in forbidden:
        if re.search(r'\b' + word + r'\b', stripped):
            return False, f"Query contains forbidden keyword: {word}"
    return True, None


def _coerce(v):
    """Coerce DuckDB return values to JSON-serialisable Python scalars."""
    import datetime as _dt
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if hasattr(v, '__float__') and not isinstance(v, (int, str, bool, type(None))):
        return float(v)
    return v


# ── PII stripping — never let individual participant/patient IDs reach the LLM ─

_PRECISE_ID_COLS = frozenset({'f2a_participant_id', 'f2a_precise_id', 'pid'})
_HE2AT_ID_COLS   = frozenset({'Patient_Identifier'})


def _strip_id_cols(result, id_cols):
    """Remove participant/patient identifier columns from execute_query results
    before they are sent back to the LLM, preventing verbatim ID disclosure."""
    if 'columns' not in result or 'rows' not in result:
        return result
    keep = [c for c in result['columns'] if c not in id_cols]
    if len(keep) == len(result['columns']):
        return result
    return {
        'columns':   keep,
        'rows':      [{k: v for k, v in r.items() if k not in id_cols}
                      for r in result['rows']],
        'row_count': result.get('row_count', len(result['rows'])),
    }


def apply_country_filter(sql, countries):
    """
    Wrap every reference to daily_data, sensor_daily, sensor_raw, and the
    fire_smoke_* / participant_outcomes tables so queries are restricted to
    the caller's approved countries.  Works for both FROM and JOIN.
    sensor_daily/sensor_raw/fire_smoke_*/participant_outcomes are also
    rewritten to use their catalog prefix.
    """
    if not countries:
        return "SELECT * FROM daily_data WHERE 1=0"

    c_list = ', '.join(f"'{c}'" for c in countries)

    # daily_data — Country column (capital C)
    subq_daily = (f"(SELECT * FROM daily_data "
                  f"WHERE Country IN ({c_list})) AS daily_data")
    filtered = re.sub(r'\bFROM\s+daily_data\b', f'FROM {subq_daily}', sql, flags=re.IGNORECASE)
    filtered = re.sub(r'\bJOIN\s+daily_data\b',  f'JOIN {subq_daily}', filtered, flags=re.IGNORECASE)

    # sensor_daily — country column (lowercase c), qualify with sensor. catalog
    subq_sensor_daily = (f"(SELECT * FROM sensor.sensor_daily "
                         f"WHERE country IN ({c_list})) AS sensor_daily")
    filtered = re.sub(r'\bFROM\s+sensor_daily\b', f'FROM {subq_sensor_daily}', filtered, flags=re.IGNORECASE)
    filtered = re.sub(r'\bJOIN\s+sensor_daily\b',  f'JOIN {subq_sensor_daily}', filtered, flags=re.IGNORECASE)

    # sensor_raw — country column (lowercase c), qualify with sensor. catalog
    subq_sensor_raw = (f"(SELECT * FROM sensor.sensor_raw "
                       f"WHERE country IN ({c_list})) AS sensor_raw")
    filtered = re.sub(r'\bFROM\s+sensor_raw\b', f'FROM {subq_sensor_raw}', filtered, flags=re.IGNORECASE)
    filtered = re.sub(r'\bJOIN\s+sensor_raw\b',  f'JOIN {subq_sensor_raw}', filtered, flags=re.IGNORECASE)

    # fire_smoke_* / participant_outcomes — Country column (capital C),
    # qualify with fire_smoke. catalog prefix
    for tbl in ('fire_smoke_daily', 'fire_smoke_period_summary',
                'fire_smoke_period_summary_wide', 'fire_smoke_country_thresholds',
                'participant_outcomes'):
        subq = (f"(SELECT * FROM fire_smoke.{tbl} "
                f"WHERE Country IN ({c_list})) AS {tbl}")
        filtered = re.sub(rf'\bFROM\s+{tbl}\b', f'FROM {subq}', filtered, flags=re.IGNORECASE)
        filtered = re.sub(rf'\bJOIN\s+{tbl}\b',  f'JOIN {subq}', filtered, flags=re.IGNORECASE)

    return filtered


def _open_conn():
    """Open precise.duckdb read-only and attach sensor.duckdb / derived fire-smoke db."""
    conn = duckdb.connect(DB_PATH, read_only=True)
    conn.execute(f"ATTACH '{SENSOR_DB_PATH}' AS sensor (READ_ONLY)")
    conn.execute(f"ATTACH '{FIRE_SMOKE_DB_PATH}' AS fire_smoke (READ_ONLY)")
    return conn


# ── Table 1 — descriptive stats split by group ──────────────────────────────

_NUMERIC_TYPE_PREFIXES = ('DOUBLE', 'FLOAT', 'BIGINT', 'INTEGER', 'HUGEINT',
                          'REAL', 'DECIMAL', 'SMALLINT', 'TINYINT', 'UBIGINT')


def _build_table1(conn, table_name, variables, group_by, id_col=None, countries=None,
                   static_agg='MAX'):
    """
    Build an epidemiology-style 'Table 1' — descriptive stats split by
    `group_by` (e.g. Country) — against `table_name`. Continuous columns are
    summarised as mean (SD); VARCHAR/categorical columns as n (%) per level.
    Every group includes an 'Overall' figure alongside it.

    If `id_col` is given, the source table has multiple rows per subject
    (e.g. daily_data — one row per participant-day), so it is first rolled
    up to one row per id_col: static_agg (default MAX) for categorical
    columns, AVG for numeric columns — the same pattern already used for
    regression queries elsewhere in this file (GROUP BY participant, MAX for
    static covariates, AVG for exposures).

    If `countries` is given, the same country ACL used for Claude-authored
    SQL (apply_country_filter) is applied before aggregation.

    Variable names that don't match any real column are reported back in
    `unavailable` instead of raising — the rest of the table still builds.
    """
    cols_info = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    col_types = {row[1]: row[2] for row in cols_info}
    if not col_types:
        raise ValueError(f"Unknown table '{table_name}'")
    col_lookup = {name.lower(): name for name in col_types}

    def resolve(name):
        return col_lookup.get(str(name).strip().lower())

    group_by_actual = resolve(group_by)
    if not group_by_actual:
        raise ValueError(f"group_by column '{group_by}' not found in {table_name}")

    resolved, unavailable = [], []
    for v in variables:
        actual = resolve(v)
        if actual is None:
            unavailable.append(v)
        elif actual != group_by_actual and actual not in resolved:
            resolved.append(actual)

    def is_numeric(colname):
        return col_types[colname].upper().startswith(_NUMERIC_TYPE_PREFIXES)

    if countries is not None:
        base_source = f"({apply_country_filter(f'SELECT * FROM {table_name}', countries)})"
    else:
        base_source = table_name

    if id_col:
        id_actual = resolve(id_col) or id_col
        exprs = [f'{static_agg}("{group_by_actual}") AS "{group_by_actual}"']
        for v in resolved:
            expr = f'AVG("{v}")' if is_numeric(v) else f'{static_agg}("{v}")'
            exprs.append(f'{expr} AS "{v}"')
        source = (f'(SELECT "{id_actual}", {", ".join(exprs)} '
                  f'FROM {base_source} GROUP BY "{id_actual}")')
    else:
        source = base_source

    groups = [r[0] for r in conn.execute(
        f'SELECT DISTINCT "{group_by_actual}" FROM {source} '
        f'WHERE "{group_by_actual}" IS NOT NULL ORDER BY 1'
    ).fetchall()]

    n_by_group = {}
    for g in groups:
        n_by_group[g] = conn.execute(
            f'SELECT COUNT(*) FROM {source} WHERE "{group_by_actual}" = ?', [g]
        ).fetchone()[0]
    overall_n = conn.execute(f'SELECT COUNT(*) FROM {source}').fetchone()[0]

    rows = []
    for v in resolved:
        if is_numeric(v):
            by_group = {r[0]: r for r in conn.execute(
                f'SELECT "{group_by_actual}", AVG("{v}"), STDDEV("{v}") '
                f'FROM {source} GROUP BY "{group_by_actual}"'
            ).fetchall()}
            values = {}
            for g in groups:
                r = by_group.get(g)
                values[g] = f"{r[1]:.1f} ({r[2]:.1f})" if r and r[1] is not None else "—"
            mean_sd = conn.execute(f'SELECT AVG("{v}"), STDDEV("{v}") FROM {source}').fetchone()
            overall = f"{mean_sd[0]:.1f} ({mean_sd[1]:.1f})" if mean_sd[0] is not None else "—"
            rows.append({'variable': v, 'type': 'continuous', 'values': values, 'overall': overall})
        else:
            cats = [r[0] for r in conn.execute(
                f'SELECT DISTINCT "{v}" FROM {source} WHERE "{v}" IS NOT NULL ORDER BY 1'
            ).fetchall()]
            for cat in cats:
                values = {}
                for g in groups:
                    denom = n_by_group.get(g) or 1
                    cnt = conn.execute(
                        f'SELECT COUNT(*) FROM {source} WHERE "{group_by_actual}" = ? AND "{v}" = ?',
                        [g, cat]
                    ).fetchone()[0]
                    values[g] = f"{cnt} ({100.0 * cnt / denom:.1f}%)"
                overall_cnt = conn.execute(
                    f'SELECT COUNT(*) FROM {source} WHERE "{v}" = ?', [cat]
                ).fetchone()[0]
                overall_pct = 100.0 * overall_cnt / (overall_n or 1)
                rows.append({
                    'variable': f"{v} = {cat}", 'type': 'categorical',
                    'values': values, 'overall': f"{overall_cnt} ({overall_pct:.1f}%)",
                })

    return {
        'group_by':    group_by_actual,
        'groups':      groups,
        'n_by_group':  n_by_group,
        'overall_n':   overall_n,
        'rows':        rows,
        'unavailable': unavailable,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route('/api/health')
def health():
    key_info, err = require_key()
    if err:
        return err

    countries = key_info['countries']
    conn = _open_conn()
    try:
        c_list = ', '.join(f"'{c}'" for c in countries)
        count = conn.execute(
            f"SELECT COUNT(*) FROM daily_data WHERE Country IN ({c_list})"
        ).fetchone()[0]
        participants = conn.execute(
            f"SELECT COUNT(DISTINCT f2a_participant_id) FROM daily_data "
            f"WHERE Country IN ({c_list})"
        ).fetchone()[0]
        return jsonify({
            'status': 'ok',
            'authorised_countries': countries,
            'total_records': count,
            'participants': participants,
            'user': key_info['name'],
        })
    finally:
        conn.close()


@app.route('/api/schema')
def schema():
    key_info, err = require_key()
    if err:
        return err

    conn = _open_conn()
    try:
        columns = conn.execute("DESCRIBE daily_data").fetchall()
        return jsonify({
            'columns': [{'name': r[0], 'type': r[1]} for r in columns],
            'authorised_countries': key_info['countries'],
        })
    finally:
        conn.close()


@app.route('/api/query', methods=['POST'])
def query():
    key_info, err = require_key()
    if err:
        return err

    data = request.json or {}
    sql  = data.get('sql', '').strip()

    if not sql:
        return jsonify({'error': 'No SQL provided'}), 400

    safe, reason = is_safe_query(sql)
    if not safe:
        return jsonify({'error': reason}), 400

    # No hard row cap — user controls this via LIMIT in their SQL.
    # Default fetch is all rows; pass max_rows to cap if needed.
    max_rows = data.get('max_rows')

    # Inject country filter — user only sees their approved countries
    filtered_sql = apply_country_filter(sql, key_info['countries'])

    conn = _open_conn()
    try:
        cur = conn.execute(filtered_sql)
        # Preserve original column order from the table definition
        columns = [d[0] for d in cur.description]

        raw = cur.fetchmany(int(max_rows)) if max_rows else cur.fetchall()

        # Return rows as ordered lists (not dicts) so column order is
        # always guaranteed when the caller does pd.DataFrame(rows, columns=columns)
        rows = [[_coerce(v) for v in row] for row in raw]

        return jsonify({
            'columns':             columns,
            'rows':                rows,
            'row_count':           len(rows),
            'truncated':           bool(max_rows and len(rows) == int(max_rows)),
            'authorised_countries': key_info['countries'],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()


@app.route('/api/query/arrow', methods=['POST'])
def query_arrow():
    """
    Same as /api/query but returns Apache Arrow IPC stream (binary).
    ~10-50x faster than JSON for large result sets — no serialisation overhead.
    Use PreciseDB.query() which calls this automatically.
    """
    key_info, err = require_key()
    if err:
        return err

    data = request.json or {}
    sql  = data.get('sql', '').strip()

    if not sql:
        return jsonify({'error': 'No SQL provided'}), 400

    safe, reason = is_safe_query(sql)
    if not safe:
        return jsonify({'error': reason}), 400

    # Optional row limit — inject as a subquery wrapper if requested
    max_rows = data.get('max_rows')
    if max_rows:
        sql = f"SELECT * FROM ({sql}) __q LIMIT {int(max_rows)}"

    filtered_sql = apply_country_filter(sql, key_info['countries'])

    conn = _open_conn()
    try:
        result      = conn.execute(filtered_sql)
        arrow_table = result.fetch_arrow_table()

        sink   = io.BytesIO()
        writer = _ipc.new_stream(sink, arrow_table.schema)
        writer.write_table(arrow_table)
        writer.close()

        return Response(
            sink.getvalue(),
            mimetype='application/vnd.apache.arrow.stream',
            headers={
                'X-Row-Count':            str(arrow_table.num_rows),
                'X-Authorised-Countries': ','.join(key_info['countries']),
            }
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()


@app.route('/api/query/csv', methods=['POST'])
def query_csv():
    """
    Same as /api/query but returns CSV text.
    Designed for R users: read.csv(text = content(resp, as = "text"))
    No special packages required beyond httr.
    """
    key_info, err = require_key()
    if err:
        return err

    data = request.json or {}
    sql  = data.get('sql', '').strip()

    if not sql:
        return jsonify({'error': 'No SQL provided'}), 400

    safe, reason = is_safe_query(sql)
    if not safe:
        return jsonify({'error': reason}), 400

    max_rows = data.get('max_rows')
    if max_rows:
        sql = f"SELECT * FROM ({sql}) __q LIMIT {int(max_rows)}"

    filtered_sql = apply_country_filter(sql, key_info['countries'])

    conn = _open_conn()
    try:
        cur     = conn.execute(filtered_sql)
        columns = [d[0] for d in cur.description]
        rows    = cur.fetchall()

        buf = io.StringIO()
        import csv as _csv
        writer = _csv.writer(buf)
        writer.writerow(columns)
        writer.writerows(rows)

        return Response(
            buf.getvalue(),
            mimetype='text/csv',
            headers={
                'X-Row-Count':            str(len(rows)),
                'X-Authorised-Countries': ','.join(key_info['countries']),
                'Content-Disposition':    'attachment; filename="precise_data.csv"',
            }
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()


_CHAT_SYSTEM_PROMPT = """You are Shmron, an expert research assistant for the PRECISE Network study on Environmental & Social Determinants of Maternal Health. 6,960 pregnant women across 525 communities: Kenya (n=3,535, 370 communities), Mozambique (n=2,097, 74 communities), The Gambia (n=1,328, 81 communities).

**CRITICAL RULES:**
1. You MUST call execute_query for every question about data, exposures, counts, distributions, trends, or statistics. Never describe what you will do — just call the tool and present the results.
2. NUMBERS MUST ADD UP: Use GROUP BY Country in a single query when comparing countries. Never run separate queries for total vs breakdown — they diverge due to NULL Country rows. If you must use separate queries, reconcile totals explicitly.
3. Always include WHERE Country IS NOT NULL if you want only the three named countries.
4. **PRIVACY — NEVER quote raw participant identifier hashes.** Do not include f2a_participant_id, f2a_precise_id, or pid hash strings in your response text. You SHOULD report participant-level statistics: counts, distributions, min/max values, percentiles, percentages. When a query finds the most-exposed participant, describe their village, country, and exposure level — omit the ID hash. Example: "The highest-exposed participant was in Nanoro, Burkina Faso with a mean UTCI of 33.4°C."

Settlement breakdown (GHSL_class): Kenya 66% Urban/8% Rural/26% Peri_Urban | Mozambique 72%/21%/7% | Gambia 40%/60%/0.2%

=== DATABASE SCHEMA (table: daily_data, 3,129,121 rows — one row per participant per exposure day) ===

ALL numeric columns below are DOUBLE unless stated otherwise. Do NOT use TRY_CAST on DOUBLE columns.

**Identifiers & Geography:**
- f2a_participant_id, f2a_precise_id, participant_status (VARCHAR)
- cohort (VARCHAR): 'UNS' for every participant in daily_data — this table covers ONLY the UNS (main longitudinal pregnancy) cohort. The broader PRECISE Network also enrolled participants under three other cohort codes ('WRA' = Women of Reproductive Age, 'HTN/FGR' = Hypertension/Fetal Growth Restriction, 'IUFD/SB' = Intrauterine Fetal Death/Stillbirth), but those participants have no rows in daily_data — do not expect to find them here. If asked "which cohort" or "is this participant UNS", answer from this column; if asked to compare cohorts, explain daily_data only contains UNS and the others aren't in this database.
- Country (VARCHAR): 'Kenya', 'Mozambique', 'Gambia'
- Village (VARCHAR), Village code (DOUBLE), Longitude, Latitude (DOUBLE)
- health_facility (VARCHAR), GHSL_class (VARCHAR): 'Urban', 'Rural', 'Peri_Urban' (underscore, not hyphen), 'No_Data'
- IPCC_zone, climate_zone, season_wb (VARCHAR)

**Dates:**
- exposure_day (TIMESTAMP_NS): cast with exposure_day::DATE for date comparisons
- conception_date (DATE), delivery_date (DATE), edd (DATE)

**Air Quality (all DOUBLE):**
- CAMS2_pm2p5_ugm3: PM2.5 μg/m³
- Fire_Smoke_PM25, Non_Fire_Smoke_PM25
- CAMS2_aod550: total aerosol optical depth
- CAMS2_bcaod550: black carbon AOD
- CAMS2_duaod550: CAMS dust AOD
- duaod550, duaod550_village, duaod550_facility: alternative dust AOD source
- CAMS2_tcno2_umolm2, CAMS2_gtco3_DU, CAMS2_tcco_gm2, CAMS2_tcso2_DU

**Dust (MERRA2 — all DOUBLE):**
- MERRA2_dust_AOD550: dust aerosol optical depth at 550nm
- MERRA2_dust_pm25_ugm3: dust-attributed PM2.5
- MERRA2_dust_column_kgm2: vertically integrated dust column

**Temperature (all DOUBLE):**
- ERA5_T2M_Mean, ERA5_T2M_Mean_village, ERA5_T2M_Mean_facility
- ERA5_T2M_Max, ERA5_T2M_Max_village, ERA5_T2M_Max_facility
- ERA5_T2M_Min, ERA5_T2M_Min_village, ERA5_T2M_Min_facility
- ERA5_T2M_Diurnal: diurnal temperature range
- ERA5_T2M_deviation, ERA5_T2M_threshold
- ERA5_LST_mean, ERA5_LST_mean_village, ERA5_LST_mean_facility: land surface temp
- ERA5_T2M_extreme_hot_day, ERA5_T2M_heatwave_day (VARCHAR: 'Yes'/'No' — use = 'Yes' or = 'No', NOT boolean TRUE/FALSE)
- MERRA2_T2M_mean, MERRA2_T2M_max: air temperature
- CAMS2_t2m_C, CAMS2_d2m_C: CAMS 2m temperature & dew point

**Heat Stress Indices (all DOUBLE):**
- UTCI_min, UTCI_mean, UTCI_max: Universal Thermal Climate Index (°C)
  UTCI stress categories: <0°C cold, 9-26°C no stress, 26-32°C moderate, 32-38°C strong, 38-46°C very strong, >46°C extreme
- WBGT_mean: Wet Bulb Globe Temperature
- WBGTsimple_mean: simplified WBGT
- WBT_mean: wet bulb temperature
- humidex_mean: humidex comfort index
- HI_mean: Heat Index
- tasapp_mean: apparent temperature
- tasdp_mean: dew point temperature
- Wind_Chill_mean, MRT_mean: mean radiant temp, NET_mean, ws_mean

**Weather (DOUBLE):**
- Relative_Humidity, Precipitation
- Precip_village, Precip_facility

**Environment (all DOUBLE):**
- NDVI, NDVI_village, NDVI_facility: vegetation index
- meanDEM: elevation (m)

**Access to Care (all DOUBLE):**
- PW_WalkDist_Fac, PW_WalkTime_Fac: walk distance (m) and time (min) to facility
- PW_DriveDist_Fac, PW_DriveTime_Fac: drive distance and time
- PW_PubTrans_Dist_Fac, PW_PubTrans_Time_Fac: public transport
- PW_EuclMajorRd, PW_EuclHwy: Euclidean distance to roads
- PW_RoadDens, RQI: road density and quality index
- PW_WalkIso_MajorRd, PW_WalkIso_Hwy, PW_DriveIso_MajorRd, PW_DriveIso_Hwy

**Socioeconomic (all DOUBLE):**
- RWI: Relative Wealth Index
- VIIRS: night-time light intensity
- PPI_score, extreme_poverty_line, poverty_line

**Soil Nutrients (all DOUBLE):**
- N_mean, P_mean, K_mean, Ca_mean

**Demographics:**
- age_enrolment (DOUBLE), Ethnicity, religion, marital_status, highest_school_level, occupation (VARCHAR)
- dietary_diversity (DOUBLE), minimum_dietary_diversity (VARCHAR)

**Maternal Anthropometry & Clinical (DOUBLE unless stated):**
- maternal_height, maternal_weight, maternal_bmi, average_muac
- maternal_bmi_categorised, average_muac_categorised (VARCHAR)
- average_dbp, average_sbp: blood pressure
- gh_overall, ch_overall, pe_overall, ht_overall, hdp_overall, gh_iso_overall (VARCHAR 'Yes'/'No'): hypertensive disorders
- ht_flag, ch_flag, gh_flag (VARCHAR 'Yes'/'No'): hypertension flags
- bp_cat (VARCHAR): 'Normal BP', 'Elevated BP', 'Stage 1 Hypertension', 'Stage 2 Hypertension'
- hiv_status, pre_gest_diab, previous_csection, previous_stillbirth, tobacco_use (VARCHAR 'Yes'/'No')
- maternal_death (VARCHAR 'Yes'/'No')
- deliverylocation (VARCHAR): 'District hospital', 'PHC', 'Regional hospital', 'Home', 'Private hospital/clinic'
- delivery_mode (VARCHAR): 'Unassisted vaginal without episiotomy', 'Unassisted vaginal with episiotomy', 'Caesarean section', 'Operative vaginal', 'Vaginal breech'
- cooking (VARCHAR): 'Biomass', 'Coal', 'Gas', 'Kerosene', 'Electric'
- heating (VARCHAR): 'Not needed', 'Electric', 'Coal', 'Biomass', 'Battery'
- lighting (VARCHAR): 'Electric', 'Battery', 'Kerosene', 'Biomass', 'Generator'
- sanitation_jmp (VARCHAR): 'At least basic', 'Limited', 'Unimproved', 'Open defecation'
- water_jmp (VARCHAR): 'At least basic', 'Limited', 'Unimproved', 'Surface water'
- hygiene_jmp (VARCHAR): 'At least limited', 'No facility'
- parity, age_edd (DOUBLE)
- nicu_admission (VARCHAR): 'Yes', 'No', "Don't know"

**Birth Outcomes:**
- Birthweight (DOUBLE): grams
- bwt_kg, bwt_percentile, bwt_zscore (DOUBLE)
- GA_PRECISE, GA_clinical (DOUBLE): gestational age in weeks
- preterm, lowbirthweight, svn, neonataldeath, stillbirth, livebirth, bornalive (VARCHAR: 'Yes'/'No')
- sga (VARCHAR): full descriptive strings — 'Appropriate for Gestational Age (10th to 90th centile)', 'Small for Gestational Age (3rd - <10th centile)', 'Severely Small for Gestational Age (<3rd centile)', 'Large for Gestational Age (>90th centile)', 'Large for Gestational Age (>97th centile)'. For binary SGA use: CASE WHEN sga LIKE 'Small%' OR sga LIKE 'Severe%' THEN 1 ELSE 0 END
- sex_of_baby (VARCHAR): 'Male', 'Female', 'Indeterminate'
- ageatdeath (BOOLEAN: TRUE/FALSE — one of the few actual booleans; use = TRUE or = FALSE)
- placenta_weight, placenta_weight_ratio (DOUBLE)

=== PERSONAL SENSOR DATABASE (343 participants, 3 countries) ===

A subset of 343 PRECISE participants wore personal air quality and environmental sensors during a monitoring window (typically 5–7 days). Unlike the satellite/reanalysis data in daily_data, these are ground-level PERSONAL measurements — what the participant themselves breathed and experienced.

**Table: sensor_daily** — 3,190 rows — ONE ROW PER PARTICIPANT PER DAY
- pid (VARCHAR): participant ID — joins to f2a_participant_id in daily_data
- country (VARCHAR): 'Kenya', 'Mozambique', 'Gambia'
- exposure_date (DATE): the calendar day
- season (VARCHAR): 'Dry' or 'Wet'
- pm25_mean, pm25_max, pm25_min, pm25_sd (DOUBLE): personal PM2.5 μg/m³ — daily aggregate
- no2_mean, no2_max, no2_min, no2_sd (DOUBLE): personal NO2 ppb — daily aggregate
- temp_mean, temp_max, temp_min, temp_sd (DOUBLE): personal temperature °C — daily aggregate
- rh_mean, rh_max, rh_min, rh_sd (DOUBLE): personal relative humidity % — daily aggregate
- lat, lon (DOUBLE): mean GPS position for the day
- n_readings (BIGINT): number of minute-level readings that day (use to assess data quality)
- monitoring_start, monitoring_end (DATE): the participant's full sensor deployment window

**Table: sensor_raw** — 3,723,990 rows — ONE ROW PER MINUTE (use sparingly — always add LIMIT)
- pid, country (VARCHAR), datetime (TIMESTAMP)
- pm25, no2, temp, rh, lat, lon (DOUBLE): raw minute-level measurements
- season, pid_season (VARCHAR)
- startdate, enddate (TIMESTAMP): monitoring window

**WHEN TO USE EACH TABLE:**
| Question | Table |
|---|---|
| Cohort characteristics, outcomes (birthweight, preterm, SGA), demographics | daily_data |
| Area-level climate exposure (ERA5, CAMS2, MERRA2), satellite data | daily_data |
| Participants WITHOUT personal sensor data (n=6,960 total) | daily_data |
| Personal sensor PM2.5, NO2, temperature, humidity | sensor_daily |
| Intra-day patterns, diurnal variation, time of peak exposure | sensor_raw |
| Comparing satellite vs personal exposure (exposure misclassification) | JOIN both |
| Fire-smoke PM2.5 exceedance/smoke-wave days by pregnancy period, or fire-smoke exposure vs. pregnancy outcomes | fire_smoke_period_summary (or _wide) |
| Day-level fire-smoke exceedance flags (not pre-aggregated) | fire_smoke_daily |
| What Fire_Smoke_PM25 value counts as "high" in a country | fire_smoke_country_thresholds |

=== FIRE-SMOKE PM2.5 EXCEEDANCE DATABASE (derived from daily_data.Fire_Smoke_PM25) ===

Landscape fire-smoke PM2.5 exposure, summarised against country-specific thresholds and broken out by pregnancy period. Built the same way as the ERA5_T2M_extreme_hot_day/heatwave_day columns in daily_data (per-group percentile threshold → binary exceedance → consecutive-run detection), applied to Fire_Smoke_PM25 instead of temperature, with three threshold levels instead of one.

**Table: fire_smoke_country_thresholds** — 3 rows, one per country
- Country (VARCHAR), n_location_days (BIGINT)
- p75_fire_smoke_pm25, p90_fire_smoke_pm25, p95_fire_smoke_pm25 (DOUBLE): the 75th/90th/95th percentile of Fire_Smoke_PM25 for that country, computed over distinct village-days (not participant-weighted) across the full available time series — this is the "what counts as smoky here" reference value.

**Table: fire_smoke_daily** — 2,641,625 rows — ONE ROW PER PARTICIPANT PER DAY within her preconception-through-postpartum window
- f2a_participant_id, Country, Village, exposure_day (DATE)
- conception_date, delivery_date (DATE, delivery_date may be NULL), ga_days_from_conception (INTEGER, negative = preconception)
- period (VARCHAR): 'preconception' (90 days before conception) / 'trimester_1' (GA days 0-90) / 'trimester_2' (91-195) / 'trimester_3' (196+) / 'postpartum' (30 days after delivery_date, only present when delivery_date is known). trimester_3 does NOT require a known delivery_date — pregnancies ending in miscarriage/stillbirth with no recorded delivery_date are still included (capped at conception_date+300 days as a fallback so the window doesn't run on indefinitely).
- Fire_Smoke_PM25 (DOUBLE), p75/p90/p95_fire_smoke_pm25 (DOUBLE, that day's country threshold)
- exceed_p75, exceed_p90, exceed_p95 (0/1): day exceeded that threshold
- run_length_p75/p90/p95 (INTEGER): length of the consecutive-exceedance run this day belongs to (0 if not exceeding)
- smokewave_day_p75/p90/p95 (0/1): day is part of a "smoke-wave" — a run of >=2 consecutive exceedance days (mirrors ERA5_T2M_heatwave_day logic)

**Table: fire_smoke_period_summary** — 95,748 rows — one row per participant x period x threshold_level (LONG/TIDY — best for stats, regression, mixed models)
- f2a_participant_id, Country, period (VARCHAR, same 5 values as above), threshold_level (VARCHAR: 'p75'/'p90'/'p95'), threshold_value (DOUBLE)
- n_days_in_period, mean_fire_smoke_pm25, max_fire_smoke_pm25
- exceedance_days (INTEGER): raw count of days > threshold in this period
- pct_days_exceeded (DOUBLE): exceedance_days / n_days_in_period
- smokewave_days (INTEGER): days that were part of a >=2-day consecutive exceedance run
- n_smokewave_events (INTEGER): number of distinct qualifying runs touching this period
- max_consecutive_run_days (INTEGER): longest exceedance run touching this period
- Pregnancy outcomes (repeated across each period/threshold row for that participant): conception_date, delivery_date, GA_PRECISE_weeks_at_outcome, bornalive, livebirth, stillbirth, neonataldeath, miscarriage, preterm, lowbirthweight, sga, nicu_admission, maternal_death, ageatdeath, baby_death_date, Birthweight, bwt_zscore (same formats as in daily_data)

**Table: fire_smoke_period_summary_wide** — 31,916 rows — one row per participant x period, threshold-suffixed columns (exceedance_days_p75/p90/p95, smokewave_days_p75/p90/p95, n_smokewave_events_p75/p90/p95, max_consecutive_run_days_p75/p90/p95) plus the same outcome columns. Easier than the long table for a quick Table 1 or CSV-style browse; use the long table for anything that needs to filter/group by threshold_level.

**Table: participant_outcomes** — 6,960 rows — one row per participant, just Country + the pregnancy outcome columns listed above (no exposure data). Useful when you need outcomes without pulling in daily_data's full row set.

**EXAMPLE — did trimester_3 smoke-waves associate with stillbirth?**
```sql
SELECT Country,
       stillbirth,
       ROUND(AVG(smokewave_days), 2) AS mean_smokewave_days_t3,
       COUNT(*) AS n_women
FROM fire_smoke_period_summary
WHERE period = 'trimester_3' AND threshold_level = 'p90' AND Country IS NOT NULL
GROUP BY Country, stillbirth
ORDER BY Country, stillbirth
```
Always filter both `period` and `threshold_level` explicitly — don't assume a default.

**IMPORTANT — TEMPORAL MISMATCH:**
The sensor monitoring window (2022–2023) was conducted AFTER most participants' pregnancy period. Do NOT join on date. Instead, join at the participant level — aggregate each participant's pregnancy-period satellite exposure from daily_data and their sensor-period personal exposure from sensor_daily separately, then compare.

**CROSS-DATABASE JOIN — satellite (pregnancy period) vs personal sensor (monitoring window):**
```sql
-- Country-level summary: average satellite PM2.5 during pregnancy vs average personal PM2.5 during sensor window
WITH sat AS (
    SELECT f2a_participant_id, Country,
           AVG(CAMS2_pm2p5_ugm3) AS satellite_pm25_pregnancy
    FROM daily_data
    WHERE Country IS NOT NULL AND CAMS2_pm2p5_ugm3 IS NOT NULL
    GROUP BY f2a_participant_id, Country
),
sens AS (
    SELECT pid, country,
           AVG(pm25_mean) AS personal_pm25_sensor,
           COUNT(*)       AS sensor_days
    FROM sensor_daily
    GROUP BY pid, country
),
joined AS (
    SELECT sat.Country,
           sat.satellite_pm25_pregnancy,
           sens.personal_pm25_sensor,
           sens.personal_pm25_sensor - sat.satellite_pm25_pregnancy AS exposure_difference
    FROM sat
    JOIN sens ON sat.f2a_participant_id = sens.pid
)
SELECT Country,
       ROUND(AVG(satellite_pm25_pregnancy),2) AS mean_satellite_pm25,
       ROUND(AVG(personal_pm25_sensor),2)     AS mean_personal_pm25,
       ROUND(AVG(exposure_difference),2)      AS mean_difference,
       COUNT(*)                               AS n_participants
FROM joined
GROUP BY Country
ORDER BY Country
```
Join key: sensor_daily.pid = daily_data.f2a_participant_id (participant level only — do NOT join on date)

NOTE: Only 343 participants have sensor data (Kenya=105, Mozambique=78, Gambia=160). Counts from sensor_daily/sensor_raw will be much smaller than daily_data. Always note this when reporting sensor-based results.

=== QUERY GUIDELINES ===
Always use aggregations — never return more than 50 raw rows.

**CHARTING RULES — MANDATORY:**
Always call render_chart IN THE SAME RESPONSE as execute_query (both tools in one turn). You decide the chart type — even when the user does not ask for one, pick the most appropriate type for the data returned.

EXCEPTION — Table 1 / descriptive statistics / characteristics table requests do NOT follow this rule. Call generate_table1 ONLY (see TABLE 1 section below) — do not call execute_query or render_chart for that request.

CRITICAL — chart data must come from the query you just ran, not a separate unrelated query. If the analysis filtered to Country='Gambia' AND PM2.5>500, the chart/map must show those exact records — never silently run a broader query to get "nicer" chart data and then present it alongside text about a different subset.

**AUTO-SELECT the right chart type:**
| Situation | Chart type |
|---|---|
| "compare X across countries/settlements", means/counts grouped by category | bar |
| "how does X vary", "distribution of", "spread", "outliers", "percentiles" | box |
| "breakdown", "proportion", "share", "what percentage" | pie or doughnut |
| "trend over time", "monthly", "seasonal", "how has X changed" | line |
| "relationship between X and Y", "does X correlate with Y" | scatter |
| "correlation matrix", "correlations between multiple variables" | heatmap |
| After run_regression with multiple predictors | forest |
| "map", "where are participants", "geographic", "spatial distribution", "show on a map" | map |
| Single scalar result (one number) | no chart needed |

**How to query and build each type:**
- **bar**: GROUP BY category → AVG/COUNT → labels=categories, datasets=[{label, data}]
- **line**: DATE_TRUNC('month', exposure_day::DATE) as month GROUP BY month ORDER BY month → labels=months
- **pie/doughnut**: COUNT GROUP BY → compute % → labels=groups, datasets=[{label, data:[...]}]
- **box** (always use PERCENTILE_CONT — never pass raw rows):
  ```sql
  SELECT Country,
    PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY col) as p5,
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY col) as q1,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY col) as median,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY col) as q3,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY col) as p95,
    AVG(col) as mean
  FROM daily_data WHERE Country IS NOT NULL AND col IS NOT NULL GROUP BY Country
  ```
  → box_data: [{name, lowerfence:p5, q1, median, q3, upperfence:p95, mean}]
  One render_chart call per variable. For multiple variables call render_chart once each.
- **heatmap**: SELECT CORR(a,b), CORR(a,c)… FROM daily_data → build symmetric z_values matrix
  → x_labels=y_labels=[var names], z_values=[[1,r_ab,…],[r_ab,1,…],…]
- **scatter**: SELECT col_a, col_b FROM daily_data ORDER BY RANDOM() LIMIT 500
  → scatter_data: [{name, x:[...], y:[...]}] — one series per group if comparing countries
- **map** (geographic scatter — always aggregate to village level for clean maps):
  ```sql
  SELECT Village,
      AVG(Latitude)              AS lat,       -- Latitude → lat (ALWAYS, never swap)
      AVG(Longitude)             AS lon,       -- Longitude → lon (ALWAYS, never swap)
      MAX(Country)               AS country,
      AVG(CAMS2_pm2p5_ugm3)      AS value,
      COUNT(DISTINCT f2a_participant_id) AS n_participants
  FROM daily_data
  WHERE Latitude IS NOT NULL AND Longitude IS NOT NULL AND Country IS NOT NULL
  GROUP BY Village
  ```
  → map_data: [{lat, lon, label: Village + " (" + country + ")", value, size: n_participants}]
  → color_label: "Mean PM2.5 (μg/m³)"
  For binary outcomes (e.g. preterm rate): AVG(CASE WHEN preterm='Yes' THEN 1.0 ELSE 0.0 END)*100 AS value → color_label: "Preterm Rate (%)"
  Participant-level maps: GROUP BY f2a_participant_id — LIMIT 3000.

  CONSISTENCY RULE — the map query MUST filter to the same subset as the analysis:
  If the text analysis filtered to Country='Gambia' AND PM2.5 > 500, the map SQL must include those exact same WHERE conditions. Never render a generic all-villages map when the analysis is about a specific filtered subset — the dots on the map must represent the same records the text is describing.

  COORDINATE VALIDATION — check your map_data before rendering:
  - Gambia:     lat 12–14°N  (positive),  lon -17 to -13°W (negative)
  - Kenya:      lat -5 to 5°N (near zero), lon  34–42°E    (positive ~39)
  - Mozambique: lat -27 to -10°S (negative), lon 32–41°E  (positive ~35)
  If any point falls outside these ranges you have swapped Latitude/Longitude — fix the SQL aliases before calling render_chart.

- **forest** (always call after run_regression — use the coefficients it returned):
  → forest_data: [{variable, coef, ci_lower, ci_upper, p_value, stars}] — exclude Intercept row
  → scale: 'OR' for Logit models (exponentiates to odds ratios, null line at 1), 'coef' for OLS
  → x_label: e.g. "Coefficient (grams)" for OLS, "Odds Ratio" for Logit

NEVER echo chart_type, labels, datasets, box_data, or any raw numbers/JSON in your text. Write only natural language findings.

=== REGRESSION & STATISTICAL INFERENCE ===

**Phase 1 — Simple regression via DuckDB (for single-predictor questions, no extra tool):**
When asked for slope, trend, or R² between two continuous variables, use REGR_* inside execute_query:
```sql
SELECT Country,
    REGR_SLOPE(Birthweight, CAMS2_pm2p5_ugm3)     AS slope,
    REGR_INTERCEPT(Birthweight, CAMS2_pm2p5_ugm3) AS intercept,
    REGR_R2(Birthweight, CAMS2_pm2p5_ugm3)        AS r_squared,
    COUNT(*)                                        AS n
FROM daily_data
WHERE CAMS2_pm2p5_ugm3 IS NOT NULL AND Birthweight IS NOT NULL AND Country IS NOT NULL
GROUP BY Country
```
Interpret: "Each 1 μg/m³ increase in PM2.5 is associated with a [slope]g change in birthweight (R²=[r_squared], n=[n])."

**Phase 2 — Multivariate regression via run_regression tool:**
Use when: user wants to control for confounders, multiple predictors in one model, binary outcomes (LBW/preterm), user asks for p-values or statistical significance.

model_type:
- OLS → continuous outcomes (Birthweight, GA_PRECISE, bwt_zscore, maternal_bmi, UTCI_mean)
- Logit → binary 0/1 outcomes — convert VARCHAR in SQL first

formula syntax (R-style):
- 'Birthweight ~ CAMS2_pm2p5_ugm3 + RWI'  — OLS, two predictors
- 'Birthweight ~ CAMS2_pm2p5_ugm3 + RWI + C(Country)'  — C() wraps categorical fixed effects
- 'lowbirthweight ~ CAMS2_pm2p5_ugm3 + RWI + C(Country)'  — Logit
- 'Birthweight ~ CAMS2_pm2p5_ugm3 * RWI'  — interaction term

SQL rules for run_regression — ALWAYS aggregate to EXACTLY one row per participant:

**EXPOSURE WINDOW — this is the most important rule:**
The dataset deliberately includes daily rows from preconception through ~1 month post-delivery. Each window is epidemiologically distinct and must be selected correctly using CASE WHEN inside AVG() — NEVER average across all rows indiscriminately.

Named exposure windows and their SQL filters:

  Preconception — 3 months before conception (default when user says "preconception"):
    exposure_day::DATE >= (conception_date - INTERVAL '90 days')
    AND exposure_day::DATE < conception_date
  Preconception — 6 months (use if user specifies):
    exposure_day::DATE >= (conception_date - INTERVAL '180 days')
    AND exposure_day::DATE < conception_date
  Whole gestation (default for most birth outcome analyses):
    exposure_day::DATE >= conception_date AND exposure_day::DATE <= delivery_date
  T1 — organogenesis, weeks 1–12:
    DATEDIFF('day', conception_date, exposure_day::DATE) BETWEEN 0 AND 83
  T2 — fetal growth, weeks 13–26:
    DATEDIFF('day', conception_date, exposure_day::DATE) BETWEEN 84 AND 181
  T3 — lung maturation/preterm risk, weeks 27–birth:
    DATEDIFF('day', conception_date, exposure_day::DATE) >= 182
  Post-delivery — 1 month after birth (neonatal outcomes):
    exposure_day::DATE > delivery_date
    AND exposure_day::DATE <= (delivery_date + INTERVAL '30 days')

When user does NOT specify a window, default to whole gestation for birth outcome regressions.
When user says "preconception", use the 3-month window unless they specify otherwise.
When reporting results, ALWAYS state which exposure window was used: "using mean exposure over the 3-month preconception period" or "gestational mean" etc.

GROUP BY f2a_participant_id only — do NOT include Country, GHSL_class, or other categorical columns in GROUP BY (they are constant per participant; include them in SELECT as MAX(Country) etc.)

Binary VARCHAR outcomes (lowbirthweight, preterm, stillbirth, neonataldeath, livebirth) MUST be converted with CASE WHEN col='Yes' THEN 1 ELSE 0 END.

LIMIT 10000 always.

**Canonical example — gestational + preconception PM2.5, comparing both windows:**
```sql
SELECT f2a_participant_id,
    MAX(Country)    AS Country,
    MAX(GHSL_class) AS GHSL_class,
    -- preconception mean (3 months before conception)
    AVG(CASE WHEN exposure_day::DATE >= (conception_date - INTERVAL '90 days')
              AND exposure_day::DATE <  conception_date
             THEN CAMS2_pm2p5_ugm3 END)                          AS PM25_preconception,
    -- whole-gestation mean
    AVG(CASE WHEN exposure_day::DATE >= conception_date
              AND exposure_day::DATE <= delivery_date
             THEN CAMS2_pm2p5_ugm3 END)                          AS PM25_gestational,
    AVG(CASE WHEN exposure_day::DATE >= conception_date
              AND exposure_day::DATE <= delivery_date
             THEN RWI END)                                         AS RWI,
    MAX(Birthweight)                                               AS Birthweight,
    MAX(CASE WHEN lowbirthweight='Yes' THEN 1 ELSE 0 END)         AS lowbirthweight,
    MAX(CASE WHEN preterm='Yes' THEN 1 ELSE 0 END)                AS preterm
FROM daily_data
WHERE Country IS NOT NULL
  AND conception_date IS NOT NULL
  AND delivery_date IS NOT NULL
  AND Birthweight IS NOT NULL
GROUP BY f2a_participant_id
LIMIT 10000
```

CRITICAL — the N should be the number of DISTINCT participants with complete data (≤ 6,960). If N > 6,960 the GROUP BY is wrong — fix it before proceeding.

When reporting results, always state clearly: "exposure averaged over the gestational period" (or "T1/T2/T3" if trimester-specific).

After run_regression returns results, ALWAYS call render_chart with chart_type='forest' using the coefficients from the result (exclude the Intercept row). Use scale='OR' for Logit models. Then write a clinical interpretation: for each significant predictor (p<0.05), state the coefficient and what it means for maternal health. Mention N and R²/pseudo-R².

**REGRESSION INTERPRETATION RULES:**
- When interpreting a regression table, every coefficient, OR, SE, CI, and p-value you quote in text MUST come directly from the run_regression result returned to you in this conversation. Never invent, round differently, or recompute them from memory.
- The reference category in a categorical variable (e.g. Gambia in C(Country)) is captured by the Intercept — it does NOT appear as a named row. If the user asks about the reference group's absolute rate, run a separate execute_query COUNT to get it from the data.
- When converting a logit coefficient to an Odds Ratio, compute exp(coef) exactly — e.g. coef=0.1050 → OR=exp(0.1050)=1.111, NOT 0.756. Never cite an OR that contradicts the coefficient in the table.
- If asked a follow-up about regression results, re-read the regression output from earlier in the conversation rather than guessing.

**TABLE 1 / DESCRIPTIVE STATISTICS TABLES:**
- When the user asks for a "Table 1", "descriptive statistics table", "characteristics table", or a summary of variables split by country/group, call generate_table1 and ONLY generate_table1 for that turn — do NOT also call execute_query or render_chart, and do NOT try to hand-build this with execute_query. This takes priority over the mandatory charting rule above.
- Map whatever variables the user names (in plain language) to the closest matching column names from the schema above and pass those to generate_table1. Don't refuse or ask for exact column names first — just make your best mapping and call the tool.
- If some requested variables don't exist in the schema, still call the tool with the ones that do — generate_table1 reports unavailable variables back to you rather than failing, so mention those to the user rather than guessing a value for them.
- group_by defaults to Country if the user doesn't specify a different grouping.

**RESPONSE STYLE:**
- Always query the live database — never guess or cite from memory
- Present results with counts, percentages, and averages
- Compare across countries when relevant
- Highlight notable findings"""

_CHAT_TOOLS = [
    {
        "name": "execute_query",
        "description": "Run a SQL SELECT query against the PRECISE database. Tables available: (1) 'daily_data' — 3,129,121 rows, one per participant per exposure day, satellite/modelled exposures + outcomes; (2) 'sensor_daily' — 3,190 rows, personal sensor data aggregated to daily means/max/min/SD per participant; (3) 'sensor_raw' — 3,723,990 minute-level sensor readings (use sparingly with LIMIT); (4) 'fire_smoke_period_summary' — one row per participant x pregnancy period (preconception/trimester_1/trimester_2/trimester_3/postpartum) x threshold_level (p75/p90/p95), with Fire_Smoke_PM25 exceedance-day counts, smoke-wave (consecutive exceedance) metrics, and joined pregnancy outcomes — the table to use for fire-smoke exposure questions; (5) 'fire_smoke_period_summary_wide' — same grain minus threshold_level, one row per participant x period with threshold-suffixed columns, easier for browsing/Table 1; (6) 'fire_smoke_daily' — 2,641,625 rows, one per participant per pregnancy-window day, the underlying daily exceedance/smoke-wave flags; (7) 'fire_smoke_country_thresholds' — one row per country, the p75/p90/p95 Fire_Smoke_PM25 values used; (8) 'participant_outcomes' — one row per participant, pregnancy outcomes only (stillbirth, miscarriage, preterm, etc.), useful for joins that don't need daily_data's full row set. See the system prompt for which table to use. You can call this tool multiple times and can JOIN across tables.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A DuckDB-compatible SELECT query. Table names: 'daily_data', 'sensor_daily', 'sensor_raw', 'fire_smoke_period_summary', 'fire_smoke_period_summary_wide', 'fire_smoke_daily', 'fire_smoke_country_thresholds', 'participant_outcomes'. ALL numeric columns in daily_data are already DOUBLE — do NOT use TRY_CAST on any of them. ERA5_T2M_extreme_hot_day and ERA5_T2M_heatwave_day are VARCHAR 'Yes'/'No' columns — use = 'Yes' not = TRUE. ageatdeath is a real BOOLEAN — use = TRUE or = FALSE. sga is VARCHAR with full descriptive strings (not Yes/No) — use LIKE 'Small%' for SGA cases. In the fire_smoke_* tables, threshold_level is VARCHAR ('p75'/'p90'/'p95') and period is VARCHAR ('preconception'/'trimester_1'/'trimester_2'/'trimester_3'/'postpartum') — filter both explicitly rather than assuming a default."
                }
            },
            "required": ["sql"]
        }
    },
    {
        "name": "render_chart",
        "description": "Render an interactive chart inline in the conversation. Call in the SAME response as execute_query. Supports bar, line, pie, doughnut, scatter, box plot, and heatmap/correlation matrix.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "enum": ["bar", "line", "pie", "doughnut", "scatter", "box", "heatmap", "forest", "map"],
                    "description": "bar/line/pie/doughnut → use labels+datasets. box → use box_data (needs PERCENTILE_CONT query). heatmap → use x_labels+y_labels+z_values. scatter → use scatter_data. forest → use forest_data (coefficients + CIs from regression). map → use map_data (lat/lon points from village or participant aggregation)."
                },
                "title": {"type": "string", "description": "Chart title"},
                "labels": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Category labels for bar/line/pie/doughnut"
                },
                "datasets": {
                    "type": "array",
                    "description": "Data series for bar/line/pie/doughnut — one per group/variable",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "data":  {"type": "array", "items": {"type": "number"}}
                        },
                        "required": ["label", "data"]
                    }
                },
                "box_data": {
                    "type": "array",
                    "description": "Pre-computed box plot stats per group. Use PERCENTILE_CONT(0.05/0.25/0.50/0.75/0.95) in SQL.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":       {"type": "string"},
                            "lowerfence": {"type": "number", "description": "p5"},
                            "q1":         {"type": "number", "description": "p25"},
                            "median":     {"type": "number", "description": "p50"},
                            "q3":         {"type": "number", "description": "p75"},
                            "upperfence": {"type": "number", "description": "p95"},
                            "mean":       {"type": "number"}
                        },
                        "required": ["name", "q1", "median", "q3"]
                    }
                },
                "x_labels": {"type": "array", "items": {"type": "string"},
                             "description": "Column variable names for heatmap"},
                "y_labels": {"type": "array", "items": {"type": "string"},
                             "description": "Row variable names for heatmap"},
                "z_values": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                    "description": "2D array [rows][cols] of values in [-1,1] for correlation heatmaps"
                },
                "scatter_data": {
                    "type": "array",
                    "description": "Data series for scatter — query LIMIT 500 for a representative sample",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "x":    {"type": "array", "items": {"type": "number"}},
                            "y":    {"type": "array", "items": {"type": "number"}}
                        },
                        "required": ["name", "x", "y"]
                    }
                },
                "forest_data": {
                    "type": "array",
                    "description": "Predictor rows for a forest plot. Use after run_regression — pull values from the coefficients it returns. Exclude Intercept. For Logit models set scale='OR' to exponentiate.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "variable": {"type": "string"},
                            "coef":     {"type": "number", "description": "Point estimate (coefficient or log-OR)"},
                            "ci_lower": {"type": "number", "description": "Lower 95% CI"},
                            "ci_upper": {"type": "number", "description": "Upper 95% CI"},
                            "p_value":  {"type": "number"},
                            "stars":    {"type": "string", "description": "Significance stars from regression: ***, **, *, or empty"}
                        },
                        "required": ["variable", "coef", "ci_lower", "ci_upper"]
                    }
                },
                "scale": {
                    "type": "string",
                    "enum": ["coef", "OR"],
                    "description": "coef (default) — plot raw coefficients with null line at 0. OR — exponentiate to odds ratios, null line at 1. Use OR for Logit models."
                },
                "map_data": {
                    "type": "array",
                    "description": "Points for a geographic scatter map. Aggregate to village level (GROUP BY Village) for cleaner maps — 525 villages is ideal. Each point has a lat/lon, a numeric value for colour, and a label for hover.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "lat":   {"type": "number", "description": "Latitude"},
                            "lon":   {"type": "number", "description": "Longitude"},
                            "label": {"type": "string",  "description": "Hover label — village name, country, or participant ID"},
                            "value": {"type": "number",  "description": "Numeric value for colour scale — e.g. mean PM2.5, preterm rate"},
                            "size":  {"type": "number",  "description": "Optional marker size hint — e.g. n_participants"},
                            "group": {"type": "string",  "description": "Optional category for colour — e.g. Country"}
                        },
                        "required": ["lat", "lon", "label"]
                    }
                },
                "color_label": {"type": "string", "description": "Label for the colour scale on a map — e.g. 'Mean PM2.5 (μg/m³)'"},
                "y_label": {"type": "string", "description": "Y-axis label"},
                "x_label": {"type": "string", "description": "X-axis label"}
            },
            "required": ["chart_type", "title"]
        }
    },
    {
        "name": "run_regression",
        "description": "Run OLS or Logistic regression with statsmodels. Use when user wants to control for confounders, get p-values/confidence intervals, or model binary outcomes (LBW, preterm). The backend runs the model and streams a publication-quality coefficient table to the user. Do NOT use for simple single-predictor questions — use execute_query with REGR_SLOPE/REGR_R2 instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model_type": {
                    "type": "string",
                    "enum": ["OLS", "Logit"],
                    "description": "OLS for continuous outcomes (Birthweight, GA_PRECISE, bwt_zscore). Logit for binary 0/1 outcomes (lowbirthweight, preterm, stillbirth — must be CASE WHEN col='Yes' THEN 1 ELSE 0 END in SQL)."
                },
                "formula": {
                    "type": "string",
                    "description": "R-style formula. E.g. 'Birthweight ~ CAMS2_pm2p5_ugm3 + RWI + C(Country)' or 'lowbirthweight ~ CAMS2_pm2p5_ugm3 + RWI'. Use C(var) for categorical predictors."
                },
                "sql_query": {
                    "type": "string",
                    "description": "DuckDB SELECT that returns ONE ROW PER PARTICIPANT (GROUP BY f2a_participant_id, Country). Include all formula variables. Binary VARCHAR outcomes must be converted to 0/1 with CASE WHEN. Use LIMIT 10000."
                },
                "title": {
                    "type": "string",
                    "description": "Descriptive title for the coefficient table, e.g. 'Effect of PM2.5 on Birthweight controlling for Wealth Index'"
                }
            },
            "required": ["model_type", "formula", "sql_query", "title"]
        }
    },
    {
        "name": "generate_table1",
        "description": "Generate a standard epidemiology 'Table 1' — descriptive statistics split by a grouping variable (defaults to Country). Continuous variables are summarised as mean (SD); categorical/Yes-No variables as n (%) per category. Includes an Overall column alongside each group. Use this instead of execute_query whenever the user asks for a 'Table 1', 'descriptive statistics table', 'characteristics table', or similar summary-by-group table. Any requested variable not found in the schema is reported back as unavailable rather than failing the whole table — the rest of the table still builds, so don't be afraid to pass through whatever variables the user names.",
        "input_schema": {
            "type": "object",
            "properties": {
                "variables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Column names to summarise — map the user's request to the closest matching column names from the schema above, e.g. ['age_enrolment', 'hiv_status', 'parity', 'average_muac', 'CAMS2_pm2p5_ugm3', 'ERA5_T2M_Mean']."
                },
                "group_by": {
                    "type": "string",
                    "description": "Column to split the table by. Defaults to 'Country' if omitted."
                },
                "title": {
                    "type": "string",
                    "description": "Table title, e.g. 'Table 1. Participant characteristics by country'"
                }
            },
            "required": ["variables"]
        }
    }
]


@app.route('/api/catalogue-login', methods=['POST'])
def catalogue_login():
    """Exchange the catalogue access code for a short-lived session token."""
    code = (request.json or {}).get('code', '').strip()
    if code != CATALOGUE_ACCESS_CODE:
        return jsonify({'ok': False, 'error': 'Invalid access code'}), 403
    return jsonify({'ok': True, 'token': access_db.issue_catalogue_token()})


@app.route('/portal/chat', methods=['POST'])
def portal_chat():
    """Public, rate-limited informational chat for the main portal widget."""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    if not _allow_ip(ip):
        return jsonify({'error': 'Too many requests — please wait a moment.'}), 429

    messages = (request.json or {}).get('messages', [])
    if not messages or messages[-1].get('role') != 'user':
        return jsonify({'error': 'messages must end with a user turn'}), 400

    def generate():
        try:
            import anthropic as _anthropic
            client = _anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
            response = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=1600,
                system=_PORTAL_SYSTEM,
                messages=messages[-10:],
            )
            text = response.content[0].text if response.content else 'Sorry, I had trouble answering that.'
            yield f"data: {json.dumps({'type': 'text', 'text': text})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/chat', methods=['POST'])
def chat():
    key_info, err = require_key()
    if err:
        return err

    data     = request.json or {}
    messages = data.get('messages', [])

    if not messages or messages[-1].get('role') != 'user':
        return jsonify({'error': 'messages must end with a user turn'}), 400

    countries = key_info['countries']
    sess_token = request.headers.get('X-Session-Token', '')

    def generate():
        try:
            import anthropic as _anthropic
            client = _anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))

            # Resolve the catalogue user for this session (may be None for API-key callers)
            cat_user    = access_db.get_catalogue_user_from_token(sess_token) if sess_token else None
            tokens_this_req = 0

            history = list(messages)
            max_iterations = 8

            for i in range(max_iterations):
                # Enforce token budget before each LLM call
                if cat_user and (cat_user['tokens_used'] + tokens_this_req) >= cat_user['token_budget']:
                    yield f"data: {json.dumps({'type': 'error', 'text': 'Your token budget is exhausted. Please contact the administrator.'})}\n\n"
                    break

                # Force tool use only on iteration 0 so Claude must call
                # execute_query + render_chart together in one turn.
                # From iteration 1 onwards Claude can write narrative freely.
                tool_choice = {'type': 'any'} if i == 0 else {'type': 'auto'}
                status = 'Thinking…' if i == 0 else 'Analysing…'
                yield f"data: {json.dumps({'type': 'status', 'text': status})}\n\n"

                response = client.messages.create(
                    model='claude-sonnet-5',
                    max_tokens=4096,
                    system=_CHAT_SYSTEM_PROMPT,
                    messages=history,
                    tools=_CHAT_TOOLS,
                    tool_choice=tool_choice,
                )

                # Accumulate token usage
                if hasattr(response, 'usage'):
                    tokens_this_req += (response.usage.input_tokens +
                                        response.usage.output_tokens)

                tool_blocks = [b for b in response.content if b.type == 'tool_use']
                text_blocks = [b for b in response.content if b.type == 'text']
                final_text  = text_blocks[0].text if text_blocks else ''

                # Append assistant turn to in-flight history
                assistant_content = []
                for b in response.content:
                    if b.type == 'text':
                        assistant_content.append({'type': 'text', 'text': b.text})
                    elif b.type == 'tool_use':
                        assistant_content.append({
                            'type': 'tool_use', 'id': b.id,
                            'name': b.name, 'input': b.input,
                        })
                history.append({'role': 'assistant', 'content': assistant_content})

                if not tool_blocks:
                    yield f"data: {json.dumps({'type': 'text', 'text': final_text})}\n\n"
                    break

                # Execute tool calls — render_chart streams to browser, execute_query hits DuckDB
                tool_results = []
                conn = _open_conn()
                try:
                    for block in tool_blocks:
                        if block.name == 'render_chart':
                            yield f"data: {json.dumps({'type': 'chart', 'spec': block.input})}\n\n"
                            result = {'rendered': True}
                        elif block.name == 'run_regression':
                            reg_sql    = block.input.get('sql_query', '')
                            formula    = block.input.get('formula', '')
                            model_type = block.input.get('model_type', 'OLS')
                            reg_title  = block.input.get('title', 'Regression Analysis')
                            safe, reason = is_safe_query(reg_sql)
                            if not safe:
                                result = {'error': reason}
                                _log_query(sess_token, cat_user['id'] if cat_user else None,
                                           'run_regression', sql=reg_sql, formula=formula,
                                           model_type=model_type, error=f'blocked: {reason}')
                            else:
                                try:
                                    import pandas as _pd
                                    import statsmodels.formula.api as _smf
                                    filtered_reg = apply_country_filter(reg_sql, countries)
                                    yield f"data: {json.dumps({'type': 'status', 'text': 'Running regression model…'})}\n\n"
                                    df = conn.execute(filtered_reg).df()
                                    # Deduplicate on participant ID if present —
                                    # guards against GROUP BY mistakes that produce
                                    # multiple rows per person.
                                    id_col = next((c for c in df.columns
                                                   if 'participant_id' in c.lower()), None)
                                    if id_col and df[id_col].duplicated().any():
                                        df = df.drop_duplicates(subset=[id_col])
                                    df = df.dropna()
                                    if len(df) < 10:
                                        result = {'error': f'Too few complete observations ({len(df)}) for regression. Check the SQL query.'}
                                        _log_query(sess_token, cat_user['id'] if cat_user else None,
                                                   'run_regression', sql=filtered_reg, formula=formula,
                                                   model_type=model_type, error='too few observations')
                                    else:
                                        if model_type == 'Logit':
                                            fit = _smf.logit(formula, data=df).fit(disp=False)
                                            r2_key, r2_val = 'pseudo_r_squared', float(fit.prsquared)
                                        else:
                                            fit = _smf.ols(formula, data=df).fit()
                                            r2_key, r2_val = 'r_squared', float(fit.rsquared)
                                        ci = fit.conf_int()
                                        coefficients = []
                                        for var in fit.params.index:
                                            p = float(fit.pvalues[var])
                                            stars = '***' if p < 0.001 else '**' if p < 0.01 else '*' if p < 0.05 else ''
                                            coefficients.append({
                                                'variable': str(var),
                                                'coef':     round(float(fit.params[var]), 4),
                                                'se':       round(float(fit.bse[var]), 4),
                                                'p_value':  round(p, 4),
                                                'ci_lower': round(float(ci.loc[var, 0]), 4),
                                                'ci_upper': round(float(ci.loc[var, 1]), 4),
                                                'stars':    stars,
                                            })
                                        stats_table = {
                                            'title':        reg_title,
                                            'model_type':   model_type,
                                            'formula':      formula,
                                            'n_obs':        int(fit.nobs),
                                            r2_key:         round(r2_val, 4),
                                            'aic':          round(float(fit.aic), 1),
                                            'coefficients': coefficients,
                                        }
                                        yield f"data: {json.dumps({'type': 'stats_table', 'result': stats_table})}\n\n"
                                        _log_query(sess_token, cat_user['id'] if cat_user else None,
                                                   'run_regression', sql=filtered_reg, formula=formula,
                                                   model_type=model_type, n_obs=stats_table['n_obs'])
                                        # Return condensed summary to Claude for narrative
                                        result = {
                                            'n_obs': stats_table['n_obs'],
                                            r2_key:  stats_table[r2_key],
                                            'aic':   stats_table['aic'],
                                            'coefficients': [
                                                {k: v for k, v in c.items() if k != 'se'}
                                                for c in coefficients
                                            ],
                                        }
                                except Exception as e:
                                    result = {'error': f'Regression failed: {str(e)}'}
                                    _log_query(sess_token, cat_user['id'] if cat_user else None,
                                               'run_regression', sql=reg_sql, formula=formula,
                                               model_type=model_type, error=str(e))

                        elif block.name == 'generate_table1':
                            t1_vars  = block.input.get('variables', [])
                            t1_group = block.input.get('group_by') or 'Country'
                            t1_title = block.input.get('title', 'Table 1. Participant characteristics')
                            try:
                                table1 = _build_table1(
                                    conn, 'daily_data', t1_vars, t1_group,
                                    id_col='f2a_participant_id', countries=countries,
                                )
                                table1['title'] = t1_title
                                yield f"data: {json.dumps({'type': 'table1', 'result': table1})}\n\n"
                                _log_query(sess_token, cat_user['id'] if cat_user else None,
                                           'generate_table1', variables=t1_vars, group_by=t1_group,
                                           n=table1['overall_n'], unavailable=table1['unavailable'])
                                result = table1
                            except Exception as e:
                                result = {'error': f'Table 1 generation failed: {str(e)}'}
                                _log_query(sess_token, cat_user['id'] if cat_user else None,
                                           'generate_table1', variables=t1_vars, group_by=t1_group,
                                           error=str(e))

                        else:  # execute_query
                            sql = block.input.get('sql', '')
                            safe, reason = is_safe_query(sql)
                            if not safe:
                                result = {'error': reason}
                                _log_query(sess_token, cat_user['id'] if cat_user else None,
                                           'execute_query', sql=sql, error=f'blocked: {reason}')
                            else:
                                filtered = apply_country_filter(sql, countries)
                                try:
                                    cur  = conn.execute(filtered)
                                    cols = [d[0] for d in cur.description]
                                    rows = cur.fetchmany(50)
                                    # Return rows as dicts so Claude reads
                                    # {"Country":"Gambia","median":44.5} directly
                                    # instead of parsing column-index arrays —
                                    # prevents country↔value mix-ups in box_data.
                                    result = _strip_id_cols({
                                        'columns':   cols,
                                        'rows':      [
                                            {cols[j]: _coerce(v) for j, v in enumerate(r)}
                                            for r in rows
                                        ],
                                        'row_count': len(rows),
                                    }, _PRECISE_ID_COLS)
                                    _log_query(sess_token, cat_user['id'] if cat_user else None,
                                               'execute_query', sql=filtered, rows=len(rows))
                                except Exception as e:
                                    result = {'error': str(e)}
                                    _log_query(sess_token, cat_user['id'] if cat_user else None,
                                               'execute_query', sql=filtered, error=str(e))
                        tool_results.append({
                            'type':        'tool_result',
                            'tool_use_id': block.id,
                            'content':     json.dumps(result),
                        })
                finally:
                    conn.close()

                history.append({'role': 'user', 'content': tool_results})

            # Persist token usage for catalogue users
            if cat_user and tokens_this_req > 0:
                access_db.add_tokens_used(cat_user['id'], tokens_this_req)

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':      'no-cache',
            'X-Accel-Buffering':  'no',   # prevents nginx from buffering the stream
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# HE2AT CENTRE — CATALOGUE AI
# ══════════════════════════════════════════════════════════════════════════════

HE2AT_DB_PATH    = '/home/USER/PRECISE/he2at.duckdb'
HE2AT_ACCESS_CODE = os.environ.get('HE2AT_ACCESS_CODE', 'HE2AT2022')

_HE2AT_SYSTEM = """You are an expert research assistant for the HE²AT Centre (Heat and Health African Transdisciplinary Center). You help researchers explore environmental exposure data linked to pregnancy cohorts across Sub-Saharan Africa.

**STUDY OVERVIEW:**
74,483 pregnant women across 9 countries and 295 study locations, 21 study cohorts, 2003–2024.
Country breakdown: Kenya 25,139 | Malawi 14,037 | Zambia 12,024 | South Africa 11,534 | The Gambia 2,987 | Ethiopia 2,547 | Burkina Faso 2,362 | Ghana 1,997 | Tanzania 1,856

**CRITICAL RULES:**
1. ALWAYS call execute_query for any question about counts, distributions, trends, or statistics. Never guess numbers.
2. ALWAYS call render_chart in the SAME response as execute_query. Choose the best chart type automatically.
3. CHART DATA MUST MATCH TEXT: the chart must show exactly the same filtered subset as your narrative.
4. Use aggregations — never return more than 50 raw rows. The AI receives only up to 50 result rows.
5. If N patients > 74,483 your query is wrong — GROUP BY Patient_Identifier to deduplicate.
6. **PRIVACY — NEVER quote raw patient identifier hashes.** Do not include Patient_Identifier hash strings in your response text. You SHOULD report patient-level statistics: counts, distributions, min/max values, percentiles, percentages. When a query finds the most-exposed patient, describe their Study, Location, Country, and exposure level — omit the ID hash. Example: "The highest-exposed patient was in the MUL140 study, Nanoro, Burkina Faso with a mean UTCI of 33.4°C."

**DATABASES — three tables, one view:**

### PRIMARY TABLE: patient_exposures  (74,483 rows — ONE ROW PER PATIENT)
Use this for almost everything: comparisons across countries/studies, distributions, correlations.

**Identifiers:**
- Patient_Identifier (VARCHAR), Country (VARCHAR), Study (VARCHAR), Location (VARCHAR)
- Latitude (DOUBLE), Longitude (DOUBLE)

**Urbanisation & Climate Zone:**
- urbanization (DOUBLE: 10=Very Low Density Rural, 11=Low Density Rural, 12=Rural Cluster, 13=Suburban/Peri-urban, 21=Semi-dense Urban, 22=Dense Urban Cluster, 23=Urban Centre)
- urbanization_class (VARCHAR): 'Very Low Density Rural', 'Low Density Rural', 'Rural Cluster', 'Suburban / Peri-urban', 'Semi-dense Urban Cluster', 'Dense Urban Cluster', 'Urban Centre'
- ipcc_climate_zone_code (DOUBLE), ipcc_climate_zone_name (VARCHAR): 'Cold', 'Polar', 'Temperate', 'Tropical'

**Geography & Soil:**
- elevation (DOUBLE): metres above sea level
- soil_nitrogen (DOUBLE), soil_phosphorus (DOUBLE), soil_organic_carbon (DOUBLE): kg/ha or g/kg

**Socioeconomic:**
- rwi (DOUBLE): Relative Wealth Index (range −0.85 to 1.92; higher = wealthier)

**Temperature (all DOUBLE — patient gestational mean):**
- tas_mean: 2m air temperature mean (°C)
- tas_min: 2m air temperature minimum (°C)
- tas_max: 2m air temperature maximum (°C)

**Heat Stress Indices (all DOUBLE):**
- WBGT_mean: Wet Bulb Globe Temperature (°C) — occupational heat stress threshold: 28°C
- humidex_mean: Humidex (°C equivalent)
- Wind_Chill_mean, HI_mean: Heat Index (°C), tasapp_mean: apparent temperature (°C)
- tasdp_mean: dew point temperature (°C)
- WBGTsimple_mean: simplified WBGT (°C), WBT_mean: wet bulb temperature (°C)
- NET_mean: Net Effective Temperature (°C)
- ws_mean: wind speed (m/s)
- UTCI_min, UTCI_mean, UTCI_max: Universal Thermal Climate Index (°C)
  UTCI stress: <0 cold stress | 9–26 no stress | 26–32 moderate heat | 32–38 strong | 38–46 very strong | >46 extreme
- MRT_min, MRT_mean, MRT_max: Mean Radiant Temperature (°C)

**Air Quality (all DOUBLE):**
- pm2p5_mean: PM2.5 μg/m³ (WHO guideline: 5 μg/m³ annual; >35 unhealthy)
- no2as_mean: NO₂ (μg/m³)
- aod550_mean: aerosol optical depth at 550nm
- od550bc_mean: black carbon AOD

**Dust (all DOUBLE):**
- duaod550_nc_mean: dust AOD 550nm
- duexttau_ee_mean: dust extinction optical depth (MERRA-2)
- dusmass25_ee_mean: dust PM2.5 mass concentration (μg/m³)
- ducmass_ee_mean: total dust column mass (kg/m²)

**Climate & Environment (all DOUBLE):**
- RH_mean: relative humidity (%)
- ndvi: NDVI vegetation index (−1 to 1; >0.3 = moderate vegetation)
- precipitation: mm/day

### SECONDARY TABLE: climate_data  (305,808 rows — ONE ROW PER LOCATION PER DAY)
Use for temporal trends (monthly/seasonal), geographic patterns across locations.
Same climate columns as patient_exposures plus: Exposure_Date (DATE).
Join to exposure_days on (Study, Location, Exposure_Date) if you need patient context.
Key columns: Country, Study, Location, Latitude, Longitude, Exposure_Date, [all climate columns]

### SECONDARY TABLE: exposure_days  (23M rows — ONE ROW PER PATIENT PER DAY)
Use only with aggressive GROUP BY. Contains: Patient_Identifier, Country, Study, Location,
Latitude, Longitude, Birth_Date (DATE), GA_Days (DOUBLE: gestational age in days),
Exposure_Date (DATE), Window_Type ('full' or 'dob_only').
Window_Type='full' means full gestational period; 'dob_only' means date of birth only.

### VIEW: exposure_climate  (joins exposure_days + climate_data)
Full patient-day dataset with all climate variables. Use with heavy aggregation only.
Always add WHERE Window_Type='full' for gestational analyses.

=== QUERY STRATEGY ===

| Question type | Table to use |
|---|---|
| Mean exposure by country/study/urbanisation | patient_exposures |
| Distribution, box plots, correlations | patient_exposures |
| Regression (exposure → health) | patient_exposures (GROUP BY Patient_Identifier already done) |
| Monthly/seasonal trends | climate_data (GROUP BY month/season) |
| Geographic map of exposures | patient_exposures (Latitude, Longitude per patient) |
| Patient count by country | patient_exposures |
| Gestational day trajectory | exposure_climate WHERE Window_Type='full' |

=== CHARTING RULES (MANDATORY) ===
Always call render_chart IN THE SAME RESPONSE as execute_query. Pick the best type automatically:

EXCEPTION — Table 1 / descriptive statistics / characteristics table requests do NOT follow this rule. Call generate_table1 ONLY (see TABLE 1 section below) — do not call execute_query or render_chart for that request.

| Situation | Chart type |
|---|---|
| Compare means across countries/studies/urban classes | bar |
| Distribution, spread, outliers | box |
| Proportion, breakdown by category | pie or doughnut |
| Trend over time (monthly, seasonal) | line |
| Relationship between two continuous variables | scatter |
| Correlation matrix | heatmap |
| Regression coefficients with CIs | forest |
| Geographic pattern, "where", "map", spatial | map |

**box** — use PERCENTILE_CONT:
```sql
SELECT Country,
  PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY col) as p5,
  PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY col) as q1,
  PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY col) as median,
  PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY col) as q3,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY col) as p95,
  AVG(col) as mean
FROM patient_exposures WHERE Country IS NOT NULL AND col IS NOT NULL GROUP BY Country
```
→ box_data: [{name, lowerfence:p5, q1, median, q3, upperfence:p95, mean}]

**map** — aggregate to location level:
```sql
SELECT Location, AVG(Latitude) AS lat, AVG(Longitude) AS lon,
       MAX(Country) AS country, AVG(pm2p5_mean) AS value,
       COUNT(DISTINCT Patient_Identifier) AS n_patients
FROM patient_exposures
WHERE Latitude IS NOT NULL AND Longitude IS NOT NULL
GROUP BY Location
```
→ map_data: [{lat, lon, label, value, size: n_patients}]

COORDINATE VALIDATION (check before rendering):
- Burkina Faso: lat 10–16°N, lon −6 to 2°E
- Ethiopia:     lat 4–15°N, lon 33–48°E
- Ghana:        lat 5–11°N, lon −4 to 1°E
- Kenya:        lat −5 to 5°N, lon 34–42°E
- Malawi:       lat −17 to −9°S, lon 32–36°E
- South Africa: lat −35 to −22°S, lon 16–33°E
- Tanzania:     lat −12 to −1°S, lon 29–41°E
- The Gambia:   lat 13–14°N, lon −17 to −13°W
- Zambia:       lat −18 to −8°S, lon 22–34°E

NEVER echo raw chart JSON, labels, or datasets in your text. Write only natural language findings.

=== REGRESSION ===
**Simple (single predictor):** use REGR_SLOPE / REGR_R2 inside execute_query on patient_exposures.
**Multivariate:** use run_regression tool. patient_exposures already has one row per patient — no GROUP BY needed.
- OLS for continuous (WBGT_mean, pm2p5_mean, UTCI_mean, tas_mean)
- Logit for binary outcomes (must be CASE WHEN ... THEN 1 ELSE 0 END)
- Always state which exposure variable and what the coefficient means clinically.

**TABLE 1 / DESCRIPTIVE STATISTICS TABLES:**
- When the user asks for a "Table 1", "descriptive statistics table", "characteristics table", or a summary of variables split by country/group, call generate_table1 and ONLY generate_table1 for that turn — do NOT also call execute_query or render_chart, and do NOT try to hand-build this with execute_query. This takes priority over the mandatory charting rule above.
- Map whatever variables the user names (in plain language) to the closest matching column names from the schema above and pass those to generate_table1. Don't refuse or ask for exact column names first — just make your best mapping and call the tool.
- If some requested variables don't exist in the schema, still call the tool with the ones that do — generate_table1 reports unavailable variables back to you rather than failing, so mention those to the user rather than guessing a value for them.
- group_by defaults to Country if the user doesn't specify a different grouping.

**RESPONSE STYLE:**
- Always query the live database — never guess or cite numbers from memory.
- Compare across countries when relevant.
- Always state N (number of patients) in any result.
- Highlight the most notable finding first."""


@app.route('/api/he2at-login', methods=['POST'])
def he2at_login():
    """Exchange the HE2AT catalogue access code for a short-lived session token."""
    code = (request.json or {}).get('code', '').strip()
    if code != HE2AT_ACCESS_CODE:
        return jsonify({'ok': False, 'error': 'Invalid access code'}), 403
    return jsonify({'ok': True, 'token': access_db.issue_catalogue_token()})


@app.route('/api/he2at/chat', methods=['POST'])
def he2at_chat():
    """HE2AT Centre AI research assistant — token-gated, streams SSE."""
    token = request.headers.get('X-Session-Token', '')
    if not token or not access_db.validate_catalogue_token(token):
        return jsonify({'error': 'Valid session token required. Please log in.'}), 401

    data     = request.json or {}
    messages = data.get('messages', [])
    if not messages or messages[-1].get('role') != 'user':
        return jsonify({'error': 'messages must end with a user turn'}), 400

    def generate():
        try:
            import anthropic as _anthropic
            client      = _anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
            cat_user    = access_db.get_catalogue_user_from_token(token)
            tokens_this_req = 0
            history     = list(messages)
            max_iterations = 8

            for i in range(max_iterations):
                # Enforce token budget
                if cat_user and (cat_user['tokens_used'] + tokens_this_req) >= cat_user['token_budget']:
                    yield f"data: {json.dumps({'type': 'error', 'text': 'Your token budget is exhausted. Please contact the administrator.'})}\n\n"
                    break

                tool_choice = {'type': 'any'} if i == 0 else {'type': 'auto'}
                yield f"data: {json.dumps({'type': 'status', 'text': 'Thinking…' if i == 0 else 'Analysing…'})}\n\n"

                response = client.messages.create(
                    model='claude-sonnet-5',
                    max_tokens=4096,
                    system=_HE2AT_SYSTEM,
                    messages=history,
                    tools=_CHAT_TOOLS,
                    tool_choice=tool_choice,
                )

                if hasattr(response, 'usage'):
                    tokens_this_req += (response.usage.input_tokens +
                                        response.usage.output_tokens)

                tool_blocks = [b for b in response.content if b.type == 'tool_use']
                text_blocks = [b for b in response.content if b.type == 'text']

                assistant_content = []
                for b in response.content:
                    if b.type == 'text':
                        assistant_content.append({'type': 'text', 'text': b.text})
                    elif b.type == 'tool_use':
                        assistant_content.append({
                            'type': 'tool_use', 'id': b.id,
                            'name': b.name, 'input': b.input,
                        })
                history.append({'role': 'assistant', 'content': assistant_content})

                if not tool_blocks:
                    yield f"data: {json.dumps({'type': 'text', 'text': text_blocks[0].text if text_blocks else ''})}\n\n"
                    break

                tool_results = []
                conn = duckdb.connect(HE2AT_DB_PATH, read_only=True)
                try:
                    for block in tool_blocks:
                        if block.name == 'render_chart':
                            yield f"data: {json.dumps({'type': 'chart', 'spec': block.input})}\n\n"
                            result = {'rendered': True}

                        elif block.name == 'run_regression':
                            reg_sql    = block.input.get('sql_query', '')
                            formula    = block.input.get('formula', '')
                            model_type = block.input.get('model_type', 'OLS')
                            reg_title  = block.input.get('title', 'Regression')
                            safe, reason = is_safe_query(reg_sql)
                            if not safe:
                                result = {'error': reason}
                            else:
                                try:
                                    import pandas as _pd
                                    import statsmodels.formula.api as _smf
                                    yield f"data: {json.dumps({'type': 'status', 'text': 'Running regression…'})}\n\n"
                                    df = conn.execute(reg_sql).df()
                                    id_col = next((c for c in df.columns if 'patient' in c.lower()), None)
                                    if id_col and df[id_col].duplicated().any():
                                        df = df.drop_duplicates(subset=[id_col])
                                    df = df.dropna()
                                    if len(df) < 10:
                                        result = {'error': f'Too few observations ({len(df)}) for regression.'}
                                    else:
                                        if model_type == 'Logit':
                                            fit = _smf.logit(formula, data=df).fit(disp=False)
                                            r2_key, r2_val = 'pseudo_r_squared', float(fit.prsquared)
                                        else:
                                            fit = _smf.ols(formula, data=df).fit()
                                            r2_key, r2_val = 'r_squared', float(fit.rsquared)
                                        ci = fit.conf_int()
                                        coefficients = []
                                        for var in fit.params.index:
                                            p = float(fit.pvalues[var])
                                            stars = '***' if p < 0.001 else '**' if p < 0.01 else '*' if p < 0.05 else ''
                                            coefficients.append({
                                                'variable': str(var),
                                                'coef':     round(float(fit.params[var]), 4),
                                                'se':       round(float(fit.bse[var]), 4),
                                                'p_value':  round(p, 4),
                                                'ci_lower': round(float(ci.loc[var, 0]), 4),
                                                'ci_upper': round(float(ci.loc[var, 1]), 4),
                                                'stars':    stars,
                                            })
                                        stats_table = {
                                            'title': reg_title, 'model_type': model_type,
                                            'formula': formula, 'n_obs': int(fit.nobs),
                                            r2_key: round(r2_val, 4),
                                            'aic': round(float(fit.aic), 1),
                                            'coefficients': coefficients,
                                        }
                                        yield f"data: {json.dumps({'type': 'stats_table', 'result': stats_table})}\n\n"
                                        result = {
                                            'n_obs': stats_table['n_obs'], r2_key: stats_table[r2_key],
                                            'aic': stats_table['aic'],
                                            'coefficients': [{k: v for k, v in c.items() if k != 'se'} for c in coefficients],
                                        }
                                except Exception as e:
                                    result = {'error': f'Regression failed: {e}'}

                        elif block.name == 'generate_table1':
                            t1_vars  = block.input.get('variables', [])
                            t1_group = block.input.get('group_by') or 'Country'
                            t1_title = block.input.get('title', 'Table 1. Patient characteristics')
                            try:
                                table1 = _build_table1(
                                    conn, 'patient_exposures', t1_vars, t1_group,
                                    id_col=None, countries=None,
                                )
                                table1['title'] = t1_title
                                yield f"data: {json.dumps({'type': 'table1', 'result': table1})}\n\n"
                                result = table1
                            except Exception as e:
                                result = {'error': f'Table 1 generation failed: {str(e)}'}

                        else:  # execute_query
                            sql = block.input.get('sql', '')
                            safe, reason = is_safe_query(sql)
                            if not safe:
                                result = {'error': reason}
                            else:
                                try:
                                    cur  = conn.execute(sql)
                                    cols = [d[0] for d in cur.description]
                                    rows = cur.fetchmany(50)
                                    result = _strip_id_cols({
                                        'columns':   cols,
                                        'rows':      [{cols[j]: _coerce(v) for j, v in enumerate(r)} for r in rows],
                                        'row_count': len(rows),
                                    }, _HE2AT_ID_COLS)
                                except Exception as e:
                                    result = {'error': str(e)}

                        tool_results.append({
                            'type': 'tool_result', 'tool_use_id': block.id,
                            'content': json.dumps(result),
                        })
                finally:
                    conn.close()

                history.append({'role': 'user', 'content': tool_results})

            if cat_user and tokens_this_req > 0:
                access_db.add_tokens_used(cat_user['id'], tokens_this_req)

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ══════════════════════════════════════════════════════════════════════════════
# SHARED CONVERSATION LINKS
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/share', methods=['POST'])
def create_share():
    """Store a conversation for public read-only sharing. Requires catalogue session token."""
    token = request.headers.get('X-Session-Token', '')
    if not token or not access_db.validate_catalogue_token(token):
        return jsonify({'error': 'Authentication required'}), 401

    data      = request.json or {}
    catalogue = data.get('catalogue', '')
    messages  = data.get('messages', [])
    title     = data.get('title', '')

    if not messages:
        return jsonify({'error': 'No messages to share'}), 400

    share_id = access_db.create_shared_conversation(catalogue, title, messages)
    return jsonify({'ok': True, 'share_id': share_id})


@app.route('/api/share/<share_id>', methods=['GET'])
def get_share(share_id):
    """Retrieve a shared conversation — public, no auth required."""
    conv = access_db.get_shared_conversation(share_id)
    if not conv:
        return jsonify({'error': 'Share not found'}), 404
    return jsonify(conv)


# ── Workshop GeoAI endpoint ─────────────────────────────────────────────────

_WORKSHOP_DB_PATH = '/opt/geoai_workshop/data/exposures_zw.duckdb'

_WORKSHOP_SCHEMA = """
Tables in exposures_zw.duckdb (Zimbabwe DHS 2015 births + real ERA5/CAMS/CHIRPS/MODIS climate data):

births — 5,794 birth records from Zimbabwe DHS 2015 (geo-referenced clusters)
  caseid TEXT, bidx INT, birth_id TEXT, cluster_id INT, cluster_lat FLOAT, cluster_lon FLOAT,
  province TEXT        -- 10 Zimbabwe provinces (Harare, Bulawayo, Manicaland, etc.)
  urban INT            -- 1=urban 0=rural
  birth_year INT, birth_month INT, conception_year INT, conception_month INT,
  sex TEXT             -- 'male'/'female'
  birth_order INT, mother_age INT,
  education TEXT       -- 'none'/'primary'/'secondary'/'higher'
  wealth_quintile INT  -- 1=poorest … 5=richest
  birth_weight_g FLOAT -- grams; NULL if not recorded
  low_birth_weight INT -- 1 if <2500g, 0 if >=2500g, NULL if not recorded
  neonatal_death INT   -- 1 if died within 28 days
  small_at_birth INT   -- 1 if mother reported small/very small baby
  preterm INT          -- 1 if gestational age <9 months (sparse)
  anc_4plus INT        -- 1 if ≥4 ANC visits (NULL if unknown)
  facility_delivery INT-- 1 if born in health facility (NULL if unknown)
  temp_t1/t2/t3 FLOAT -- ERA5 2m air temperature (°C) mean by trimester
  dust_t1/t2/t3 FLOAT -- CAMS dust AOD550 mean by trimester
  pm25_t1/t2/t3 FLOAT -- CAMS PM2.5 (µg/m³) mean by trimester
  rain_t1/t2/t3 FLOAT -- CHIRPS daily rainfall (mm/day) mean by trimester
  ndvi_t1/t2/t3 FLOAT -- MODIS NDVI mean by trimester
  hot_t3 BOOLEAN       -- TRUE if T3 mean temp_t3 > 19°C
  drought_exposed BOOLEAN -- TRUE if rain_t2 < 1.0 AND rain_t3 < 1.0 mm/day

clusters — 399 DHS cluster-level summaries
  cluster_id INT, cluster_lat FLOAT, cluster_lon FLOAT,
  province TEXT, urban INT,
  n_births INT, lbw_n INT, lbw_denom INT, lbw_rate FLOAT,
  neonatal_deaths INT, neonatal_mr FLOAT

climate — monthly environmental data per cluster (28,329 rows: 399 clusters × ~71 months)
  cluster_id INT, year INT, month INT,
  temp_c FLOAT      -- ERA5 2m air temperature (°C)
  rain_mm FLOAT     -- CHIRPS total rainfall (mm/month)
  ndvi FLOAT        -- MODIS NDVI
  dust_aod550 FLOAT -- CAMS dust aerosol optical depth
  pm25 FLOAT        -- CAMS PM2.5 (µg/m³)
  province TEXT, cluster_lat FLOAT, cluster_lon FLOAT, urban INT

facilities — 45 Zimbabwe health facilities
  facility_id TEXT, facility_type TEXT, province TEXT,
  latitude FLOAT, longitude FLOAT, catchment_pop INT, beds INT, has_maternity INT
"""

_WORKSHOP_SYSTEM_PROMPT = f"""You are a GeoAI SQL assistant for a workshop on climate, environment and health.
The user will ask a question in plain English about Zimbabwe DHS 2015 birth data linked to satellite climate data.

Schema:
{_WORKSHOP_SCHEMA}

Response rules:
- If the question is clear and answerable from the schema, respond with a SINGLE DuckDB SQL SELECT statement
  starting with SELECT or WITH, and nothing else — no explanation, no markdown fences.
- If the question is too vague, incomplete, or cannot be answered from this dataset, respond with a single
  plain-English sentence starting with "CLARIFY:" explaining what information you need or why the question
  cannot be answered. Example: "CLARIFY: I need more detail — which outcome are you interested in (LBW rate,
  neonatal mortality, birth weight)? And what do you mean by 'most devastating'?"
- Never invent data, never return fabricated numbers. Only query what exists in the schema above.
- Never use INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, ATTACH, COPY, PRAGMA, or multiple statements.
- Use ROUND(..., 2) for float columns in output.
- Always add a LIMIT 50 unless the query is a pure aggregate (one row output).
- For LBW rate: ROUND(100.0 * SUM(low_birth_weight) / NULLIF(SUM(CASE WHEN low_birth_weight IS NOT NULL THEN 1 END), 0), 1)
- For neonatal mortality rate per 1000: ROUND(1000.0 * SUM(neonatal_death) / COUNT(*), 1)
- Window functions (NTILE, PERCENT_RANK) cannot be used directly in GROUP BY. Always use SELECT * in the CTE:
  WITH t AS (SELECT *, NTILE(4) OVER (ORDER BY col) AS q FROM births) SELECT q, COUNT(*) FROM t GROUP BY q
- Temperature columns are temp_t1/t2/t3 (not lst_t1). Rain is rain_t1/t2/t3. Climate table: temp_c, rain_mm.
"""


@app.route('/api/workshop/geoai', methods=['POST'])
def workshop_geoai():
    """Natural-language to SQL + results for the workshop DuckDB database."""
    workshop_key  = os.environ.get('WORKSHOP_KEY', '')
    request_key   = request.headers.get('X-Workshop-Key', '')
    session_token = request.headers.get('X-Session-Token', '')

    authed = False
    if workshop_key and request_key == workshop_key:
        authed = True
    elif session_token and access_db.validate_catalogue_token(session_token):
        authed = True
    if not authed and not workshop_key:
        authed = True

    if not authed:
        return jsonify({'error': 'Unauthorised'}), 401

    body = request.get_json(silent=True) or {}
    question = (body.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'question is required'}), 400

    hub_username = (body.get('username') or '').strip() or 'unknown'
    wuser = access_db.get_or_create_workshop_user(hub_username)
    if wuser['tokens_used'] >= wuser['token_budget']:
        return jsonify({
            'sql': None, 'rows': [], 'columns': [],
            'error': f"Token budget exhausted ({wuser['tokens_used']:,}/{wuser['token_budget']:,}). "
                     f"Ask your instructor if you need more.",
        }), 200

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))

        last_sql = None
        last_err = None
        clarify_msg = None
        tokens_this_req = 0
        for attempt in range(2):
            msg = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=512,
                system=_WORKSHOP_SYSTEM_PROMPT,
                messages=[{'role': 'user', 'content': question}],
            )
            tokens_this_req += msg.usage.input_tokens + msg.usage.output_tokens
            raw_sql = msg.content[0].text.strip()
            if raw_sql.startswith('```'):
                raw_sql = '\n'.join(
                    line for line in raw_sql.splitlines()
                    if not line.startswith('```')
                ).strip()

            # Model signalled it needs clarification — surface to user immediately
            if raw_sql.startswith('CLARIFY:'):
                clarify_msg = raw_sql[len('CLARIFY:'):].strip()
                break

            safe, reason = is_safe_query(raw_sql)
            if safe:
                last_sql = raw_sql
                break
            last_err = f'Generated SQL was not safe (attempt {attempt+1}): {reason}'

        access_db.add_workshop_tokens_used(hub_username, tokens_this_req)

        if clarify_msg:
            return jsonify({'sql': None, 'rows': [], 'columns': [],
                            'clarify': clarify_msg, 'error': None}), 200

        if last_sql is None:
            return jsonify({'sql': None, 'rows': [], 'columns': [], 'error': last_err}), 200

        # Execute against the workshop database and return results
        import duckdb as _duckdb
        try:
            con = _duckdb.connect(_WORKSHOP_DB_PATH, read_only=True)
            rel = con.execute(last_sql)
            columns = [d[0] for d in rel.description]
            rows = rel.fetchall()
            con.close()
            # Serialise to JSON-safe types
            safe_rows = [
                [v if not hasattr(v, 'item') else v.item() for v in row]
                for row in rows
            ]
            return jsonify({'sql': last_sql, 'columns': columns, 'rows': safe_rows, 'error': None})
        except Exception as db_err:
            return jsonify({'sql': last_sql, 'rows': [], 'columns': [], 'error': f'SQL error: {db_err}'}), 200

    except Exception as e:
        return jsonify({'sql': None, 'rows': [], 'columns': [], 'error': str(e)}), 500


# ── Workshop GeoAI Analysis endpoint (Shmron-style: NL → Python → results + plots) ──

_WORKSHOP_ANALYSIS_SYSTEM = """You are the GeoAI assistant for the GeoAI for Climate, Environment & Health workshop.
You analyse real Zimbabwe DHS 2015 birth records linked to satellite-derived climate exposures.
You maintain context across follow-up questions — if the user says "now break that down by province"
or "compare with urban clusters", apply it to the same analysis thread.

PRIVACY — non-negotiable:
- NEVER print, reference, or expose caseid, bidx, birth_id, or any individual identifier.
- If asked for individual-level records, aggregate to groups. There are no exceptions.

Pre-loaded variables:
  births    — pd.DataFrame, 5794 rows (individual birth records, identifiers already removed)
               key cols: cluster_id, province, urban, birth_year, birth_month, conception_month,
               low_birth_weight, neonatal_death, small_at_birth, preterm,
               birth_weight_g, mother_age, education, wealth_quintile, birth_order,
               temp_t1/t2/t3 (ERA5 air temp °C), dust_t1/t2/t3 (CAMS AOD),
               pm25_t1/t2/t3 (µg/m³), rain_t1/t2/t3 (mm/day), ndvi_t1/t2/t3,
               hot_t3 (bool — top quartile T3 temp), drought_exposed (bool — bottom quartile T3 rain),
               facility_delivery, anc_4plus
  clusters  — pd.DataFrame, 399 rows (cluster-level summaries)
               cols: cluster_id, cluster_lat, cluster_lon, province, urban,
               n_births, lbw_n, lbw_denom, lbw_rate, neonatal_deaths, neonatal_mr
  climate   — pd.DataFrame, ~28K rows, monthly per cluster 2010-2016
               cols: cluster_id, year, month, temp_c, rain_mm, ndvi, dust_aod550, pm25,
               province, cluster_lat, cluster_lon, urban
  pd, np, plt, stats (scipy.stats), sm (statsmodels.api) — all imported

What you can produce:
- Outcome distributions: LBW rate, neonatal mortality by province/education/wealth/urban/season
- Environmental exposure-outcome associations: trimester temperature/rain/NDVI vs LBW
- Logistic regression (sm.Logit) for LBW; OLS for birth weight
- Seasonal patterns: by birth_month or conception_month
- Spatial maps: scatter of cluster_lat/lon coloured by lbw_rate, temp, NDVI etc.
- Correlation matrices and pair plots of trimester exposures
- Subgroup comparisons: hot vs not-hot, drought-exposed vs not, urban vs rural
- Time series: from the monthly climate DataFrame

Honesty rules — non-negotiable:
- NEVER invent, estimate, or hallucinate data values. Every number in your output must come from
  the actual dataset variables (births, clusters, climate). If you cannot compute something from
  the available columns, say so explicitly in the print() summary.
- If a question is ambiguous or cannot be answered from the dataset, output ONLY a print() that
  asks the user to clarify — e.g. print("I need clarification: what do you mean by 'most
  devastating'? Do you mean highest LBW rate, highest neonatal mortality, or something else?")
  Do not guess. Do not fill in with plausible-sounding numbers.
- If the question cannot be answered because the required data column does not exist in the
  schema (e.g. maternal BMI, gestation weeks), print that fact clearly.

Code rules:
- Output ONLY executable Python code. No prose, no markdown fences, no comments.
- FIRST line must be a print() with a 1-2 sentence plain-English summary of the finding.
  Example: print("LBW rate is highest in Bulawayo (10.8%) and lowest in Matabeleland South (4.7%).")
- Use matplotlib (plt) for all plots. Call plt.tight_layout() then plt.show().
- No import statements — all libraries are pre-loaded.
- No file I/O, no os/sys/subprocess/eval/exec/network calls.
- Keep code under 45 lines.
- For LBW rate: use births['low_birth_weight'].mean()*100 or groupby+mean()*100.
- Column names: temp_t1/t2/t3 (not lst_t1), rain_t1/t2/t3 (mm/day, not chirps_mm).
- climate cols: temp_c, rain_mm, ndvi, dust_aod550, pm25.
- When examining environmental associations, adjust for wealth_quintile and urban.
- For ANY regression (sm.Logit or sm.OLS): encode categorical columns before use.
  education must be mapped: edu_num = births['education'].map({'none':0,'primary':1,'secondary':2,'higher':3})
  Always use `births` directly — NEVER invent aliases like b, b_lbw, df, df_lbw.
  Required pattern (copy exactly):
    predictors = ['col1', 'col2', ...]
    mask = births['low_birth_weight'].notna() & births[predictors].notna().all(axis=1)
    df = births[mask].copy()
    y = df['low_birth_weight'].astype(float)
    X = sm.add_constant(df[predictors].astype(float))
    model = sm.Logit(y, X).fit(disp=False)
- When comparing urban vs rural across ALL provinces, Bulawayo is 100% urban — a Rural
  series will have only 9 provinces while Urban has 10. NEVER broadcast .values from two
  Series of different lengths. Always align with pivot_table first:
    tbl = (births[births['low_birth_weight'].notna()]
           .groupby(['province', 'urban'])['low_birth_weight']
           .mean().mul(100).unstack(fill_value=np.nan))
    tbl.columns = ['Rural', 'Urban']
    tbl = tbl.sort_values('Urban', ascending=True)
    fig, ax = plt.subplots(figsize=(10, 6))
    y_pos = np.arange(len(tbl))
    ax.barh(y_pos - 0.2, tbl['Urban'].fillna(0).values, 0.35, label='Urban', color='#2a9d8f')
    ax.barh(y_pos + 0.2, tbl['Rural'].fillna(0).values, 0.35, label='Rural', color='#e76f51')
    ax.set_yticks(y_pos); ax.set_yticklabels(tbl.index)
"""

_ALLOWED_ANALYSIS_IMPORTS = {'pandas', 'numpy', 'matplotlib', 'scipy', 'statsmodels', 'math', 'warnings'}
_BLOCKED_ANALYSIS_TOKENS  = ['import os', 'import sys', 'subprocess', 'open(', '__import__',
                               'eval(', 'exec(', 'socket', 'urllib', 'requests', 'http', 'pickle',
                               'shelve', 'tempfile', 'shutil', 'pathlib']


def _is_safe_analysis_code(code: str) -> tuple[bool, str]:
    for token in _BLOCKED_ANALYSIS_TOKENS:
        if token in code:
            return False, f'Blocked token: {token}'
    return True, ''


@app.route('/api/workshop/geoai_analyze', methods=['POST'])
def workshop_geoai_analyze():
    """Natural language → Python analysis code → executed results + base64 plots."""
    workshop_key  = os.environ.get('WORKSHOP_KEY', '')
    request_key   = request.headers.get('X-Workshop-Key', '')
    session_token = request.headers.get('X-Session-Token', '')

    authed = False
    if workshop_key and request_key == workshop_key:
        authed = True
    elif session_token and access_db.validate_catalogue_token(session_token):
        authed = True
    if not authed and not workshop_key:
        authed = True
    if not authed:
        return jsonify({'error': 'Unauthorised'}), 401

    body = request.get_json(silent=True) or {}
    question = (body.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'question is required'}), 400

    # Conversation history: list of {role, content} pairs from previous turns
    history = body.get('history') or []
    if not isinstance(history, list):
        history = []
    # Sanitise history — only allow role/content keys, max 20 turns to control token cost
    history = [{'role': m['role'], 'content': str(m['content'])}
               for m in history if isinstance(m, dict) and m.get('role') in ('user', 'assistant')][-20:]

    hub_username = (body.get('username') or '').strip() or 'unknown'
    wuser = access_db.get_or_create_workshop_user(hub_username)
    if wuser['tokens_used'] >= wuser['token_budget']:
        return jsonify({
            'code': None, 'output': '', 'figures': [], 'history': history,
            'error': "Token budget exhausted. Ask your instructor for more.",
        }), 200

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))

        messages = history + [{'role': 'user', 'content': question}]

        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1024,
            system=_WORKSHOP_ANALYSIS_SYSTEM,
            messages=messages,
        )
        tokens_used = msg.usage.input_tokens + msg.usage.output_tokens
        access_db.add_workshop_tokens_used(hub_username, tokens_used)

        raw_code = msg.content[0].text.strip()
        # Robust extraction: find the Python block regardless of surrounding prose
        if '```' in raw_code:
            segments = raw_code.split('```')
            # segments[1] is the first fenced block (language tag + code)
            if len(segments) >= 3:
                block = segments[1]
                # Strip optional language tag (python / py / empty first line)
                first_nl = block.find('\n')
                if first_nl != -1 and block[:first_nl].strip().lower() in ('python', 'py', ''):
                    block = block[first_nl + 1:]
                raw_code = block.strip()
            else:
                raw_code = raw_code.replace('```', '').strip()

        safe, reason = _is_safe_analysis_code(raw_code)
        if not safe:
            return jsonify({'code': raw_code, 'output': '', 'figures': [],
                            'error': f'Generated code was blocked: {reason}'}), 200

        # Execute the code in a restricted environment
        import io, base64, traceback
        import pandas as _pd
        import numpy as _np
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as _plt
        from scipy import stats as _stats
        import statsmodels.api as _sm

        # Load workshop data fresh for each request
        _data_dir = '/opt/geoai_workshop/data'
        _births   = _pd.read_csv(os.path.join(_data_dir, 'zw_births_trimester.csv'))
        _clusters = _pd.read_csv(os.path.join(_data_dir, 'zw_clusters.csv'))
        _climate  = _pd.read_csv(os.path.join(_data_dir, 'zw_climate_monthly.csv'))

        # Strip all individual identifiers before passing to exec — privacy enforcement
        _id_cols = ['caseid', 'bidx', 'birth_id']
        _births  = _births.drop(columns=[c for c in _id_cols if c in _births.columns])

        exec_globals = {
            'pd': _pd, 'np': _np, 'plt': _plt,
            'stats': _stats, 'sm': _sm,
            'births': _births, 'clusters': _clusters, 'climate': _climate,
            'warnings': __import__('warnings'),
        }
        import builtins as _builtins_mod
        _safe_names = ['print', 'len', 'range', 'enumerate', 'zip', 'list', 'dict',
                       'tuple', 'set', 'str', 'int', 'float', 'bool', 'abs', 'round',
                       'min', 'max', 'sum', 'sorted', 'isinstance', 'hasattr', 'getattr',
                       'type', 'repr', 'format', 'any', 'all', 'map', 'filter', 'next',
                       'iter', 'vars', 'dir', 'help', 'id', 'hash', 'hex', 'bin', 'oct',
                       'divmod', 'pow', 'slice', 'reversed', 'None', 'True', 'False']
        _allowed_mods = {'pandas', 'numpy', 'matplotlib', 'scipy', 'statsmodels', 'math', 'warnings', 'io'}
        def _safe_import(name, *a, **kw):
            top = name.split('.')[0]
            if top not in _allowed_mods:
                raise ImportError(f'Import of {name!r} is not allowed in this environment')
            return __import__(name, *a, **kw)
        exec_globals['__builtins__'] = {
            k: getattr(_builtins_mod, k) for k in _safe_names if hasattr(_builtins_mod, k)
        }
        exec_globals['__builtins__']['__import__'] = _safe_import

        stdout_capture = io.StringIO()
        import sys as _sys
        old_stdout = _sys.stdout
        _sys.stdout = stdout_capture

        figures_b64 = []
        error_msg = None
        try:
            _plt.close('all')
            exec(raw_code, exec_globals)  # noqa: S102
            # Capture any open figures
            for fig_num in _plt.get_fignums():
                fig = _plt.figure(fig_num)
                buf = io.BytesIO()
                fig.savefig(buf, format='png', dpi=120, bbox_inches='tight')
                buf.seek(0)
                figures_b64.append(base64.b64encode(buf.read()).decode())
                buf.close()
            _plt.close('all')
        except Exception:
            error_msg = traceback.format_exc()
        finally:
            _sys.stdout = old_stdout

        output_text = stdout_capture.getvalue()

        # Build updated history for next follow-up question
        updated_history = history + [
            {'role': 'user',      'content': question},
            {'role': 'assistant', 'content': raw_code},
        ]

        return jsonify({
            'code':    raw_code,
            'output':  output_text,
            'figures': figures_b64,
            'error':   error_msg,
            'history': updated_history,
        })

    except Exception as e:
        return jsonify({'code': None, 'output': '', 'figures': [], 'error': str(e)}), 500


@app.route('/api/workshop/ee_query', methods=['POST'])
def workshop_ee_query():
    """Proxy EE point queries for the workshop — key stays server-side, never exposed to students.

    POST JSON: { "collection": str, "band": str, "lat": float, "lon": float,
                 "date": "YYYY-MM-DD", "scale": int, "value_scale": float (optional),
                 "date_window_days": int (optional) }
    Returns:   { "value": float|null, "date": str, "collection": str, "band": str }
    """
    ALLOWED_COLLECTIONS = {
        'UCSB-CHG/CHIRPS/DAILY':       'precipitation',
        'MODIS/MOD09GA_006_NDVI':       'NDVI',
        'MODIS/061/MOD13A1':            'NDVI',
        'ECMWF/ERA5_LAND/DAILY_AGGR':   'temperature_2m_max',
    }
    try:
        import ee as _ee
        d = request.get_json(force=True) or {}
        collection = d.get('collection', '').strip()
        band       = d.get('band', '').strip()
        lat        = float(d.get('lat', 0))
        lon        = float(d.get('lon', 0))
        date_str   = d.get('date', '').strip()
        scale      = int(d.get('scale', 5000))
        value_scale = float(d.get('value_scale', 1.0))
        window_days = int(d.get('date_window_days', 0))

        if collection not in ALLOWED_COLLECTIONS:
            return jsonify({'error': f'Collection not permitted: {collection}'}), 400
        if not date_str:
            return jsonify({'error': 'date required (YYYY-MM-DD)'}), 400

        SA_KEY   = os.path.join(os.path.dirname(__file__), 'palsearth', 'palsearth-sa-key.json')
        SA_EMAIL = 'ee-service-account@your-project.iam.gserviceaccount.com'
        creds = _ee.ServiceAccountCredentials(SA_EMAIL, SA_KEY)
        _ee.Initialize(creds)

        pt   = _ee.Geometry.Point([lon, lat])
        d0   = _ee.Date(date_str)
        coll = _ee.ImageCollection(collection)
        w    = max(window_days // 2, 0)
        if window_days:
            img = coll.filterDate(d0.advance(-w, 'day'), d0.advance(w + 1, 'day')).mean()
        else:
            img = coll.filterDate(d0, d0.advance(1, 'day')).first()

        info = img.reduceRegion(_ee.Reducer.mean(), pt, scale).getInfo() or {}
        v    = info.get(band)
        value = float(v) * value_scale if v is not None else None

        return jsonify({'value': value, 'date': date_str,
                        'collection': collection, 'band': band})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── GeoAI Explorer chat (high-school course, public, no login) ──────────────
# Reuses the same Claude pattern as /api/workshop/geoai but scoped to a tiny
# public dataset with hard usage caps, since this endpoint has no auth at all.

_GEOAI_EXPLORER_LOCK        = threading.Lock()
_GEOAI_EXPLORER_IP_LOG      = {}   # ip -> list[timestamp]
_GEOAI_EXPLORER_RATE_LIMIT  = 20   # requests per IP per rolling hour
_GEOAI_EXPLORER_RATE_WINDOW = 3600
_GEOAI_EXPLORER_TOKEN_CAP   = 3_000_000  # all-time ceiling, never resets
_GEOAI_EXPLORER_USAGE_FILE  = '/home/USER/PRECISE/geoai_explorer_usage.json'

_GEOAI_EXPLORER_SYSTEM = """You are a friendly GeoAI teaching assistant inside a short high-school course
called "GeoAI: Air Quality Explorer" on placealert.org. A student has built their own small table of
real places, their own picks (home, school, a city they are curious about) plus two fixed real
reference cities, Lahore, Pakistan (one of the most polluted cities on Earth) and Reykjavik, Iceland
(one of the cleanest). Live PM2.5 for each place is given to you as JSON below. Each entry has: name,
pm25 (live, in micrograms per cubic meter), above_who_guideline (true if pm25 is over the WHO annual
guideline of 15), and reference_city (true for Lahore/Reykjavik, false for the student's own picks).

Rules:
- Only answer using the data provided below. Never invent numbers, and never invent facts about a
  place (traffic, green cover, population, etc.) that are not actual columns in the data below.
- Keep answers short: two to four sentences, plain English, no jargon a high schooler wouldn't know.
- If the student names a SPECIFIC real place that is NOT in the data below, do not say you are "not
  sure what they are asking". Instead say clearly you only have data for the places listed below, name
  them, and suggest they add that place using the "Add A Place" box above if they want to compare it too.
- This fallback is ONLY for when a specific named place is genuinely absent from the data. If the
  student uses a vague reference like "my place" or "my places" without naming one, just answer using
  whatever entries are actually present in the data below. Never deny having data that is right there
  in the JSON, and never contradict yourself by denying a place in one sentence and then naming it in
  the next.
- If asked something unrelated to air quality, GeoAI, or this dataset entirely, gently redirect:
  say you are only set up to talk about this dataset right now.
- You can compare pm25 values, rank places, and note how each sits relative to the WHO guideline and
  to the two reference cities, but stay strictly within what the numbers actually show.
- End with a short, upbeat nudge to try another question when it fits naturally, but do not overdo it.
- Never use em dashes. Use commas, periods, or "and"/"but" instead.

Place data (JSON):
{dataset_json}
"""


def _geoai_explorer_total_tokens():
    try:
        with open(_GEOAI_EXPLORER_USAGE_FILE) as f:
            return json.load(f).get('total_tokens', 0)
    except Exception:
        return 0


def _geoai_explorer_add_tokens(n):
    with _GEOAI_EXPLORER_LOCK:
        total = _geoai_explorer_total_tokens() + n
        try:
            with open(_GEOAI_EXPLORER_USAGE_FILE, 'w') as f:
                json.dump({'total_tokens': total}, f)
        except Exception:
            pass
        return total


@app.route('/api/geoai-explorer/chat', methods=['POST'])
def geoai_explorer_chat():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
    now = time.time()

    with _GEOAI_EXPLORER_LOCK:
        hits = [t for t in _GEOAI_EXPLORER_IP_LOG.get(ip, []) if now - t < _GEOAI_EXPLORER_RATE_WINDOW]
        if len(hits) >= _GEOAI_EXPLORER_RATE_LIMIT:
            return jsonify({'answer': None,
                             'error': "You've asked a lot of questions in the last hour. Take a short "
                                      "break and try again a bit later."}), 200
        hits.append(now)
        _GEOAI_EXPLORER_IP_LOG[ip] = hits

    if _geoai_explorer_total_tokens() >= _GEOAI_EXPLORER_TOKEN_CAP:
        return jsonify({'answer': None,
                         'error': "The AI assistant has reached its usage limit for this course. "
                                  "Please check back another time."}), 200

    body     = request.get_json(silent=True) or {}
    question = (body.get('question') or '').strip()[:400]
    dataset  = body.get('dataset') or []
    if not question:
        return jsonify({'answer': None, 'error': 'question is required'}), 400
    if not isinstance(dataset, list) or len(dataset) > 20:
        return jsonify({'answer': None, 'error': 'invalid dataset'}), 400

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
        system_prompt = _GEOAI_EXPLORER_SYSTEM.replace('{dataset_json}', json.dumps(dataset)[:4000])
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=260,
            system=system_prompt,
            messages=[{'role': 'user', 'content': question}],
        )
        _geoai_explorer_add_tokens(msg.usage.input_tokens + msg.usage.output_tokens)
        return jsonify({'answer': msg.content[0].text.strip(), 'error': None})
    except Exception:
        return jsonify({'answer': None,
                         'error': 'The assistant is temporarily unavailable. Please try again shortly.'}), 200


@app.route('/api/geo/countries', methods=['GET'])
def geo_countries():
    """No auth — non-sensitive coverage metadata, lets users check whether
    their AOI is in scope before building a real request."""
    bounds = _load_country_bounds()
    return jsonify({
        'count': len(bounds),
        'countries': sorted(
            [{'iso3': c['iso3'], 'name': c['name']} for c in bounds],
            key=lambda x: x['name'] or ''
        ),
    })


def _geodata_query_core(tool, max_area_km2, force_include_cells=False):
    """Shared AOI parsing + cell-selection + weighted-stats logic used by both
    the keyed and unauthenticated-preview query endpoints. Returns
    (flask_response, status_code, meta) — meta is None on any error path,
    otherwise a small dict the caller uses to log the query."""
    try:
        aoi_geom, area_km2, notes = _parse_aoi_from_request(max_area_km2=max_area_km2)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400, None
    except Exception as e:
        return jsonify({'error': f'Could not parse AOI: {e}'}), 400, None

    iso_list = _countries_intersecting(aoi_geom)
    if not iso_list:
        return jsonify({'error': 'The submitted AOI does not overlap any of the 57 supported '
                                  'African countries. See /api/geo/countries for coverage.'}), 400, None
    if len(iso_list) > _GEO_MAX_COUNTRIES:
        return jsonify({'error': f'AOI spans {len(iso_list)} countries; this endpoint supports up '
                                  f'to {_GEO_MAX_COUNTRIES} per request. Split the request into '
                                  f'smaller AOIs.'}), 400, None

    fields = GEODATA_TOOLS[tool]['value_fields']
    aoi_prepared = prep(aoi_geom)
    per_country = {}
    all_selected = []
    for iso3 in iso_list:
        selected = _select_cells(tool, iso3, aoi_prepared)
        if not selected:
            continue
        stats = _weighted_stats(selected, fields)
        if tool == 'roadnet':
            stats['class_breakdown'] = _roadnet_class_breakdown(selected)
        per_country[iso3] = stats
        all_selected.extend(selected)

    if not all_selected:
        return jsonify({'error': 'No H3 cells with data were found inside the submitted AOI — it '
                                  'may be too small (cells are ~5 km²) or fall in an uncovered gap.'}), 404, None

    combined = _weighted_stats(all_selected, fields)
    if tool == 'roadnet':
        combined['class_breakdown'] = _roadnet_class_breakdown(all_selected)

    response = {
        'tool': tool,
        'aoi_area_km2': round(area_km2, 2),
        'countries': iso_list,
        'per_country': per_country,
        'combined': combined,
        'notes': notes,
    }
    if force_include_cells or request.args.get('include_cells', '').lower() in ('1', 'true', 'yes'):
        response['cells'] = _cells_to_geojson(all_selected)

    meta = {'area_km2': round(area_km2, 2), 'iso_list': iso_list, 'n_cells': combined['n_cells']}
    return jsonify(response), 200, meta


def _geodata_query(tool):
    """Keyed entry point for external integrators — generous area cap,
    per-key rate limit."""
    key_info, err = require_key()
    if err:
        return err
    if not _allow_geo_key(get_api_key()):
        return jsonify({'error': f'Rate limit exceeded ({_GEO_RATE_LIMIT} requests/{_GEO_RATE_WINDOW}s). Please slow down.'}), 429

    resp, status, meta = _geodata_query_core(tool, _GEO_MAX_AREA_KM2)
    if meta:
        _log_query(None, key_info.get('name'), f'{tool}-api',
                   aoi_area_km2=meta['area_km2'], countries=meta['iso_list'], n_cells=meta['n_cells'])
    return resp, status


def _geodata_query_preview(tool):
    """Unauthenticated entry point for the in-browser "Analyze my area" tool —
    tighter area cap, per-IP rate limit, always returns cells (needed to
    highlight the matched hexagons on the map)."""
    ip = (request.headers.get('X-Forwarded-For', '') or request.remote_addr or '').split(',')[0].strip()
    if not _allow_preview_ip(ip):
        return jsonify({'error': f'Rate limit exceeded ({_PREVIEW_RATE_LIMIT} requests/{_PREVIEW_RATE_WINDOW}s). '
                                  f'Please slow down or try a smaller area.'}), 429

    resp, status, meta = _geodata_query_core(tool, _PREVIEW_MAX_AREA_KM2, force_include_cells=True)
    if meta:
        ip_tag = f'preview:{hashlib.sha256(ip.encode()).hexdigest()[:12]}' if ip else 'preview:unknown'
        _log_query(None, ip_tag, f'{tool}-preview',
                   aoi_area_km2=meta['area_km2'], countries=meta['iso_list'], n_cells=meta['n_cells'])
    return resp, status


@app.route('/api/roadnet/query', methods=['POST'])
def roadnet_query():
    """Population-weighted road network density for a user-submitted AOI
    (GeoJSON polygon or zipped shapefile). Requires an API key — see
    /api/roadnet/query-preview for the unauthenticated in-browser version, or
    /api/roadnet/admin for precomputed admin-unit lookups instead."""
    return _geodata_query('roadnet')


@app.route('/api/euclidean/query', methods=['POST'])
def euclidean_query():
    """Population-weighted distance-to-highway/major-road for a user-submitted
    AOI (GeoJSON polygon or zipped shapefile). Requires an API key — see
    /api/euclidean/query-preview for the unauthenticated in-browser version, or
    /api/euclidean/admin for precomputed admin-unit lookups instead."""
    return _geodata_query('euclidean')


@app.route('/api/roadnet/query-preview', methods=['POST'])
def roadnet_query_preview():
    """No API key required — powers the "Analyze my area" tool on the roadnet
    map page. Tighter area cap and per-IP rate limit than /api/roadnet/query."""
    return _geodata_query_preview('roadnet')


@app.route('/api/euclidean/query-preview', methods=['POST'])
def euclidean_query_preview():
    """No API key required — powers the "Analyze my area" tool on the
    euclidean map page. Tighter area cap and per-IP rate limit than
    /api/euclidean/query."""
    return _geodata_query_preview('euclidean')


def _geodata_admin(tool):
    key_info, err = require_key()
    if err:
        return err

    iso3   = (request.args.get('iso3') or '').strip().upper()
    level  = (request.args.get('level') or '1').strip()
    name_q = (request.args.get('name') or '').strip().lower()

    if level not in ('1', '2', '3'):
        return jsonify({'error': 'level must be 1, 2, or 3'}), 400
    if not iso3 and not name_q:
        return jsonify({'error': 'Provide iso3 and/or name to filter — listing all units is not '
                                  'supported (level 2/3 have thousands of entries).'}), 400

    country_prepared = None
    if iso3:
        country = next((c for c in _load_country_bounds() if c['iso3'] == iso3), None)
        if country is None:
            return jsonify({'error': f'Unknown iso3 "{iso3}". See /api/geo/countries for supported codes.'}), 400
        country_prepared = country['prepared']

    try:
        index = _load_admin_index(tool, level)
    except FileNotFoundError:
        return jsonify({'error': f'No admin-{level} data available for {tool}.'}), 404

    matches = []
    for pt, props, geometry in index:
        if country_prepared is not None and not country_prepared.contains(pt):
            continue
        if name_q:
            haystack = f"{props.get('name', '')} {props.get('name_en', '')}".lower()
            if name_q not in haystack:
                continue
        matches.append({'properties': props, 'geometry': geometry})
        if len(matches) >= 200:
            break

    if not matches:
        return jsonify({'error': 'No matching admin units found for the given filters.'}), 404

    return jsonify({
        'tool': tool, 'level': int(level), 'iso3': iso3 or None,
        'name_query': name_q or None, 'count': len(matches), 'units': matches,
    })


@app.route('/api/roadnet/admin', methods=['GET'])
def roadnet_admin():
    """Precomputed population-weighted road density for named admin units
    (level=1 country-level, 2 province/district, 3 sub-district — labels are
    approximate; this is OSM admin_level-derived, not a strict adm1/2/3
    hierarchy). No on-the-fly computation — instant lookup."""
    return _geodata_admin('roadnet')


@app.route('/api/euclidean/admin', methods=['GET'])
def euclidean_admin():
    """Precomputed population-weighted distance-to-road for named admin units.
    Same level caveats as /api/roadnet/admin."""
    return _geodata_admin('euclidean')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
