"""
Harare access-to-care + flood hazard - date-driven Flask app.

Lets the user pick a rainfall event window (end date + 1 week/1 month length)
and get back a map showing how THAT window's rainfall changes road-network
access to Harare's 152 health facilities, vs the static baseline.

Expensive, date-INDEPENDENT setup (road graph build, floodplain dissolve,
baseline travel times) happens once at startup in a background thread.
Only the rainfall fetch + edge-removal + re-routing happens per request.
"""
import os
import json
import gzip
import pickle
import tempfile
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
import h3
import networkx as nx
import requests
import ee
from scipy.spatial import cKDTree
from shapely.geometry import Polygon
from PIL import Image
from flask import Flask, request, redirect, url_for, render_template_string, send_from_directory

import flood_accessibility_pipeline as fap

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE_DIR, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

SA_EMAIL = "ee-service-account@your-project.iam.gserviceaccount.com"
SA_KEY = "/home/USER/PRECISE/palsearth/palsearth-sa-key.json"
EE_PROJECT = "your-earth-engine-project"

BBOX = fap.SITE_CONFIGS["harare"]["bbox"]  # [west, south, east, north]
CENTER = [(BBOX[1] + BBOX[3]) / 2, (BBOX[0] + BBOX[2]) / 2]

REGIONAL_FLOOD_THRESHOLD_MM = fap.REGIONAL_FLOOD_THRESHOLD_MM
ANTECEDENT_TRIGGER_MM = fap.ANTECEDENT_TRIGGER_MM
ANTECEDENT_SATURATION_MM = fap.ANTECEDENT_SATURATION_MM

# Drainage-area threshold presets (km^2) - this controls which terrain cells
# count as a "stream" at all for the HAND floodplain trace (Section 4 of the
# docs). It's precomputed as a few labelled presets, not a free-typed number
# like the rainfall thresholds: changing it means re-running the HAND trace
# over the whole DEM (~10-30s here), not a cheap per-request comparison, so
# instant arbitrary values aren't practical - these 4 give real, fast-to-
# switch-between choices spanning the literature range. Coverage % figures
# are measured directly (within the Harare boundary), not estimates.
DRAINAGE_PRESETS = {
    "dense": {
        "km2": 0.05, "label": "Dense (0.05 km²)", "coverage_pct": 25.5,
        "tif": "DEM/harare_floodplain_dense.tif",
        "note": "Recognises even tiny gullies as streams - broadest, most cautious floodplain extent, but likely "
                "overstates real flood risk away from actual rivers.",
    },
    "moderate": {
        "km2": 1.0, "label": "Moderate (1.0 km²)", "coverage_pct": 8.1,
        "tif": "DEM/harare_floodplain_moderate.tif",
        "note": "A middle ground between the dense default and the literature-recommended value below.",
    },
    "recommended": {
        "km2": 4.05, "label": "Recommended (4.05 km²)", "coverage_pct": 5.7,
        "tif": "DEM/harare_selfcomputed_floodplain.tif",
        "note": "Literature value (Nobre et al. 2016, via Hu & Demir 2021) - only real, substantial streams "
                "count. This is the default and the one described in the documentation's worked example.",
    },
    "conservative": {
        "km2": 10.0, "label": "Conservative (10.0 km²)", "coverage_pct": 4.2,
        "tif": "DEM/harare_floodplain_conservative.tif",
        "note": "Only major rivers count as drainage - the narrowest floodplain. Risks missing real flood-prone "
                "areas along smaller but still significant streams.",
    },
}
DEFAULT_DRAINAGE_PRESET = "recommended"

TYPE_LABELS = {"hospital": "Hospital", "clinic": "Clinic", "doctors": "Doctors"}
# Icon markers, not circle markers - Leaflet keeps marker icons in a pane
# above every polygon/GeoJson overlay, so hover/popups stay reachable no
# matter how many choropleth layers sit underneath. Colors avoid red/orange
# for facility TYPE - red is reserved everywhere else on this map for
# "slow/at-risk", so a red hospital marker would misread as something being
# wrong with the hospital itself.
TYPE_ICON_COLOR = {"hospital": "darkblue", "clinic": "cadetblue", "doctors": "purple"}
TYPE_ICON_COLOR_HEX = {"hospital": "#0a3d7a", "clinic": "#5f9ea0", "doctors": "#6a3d9a"}
TYPE_ICON_GLYPH = {"hospital": "plus", "clinic": "medkit", "doctors": "user-md"}
AT_RISK_ICON_COLOR = "red"
AT_RISK_ICON_GLYPH = "exclamation-triangle"
VARIANTS = [("All facilities", "All facilities"), ("Hospital", "Hospitals only"),
            ("Clinic", "Clinics only"), ("Doctors", "Doctors only"),
            ("Public", "Public facilities"), ("Private", "Private facilities")]

TIME_BINS = [0, 15, 30, 45, 60, 90, 99999]
TIME_COLORS = ["#ffffb2", "#fed976", "#feb24c", "#fd8d3c", "#f03b20", "#bd0026"]
TIME_BIN_LABELS = ["0-15", "15-30", "30-45", "45-60", "60-90", "90+"]
DELTA_BINS = [-99999, 1, 10, 20, 30, 99999]
DELTA_COLORS = ["#f0f0f0", "#fee8c8", "#fdbb84", "#e34a33", "#b30000"]
DELTA_LABELS = ["No real change (<1 min)", "+1-10 min slower", "+10-20 min slower",
                "+20-30 min slower", "+30 min slower"]
NEWLY_CUTOFF_COLOR = "#000000"

OSM_TILE = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

app = Flask(__name__)
STATE = {"ready": False, "building": True, "error": None, "started_at": time.time()}
LOCK = threading.Lock()
CTX = {}  # populated by _startup(): G_baseline, node_tree, hex_* , facilities, crossings, floodplain_union, waterways, boundary, tt_base


def _ee_init():
    creds = ee.ServiceAccountCredentials(SA_EMAIL, SA_KEY)
    ee.Initialize(credentials=creds, project=EE_PROJECT)


GRAPH_CACHE_PATH = os.path.join(BASE_DIR, "cache", "G_baseline.pkl")


def _graph_cache_key():
    pbf_path = os.path.join(BASE_DIR, "PBFs", "zimbabwe-latest.osm.pbf")
    st = os.stat(pbf_path)
    return f"{st.st_size}-{int(st.st_mtime)}-{BBOX}-pyrosm0.6.2"


def _load_or_build_graph(pipeline):
    """Every code change needs the whole process to restart, and
    build_graphs() (PBF read + graph construction) is ~6-7 min of that -
    wasteful to redo for e.g. a legend tweak. Cache the built graph to disk,
    keyed on the PBF file's own size+mtime (+ bbox + pyrosm version) so a
    real data change still forces a rebuild rather than silently serving a
    stale graph."""
    os.makedirs(os.path.dirname(GRAPH_CACHE_PATH), exist_ok=True)
    key = _graph_cache_key()
    if os.path.exists(GRAPH_CACHE_PATH):
        try:
            t0 = time.time()
            with open(GRAPH_CACHE_PATH, "rb") as f:
                cached_key, G = pickle.load(f)
            if cached_key == key:
                print(f"[OK] loaded cached road graph in {time.time()-t0:.1f}s "
                      f"({G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges) - skipped the ~7min rebuild")
                return G
            print(f"[i] graph cache key mismatch (was {cached_key!r}, now {key!r}) - rebuilding")
        except Exception as e:
            print(f"[!] graph cache load failed ({e}) - rebuilding")
    pipeline.build_graphs()
    G = pipeline.G_baseline
    try:
        t0 = time.time()
        with open(GRAPH_CACHE_PATH, "wb") as f:
            pickle.dump((key, G), f, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"[OK] cached road graph in {time.time()-t0:.1f}s -> {GRAPH_CACHE_PATH}")
    except Exception as e:
        print(f"[!] failed to write graph cache (non-fatal, will just rebuild next time): {e}")
    return G


def _startup():
    try:
        _ee_init()
        pipeline = fap.FloodAccessibilityPipeline("harare")
        G_baseline = _load_or_build_graph(pipeline)

        node_ids = list(G_baseline.nodes())
        node_coords = np.array([[G_baseline.nodes[n]["x"], G_baseline.nodes[n]["y"]] for n in node_ids])
        node_tree = cKDTree(node_coords)

        def nearest_node(lon, lat):
            _, idx = node_tree.query([lon, lat])
            return node_ids[idx]

        # Static edge midpoints, for flood-edge removal (same geometry every request)
        edge_list = list(G_baseline.edges(keys=True, data=True))
        mids, refs = [], []
        for u, v, k, data in edge_list:
            geom = data.get("geometry")
            if geom is not None:
                m = geom.interpolate(0.5, normalized=True)
                mids.append([m.x, m.y])
            else:
                ux, uy = G_baseline.nodes[u]["x"], G_baseline.nodes[u]["y"]
                vx, vy = G_baseline.nodes[v]["x"], G_baseline.nodes[v]["y"]
                mids.append([(ux + vx) / 2, (uy + vy) / 2])
            refs.append((u, v, k))
        edge_tree = cKDTree(np.array(mids))

        facilities = gpd.read_file(os.path.join(BASE_DIR, "Harare_extract", "harare_health_facilities.gpkg"))
        facility_nearest_node = {row["fac_code"]: nearest_node(row.geometry.x, row.geometry.y)
                                  for _, row in facilities.iterrows()}

        districts = gpd.read_file(os.path.join(BASE_DIR, "Population", "zimbabwe_districts.gpkg"))
        boundary = districts[districts["ADM2_NAME"] == "Harare"]
        boundary_union = boundary.geometry.union_all()
        bminx, bminy, bmaxx, bmaxy = boundary.total_bounds
        map_fit_bounds = [[bminy, bminx], [bmaxy, bmaxx]]  # [[south,west],[north,east]] for Leaflet fitBounds

        # cluster_id/highway/waterway/geometry only - NOT the stale
        # selfcomputed_floodplain column from this CSV. That column reflects
        # whatever drainage threshold happened to run last (it gets
        # overwritten as a side effect every time compute_floodplain() runs
        # for ANY preset - caught this exact clobbering while building the
        # presets below). The geomorphic flag is now sampled fresh per
        # request from whichever preset raster is selected (same pattern
        # already used for facility at_risk), so this file's own flag column
        # is simply unused from here on - no risk of serving stale data.
        crossings_geom = gpd.read_file(os.path.join(BASE_DIR, "Harare_extract", "harare_crossings_distinct.gpkg"))
        crossings_full = crossings_geom[["cluster_id", "highway", "waterway", "geometry"]].copy()
        # Clipped to the Harare boundary like everything else below - 404 of
        # 902 crossings fall inside it; leaving the other 498 in made
        # crossing markers appear scattered with no nearby river/road
        # visible (those ARE clipped), looking like a data bug when it was
        # really just an inconsistent clip between layers.
        crossings = crossings_full[crossings_full.geometry.intersects(boundary_union)].copy()
        print(f"[OK] crossings clipped to Harare boundary: {len(crossings):,} (from {len(crossings_full):,} in the full bbox)")

        # Rendered as a raster PNG overlay, not vector polygons - the HAND
        # method produces a genuinely speckled pattern at 90m (median
        # contiguous patch is a SINGLE pixel, confirmed: 108,639 polygons,
        # 75% of them 1-3 cells - real signal, not noise). Vector
        # simplification (block-downsample+threshold, area-filtering) either
        # lost 35-50% of real flooded area or produced too many features to
        # render fast; a raster overlay's size depends only on image
        # dimensions, not feature count, and loses nothing. Kept at the full
        # bbox extent (not clipped) - the earlier "not showing" report was
        # the map being zoomed/centered on the old wide-bbox view instead of
        # fitting to Harare (fixed via map_fit_bounds below), making Harare a
        # small, easy-to-miss patch.
        #
        # One PNG per drainage-threshold preset (Section "Selectable
        # floodplain sensitivity" in the docs) - all precomputed at startup
        # since each is a cheap numpy->PNG conversion (the expensive part,
        # the HAND trace itself, already happened once per preset to produce
        # these .tif files - see the precompute step run manually before
        # this deploy). Switching presets in the sidebar is then instant,
        # no per-request recomputation.
        floodplain_pngs, floodplain_bounds_by_preset, floodplain_coverage = {}, {}, {}
        for preset_key, preset in DRAINAGE_PRESETS.items():
            png_path = os.path.join(BASE_DIR, f"static_floodplain_overlay_{preset_key}.png")
            tif_path = os.path.join(BASE_DIR, preset["tif"])
            with rasterio.open(tif_path) as src:
                data = np.nan_to_num(src.read(1), nan=0.0)
                floodplain_bounds_by_preset[preset_key] = src.bounds
                rgba = np.zeros((*data.shape, 4), dtype=np.uint8)
                rgba[data == 1] = [49, 130, 189, 140]
                Image.fromarray(rgba).save(png_path)
            floodplain_pngs[preset_key] = png_path
        floodplain_png = floodplain_pngs[DEFAULT_DRAINAGE_PRESET]
        floodplain_bounds = floodplain_bounds_by_preset[DEFAULT_DRAINAGE_PRESET]
        print(f"[OK] floodplain overlays generated for {len(DRAINAGE_PRESETS)} drainage-threshold presets")

        # Waterways and roads clipped to the Harare district boundary, not
        # the full ~13,400 km^2 analysis bbox - the wide rural halo was
        # drowning out the actual road/river detail the user needs to see
        # (e.g. where a road crosses a river - the whole point of the
        # crossings layer). Roads additionally drop footway/cycleway/steps/
        # path (foot-only, irrelevant to driving + visual clutter at this
        # zoom) - still ~thousands of real road segments, not the full 555K
        # raw ways across the wide bbox.
        # simplify(): OSM ways carry far more vertices than needed for
        # display at this zoom - ~0.00005deg (~5.5m) tolerance is invisible
        # at any zoom level the user would actually use, but cuts the
        # payload (and browser paint cost) meaningfully across 28K+ road
        # segments. Geometry only, no attribute/feature loss.
        SIMPLIFY_TOL = 0.00005
        waterways_full = gpd.read_file(os.path.join(BASE_DIR, "Harare_extract", "harare_waterways.gpkg"))
        waterways = waterways_full[waterways_full.geometry.intersects(boundary_union)].copy()
        waterways["geometry"] = waterways.geometry.simplify(SIMPLIFY_TOL)
        # Geometry only - the source file carries 34/7 columns per feature
        # (raw OSM 'tags' JSON blob, timestamp, version, etc.), none of
        # which the map actually uses (style is a constant, no per-feature
        # properties referenced) - folium.GeoJson serializes EVERY column
        # into EVERY feature's properties regardless of whether anything
        # reads them, which was the real driver of a 40+MB page, not vertex
        # count (confirmed: simplify() alone barely moved the file size).
        waterways = waterways[["geometry"]]

        roads_full = gpd.read_file(os.path.join(BASE_DIR, "Harare_extract", "harare_roads.gpkg"))
        roads = roads_full[roads_full.geometry.intersects(boundary_union)].copy()
        if "highway" in roads.columns:
            roads = roads[~roads["highway"].isin({"footway", "cycleway", "steps", "path", "pedestrian"})]
        roads["geometry"] = roads.geometry.simplify(SIMPLIFY_TOL)
        roads = roads[["geometry"]]
        print(f"[OK] roads clipped to Harare boundary: {len(roads):,} segments (from {len(roads_full):,} in the full bbox)")

        # Roads stay VECTOR (a raster PNG goes blurry/jagged on zoom - not
        # worth it for lines that are mostly straight segments anyway) but
        # are NOT embedded inline in the Folium-generated HTML, which is
        # what actually caused the slow render: folium.GeoJson serializes
        # all 28K+ features as literal JS/JSON text into the page itself,
        # so every run paid the cost of generating, downloading and
        # parsing that blob before a single pixel painted.
        # Same technique as the existing roadnet tool
        # (portal.placealert.org/roadnet, /home/USER/PRECISE/roadnet):
        # pre-serialize once to gzip-compressed JSON on disk, serve it as
        # a small static file, and let the browser fetch + decompress
        # (DecompressionStream) + render it client-side via Leaflet AFTER
        # the base map is already up and interactive - roads "pop in" a
        # moment later instead of blocking everything else. Roads don't
        # change per request, so this file is built once at startup and
        # reused by every scenario run.
        roads_geojson = json.loads(roads.to_json())
        roads_gz_path = os.path.join(BASE_DIR, "static_roads.json.gz")
        with gzip.open(roads_gz_path, "wt", encoding="utf-8") as f:
            json.dump(roads_geojson, f)
        print(f"[OK] roads serialized to gzip JSON ({os.path.getsize(roads_gz_path)/1024:.0f} KB) -> {roads_gz_path}")

        tt_base = pd.read_csv(os.path.join(BASE_DIR, "Harare_extract", "harare_travel_time_by_type.csv"))
        hex_geoms, hex_centroid_nodes = [], []
        for hid in tt_base["h3_id"]:
            b = h3.cell_to_boundary(hid)
            poly = Polygon([(lon, lat) for lat, lon in b])
            hex_geoms.append(poly)
            c = poly.centroid
            hex_centroid_nodes.append(nearest_node(c.x, c.y))
        hex_base_full = gpd.GeoDataFrame(tt_base, geometry=hex_geoms, crs="EPSG:4326")

        # Drive-time hexes restricted to the Harare boundary too - same
        # reasoning. run_scenario() and render_map() both read hex_base and
        # hex_centroid_nodes from CTX, so filtering once here is enough.
        within_mask = hex_base_full.geometry.intersects(boundary_union).values
        hex_base = hex_base_full[within_mask].reset_index(drop=True)
        hex_centroid_nodes = [n for n, keep in zip(hex_centroid_nodes, within_mask) if keep]
        print(f"[OK] hexes clipped to Harare boundary: {len(hex_base):,} (from {len(hex_base_full):,} in the full bbox)")

        CTX.update(dict(
            pipeline=pipeline, G_baseline=G_baseline, node_tree=node_tree, node_ids=node_ids,
            nearest_node=nearest_node, edge_tree=edge_tree, edge_refs=refs,
            facilities=facilities, facility_nearest_node=facility_nearest_node,
            crossings=crossings, floodplain_png=floodplain_png, floodplain_bounds=floodplain_bounds,
            floodplain_pngs=floodplain_pngs, floodplain_bounds_by_preset=floodplain_bounds_by_preset,
            waterways=waterways, roads_gz_path=roads_gz_path, map_fit_bounds=map_fit_bounds,
            boundary=boundary, hex_base=hex_base, hex_centroid_nodes=hex_centroid_nodes,
        ))
        STATE["ready"] = True
        STATE["building"] = False
        print("[OK] startup complete, app ready")
    except Exception as e:
        STATE["error"] = str(e)
        STATE["building"] = False
        print(f"[FATAL] startup failed: {e}")


# Run synchronously in the main thread, NOT a background thread. Earth
# Engine's ee.Initialize() sets up a gRPC channel, and gRPC's C-core has
# known issues when its channel/completion-queue threads are first created
# off the main thread (observed directly: ee.Initialize() spawned ~55
# worker threads that immediately parked on futex_do_wait with the main
# thread stuck in poll_schedule_timeout - zero progress, zero CPU, for 11+
# minutes, BEFORE even pyrosm's own log line printed - so this was never a
# pyrosm/workers=1 issue, that was a mis-diagnosis on the first pass).
# Blocking Flask's own startup for ~7-10 min is fine for a single-user tool.
_startup()


# ---------------------------------------------------------------------------
# Per-request rainfall + rerouting
# ---------------------------------------------------------------------------
def fetch_chirps_thresholds_window(event_start_date, event_end_date, out_path):
    region = ee.Geometry.Rectangle(BBOX)
    event_start = datetime.strptime(event_start_date, "%Y-%m-%d")
    antecedent_start = (event_start - timedelta(days=3)).strftime("%Y-%m-%d")
    chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
    daily_max = chirps.filterDate(event_start_date, event_end_date).max()
    antecedent_total = chirps.filterDate(antecedent_start, event_start_date).sum()
    img = ee.Image.cat([daily_max.rename("daily_max"), antecedent_total.rename("antecedent")]).clip(region)
    url = img.getDownloadURL({"region": region, "scale": 5566, "crs": "EPSG:4326", "format": "GEO_TIFF"})
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)


def sample_thresholds(tif_path, points):
    with rasterio.open(tif_path) as src:
        dm_band, an_band = src.read(1), src.read(2)
        dm_vals, an_vals = [], []
        for pt in points:
            row, col = src.index(pt.x, pt.y)
            if 0 <= row < dm_band.shape[0] and 0 <= col < dm_band.shape[1]:
                dm_vals.append(float(dm_band[row, col]))
                an_vals.append(float(an_band[row, col]))
            else:
                dm_vals.append(0.0)
                an_vals.append(0.0)
    return np.nan_to_num(np.array(dm_vals)), np.nan_to_num(np.array(an_vals))


def had_rain_gate(daily_max, antecedent, regional_threshold_mm, antecedent_trigger_mm, antecedent_saturation_mm):
    """User-adjustable now - the module-level REGIONAL_FLOOD_THRESHOLD_MM /
    ANTECEDENT_TRIGGER_MM / ANTECEDENT_SATURATION_MM constants are only the
    form's default values, not hardcoded truth. Different soil/drainage
    conditions genuinely need different trigger points - see the
    documentation page."""
    return (daily_max > regional_threshold_mm) | (
        (daily_max > antecedent_trigger_mm) & (antecedent > antecedent_saturation_mm))


def facility_label(selected_types):
    """'Hospital' / 'Hospital + Clinic' / 'All types (Hospital, Clinic, Doctors)'."""
    names = [TYPE_LABELS[t] for t in ["hospital", "clinic", "doctors"] if t in selected_types]
    if len(names) == 3:
        return "All types (Hospital, Clinic, Doctors)"
    return " + ".join(names) if names else "(none selected)"


def sample_floodplain(tif_path, points):
    """Fresh per-request sample of a floodplain raster at a set of points -
    reused for both crossings and facilities, parameterized by which
    drainage-threshold preset's .tif to read (see DRAINAGE_PRESETS)."""
    with rasterio.open(tif_path) as fp_src:
        fp_band = fp_src.read(1)
        out = []
        for geom in points:
            row, col = fp_src.index(geom.x, geom.y)
            v = fp_band[row, col] if (0 <= row < fp_band.shape[0] and 0 <= col < fp_band.shape[1]) else 0
            out.append(bool(v == 1))
    return out


def run_scenario(event_start_date, event_end_date, selected_types,
                  regional_threshold_mm, antecedent_trigger_mm, antecedent_saturation_mm,
                  drainage_preset):
    """selected_types: list of 'hospital'/'clinic'/'doctors', any non-empty
    subset - the user toggles these independently (not a fixed preset), so
    accessibility is computed fresh for whichever combination is checked:
    all three checked = nearest ANY facility, one checked = nearest facility
    of just that type, etc. Both scenarios run fresh here now (previously
    baseline was read from a cache built for only 6 fixed presets, which
    couldn't represent an arbitrary combination like "Hospital + Doctors").

    regional_threshold_mm/antecedent_trigger_mm/antecedent_saturation_mm:
    user-adjustable flood-trigger thresholds (form defaults: 50/25/40) -
    see had_rain_gate().

    drainage_preset: key into DRAINAGE_PRESETS - picks which precomputed
    floodplain raster to sample for the geomorphic "could this flood at
    all" flag (crossings and facilities both read the SAME preset's .tif,
    sampled fresh here rather than from a stale cached column - see the
    comment in _startup() on why the old CSV column was dropped)."""
    t0 = time.time()
    floodplain_tif = os.path.join(BASE_DIR, DRAINAGE_PRESETS[drainage_preset]["tif"])
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        tif_path = tmp.name
    try:
        fetch_chirps_thresholds_window(event_start_date, event_end_date, tif_path)

        crossings = CTX["crossings"].copy()
        in_floodplain = sample_floodplain(floodplain_tif, list(crossings.geometry.centroid))
        dm, an = sample_thresholds(tif_path, list(crossings.geometry.centroid))
        had_rain = had_rain_gate(dm, an, regional_threshold_mm, antecedent_trigger_mm, antecedent_saturation_mm)
        crossings["flooded_this_scenario"] = [fp and bool(hr) for fp, hr in zip(in_floodplain, had_rain)]
        n_flooded = int(sum(crossings["flooded_this_scenario"]))

        facilities = CTX["facilities"].copy()
        in_fp = sample_floodplain(floodplain_tif, list(facilities.geometry))
        fdm, fan = sample_thresholds(tif_path, list(facilities.geometry))
        f_had_rain = had_rain_gate(fdm, fan, regional_threshold_mm, antecedent_trigger_mm, antecedent_saturation_mm)
        facilities["at_risk"] = [a and bool(b) for a, b in zip(in_fp, f_had_rain)]
        n_at_risk = int(facilities["at_risk"].sum())

        G_flooded = CTX["G_baseline"].copy()
        flooded_set = set()
        for geom in crossings.loc[crossings["flooded_this_scenario"], "geometry"]:
            c = geom.centroid if geom.geom_type != "Point" else geom
            _, idx = CTX["edge_tree"].query([c.x, c.y])
            flooded_set.add(CTX["edge_refs"][idx])
        for u, v, k in flooded_set:
            if G_flooded.has_edge(u, v, k):
                G_flooded.remove_edge(u, v, k)
            if G_flooded.has_edge(v, u, k):
                G_flooded.remove_edge(v, u, k)

        hex_centroid_nodes = CTX["hex_centroid_nodes"]
        matching = facilities[facilities["amenity"].isin(selected_types)]

        # Baseline: no flooding at all, so every matching facility counts -
        # at_risk is specific to THIS window's rainfall, not applicable here.
        base_sources = [CTX["facility_nearest_node"][c] for c in matching["fac_code"]]
        if base_sources:
            base_times = nx.multi_source_dijkstra_path_length(CTX["G_baseline"], sources=base_sources, weight="travel_time_min")
            base_vals = [base_times.get(n, np.nan) for n in hex_centroid_nodes]
        else:
            base_vals = [np.nan] * len(hex_centroid_nodes)

        # Flooded: matching facilities that aren't at-risk this window, on
        # the graph with this window's flooded crossings' edges removed.
        flood_cand = matching[~matching["at_risk"]]
        flood_sources = [CTX["facility_nearest_node"][c] for c in flood_cand["fac_code"]]
        if flood_sources:
            flood_times = nx.multi_source_dijkstra_path_length(G_flooded, sources=flood_sources, weight="travel_time_min")
            flood_vals = [flood_times.get(n, np.nan) for n in hex_centroid_nodes]
        else:
            flood_vals = [np.nan] * len(hex_centroid_nodes)

        hex_both = CTX["hex_base"][["h3_id", "geometry", "population"]].copy()
        hex_both["baseline"] = base_vals
        hex_both["flooded"] = flood_vals

        # Dashboard metrics - computed once here so both render_map()'s
        # on-map caption and the /run JSON response (for the sidebar
        # dashboard panel) read the same numbers instead of recomputing
        # them twice with room to drift apart.
        peak_daily_rain_mm = float(max(dm.max() if len(dm) else 0.0, fdm.max() if len(fdm) else 0.0))
        peak_antecedent_mm = float(max(an.max() if len(an) else 0.0, fan.max() if len(fan) else 0.0))

        pop_cutoff_total = float(hex_both.loc[hex_both["baseline"].notna() & hex_both["flooded"].isna(), "population"].sum())
        pop_delayed_total = float(hex_both.loc[
            hex_both["baseline"].notna() & hex_both["flooded"].notna() & (hex_both["flooded"] > hex_both["baseline"]),
            "population"].sum())

        # Population-weighted mean travel time - same formula already used
        # elsewhere in the pipeline (e.g. regional/district summaries for
        # the other CASCADE sites) for a single headline "how much worse,
        # on average, weighted by where people actually live" number,
        # rather than an unweighted per-hexagon average that treats an
        # empty hexagon the same as a dense one.
        mask_b = hex_both["baseline"].notna()
        w_base = (float((hex_both.loc[mask_b, "baseline"] * hex_both.loc[mask_b, "population"]).sum()
                         / hex_both.loc[mask_b, "population"].sum())
                  if mask_b.any() and hex_both.loc[mask_b, "population"].sum() > 0 else None)
        mask_f = hex_both["flooded"].notna()
        w_flood = (float((hex_both.loc[mask_f, "flooded"] * hex_both.loc[mask_f, "population"]).sum()
                          / hex_both.loc[mask_f, "population"].sum())
                   if mask_f.any() and hex_both.loc[mask_f, "population"].sum() > 0 else None)
        pct_change = ((w_flood - w_base) / w_base * 100) if (w_base and w_flood is not None) else None

        elapsed = time.time() - t0
        return dict(crossings=crossings, facilities=facilities, hex_both=hex_both,
                    selected_types=selected_types, n_matching=len(matching), drainage_preset=drainage_preset,
                    n_flooded=n_flooded, n_at_risk=n_at_risk, elapsed=elapsed,
                    peak_daily_rain_mm=peak_daily_rain_mm, peak_antecedent_mm=peak_antecedent_mm,
                    pop_cutoff_total=pop_cutoff_total, pop_delayed_total=pop_delayed_total,
                    w_base=w_base, w_flood=w_flood, pct_change=pct_change)
    finally:
        try:
            os.remove(tif_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Rendering (same legend/choropleth design as the static build)
# ---------------------------------------------------------------------------
def time_to_color(v):
    if pd.isna(v):
        return "#cccccc"
    for i in range(len(TIME_BINS) - 1):
        if TIME_BINS[i] <= v < TIME_BINS[i + 1]:
            return TIME_COLORS[i]
    return TIME_COLORS[-1]


def delta_to_color(baseline, flooded):
    if pd.isna(baseline) and pd.isna(flooded):
        return "#eeeeee"
    if pd.notna(baseline) and pd.isna(flooded):
        return NEWLY_CUTOFF_COLOR
    if pd.isna(baseline) and pd.notna(flooded):
        return "#3182bd"
    d = flooded - baseline
    if d < DELTA_BINS[1]:
        return DELTA_COLORS[0]
    for i in range(1, len(DELTA_BINS) - 1):
        if DELTA_BINS[i] <= d < DELTA_BINS[i + 1]:
            return DELTA_COLORS[i]
    return DELTA_COLORS[-1]


def legend_box(title, rows, caption=None, width="300px"):
    def _row(sw, label):
        if sw.startswith("<b>"):  # section header, not a swatch row
            return (f'<div style="font-weight:bold; font-size:11.5px; color:#555; text-transform:uppercase; '
                     f'letter-spacing:0.3px; margin:10px 0 4px 0; border-top:1px solid #eee; padding-top:8px;">'
                     f'{sw.replace("<b>","").replace("</b>","")}</div>')
        return (f'<div style="display:flex; align-items:center; margin:3px 0;">'
                f'<div style="flex:0 0 22px;">{sw}</div>'
                f'<div style="font-size:12.5px; color:#222;">{label}</div></div>')
    rows_html = "".join(_row(sw, label) for sw, label in rows)
    cap = f'<div style="font-size:11px; color:#666; margin-top:6px; line-height:1.4;">{caption}</div>' if caption else ""
    # Native <details>/<summary> - collapsible with zero JS. Starts OPEN
    # (the "open" attribute) the first time the page loads, but the user can
    # click the title to collapse it out of the way once they've read it.
    return (f'<details open style="position: fixed; top: 10px; right: 10px; z-index: 9999; background: white; '
            f'padding: 8px 12px; border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.35); '
            f'font-family: Arial, sans-serif; width: {width}; max-height: 85vh; overflow-y: auto;">'
            f'<summary style="font-weight:bold; font-size:13.5px; cursor:pointer; padding-bottom:4px; '
            f'border-bottom:1px solid #ddd; outline:none;">{title} <span style="font-weight:normal; '
            f'font-size:10.5px; color:#888;">(click to collapse)</span></summary>'
            f'<div style="margin-top:6px;">{rows_html}{cap}</div></details>')


def circle_swatch(color, border="#333"):
    return f'<div style="width:14px;height:14px;border-radius:50%;background:{color};border:1.5px solid {border};margin:auto;"></div>'


def ring_swatch():
    return '<div style="width:16px;height:16px;border-radius:50%;background:#eee;border:3px solid #000;margin:auto;"></div>'


def line_swatch(color, dash=False, weight=3):
    style = f"border-top: {weight}px {'dashed' if dash else 'solid'} {color};"
    return f'<div style="{style} width:20px; margin: 8px auto;"></div>'


def square_swatch(color, opacity=0.5):
    return f'<div style="width:16px;height:16px;background:{color};opacity:{opacity};border:1px solid #333;margin:auto;"></div>'


def triangle_swatch(color):
    return (f'<div style="width:0;height:0;margin:2px auto;border-left:7px solid transparent;'
            f'border-right:7px solid transparent;border-bottom:12px solid {color};"></div>')


PULSE_CSS = """
<style>
@keyframes floodPulseRing {
  0%   { transform: scale(0.6); opacity: 0.9; }
  70%  { transform: scale(2.6); opacity: 0; }
  100% { transform: scale(2.6); opacity: 0; }
}
.flood-pulse-wrap { position: relative; width: 18px; height: 18px; }
.flood-pulse-ring {
  position: absolute; left: 1px; top: 1px; width: 16px; height: 16px;
  border-radius: 50%; background: rgba(230,85,13,0.55);
  animation: floodPulseRing 1.4s ease-out infinite;
}
.flood-pulse-tri {
  position: absolute; left: 2px; top: 3px; width: 0; height: 0;
  border-left: 7px solid transparent; border-right: 7px solid transparent;
  border-bottom: 12px solid #e6550d; filter: drop-shadow(0 0 1px #000);
}
</style>
"""


def pulsing_triangle_swatch(color):
    return (f'<div style="position:relative; width:18px; height:16px; margin:auto;">'
            f'<div class="flood-pulse-ring" style="animation-iteration-count:1; position:absolute; left:1px; top:0px;"></div>'
            f'{triangle_swatch(color)}</div>')


def flooded_crossing_icon_html():
    return ('<div class="flood-pulse-wrap"><div class="flood-pulse-ring"></div>'
            '<div class="flood-pulse-tri"></div></div>')


def add_choropleth_layer(m, gdf, color_col, tooltip_col, name, show, control=True):
    import folium
    fg = folium.FeatureGroup(name=name, show=show, control=control)
    folium.GeoJson(
        gdf.__geo_interface__,
        style_function=lambda f: {
            "fillColor": f["properties"][color_col], "color": "#999999", "weight": 0.3,
            "fillOpacity": 0.1 if f["properties"][color_col] in ("#cccccc", "#eeeeee") else 0.7,
        },
        tooltip=folium.GeoJsonTooltip(fields=[tooltip_col], aliases=[""], labels=False),
    ).add_to(fg)
    fg.add_to(m)
    return fg


def render_map(scenario, event_start_date, event_end_date):
    import folium
    import folium.plugins

    hex_both = scenario["hex_both"]
    crossings = scenario["crossings"]
    facilities = scenario["facilities"]
    n_flooded, n_at_risk = scenario["n_flooded"], scenario["n_at_risk"]
    selected_types = scenario["selected_types"]
    label = facility_label(selected_types)

    # Fit to the Harare boundary, not the wide analysis bbox - previously
    # the map opened zoomed out over the full ~13,400 km^2 area, making the
    # (already boundary-clipped) roads/rivers/floodplain a tiny, easy-to-
    # miss patch. The underlying data still spans the wider area (facilities
    # included per the user's "include all 152" instruction), just the
    # default view now centers on what's actually being shown.
    # prefer_canvas: Leaflet renders vector layers (roads, hexes, crossings)
    # on a single <canvas> instead of one SVG element per feature - much
    # faster to paint at this density (28K road segments + ~600 hex
    # polygons across 3 views + 400+ crossing markers).
    m = folium.Map(tiles=None, control_scale=True, prefer_canvas=True)
    m.fit_bounds(CTX["map_fit_bounds"])
    folium.TileLayer(tiles=OSM_TILE, attr=OSM_ATTR, name="OpenStreetMap", control=False).add_to(m)

    # Only 3 drive-time layers (for the ONE facility type picked in the form)
    # - not 18. Each is clearly labelled with that type by name, and the
    # on-map banner below restates it, so "which facility type am I looking
    # at" is never ambiguous.
    # Population per cell (from the WorldPop-derived grid already built by
    # the pipeline for the regional/priority-ranking stats - see
    # build_population_grid() in flood_accessibility_pipeline.py) folded
    # into every tooltip below, so a time delta reads as "7 extra minutes
    # for ~480 people" rather than just a number with no sense of how many
    # people that actually affects.
    pop = hex_both["population"]

    def pop_phrase(p):
        return f"~{p:,.0f} people" if pd.notna(p) and p > 0 else "no estimated population"

    time_layers = []
    sub = hex_both[["geometry", "baseline"]].copy()
    sub["color"] = sub["baseline"].apply(time_to_color)
    sub["tooltip"] = [
        (f"No flooding: {v:.0f} min to nearest {label} ({pop_phrase(p)} in this cell)" if pd.notna(v)
         else f"No flooding: no {label} reachable ({pop_phrase(p)} in this cell)")
        for v, p in zip(sub["baseline"], pop)]
    time_layers.append(add_choropleth_layer(
        m, sub, "color", "tooltip", f"A. NO FLOODING - drive time to nearest {label}", show=True, control=False))

    sub = hex_both[["geometry", "flooded"]].copy()
    sub["color"] = sub["flooded"].apply(time_to_color)
    sub["tooltip"] = [
        (f"With this flood: {v:.0f} min to nearest {label} ({pop_phrase(p)} in this cell)" if pd.notna(v)
         else f"With this flood: no {label} reachable ({pop_phrase(p)} in this cell)")
        for v, p in zip(sub["flooded"], pop)]
    time_layers.append(add_choropleth_layer(
        m, sub, "color", "tooltip", f"B. WITH FLOODING ({event_start_date} to {event_end_date}) - same, during the flood", show=False, control=False))

    sub = hex_both[["geometry"]].copy()
    b, f_ = hex_both["baseline"], hex_both["flooded"]
    sub["color"] = [delta_to_color(bb, ff) for bb, ff in zip(b, f_)]
    sub["tooltip"] = [
        (f"Flooding cuts off {pop_phrase(p)} entirely" if pd.notna(bb) and pd.isna(ff) else
         (f"Flooding adds {ff - bb:.0f} extra minutes for {pop_phrase(p)}" if pd.notna(bb) and pd.notna(ff) else "No data"))
        for bb, ff, p in zip(b, f_, pop)]
    time_layers.append(add_choropleth_layer(
        m, sub, "color", "tooltip", "C. DIFFERENCE (B minus A) - how much WORSE flooding makes it", show=False, control=False))

    preset = DRAINAGE_PRESETS[scenario["drainage_preset"]]
    fp_bounds = CTX["floodplain_bounds_by_preset"][scenario["drainage_preset"]]
    fg_fp = folium.FeatureGroup(name=f"Self-computed floodplain (HAND, {preset['label']})", show=True)
    folium.raster_layers.ImageOverlay(
        image=CTX["floodplain_pngs"][scenario["drainage_preset"]],
        bounds=[[fp_bounds.bottom, fp_bounds.left], [fp_bounds.top, fp_bounds.right]],
        opacity=1.0,
    ).add_to(fg_fp)
    fg_fp.add_to(m)

    # Roads are NOT added here as a Folium layer at all - they're fetched
    # and rendered by the browser itself (see the deferred <script> below),
    # so nothing about them gets serialized into this HTML file.

    fg_w = folium.FeatureGroup(name="Rivers & streams (Harare boundary)", show=True)
    folium.GeoJson(CTX["waterways"], style_function=lambda f: {"color": "#2171b5", "weight": 2, "opacity": 0.9}).add_to(fg_w)
    fg_w.add_to(m)

    flooded_x = crossings[crossings["flooded_this_scenario"]]
    other_x = crossings[~crossings["flooded_this_scenario"]]
    m.get_root().html.add_child(folium.Element(PULSE_CSS))
    fg_fx = folium.FeatureGroup(name=f"⚠ FLOODED crossings this window ({n_flooded})", show=True)
    for _, row in flooded_x.iterrows():
        folium.Marker(
            location=[row.geometry.y, row.geometry.x],
            icon=folium.DivIcon(html=flooded_crossing_icon_html(), icon_size=(18, 18), icon_anchor=(9, 9)),
            tooltip=(f"FLOODED crossing - {row.get('highway','road')} x {row.get('waterway','waterway')} "
                     f"(flood-prone terrain + heavy rain this window - road removed from the flooded view)")).add_to(fg_fx)
    fg_fx.add_to(m)
    fg_ox = folium.FeatureGroup(name=f"Other crossings ({len(other_x)})", show=True)
    for _, row in other_x.iterrows():
        folium.CircleMarker(location=[row.geometry.y, row.geometry.x], radius=2.5, color="#888888", weight=1,
                             fill=True, fill_color="#bbbbbb", fill_opacity=0.6, tooltip="Not flooded this window").add_to(fg_ox)
    fg_ox.add_to(m)

    MARKER_LETTER = {"hospital": "H", "clinic": "C", "doctors": "D"}
    facility_fgs = {}
    # Keyed by fac_code so the dashboard's "locate" buttons (see the /run
    # route's at_risk_facilities list and the deferred script below) can
    # jump the map straight to a specific at-risk facility instead of
    # making the user hunt through up to 152 markers for it.
    at_risk_marker_js = {}
    for t in ["hospital", "clinic", "doctors"]:
        sub_f = facilities[facilities["amenity"] == t]
        fg = folium.FeatureGroup(name=f"Facility markers - {TYPE_LABELS[t]} ({len(sub_f)})", show=(t in selected_types))
        facility_fgs[t] = fg
        for _, row in sub_f.iterrows():
            at_risk = bool(row["at_risk"])
            color = AT_RISK_ICON_COLOR if at_risk else TYPE_ICON_COLOR_HEX[t]
            letter = "!" if at_risk else MARKER_LETTER[t]
            size = 20 if at_risk else 16
            # Compact badge, not Leaflet's default ~41px pin - the pins were
            # swamping the map at this marker density (152 facilities).
            html = (f'<div style="width:{size}px;height:{size}px;border-radius:50%;background:{color};'
                    f'border:{"2.5px solid #000" if at_risk else "1.5px solid #222"};display:flex;'
                    f'align-items:center;justify-content:center;color:white;font-size:{10 if at_risk else 9}px;'
                    f'font-weight:bold;font-family:Arial,sans-serif;box-shadow:0 1px 3px rgba(0,0,0,0.5);">{letter}</div>')
            mk = folium.Marker(
                location=[row.geometry.y, row.geometry.x],
                icon=folium.DivIcon(html=html, icon_size=(size, size), icon_anchor=(size // 2, size // 2)),
                popup=folium.Popup(f"<b>{row['name']}</b><br>Type: {TYPE_LABELS[t]}<br>"
                                    f"Classification: {row['classification']}<br>CEmONC: {row['CEmONC']}<br>"
                                    f"<b>{'AT RISK this window' if at_risk else 'Not at risk this window'}</b>"
                                    + (f"<div style='font-size:11px;color:#555;margin-top:3px;'>Sits on flood-prone "
                                       f"terrain and heavy rain was recorded here this window - treated as closed "
                                       f"in the flooded view.</div>" if at_risk else ""), max_width=260),
                tooltip=f"{row['name']}{' - AT RISK' if at_risk else ''}")
            mk.add_to(fg)
            if at_risk:
                at_risk_marker_js[row["fac_code"]] = {"var": mk.get_name(), "group": t,
                                                        "lat": row.geometry.y, "lon": row.geometry.x}
        fg.add_to(m)

    fg_b = folium.FeatureGroup(name="Harare district boundary (ADM2)", show=True)
    folium.GeoJson(CTX["boundary"], style_function=lambda f: {"fillOpacity": 0, "color": "#000000", "weight": 3, "dashArray": "8, 5"}).add_to(fg_b)
    fg_b.add_to(m)

    # No folium LayerControl/GroupedLayerControl and no floating legend here
    # at all now - both are gone per the user's explicit ask to drop the
    # in-map legend and control it from the sidebar instead. Every toggleable
    # layer's JS object is exposed below so the PARENT page's sidebar
    # (same-origin iframe) can show/hide each one directly, with the
    # legend swatches living next to each checkbox in that sidebar instead
    # of floating on the map - "is this layer on" and "what does it look
    # like" become the same UI element instead of two separate ones.
    layer_js = {
        "view_a": time_layers[0].get_name(), "view_b": time_layers[1].get_name(), "view_c": time_layers[2].get_name(),
        "floodplain": fg_fp.get_name(), "rivers": fg_w.get_name(),
        "flooded_crossings": fg_fx.get_name(), "other_crossings": fg_ox.get_name(), "boundary": fg_b.get_name(),
        "hospital_markers": facility_fgs["hospital"].get_name(),
        "clinic_markers": facility_fgs["clinic"].get_name(),
        "doctors_markers": facility_fgs["doctors"].get_name(),
    }
    layer_js_obj = "{" + ", ".join(f'"{k}": {v}' for k, v in layer_js.items()) + "}"
    # Keyed on fac_code, value holds a reference to the ACTUAL Leaflet
    # marker object (not just its coordinates) so focusFacility() below can
    # open its popup directly, not just pan/zoom near it.
    facility_markers_js_obj = "{" + ", ".join(
        f'{json.dumps(code)}: {{"marker": {info["var"]}, "group": {json.dumps(info["group"])}, '
        f'"lat": {info["lat"]}, "lon": {info["lon"]}}}'
        for code, info in at_risk_marker_js.items()) + "}"
    # Deferred to window 'load' - folium's own map/layer-creation script runs
    # as a separate inline <script> block LATER in the document than
    # anything added via html.add_child(), so referencing map_xxx/
    # feature_group_xxx here immediately (undeferred) hit a ReferenceError
    # before those variables existed, which silently broke window.
    # setLayerVisible entirely (never got defined past the error) - this is
    # why NO toggle worked, not a per-layer bug and not "just slow
    # rendering". 'load' fires only after all synchronous inline scripts,
    # including folium's, have already run.
    m.get_root().html.add_child(folium.Element(f"""
<script>
window.addEventListener('load', function() {{
  window.MAP_OBJ = {m.get_name()};
  window.LAYERS = {layer_js_obj};
  // Remembers the last requested visibility per layer, even for a layer
  // that doesn't exist yet (e.g. "roads" while it's still loading async
  // below) - so a checkbox flipped during that window still gets applied
  // the instant the layer shows up, instead of being silently dropped.
  window._desiredVisible = {{}};
  window.setLayerVisible = function(key, show) {{
    window._desiredVisible[key] = show;
    var layer = window.LAYERS[key];
    if (!layer) return;
    if (show) {{ if (!window.MAP_OBJ.hasLayer(layer)) window.MAP_OBJ.addLayer(layer); }}
    else {{ if (window.MAP_OBJ.hasLayer(layer)) window.MAP_OBJ.removeLayer(layer); }}
  }};
  window.setView = function(which) {{
    ['view_a','view_b','view_c'].forEach(function(k) {{ window.setLayerVisible(k, k === which); }});
  }};
  window.LAYERS_READY = true;

  // Lets the sidebar dashboard's "locate" buttons (one per at-risk
  // facility) jump straight to that facility instead of making the user
  // hunt through up to 152 markers for it - see the /run route's
  // at_risk_facilities list and fillDashboard() in the parent page.
  window.FACILITY_MARKERS = {facility_markers_js_obj};
  window.focusFacility = function(code) {{
    var entry = window.FACILITY_MARKERS[code];
    if (!entry) return;
    window.setLayerVisible(entry.group + '_markers', true);
    window.MAP_OBJ.setView([entry.lat, entry.lon], 17);
    entry.marker.openPopup();
  }};

  // Roads: fetched + decompressed client-side instead of being embedded
  // in this page (same technique as the existing roadnet tool at
  // portal.placealert.org/roadnet - small gzip chunk, fetched on demand,
  // decompressed with DecompressionStream). This keeps roads crisp vector
  // lines at every zoom, while not blocking the rest of the map on
  // parsing 28K+ features.
  fetch('/harare-flood/roads.json.gz').then(function(res) {{
    return new Response(res.body.pipeThrough(new DecompressionStream('gzip'))).json();
  }}).then(function(geojson) {{
    var roadsLayer = L.geoJSON(geojson, {{
      style: function() {{ return {{ color: '#555555', weight: 1.4, opacity: 0.85 }}; }}
    }});
    window.LAYERS['roads'] = roadsLayer;
    var wantVisible = ('roads' in window._desiredVisible) ? window._desiredVisible['roads'] : true;
    window.setLayerVisible('roads', wantVisible);
  }}).catch(function(err) {{ console.error('roads layer failed to load', err); }});
}});
</script>
"""))

    # Headline population-affected figures - computed once in run_scenario()
    # (shared with the sidebar dashboard panel, see the /run route) rather
    # than recomputed here.
    pop_cutoff_total = scenario["pop_cutoff_total"]
    pop_delayed_total = scenario["pop_delayed_total"]

    # Small, non-interactive caption only - no legend, no controls. Both now
    # live in the parent sidebar.
    m.get_root().html.add_child(folium.Element(
        f'<div style="position: fixed; top: 10px; left: 10px; z-index: 9999; background: white; padding: 6px 12px; '
        f'border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.35); font-family: Arial, sans-serif; max-width: 420px;">'
        f'<div style="font-weight:bold; font-size:13px;">Nearest {label}</div>'
        f'<div style="font-size:11px; color:#555; margin-top:2px; line-height:1.4;">'
        f'Window {event_start_date} to {event_end_date}: {n_flooded}/{len(crossings)} crossings flooded, '
        f'{n_at_risk}/{len(facilities)} facilities at risk. Floodplain: {preset["label"]}.<br>'
        f'~{pop_cutoff_total:,.0f} people cut off entirely, ~{pop_delayed_total:,.0f} people facing longer travel '
        f'(hover a cell on view C for the per-cell breakdown).</div></div>'))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = label.replace(" ", "").replace("(", "").replace(")", "").replace(",", "")
    fname = f"harare_{safe_label}_{event_start_date}_{event_end_date}_{ts}.html"
    out_path = os.path.join(RESULTS_DIR, fname)
    m.save(out_path)
    return fname


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
MAIN_HTML = """
<!doctype html><html><head><title>Harare flood/access tool</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* { box-sizing: border-box; }
html, body { margin:0; padding:0; height:100%; font-family:Arial,sans-serif; color:#222; }
#layout { display:flex; height:100vh; }
#sidebar { width:320px; flex:0 0 320px; padding:14px; background:#fafafa; border-right:1px solid #ddd;
           overflow-y:auto; }
#mapArea { flex:1; position:relative; }
#mapFrame { width:100%; height:100%; border:none; }
#placeholder { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
               color:#999; font-size:14px; text-align:center; padding:20px; }
h2 { font-size:16px; margin:0 0 10px 0; }
h3 { font-size:12px; margin:0; text-transform:uppercase; letter-spacing:0.3px; color:#555; }
label { display:block; margin-top:12px; font-weight:bold; font-size:12.5px; }
.row { display:flex; align-items:center; gap:7px; margin-top:5px; font-weight:normal; font-size:13px; }
.row input { margin:0; flex:0 0 auto; }
.swatch { flex:0 0 16px; height:12px; border-radius:2px; }
.swatch.circle { border-radius:50%; height:16px; }
input[type=date], select { width:100%; padding:6px; font-size:13px; margin-top:4px; }
button { margin-top:14px; width:100%; padding:10px; font-size:14px; background:#1f78b4; color:white;
         border:none; border-radius:6px; cursor:pointer; }
button:disabled { background:#aaa; cursor:not-allowed; }
.note { font-size:11px; color:#666; margin-top:6px; line-height:1.45; }
.warn { background:#fff3cd; border:1px solid #ffe69c; padding:8px; border-radius:6px; margin-bottom:12px; font-size:12.5px; }
.status { margin-top:10px; font-size:12px; padding:7px; border-radius:6px; display:none; }
.status.running { display:block; background:#e7f1fa; color:#1f78b4; }
.status.error { display:block; background:#fde8e8; color:#c0392b; }
.status.done { display:block; background:#e9f8ee; color:#1a7a3c; }
.section { margin-top:14px; padding-top:12px; border-top:1px solid #e3e3e3; }
.disabled-note { color:#999; font-style:italic; }
#viewScale { margin-top:6px; }
#viewScale .row { margin-top:2px; font-size:11.5px; }
/* Floating over the map, top-right, instead of buried partway down the
   long scrollable sidebar - the whole point is that it's visible at a
   glance right after a run, without scrolling past the form controls to
   find it. */
#dashboard { display:none; position:absolute; top:14px; right:14px; z-index:600; width:290px;
             max-height:calc(100% - 28px); overflow-y:auto; background:rgba(255,255,255,0.98);
             border-radius:10px; box-shadow:0 3px 16px rgba(0,0,0,0.28); font-family:Arial,sans-serif;
             border:1px solid rgba(0,0,0,0.08); }
#dashboardHeader { display:flex; align-items:center; justify-content:space-between; padding:10px 12px;
                    cursor:pointer; font-size:13px; font-weight:bold; color:#222; user-select:none; }
#dashboardToggle { background:none; border:none; font-size:13px; color:#555; cursor:pointer; padding:2px 4px; }
#dashboard.collapsed #dashboardBody { display:none; }
#dashboard.collapsed #dashboardToggle { transform:rotate(-90deg); }
#dashboardBody { padding:0 12px 12px 12px; border-top:1px solid #eee; }
.stat-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:8px; }
.stat { background:#fafafa; border:1px solid #e3e3e3; border-radius:7px; padding:7px 9px; }
.stat-value { font-size:16px; font-weight:bold; color:#222; line-height:1.2; }
.stat-value.warn-value { color:#c0392b; }
.stat-label { font-size:10.5px; color:#777; margin-top:2px; line-height:1.3; }
.locate-btn { display:flex; align-items:center; justify-content:space-between; width:100%; text-align:left;
              background:#fdeeea; border:1px solid #f2c6ba; color:#c0392b; border-radius:6px; padding:5px 8px;
              font-size:11.5px; margin-top:4px; cursor:pointer; font-family:Arial,sans-serif; }
.locate-btn:hover { background:#fbdcd3; }
.locate-btn span.pin { font-size:12px; }
@media (max-width: 760px) {
  #dashboard { width:min(290px, calc(100% - 28px)); }
}
</style></head><body>
<div id="layout">
  <div id="sidebar">
    <h2>Harare access &amp; flood</h2>
    <div style="font-size:12px; margin-bottom:12px;">
      <a href="{{ url_for('docs') }}" target="_blank">How this tool works &amp; its assumptions &rarr;</a>
    </div>
    {% if not ready %}
      <div class="warn" id="warmup">{% if error %}Startup failed: {{ error }}{% else %}Warming up (building the road network, first run only) - this will enable itself once ready.{% endif %}</div>
    {% endif %}

    <form id="runForm">
      <label>Facility types</label>
      {% for t, letter, color in [('hospital','H','#0a3d7a'), ('clinic','C','#5f9ea0'), ('doctors','D','#6a3d9a')] %}
      <div class="row">
        <input type="checkbox" name="facility_types" value="{{ t }}" id="ft_{{ t }}" checked data-layer-key="{{ t }}_markers">
        <span class="swatch circle" style="background:{{ color }};"></span>
        <label for="ft_{{ t }}" style="margin:0; font-weight:normal;">{{ t.capitalize() }} ({{ letter }}) marker</label>
      </div>
      {% endfor %}
      <div class="note">Unchecking a type instantly hides its markers on the map. It also narrows the NEXT
      Run's accessibility calculation to only the checked types - e.g. just Hospital checked = nearest hospital only.
      All three = nearest ANY facility. A marker with a red "!" is <b>at risk this window</b> - treated as
      closed in the flooded view (<a href="{{ url_for('docs') }}#trigger" target="_blank">what does that mean?</a>).</div>

      <label>Event end date</label>
      <input type="date" name="end_date" id="end_date" required>
      <label>Window length</label>
      <select name="window">
        <option value="week">1 week (a specific storm/event)</option>
        <option value="month">1 month (wider search)</option>
      </select>
      <div class="note">Searches inside the window for the single heaviest rainfall day, or a lighter day
      following already-wet antecedent conditions (thresholds below - literature-inspired defaults, not
      locally calibrated; actual trigger points vary with soil type and drainage, so they're adjustable).</div>

      <details style="margin-top:10px;" open>
        <summary style="cursor:pointer; font-size:12px; font-weight:bold; color:#555;">Advanced: flood trigger thresholds</summary>
        <div style="margin-top:8px;">
          <label style="font-weight:normal; font-size:12px;">Single-day rainfall that triggers on its own (mm)</label>
          <input type="number" name="regional_threshold_mm" value="50" min="1" step="1" style="width:100%; padding:5px; font-size:12.5px;">
          <label style="font-weight:normal; font-size:12px; margin-top:8px;">Lighter day that still triggers IF ground already wet (mm)</label>
          <input type="number" name="antecedent_trigger_mm" value="25" min="1" step="1" style="width:100%; padding:5px; font-size:12.5px;">
          <label style="font-weight:normal; font-size:12px; margin-top:8px;">3-day antecedent total counted as "already wet" (mm)</label>
          <input type="number" name="antecedent_saturation_mm" value="40" min="1" step="1" style="width:100%; padding:5px; font-size:12.5px;">
          <div class="note">Lower these for areas with poor drainage or clay-heavy soils where less rain
          triggers flooding; raise them for free-draining/sandy areas. See the documentation for the
          literature these defaults are drawn from.</div>
        </div>
      </details>

      <details style="margin-top:10px;" open>
        <summary style="cursor:pointer; font-size:12px; font-weight:bold; color:#555;">Advanced: floodplain sensitivity</summary>
        <div style="margin-top:8px;">
          {% for key, preset in drainage_presets.items() %}
          <div class="row" style="align-items:flex-start;">
            <input type="radio" name="drainage_preset" id="dp_{{ key }}" value="{{ key }}" {% if key == default_drainage_preset %}checked{% endif %} style="margin-top:3px;">
            <label for="dp_{{ key }}" style="margin:0; font-weight:normal;">
              <b>{{ preset.label }}</b> - {{ preset.coverage_pct }}% of Harare flagged floodplain
              {% if key == default_drainage_preset %}<span style="color:#1a7a3c;">(recommended)</span>{% endif %}
              <div class="note" style="margin-top:2px;">{{ preset.note }}</div>
            </label>
          </div>
          {% endfor %}
          <div class="note">Controls which terrain is even considered near a stream (Section 4 of the
          documentation). Precomputed, so switching is instant - unlike the rainfall thresholds above, this
          doesn't rerun anything.</div>
        </div>
      </details>

      <button type="submit" id="runBtn" {% if not ready %}disabled{% endif %}>Run</button>
    </form>
    <div class="status" id="status"></div>

    <div class="section">
      <h3>Accessibility view</h3>
      <div class="row"><input type="radio" name="viewPick" id="view_a" value="view_a" checked>
        <label for="view_a" style="margin:0; font-weight:normal;">A. No flooding (baseline)</label></div>
      <div class="row"><input type="radio" name="viewPick" id="view_b" value="view_b">
        <label for="view_b" style="margin:0; font-weight:normal;">B. With this window's flooding</label></div>
      <div class="row"><input type="radio" name="viewPick" id="view_c" value="view_c">
        <label for="view_c" style="margin:0; font-weight:normal;">C. Difference (B minus A)</label></div>
      <div class="note disabled-note" id="viewNote">A and B use the same drive-time color scale, so you can
      compare them directly. C shows how many extra minutes flooding adds.</div>
      <div id="viewScale">
        <div id="scaleAB">
          {% for c, l in time_scale %}<div class="row"><span class="swatch" style="background:{{ c }};"></span><span>{{ l }} min drive</span></div>{% endfor %}
        </div>
        <div id="scaleC" style="display:none;">
          {% for c, l in delta_scale %}<div class="row"><span class="swatch" style="background:{{ c }};"></span><span>{{ l }}</span></div>{% endfor %}
          <div class="row"><span class="swatch" style="background:#000000;"></span><span>Cut off entirely</span></div>
        </div>
      </div>
    </div>

    <div class="section">
      <h3>Map layers</h3>
      <div class="row"><input type="checkbox" id="ly_floodplain" data-layer-key="floodplain" checked>
        <span class="swatch" style="background:#3182bd; opacity:0.5;"></span>
        <label for="ly_floodplain" style="margin:0; font-weight:normal;">Floodplain (could flood, by terrain)</label></div>
      <div class="row"><input type="checkbox" id="ly_roads" data-layer-key="roads" checked>
        <span class="swatch" style="background:#555555;"></span>
        <label for="ly_roads" style="margin:0; font-weight:normal;">Roads</label></div>
      <div class="row"><input type="checkbox" id="ly_rivers" data-layer-key="rivers" checked>
        <span class="swatch" style="background:#2171b5;"></span>
        <label for="ly_rivers" style="margin:0; font-weight:normal;">Rivers &amp; streams</label></div>
      <div class="row"><input type="checkbox" id="ly_flooded_crossings" data-layer-key="flooded_crossings" checked>
        <span class="swatch" style="background:#e6550d;"></span>
        <label for="ly_flooded_crossings" style="margin:0; font-weight:normal;">⚠ Flooded crossings this window</label></div>
      <div class="note" style="margin-top:2px;">A road-river crossing flagged this way is removed from the
      flooded-view road network for this date window only
      (<a href="{{ url_for('docs') }}#trigger" target="_blank">what does that mean?</a>).</div>
      <div class="row"><input type="checkbox" id="ly_other_crossings" data-layer-key="other_crossings" checked>
        <span class="swatch" style="background:#bbbbbb;"></span>
        <label for="ly_other_crossings" style="margin:0; font-weight:normal;">Other crossings (not flooded)</label></div>
      <div class="row"><input type="checkbox" id="ly_boundary" data-layer-key="boundary" checked>
        <span class="swatch" style="background:#ffffff; border:2px dashed #000;"></span>
        <label for="ly_boundary" style="margin:0; font-weight:normal;">Harare district boundary</label></div>
    </div>
  </div>
  <div id="mapArea">
    <div id="dashboard">
      <div id="dashboardHeader">
        <span>Scenario summary</span>
        <button type="button" id="dashboardToggle" aria-label="Collapse">&#9662;</button>
      </div>
      <div id="dashboardBody">
        <div class="note" id="dashWindow" style="margin-top:0;"></div>
        <div class="stat-grid">
          <div class="stat"><div class="stat-value" id="statCrossings">-</div><div class="stat-label">Crossings flooded</div></div>
          <div class="stat"><div class="stat-value" id="statFacilities">-</div><div class="stat-label">Facilities at risk</div></div>
          <div class="stat"><div class="stat-value" id="statRain">-</div><div class="stat-label">Peak daily rainfall (CHIRPS)</div></div>
          <div class="stat"><div class="stat-value" id="statAntecedent">-</div><div class="stat-label">Peak 3-day antecedent rain</div></div>
          <div class="stat"><div class="stat-value" id="statCutoff">-</div><div class="stat-label">People cut off entirely</div></div>
          <div class="stat"><div class="stat-value" id="statDelayed">-</div><div class="stat-label">People facing longer travel</div></div>
          <div class="stat" style="grid-column:1 / span 2;"><div class="stat-value" id="statAvgTime">-</div>
            <div class="stat-label">Avg. drive time, population-weighted (no flooding &rarr; with flooding)</div></div>
        </div>
        <div id="atRiskList" style="display:none; margin-top:8px;">
          <div class="stat-label" style="margin-bottom:4px;">Click to locate on the map:</div>
        </div>
        <div class="note" id="dashPreset" style="margin-top:6px;"></div>
        <div class="note">Population figures use the same WorldPop-derived grid as the per-cell map tooltips
        (view C). "Avg. drive time" weights each hexagon by how many people live there, so a change in a
        busy area counts for more than the same change in an empty one.</div>
      </div>
    </div>
    <div id="placeholder">Set your dates and facility types, then click Run.</div>
    <iframe id="mapFrame" style="display:none;"></iframe>
  </div>
</div>
<script>
const form = document.getElementById('runForm');
const statusEl = document.getElementById('status');
const btn = document.getElementById('runBtn');
const frame = document.getElementById('mapFrame');
const placeholder = document.getElementById('placeholder');

document.getElementById('dashboardHeader').addEventListener('click', () => {
  document.getElementById('dashboard').classList.toggle('collapsed');
});

function fillDashboard(m) {
  document.getElementById('dashboard').style.display = 'block';
  document.getElementById('dashWindow').textContent =
    'Window ' + m.event_start_date + ' to ' + m.event_end_date + ' - ' + m.facility_label;
  document.getElementById('statCrossings').textContent = m.crossings_flooded + ' / ' + m.crossings_total;
  const facEl = document.getElementById('statFacilities');
  facEl.textContent = m.facilities_at_risk + ' / ' + m.facilities_total;
  facEl.className = 'stat-value' + (m.facilities_at_risk > 0 ? ' warn-value' : '');
  document.getElementById('statRain').textContent = m.peak_daily_rain_mm.toFixed(0) + ' mm';
  document.getElementById('statAntecedent').textContent = m.peak_antecedent_mm.toFixed(0) + ' mm';
  document.getElementById('statCutoff').textContent = '~' + Math.round(m.pop_cutoff).toLocaleString();
  document.getElementById('statDelayed').textContent = '~' + Math.round(m.pop_delayed).toLocaleString();
  const avgEl = document.getElementById('statAvgTime');
  if (m.weighted_baseline_min != null && m.weighted_flooded_min != null) {
    const pct = m.pct_change != null ? ' (' + (m.pct_change >= 0 ? '+' : '') + m.pct_change.toFixed(0) + '%)' : '';
    avgEl.textContent = m.weighted_baseline_min.toFixed(0) + ' min → ' + m.weighted_flooded_min.toFixed(0) + ' min' + pct;
  } else {
    avgEl.textContent = 'No reachable population in one of the two scenarios';
  }
  document.getElementById('dashPreset').textContent =
    'Floodplain sensitivity: ' + m.drainage_preset_label + ' (' + m.drainage_coverage_pct + '% of Harare flagged).';

  const listEl = document.getElementById('atRiskList');
  const facs = m.at_risk_facilities || [];
  listEl.querySelectorAll('.locate-btn').forEach(b => b.remove());
  if (facs.length === 0) {
    listEl.style.display = 'none';
  } else {
    listEl.style.display = 'block';
    facs.forEach(f => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'locate-btn';
      b.innerHTML = '<span>' + f.name + '</span><span class="pin">📍</span>';
      b.addEventListener('click', () => {
        whenFrameReady(() => frame.contentWindow.focusFacility(f.fac_code));
      });
      listEl.appendChild(b);
    });
  }
}

function currentLayerState() {
  const state = {};
  document.querySelectorAll('[data-layer-key]').forEach(el => { state[el.dataset.layerKey] = el.checked; });
  return state;
}
function currentView() {
  const el = document.querySelector('input[name=viewPick]:checked');
  return el ? el.value : 'view_a';
}
// The map inside the iframe sets window.LAYERS_READY itself once its own
// 'load' handler has run (see render_map's injected script) - the iframe's
// own 'load' event and that inner handler can land in either order, so
// poll briefly instead of assuming the single 'load' firing means it's ready.
function whenFrameReady(fn, triesLeft) {
  triesLeft = triesLeft === undefined ? 20 : triesLeft;
  if (frame.contentWindow && frame.contentWindow.LAYERS_READY) { fn(); return; }
  if (triesLeft <= 0) return;
  setTimeout(() => whenFrameReady(fn, triesLeft - 1), 150);
}
function applyAllToFrame() {
  whenFrameReady(() => {
    const state = currentLayerState();
    Object.keys(state).forEach(k => frame.contentWindow.setLayerVisible(k, state[k]));
    frame.contentWindow.setView(currentView());
  });
}
// Live toggle: any checkbox with data-layer-key applies INSTANTLY to the
// current map, no re-run needed - these just show/hide, they don't change
// the underlying computation (that only happens on Run).
document.querySelectorAll('[data-layer-key]').forEach(el => {
  el.addEventListener('change', () => {
    whenFrameReady(() => frame.contentWindow.setLayerVisible(el.dataset.layerKey, el.checked));
    if (el.name === 'facility_types') {
      btn.textContent = 'Run (selection changed - click to update colors)';
    }
  });
});
document.querySelectorAll('input[name=viewPick]').forEach(el => {
  el.addEventListener('change', () => {
    document.getElementById('scaleAB').style.display = (el.value === 'view_c') ? 'none' : 'block';
    document.getElementById('scaleC').style.display = (el.value === 'view_c') ? 'block' : 'none';
    whenFrameReady(() => frame.contentWindow.setView(el.value));
  });
});
frame.addEventListener('load', applyAllToFrame);

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const checked = form.querySelectorAll('input[name=facility_types]:checked');
  if (checked.length === 0) {
    statusEl.className = 'status error';
    statusEl.textContent = 'Check at least one facility type.';
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Running...';
  statusEl.className = 'status running';
  statusEl.textContent = 'Fetching rainfall and re-routing...';
  try {
    const resp = await fetch('{{ url_for("run") }}', { method: 'POST', body: new FormData(form) });
    const data = await resp.json();
    if (!data.ok) {
      statusEl.className = 'status error';
      statusEl.textContent = data.error || 'Something went wrong.';
    } else {
      statusEl.className = 'status done';
      statusEl.textContent = 'Done - ' + data.summary;
      placeholder.style.display = 'none';
      frame.style.display = 'block';
      frame.src = data.url;
      if (data.metrics) fillDashboard(data.metrics);
    }
  } catch (err) {
    statusEl.className = 'status error';
    statusEl.textContent = 'Request failed: ' + err;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Run';
  }
});

{% if not ready %}
// Poll readiness while warming up, enable the button once the server says ready.
const poll = setInterval(async () => {
  const resp = await fetch('{{ url_for("status_check") }}');
  const data = await resp.json();
  if (data.ready) {
    document.getElementById('warmup').textContent = 'Ready.';
    btn.disabled = false;
    clearInterval(poll);
  } else if (data.error) {
    document.getElementById('warmup').textContent = 'Startup failed: ' + data.error;
    clearInterval(poll);
  }
}, 10000);
{% endif %}
</script>
</body></html>
"""


DOCS_HTML = """
<!doctype html><html><head><title>Harare flood/access tool - documentation</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body { font-family: Arial, sans-serif; max-width: 860px; margin: 0 auto; padding: 24px 20px 80px 20px; color:#222; line-height:1.55; }
h1 { font-size: 22px; }
h2 { font-size: 17px; margin-top: 36px; border-bottom: 2px solid #1f78b4; padding-bottom: 4px; }
h3 { font-size: 14.5px; margin-top: 20px; }
code { background:#f3f3f3; padding:1px 5px; border-radius:3px; font-size:12.5px; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 13px; }
th, td { border: 1px solid #ddd; padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: #f5f5f5; }
.warn { background:#fff3cd; border:1px solid #ffe69c; padding:10px 14px; border-radius:6px; margin:14px 0; }
.note { background:#e7f1fa; border:1px solid #bcd8ee; padding:10px 14px; border-radius:6px; margin:14px 0; }
a.back { display:inline-block; margin-bottom: 16px; font-size:13px; }
.toc { background:#fafafa; border:1px solid #e3e3e3; border-radius:6px; padding:12px 18px; font-size:13px; }
.toc a { display:block; margin:3px 0; }
.fig { text-align:center; margin: 18px 0; }
.fig svg { max-width:100%; height:auto; background:#fcfcfc; border:1px solid #eee; border-radius:6px; }
.fig-caption { font-size:12px; color:#666; margin-top:6px; }
.refs li { margin-bottom:10px; font-size:13px; }
</style></head><body>

<a class="back" href="/harare-flood/">&larr; Back to the tool</a>
<h1>Harare access-to-care &amp; flood hazard tool - documentation</h1>
<p>This page explains what the tool computes, where every number comes from, and - most importantly - what it
does <b>not</b> know. Read the limitations section before presenting any output from this tool as a finding.</p>

<div class="toc">
<b>Contents</b>
<a href="#overview">1. What this tool answers</a>
<a href="#data">2. Data sources</a>
<a href="#roads">3. Road network &amp; travel time</a>
<a href="#flood">4. Flood hazard - the HAND method</a>
<a href="#dem">5. Why MERIT Hydro, not a 30m DEM</a>
<a href="#trigger">6. What makes a crossing/facility "flooded this window" (adjustable)</a>
<a href="#access">7. Accessibility computation (the three views)</a>
<a href="#scope">8. Geographic scope</a>
<a href="#limits">9. Assumptions &amp; limitations (read this)</a>
<a href="#refs">10. References</a>
</div>

<h2 id="overview">1. What this tool answers</h2>
<p>Two linked questions: <b>(a)</b> how long does it take to drive from anywhere in the Harare area to the
nearest health facility of a chosen <b>type</b> (Hospital, Clinic, Doctors - you select a category, not
individual facilities - see Section 7), and <b>(b)</b> how does that change if a specific week or month of
rainfall triggers flooding on the road network. It is built for a quick, repeatable "what if" look, not as a
validated engineering or epidemiological model.</p>

<h2 id="data">2. Data sources</h2>
<table>
<tr><th>Input</th><th>Source</th><th>Resolution / coverage</th><th>Used for</th></tr>
<tr><td>Road network</td><td>OpenStreetMap, Zimbabwe extract (<code>zimbabwe-latest.osm.pbf</code>), read via <code>pyrosm</code></td>
    <td>Whatever OSM contributors have mapped - generally good in urban Harare, sparser in rural fringe areas</td>
    <td>The routable graph every travel time is computed on</td></tr>
<tr><td>Health facilities</td><td>A curated list of 152 facilities (type, ownership, CEmONC status) previously
    sourced from earlier project work (ONTIME)</td>
    <td>Point locations</td>
    <td>Destinations for every accessibility calculation</td></tr>
<tr><td>Terrain / elevation</td><td>MERIT Hydro (elevation, flow direction, upstream drainage area), via Google Earth Engine - see Section 5 for why this DEM specifically</td>
    <td>90m</td>
    <td>The self-computed floodplain (HAND method, Section 4)</td></tr>
<tr><td>Rainfall</td><td>CHIRPS Daily, via Google Earth Engine</td>
    <td>~5.5km (0.05&deg;)</td>
    <td>Decides whether a geomorphically-floodable crossing/facility actually floods in your chosen window</td></tr>
<tr><td>Rivers &amp; streams</td><td>OpenStreetMap waterways (river/stream tags)</td>
    <td>As mapped in OSM</td>
    <td>The rivers layer, and locating road/river crossings</td></tr>
<tr><td>Harare boundary</td><td>FAO GAUL 2015 admin level-2 boundaries</td>
    <td>District-level polygon</td>
    <td>Scoping the map view (Section 8)</td></tr>
</table>

<h2 id="roads">3. Road network &amp; travel time</h2>
<p>The OSM road network is loaded and turned into a routable graph (541,787 nodes, 1,146,146 edges once non-driveable
ways - footpaths, steps, cycleways - are removed). Every remaining edge gets a travel time based on its road
type and surface (motorway 120km/h down to service/track 20km/h, scaled by a paved/unpaved surface factor where
OSM doesn't tag surface directly). <b>These are heuristic speed assumptions, not measured traffic data</b> - no
congestion, time-of-day, or calibrated travel-time data is used.</p>
<p>Travel time to the nearest facility of your selected type(s) is computed with Dijkstra's shortest-path
algorithm, run simultaneously from every matching facility - so "nearest ANY of these" falls out of the
algorithm itself, it isn't approximated.</p>

<h2 id="flood">4. Flood hazard - the HAND method</h2>
<p>No ready-made, validated flood hazard map exists for Harare. Rather than skip flood context entirely or rely
on a coarse global product (e.g. JRC Global Surface Water, which only captures permanent/seasonal water and
misses local flash flooding), this tool computes its own flood-prone-area layer from terrain, using a method
called <b>HAND (Height Above Nearest Drainage)</b>, originally developed by Rennó et al. (2008) <sup>[1]</sup>
for mapping terra-firme vs. flood-prone rainforest terrain and since used fairly widely in flood-susceptibility
screening. This was arrived at through a literature review of openly-available flood-modelling approaches
suited to a data-sparse setting like Harare - it is not a method inherited from any specific prior project.</p>

<h3>How it works</h3>
<ol>
<li>For every 90m terrain cell, trace downhill (following MERIT Hydro's flow-direction grid) until reaching a
cell classified as a stream (upstream drainage area &ge; <b>4.05 km&sup2;</b>). That specific threshold follows
Hu &amp; Demir (2021) <sup>[5]</sup>, a real-time client-side HAND implementation validated against FEMA flood
maps in Iowa (6-10% difference from the reference maps, clearly beating a basic flood-fill baseline's 15-30%
difference) - they report 4 km&sup2; as their effective drainage threshold, citing Nobre et al. (2016) <sup>[6]</sup>'s
4.05 km&sup2; literature value, which is what's used here. This is a Harare-specific choice, not the pipeline's
general-purpose default (0.05 km&sup2;, tuned for stream-crossing detection on the CASCADE project's other
Zimbabwe sites) - a larger drainage threshold means fewer, larger streams get recognised, which produces a
tighter, more literature-consistent floodplain extent.</li>
<li><b>HAND</b> = that cell's elevation minus the stream cell's elevation it drains to - literally, "how far
above the nearest drainage line is this point."</li>
<li>The stream cell's plausible flood water level is estimated with a power-law relationship between channel
width/depth and drainage area, the same family of empirical relationships described in Leopold, Wolman &amp;
Miller's classic hydraulic-geometry work (1964) <sup>[2]</sup>:
<code>water_level = 0.01 &times; drainage_area_m&sup2;<sup>0.30</sup></code> (bigger catchment upstream &rarr; higher
plausible flood level).</li>
<li>A cell is flagged <b>floodplain</b> if its HAND value is &le; that estimated water level - i.e. it sits low
enough, relative to the nearest stream, that a flood of a plausible size could reach it.</li>
</ol>

<div class="fig">
<svg viewBox="0 0 640 300" xmlns="http://www.w3.org/2000/svg">
  <polygon points="10,40 180,230 230,250 410,250 460,230 630,40 630,280 10,280"
           fill="#eef3ea" stroke="none"/>
  <polyline points="10,40 180,230 230,250 410,250 460,230 630,40"
            fill="none" stroke="#6b8f5e" stroke-width="3"/>
  <rect x="225" y="245" width="10" height="10" fill="#2171b5"/>
  <text x="230" y="275" font-size="12" text-anchor="middle" fill="#222">S (stream cell)</text>
  <circle cx="110" cy="135" r="5" fill="#c0392b"/>
  <text x="118" y="132" font-size="12" fill="#222">P (hillslope point)</text>
  <line x1="110" y1="135" x2="110" y2="250" stroke="#c0392b" stroke-width="1.5" stroke-dasharray="4,3"/>
  <line x1="110" y1="250" x2="230" y2="250" stroke="#c0392b" stroke-width="1.5" stroke-dasharray="4,3"/>
  <text x="70" y="195" font-size="12" fill="#c0392b" transform="rotate(-90 70 195)">HAND = elev(P) - elev(S)</text>
  <line x1="150" y1="222" x2="520" y2="222" stroke="#1f78b4" stroke-width="1.5" stroke-dasharray="6,3"/>
  <text x="525" y="226" font-size="12" fill="#1f78b4">plausible water level (from drainage area)</text>
  <polygon points="183,250 230,250 410,250 457,250 457,222 183,222" fill="#9fc3e0" opacity="0.5"/>
  <text x="300" y="240" font-size="12" fill="#13477a" text-anchor="middle">self-computed floodplain
    (HAND &le; water level)</text>
  <text x="110" y="120" font-size="11" fill="#6b8f5e">P is ABOVE the threshold &rarr; not flagged floodplain</text>
</svg>
<div class="fig-caption">Fig. 1 - HAND cross-section concept. Point P's HAND value (vertical distance above the
stream) is compared against a water-level threshold that scales with upstream drainage area; cells below the
threshold are flagged floodplain, cells above are not.</div>
</div>

<div class="note">This is a <b>geomorphic</b> method - it answers "could this point plausibly flood, given its
position relative to the drainage network and terrain," not "will it flood on this specific date at this
specific depth." It is not a hydraulic/hydrodynamic model (no simulated water flow, volume, or timing) and the
90m/0.30 exponent parameters are standard literature defaults for this method family, not fitted to any
Harare-specific flood observation. HAND also doesn't account for river width (a wide river reads as a narrow,
deep "canyon" since the stream is only ever one pixel wide) or cap flooding by absolute elevation (a high
plateau draining toward a distant low-lying stream can be flagged floodable purely on relative height) - Hu
&amp; Demir (2021) <sup>[5]</sup> add explicit HAND-threshold and absolute-height parameters to handle exactly
this; this tool does not yet implement an absolute-height cap, which is a known gap, not an oversight hidden
from this documentation.</div>
<p>Within the Harare district boundary, <b>5.7%</b> of resolved terrain falls in this floodplain category with
the 4.05 km&sup2; threshold above (confirmed by direct raster sampling) - noticeably tighter than the pipeline's
general-purpose default would have given (25.5%), which is the expected, literature-consistent effect of a
larger drainage threshold recognising fewer, larger streams.</p>

<h3>Selectable floodplain sensitivity</h3>
<p>Since there's no locally-verified "correct" drainage threshold for Harare specifically, the tool exposes 4
precomputed presets spanning the credible range (the "Advanced: floodplain sensitivity" section on the form) -
unlike the rainfall-gate thresholds (Section 6), this isn't a free-typed number, because changing it means
re-running the HAND trace over the whole terrain grid, not a cheap comparison; precomputing a few labelled
options keeps switching instant.</p>
<table>
<tr><th>Preset</th><th>Threshold</th><th>Harare coverage</th><th>Recommendation / implication</th></tr>
<tr><td>Dense</td><td>0.05 km&sup2;</td><td>25.5%</td><td>Recognises even tiny gullies as streams - broadest,
most cautious extent, but likely overstates real flood risk away from actual rivers.</td></tr>
<tr><td>Moderate</td><td>1.0 km&sup2;</td><td>8.1%</td><td>A middle ground.</td></tr>
<tr><td><b>Recommended</b></td><td>4.05 km&sup2;</td><td>5.7%</td><td>Literature value (Nobre et al. 2016, via Hu
&amp; Demir 2021) - the default used throughout the rest of this page's worked examples.</td></tr>
<tr><td>Conservative</td><td>10.0 km&sup2;</td><td>4.2%</td><td>Only major rivers count - narrowest extent, risks
missing real flood-prone areas along smaller but still significant streams.</td></tr>
</table>

<h2 id="dem">5. Why MERIT Hydro, not a 30m DEM</h2>
<p>A fair question: Copernicus GLO-30 and SRTM are both finer resolution (30m vs MERIT Hydro's 90m) and freely
available in Earth Engine - why not use one of those instead?</p>
<ul>
<li><b>HAND needs more than elevation</b> - it needs flow-direction and upstream-drainage-area grids too (steps
1-3 above). MERIT Hydro ships these pre-computed, globally consistent, and derived directly from its own
elevation layer. GLO-30 and raw SRTM are elevation-only; using either would mean computing flow-direction and
flow-accumulation from scratch, which first requires depression-filling/hydrological conditioning (noise in a
raw DEM creates artificial pits that block flow routing) - a real additional modelling step this tool doesn't
currently do.</li>
<li><b>Copernicus GLO-30 is a surface model, not bare-earth</b> - it represents buildings, infrastructure and
vegetation canopy as part of the elevation surface (water bodies are edited for consistent flow, but built-up
areas and tree canopy are not stripped out). For flow-routing in a city, a building or a stand of trees showing
up as elevation can misdirect or block computed flow paths that would, on the real ground surface, pass
straight through or around them.</li>
<li><b>SRTM has known, well-documented error sources</b> (stripe noise, speckle noise, and vegetation-height
bias from the C-band radar not always reaching bare ground) - the MERIT DEM that underlies MERIT Hydro was
specifically built to correct these by cross-referencing SRTM against AW3D and Viewfinder Panoramas DEMs
(Yamazaki et al. 2017) <sup>[3]</sup>; using SRTM directly would reintroduce exactly the noise MERIT already
removed.</li>
</ul>
<p>The honest trade-off: 90m is coarser, and a finer bare-earth hydrologically-conditioned DEM (if one existed
ready-made for this area) would resolve smaller drainage features MERIT Hydro's 90m can miss. That's a real
limitation (Section 9), not a resolved question - but it was the practical, methodologically sound choice
available without building a custom DEM-conditioning pipeline from scratch.</p>

<h2 id="trigger">6. What makes a crossing/facility "flooded this window" (adjustable)</h2>
<div class="warn" style="background:#eef6ff; border-color:#9ec5e8;">
<p style="margin-top:0;"><b>In plain terms:</b></p>
<p><b>⚠ A "flooded crossing"</b> is a point where a road crosses a river or stream, AND, for the specific date
window you picked, (1) this tool's own terrain analysis says that spot sits low and close enough to the
stream to plausibly flood (Section 4), <b>and</b> (2) real satellite rainfall data (CHIRPS) says it rained
hard enough there during that window to plausibly cause it (Section 6 below). <b>Effect on the map:</b> the
road at that exact point is deleted from the "with flooding" road network (view B/C) - any route that needed
to cross there has to find another way around, or is cut off if there isn't one.</p>
<p><b>A "facility at risk"</b> (shown with a red "!" marker) is a hospital/clinic/doctors location where the
exact same two conditions hold at the facility's own coordinates - it sits on terrain that could flood, and
the rainfall during your chosen window was heavy enough to plausibly flood it. <b>Effect on the map:</b> that
facility is treated as closed/unreachable in the "with flooding" view (B/C) - it no longer counts as
somewhere anyone can reach, even if the roads leading to it are fine.</p>
<p>Both flags are specific to the ONE date window you've selected - the same crossing or facility can be
flagged in a wet month's run and not flagged in a dry month's run; it isn't a fixed property of the location,
it depends on the rainfall that actually fell during your chosen dates.</p>
</div>
<p style="margin-top:14px;"><b>How this is computed:</b> being geomorphically in the floodplain is necessary but
not sufficient - a floodplain cell in a drought month isn't flooded. For your chosen date window, a crossing or
facility is only flagged as <b>actually</b> flooded if BOTH:</p>
<ul>
<li>it sits in the self-computed floodplain (Section 4), <b>AND</b></li>
<li>the rainfall gate triggers: either the single heaviest CHIRPS day inside your window exceeds a threshold on
its own, or a lighter day follows 3 antecedent days (just before your window starts) that together exceeded a
saturation threshold (a proxy for "the ground was already wet").</li>
</ul>
<p>The default thresholds (50mm single-day / 25mm+antecedent-40mm) are commonly cited as rough Southern-Africa
flash-flood rainfall-intensity benchmarks, but <b>we don't have one specific, locally-verified citation to pin
to that exact figure</b> - treat them as a reasonable literature-inspired starting point, not an authoritative
number. That's exactly why they're <b>adjustable in the tool itself</b> (the "Advanced: flood trigger
thresholds" section on the form): the same rainfall genuinely triggers flooding at very different intensities
depending on soil type, drainage infrastructure, and antecedent land-use conditions - an area with poor drainage
or heavy clay soils can flood at well under 50mm, while free-draining sandy soils may absorb considerably more
before anything backs up. Lower the thresholds to model a more flood-prone assumption; raise them for a more
flood-resistant one.</p>
<div class="warn">There is still no crossing-capacity engineering dataset behind this gate - adjusting the
thresholds changes the rainfall-intensity assumption, not a measurement of what actually overwhelms a specific
bridge or culvert. That data doesn't exist for this area as far as this tool is aware.</div>

<h2 id="access">7. Accessibility computation (the three views)</h2>
<p>You select <b>facility types</b> (Hospital / Clinic / Doctors) with the checkboxes on the form - not
individual facilities. Checking all three means "nearest facility of ANY of these types"; checking just one
narrows every calculation to that type alone. There's no hand-picking of specific hospitals or clinics out of
the 152 - the selection is always by category.</p>
<table>
<tr><th>View</th><th>What it computes</th></tr>
<tr><td><b>A. No flooding (baseline)</b></td><td>Drive time to the nearest matching-type facility on the normal
road network - no edges removed, every matching facility counts regardless of this window's rain.</td></tr>
<tr><td><b>B. With this window's flooding</b></td><td>Same calculation, but (1) any road segment at a crossing
flagged "flooded this window" is removed from the graph entirely, and (2) any matching-type facility flagged
"at risk this window" is excluded as a destination - treated as closed.</td></tr>
<tr><td><b>C. Difference (B minus A)</b></td><td>The direct subtraction, in minutes, area by area. Where a
hexagon that was reachable in A becomes completely unreachable in B, it's shown as "cut off entirely" rather
than a minute value.</td></tr>
</table>

<h2 id="scope">8. Geographic scope</h2>
<p>The underlying facility list spans a wider area than Harare city proper - it includes facilities in
Marondera, Bindura, Norton and other surrounding areas (152 total). All 152 are included in the
routing/accessibility calculations regardless of the map's visual extent; there's no accuracy cost to this, the
wider facility set can only make nearest-facility results more correct. The map's <b>visual display</b> (roads,
rivers, the colored hexagons, crossings) is clipped to the official Harare district (FAO GAUL ADM2) boundary for
legibility - 199 of 2,086 analysis hexagons and 404 of 902 road/river crossings fall inside that boundary.</p>

<h2 id="limits">9. Assumptions &amp; limitations (read this)</h2>
<ul>
<li><b>The rainfall window is whatever you pick - it is not a documented, historical flood event</b> unless you
specifically choose dates matching one.</li>
<li><b>The flood layer is a screening tool, not an engineering hazard map</b> and has not been validated against
any observed flood extent in Harare specifically.</li>
<li><b>90m terrain resolution</b> can miss small drainage features (Section 5).</li>
<li><b>~5.5km rainfall resolution</b> means every point within roughly a 5.5km cell is assumed to get the same
rainfall - real storms are frequently more localized than that.</li>
<li><b>No crossing-capacity data</b> - the flood trigger is a rainfall-intensity proxy, adjustable but still not
a measurement of any specific bridge or culvert's actual capacity.</li>
<li><b>Road speeds are heuristic</b>, based on OSM road-type/surface tags, not measured traffic or real travel
times.</li>
<li><b>"Nearest" only, not "best."</b> The tool finds whichever matching-type facility is closest by drive time -
it says nothing about capacity, quality of care, or whether it's actually open/staffed.</li>
<li><b>Facility locations</b> are geocoded points, each snapped to its nearest road network node for routing,
which can introduce small errors where a facility's mapped point sits some distance from its actual road
entrance.</li>
<li><b>Geometry is simplified</b> for rendering performance - this affects only how lines are drawn, not any
computation.</li>
</ul>

<h2 id="refs">10. References</h2>
<ol class="refs">
<li>[1] Renn&oacute;, C.D., Nobre, A.D., Cuartas, L.A., Soares, J.V., Hodnett, M.G., Tomasella, J. and Waterloo,
M.J. (2008). "HAND, a new terrain descriptor using SRTM-DEM: Mapping terra-firme rainforest environments in
Amazonia." <i>Remote Sensing of Environment</i>, 112(9), 3469-3481.</li>
<li>[2] Leopold, L.B., Wolman, M.G. and Miller, J.P. (1964). <i>Fluvial Processes in Geomorphology.</i> W.H.
Freeman and Company. (Source of the hydraulic-geometry power-law scaling between channel dimensions and
drainage area/discharge used for the water-level threshold.)</li>
<li>[3] Yamazaki, D., Ikeshima, D., Sosa, J., Bates, P.D., Allen, G.H. and Pavelsky, T.M. (2019). "MERIT Hydro: A
high-resolution global hydrography map based on latest topography datasets." <i>Water Resources Research</i>,
55(6), 5053-5073. (MERIT Hydro flow-direction/drainage-area product used throughout.)</li>
<li>Yamazaki, D., Ikeshima, D., Tawatari, R., Yamaguchi, T., O'Loughlin, F., Neal, J.C., Sampson, C.C., Kanae, S.
and Bates, P.D. (2017). "A high-accuracy map of global terrain elevations." <i>Geophysical Research Letters</i>,
44(11), 5844-5853. (The underlying MERIT DEM and its error-correction method, referenced in Section 5.)</li>
<li>Funk, C. et al. (2015). "The climate hazards infrared precipitation with stations - a new environmental
record for monitoring extremes." <i>Scientific Data</i>, 2, 150066. (CHIRPS rainfall dataset.)</li>
<li>[5] Hu, A. and Demir, I. (2021). "Real-Time Flood Mapping on Client-Side Web Systems Using HAND Model."
<i>Hydrology</i>, 8(2), 65. https://doi.org/10.3390/hydrology8020065. (Source of Harare's 4.05 km&sup2; drainage
threshold and the HAND-threshold/absolute-height-cap discussion in Section 4; validated their implementation at
6-10% difference from FEMA reference flood maps in two Iowa sites.)</li>
<li>[6] Nobre, A.D., Cuartas, L.A., Momo, M.R., Severo, D.L., Pinheiro, A. and Nobre, C.A. (2016). "HAND contour:
A new proxy predictor of inundation extent." <i>Hydrological Processes</i>, 30(2), 320-333. (Original source of
the 4.05 km&sup2; drainage-threshold value, cited via Hu &amp; Demir 2021 above.)</li>
</ol>

<a class="back" href="/harare-flood/">&larr; Back to the tool</a>
</body></html>
"""


@app.route("/harare-flood/")
def index():
    return render_template_string(
        MAIN_HTML, ready=STATE["ready"], error=STATE["error"],
        time_scale=list(zip(TIME_COLORS, TIME_BIN_LABELS)),
        delta_scale=list(zip(DELTA_COLORS, DELTA_LABELS)),
        drainage_presets=DRAINAGE_PRESETS, default_drainage_preset=DEFAULT_DRAINAGE_PRESET)


@app.route("/harare-flood/docs")
def docs():
    return DOCS_HTML


@app.route("/harare-flood/status")
def status_check():
    return {"ready": STATE["ready"], "error": STATE["error"]}


@app.route("/harare-flood/run", methods=["POST"])
def run():
    if not STATE["ready"]:
        return {"ok": False, "error": "Still warming up - not ready yet."}, 503
    end_date = request.form.get("end_date")
    if not end_date:
        return {"ok": False, "error": "Pick an end date."}, 400
    window = request.form.get("window", "week")
    selected_types = request.form.getlist("facility_types") or ["hospital", "clinic", "doctors"]
    selected_types = [t for t in selected_types if t in ("hospital", "clinic", "doctors")]
    if not selected_types:
        return {"ok": False, "error": "Check at least one facility type."}, 400
    try:
        regional_threshold_mm = float(request.form.get("regional_threshold_mm", REGIONAL_FLOOD_THRESHOLD_MM))
        antecedent_trigger_mm = float(request.form.get("antecedent_trigger_mm", ANTECEDENT_TRIGGER_MM))
        antecedent_saturation_mm = float(request.form.get("antecedent_saturation_mm", ANTECEDENT_SATURATION_MM))
    except ValueError:
        return {"ok": False, "error": "Flood trigger thresholds must be numbers."}, 400
    drainage_preset = request.form.get("drainage_preset", DEFAULT_DRAINAGE_PRESET)
    if drainage_preset not in DRAINAGE_PRESETS:
        return {"ok": False, "error": f"Unknown floodplain sensitivity '{drainage_preset}'."}, 400
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=7 if window == "week" else 30)
    event_start_date, event_end_date = start_dt.strftime("%Y-%m-%d"), end_date
    try:
        with LOCK:
            scenario = run_scenario(event_start_date, event_end_date, selected_types,
                                     regional_threshold_mm, antecedent_trigger_mm, antecedent_saturation_mm,
                                     drainage_preset)
            fname = render_map(scenario, event_start_date, event_end_date)
    except Exception as e:
        return {"ok": False, "error": f"Run failed: {e}"}, 500
    at_risk_names = scenario["facilities"].loc[scenario["facilities"]["at_risk"], "name"].tolist()
    summary = f"{scenario['n_flooded']}/{len(scenario['crossings'])} crossings flooded, " \
              f"{scenario['n_at_risk']} facilities at risk"
    if at_risk_names:
        summary += ": " + ", ".join(at_risk_names)
    preset = DRAINAGE_PRESETS[drainage_preset]
    # Structured, not just the text 'summary' above - lets the sidebar
    # render a proper dashboard (stat cards) instead of parsing a sentence.
    metrics = {
        "event_start_date": event_start_date, "event_end_date": event_end_date,
        "facility_label": facility_label(selected_types),
        "crossings_flooded": scenario["n_flooded"], "crossings_total": len(scenario["crossings"]),
        "facilities_at_risk": scenario["n_at_risk"], "facilities_total": len(scenario["facilities"]),
        "pop_cutoff": scenario["pop_cutoff_total"], "pop_delayed": scenario["pop_delayed_total"],
        "peak_daily_rain_mm": scenario["peak_daily_rain_mm"], "peak_antecedent_mm": scenario["peak_antecedent_mm"],
        "weighted_baseline_min": scenario["w_base"], "weighted_flooded_min": scenario["w_flood"],
        "pct_change": scenario["pct_change"],
        "drainage_preset_label": preset["label"], "drainage_coverage_pct": preset["coverage_pct"],
        # fac_code lets the dashboard's "locate" buttons call
        # focusFacility(fac_code) inside the map iframe (see
        # window.FACILITY_MARKERS in render_map) to jump straight to it.
        "at_risk_facilities": scenario["facilities"].loc[
            scenario["facilities"]["at_risk"], ["fac_code", "name"]].to_dict("records"),
    }
    return {"ok": True, "url": url_for("results", fname=fname), "summary": summary, "metrics": metrics}


@app.route("/harare-flood/results/<fname>")
def results(fname):
    return send_from_directory(RESULTS_DIR, fname)


@app.route("/harare-flood/roads.json.gz")
def roads_gz():
    # Served as a plain gzip blob (not Content-Encoding: gzip) - the
    # browser decompresses it itself via DecompressionStream, matching
    # roadnet's own loadGZ() pattern. Same file every request (roads don't
    # change per scenario run), so this is a cheap static send, not a
    # per-request computation.
    path = CTX["roads_gz_path"]
    return send_from_directory(os.path.dirname(path), os.path.basename(path),
                                mimetype="application/gzip")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8505))
    app.run(host="127.0.0.1", port=port, threaded=True)
