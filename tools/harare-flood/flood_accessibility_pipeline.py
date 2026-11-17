"""
Plug-and-play flood-disruption accessibility pipeline.

Phase A + Phase B, both now part of this pipeline:

  Phase A (data acquisition, cached to disk - re-run is a no-op unless
  skip_cache=True): roads/waterways/crossings extraction, crossing dedup +
  flood-risk-index ranking, MERIT Hydro elevation/drainage-area fetch (Earth
  Engine), CHIRPS event rainfall fetch (Earth Engine), self-computed HAND
  floodplain, health-facility extraction.

  Phase B (accessibility computation, one graph build feeds all outputs):
  1. v2 mean-travel-time accessibility (baseline vs. flooded)
  2. facility catchment shift (nearest-facility reassignment)
  3. E2SFCA (capacity-weighted accessibility)
  4. population per travel-time band
  5. crisis-response priority ranking (catchment population x vulnerability
     x accessibility x capacity exceedance x disruption x flood exposure)
  6. underserved-area identification (CHW-placement input)

  Opt-in, not part of run_all() (needs a scenario-specific location choice):
  run_intervention_scenario(interventions, scenario_name) - re-runs
  accessibility on the flooded graph with a temp health point / new fixed or
  mobile clinic added. See run_intervention_examples.py for worked examples.

Vulnerability (used by priority ranking) comes from RWI (Relative Wealth
Index, 2.4km, GEE), validated locally against the user's own Zimbabwe DHS
2015 household wealth data (r=0.837, n=400 clusters - see
validate_rwi_against_dhs.py) before being trusted as the per-hex layer. Run
extract_dhs_wealth_clusters.py, fetch_relative_wealth_index.py, then
validate_rwi_against_dhs.py once (writes Population/vulnerability_source_
decision.json) before run_all() will have vulnerability data to join.

Adding a new site = adding one entry to SITE_CONFIGS (bbox, event dates,
output paths) and running the pipeline - Phase A executes automatically the
first time, then every later run reuses the cached files. Earth Engine auth
(ee.Initialize) is still an external dependency - the one piece of Phase A
that can't be removed, only automated around.

Usage:
    python flood_accessibility_pipeline.py <site_id> [--skip-cache]
"""
import os
import sys
import json
from datetime import datetime, timedelta
import ee
import requests
from sklearn.cluster import DBSCAN
import pyrosm
import h3
import geopandas as gpd
import numpy as np
import pandas as pd
import networkx as nx
import rasterio
from rasterio.mask import mask
from shapely.geometry import Polygon
from scipy.spatial import cKDTree

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PBF_PATH = os.path.join(BASE_DIR, "PBFs", "zimbabwe-latest.osm.pbf")
POP_RASTER_PATH = os.path.join(BASE_DIR, "Population", "zwe_pop_2026_CN_100m_R2025A_v1.tif")
NATIONAL_CAPACITY_CSV = os.path.join(BASE_DIR, "national_facility_capacity_estimates.csv")
# Vulnerability: RWI (dense, 2.4km) validated locally against DHS 2015 ground
# truth (r=0.837, n=400 clusters, see validate_rwi_against_dhs.py) - the
# decision file records which layer actually won and why, rather than
# hardcoding the choice here.
RWI_PATH = os.path.join(BASE_DIR, "Population", "zimbabwe_rwi.csv")
DHS_WEALTH_PATH = os.path.join(BASE_DIR, "Population", "zimbabwe_dhs2015_wealth_clusters.gpkg")
VULN_DECISION_PATH = os.path.join(BASE_DIR, "Population", "vulnerability_source_decision.json")
ADMIN_BOUNDARIES_PATH = os.path.join(BASE_DIR, "Population", "zimbabwe_districts.gpkg")

H3_RESOLUTION = 9
BETA = 0.05  # per minute, E2SFCA negative-exponential decay
NON_DRIVEABLE_TYPES = {"footway", "steps", "pedestrian", "bridleway", "cycleway"}
SPEED_LIMITS_BY_TYPE = {
    'motorway': 120, 'motorway_link': 80, 'trunk': 100, 'trunk_link': 60,
    'primary': 80, 'primary_link': 50, 'secondary': 60, 'secondary_link': 40,
    'tertiary': 50, 'tertiary_link': 30, 'residential': 30, 'unclassified': 30,
    'service': 20, 'track': 20, 'path': 8, 'living_street': 20, 'road': 30,
}
SURFACE_SPEED_FACTORS = {
    'paved': 1.0, 'asphalt': 1.0, 'concrete': 1.0, 'unpaved': 0.5, 'gravel': 0.5,
    'fine_gravel': 0.6, 'compacted': 0.6, 'dirt': 0.4, 'earth': 0.4, 'mud': 0.2,
    'sand': 0.3, 'grass': 0.3, 'ground': 0.4,
}

# ---------------------------------------------------------------------------
# Site configs - the ONLY thing a new site needs, once Phase A has been run.
# ---------------------------------------------------------------------------
SITE_CONFIGS = {
    "chimanimani": {
        "bbox": [32.75, -19.95, 32.98, -19.65],
        "prefix": "chimanimani",
        "extract_dir": os.path.join(BASE_DIR, "Chimanimani_extract"),
        "facilities_path": os.path.join(BASE_DIR, "Chimanimani_extract", "chimanimani_health_facilities.gpkg"),
        "roads_path": os.path.join(BASE_DIR, "Chimanimani_extract", "chimanimani_roads.gpkg"),
        "waterways_path": os.path.join(BASE_DIR, "Chimanimani_extract", "chimanimani_waterways.gpkg"),
        "raw_crossings_path": os.path.join(BASE_DIR, "Chimanimani_extract", "chimanimani_crossings.gpkg"),
        "crossings_path": os.path.join(BASE_DIR, "Chimanimani_extract", "chimanimani_crossings_distinct.gpkg"),
        "crossings_flag_csv": os.path.join(BASE_DIR, "Chimanimani_extract", "chimanimani_crossings_with_selfcomputed_floodplain.csv"),
        "floodplain_tif": os.path.join(BASE_DIR, "DEM", "chimanimani_selfcomputed_floodplain.tif"),
        "merit_tif": os.path.join(BASE_DIR, "DEM", "chimanimani_merit_hydro.tif"),
        "chirps_tif": os.path.join(BASE_DIR, "Rainfall", "chimanimani_chirps_idai_total.tif"),
        "event_start_date": "2019-03-13",
        "event_end_date": "2019-03-19",
        "utm_crs": "EPSG:32736",
        "event_label": "Idai scenario (13-19 Mar 2019)",
        "rainfall_col": "idai_rainfall_mm",  # legacy name in the cached crossings file
        "flood_mechanism": "riverine",  # rainfall-driven - the event-rainfall gate applies
    },
    "chimanimani_district": {
        # District-scale test (2026-08-27): same event/mechanism as the
        # "chimanimani" pilot, but the bbox is Chimanimani District's real
        # FAO GAUL boundary (ee.FeatureCollection("FAO/GAUL/2015/level2"),
        # ADM2_NAME="Chimanimani") rather than the small pilot cut-out -
        # 3,334 km^2, 4.15x the pilot bbox area. Built entirely fresh through
        # this pipeline's own Phase A (not the pilot's separate legacy
        # scripts), so rainfall_col follows the pipeline's own naming
        # convention (event_rainfall_mm), same as "beitbridge".
        "bbox": [32.348, -20.120, 33.056, -19.374],
        "prefix": "chimanimani_district",
        "extract_dir": os.path.join(BASE_DIR, "ChimanimaniDistrict_extract"),
        "facilities_path": os.path.join(BASE_DIR, "ChimanimaniDistrict_extract", "chimanimani_district_health_facilities.gpkg"),
        "roads_path": os.path.join(BASE_DIR, "ChimanimaniDistrict_extract", "chimanimani_district_roads.gpkg"),
        "waterways_path": os.path.join(BASE_DIR, "ChimanimaniDistrict_extract", "chimanimani_district_waterways.gpkg"),
        "raw_crossings_path": os.path.join(BASE_DIR, "ChimanimaniDistrict_extract", "chimanimani_district_crossings.gpkg"),
        "crossings_path": os.path.join(BASE_DIR, "ChimanimaniDistrict_extract", "chimanimani_district_crossings_distinct.gpkg"),
        "crossings_flag_csv": os.path.join(BASE_DIR, "ChimanimaniDistrict_extract", "chimanimani_district_crossings_with_selfcomputed_floodplain.csv"),
        "floodplain_tif": os.path.join(BASE_DIR, "DEM", "chimanimani_district_selfcomputed_floodplain.tif"),
        "merit_tif": os.path.join(BASE_DIR, "DEM", "chimanimani_district_merit_hydro.tif"),
        "chirps_tif": os.path.join(BASE_DIR, "Rainfall", "chimanimani_district_chirps_total.tif"),
        "event_start_date": "2019-03-13",
        "event_end_date": "2019-03-19",
        "utm_crs": "EPSG:32736",
        "event_label": "Idai scenario (13-19 Mar 2019), district scale",
        "rainfall_col": "event_rainfall_mm",
        "flood_mechanism": "riverine",
    },
    "tokwe": {
        "bbox": [30.25, -21.45, 30.65, -20.85],
        "prefix": "tokwe",
        "extract_dir": os.path.join(BASE_DIR, "TokweMukosi_extract"),
        "facilities_path": os.path.join(BASE_DIR, "TokweMukosi_extract", "tokwe_facilities_coded.gpkg"),
        "roads_path": os.path.join(BASE_DIR, "TokweMukosi_extract", "tokwe_roads.gpkg"),
        "waterways_path": os.path.join(BASE_DIR, "TokweMukosi_extract", "tokwe_waterways.gpkg"),
        "raw_crossings_path": os.path.join(BASE_DIR, "TokweMukosi_extract", "tokwe_crossings.gpkg"),
        "crossings_path": os.path.join(BASE_DIR, "TokweMukosi_extract", "tokwe_crossings_with_selfcomputed_floodplain.gpkg"),
        "crossings_flag_csv": None,  # flag already in the gpkg for this site
        "floodplain_tif": os.path.join(BASE_DIR, "DEM", "tokwe_selfcomputed_floodplain.tif"),
        "merit_tif": os.path.join(BASE_DIR, "DEM", "tokwe_merit_hydro.tif"),
        "chirps_tif": os.path.join(BASE_DIR, "Rainfall", "tokwe_chirps_total.tif"),
        "event_start_date": "2026-03-01",
        "event_end_date": "2026-03-31",
        "utm_crs": "EPSG:32736",
        "event_label": "2025/26 dam-spillage scenario (March 2026)",
        "rainfall_col": "mar2026_rainfall_mm",  # legacy name in the cached crossings file
        "flood_mechanism": "dam_spillage",  # NOT rainfall-driven at the point of impact - a
        # crossing downstream of the dam can flood from released reservoir water while bone
        # dry locally. The event-rainfall gate does not apply to this mechanism (see build_graphs).
    },
    "beitbridge": {
        # From-scratch pipeline test site (2026-08-26): the CASCADE team's own
        # reference dashboard location (GIS analysis.docx, image1.png), on the
        # Limpopo River - urban border town, riverine flood mechanism, a
        # genuinely different character than Chimanimani (rural highland) or
        # Tokwe-Mukosi (dam-adjacent lowveld). Event window is illustrative
        # (peak DJF wet season), NOT tied to a specific documented flood - this
        # is a pipeline-mechanics test, not a deck-ready case study.
        "bbox": [29.85, -22.35, 30.15, -22.05],
        "prefix": "beitbridge",
        "extract_dir": os.path.join(BASE_DIR, "Beitbridge_extract"),
        "facilities_path": os.path.join(BASE_DIR, "Beitbridge_extract", "beitbridge_health_facilities.gpkg"),
        "roads_path": os.path.join(BASE_DIR, "Beitbridge_extract", "beitbridge_roads.gpkg"),
        "waterways_path": os.path.join(BASE_DIR, "Beitbridge_extract", "beitbridge_waterways.gpkg"),
        "raw_crossings_path": os.path.join(BASE_DIR, "Beitbridge_extract", "beitbridge_crossings.gpkg"),
        "crossings_path": os.path.join(BASE_DIR, "Beitbridge_extract", "beitbridge_crossings_distinct.gpkg"),
        "crossings_flag_csv": os.path.join(BASE_DIR, "Beitbridge_extract", "beitbridge_crossings_with_selfcomputed_floodplain.csv"),
        "floodplain_tif": os.path.join(BASE_DIR, "DEM", "beitbridge_selfcomputed_floodplain.tif"),
        "merit_tif": os.path.join(BASE_DIR, "DEM", "beitbridge_merit_hydro.tif"),
        "chirps_tif": os.path.join(BASE_DIR, "Rainfall", "beitbridge_chirps_total.tif"),
        "event_start_date": "2025-02-01",
        "event_end_date": "2025-02-28",
        "utm_crs": "EPSG:32736",
        "event_label": "illustrative peak wet-season scenario (Feb 2025) - pipeline test, not a documented event",
        "rainfall_col": "event_rainfall_mm",  # this pipeline's own naming (site built by rank_and_dedupe_crossings)
        "flood_mechanism": "riverine",
    },
    "harare": {
        # Quick access-to-care + flood-hazard deliverable (2026-10-19), driven
        # by the user's own curated facility list (Harare_healthfacilities_
        # classified_v2.csv - amenity type + Public/Private classification +
        # CEmONC), not the national MoH list extract_facilities() would pull.
        # Bbox widened 2026-10-19 (user scope change) to cover the CSV's full
        # extent - ALL 152 facilities, including the Marondera/Bindura/Norton
        # outliers - with a ~2km margin, rather than the tighter Greater-
        # Harare-metro box originally used (which excluded 39 of them).
        # ~13,400 km^2, ~5x the metro box and bigger than any existing site
        # here (chimanimani_district was previously the largest at 3,334 km^2).
        # Event window is illustrative (peak DJF wet season), NOT tied to a
        # specific documented flood - same convention as beitbridge.
        "bbox": [30.66, -18.22, 31.70, -17.17],
        "prefix": "harare",
        # Literature-cited drainage threshold (2026-10-20), per Hu & Demir
        # (2021, Hydrology 8:65), citing Nobre et al. (2016)'s validated
        # 4.05 km^2 - see compute_floodplain()'s is_stream line for the
        # fallback this overrides. Harare-only; other sites keep BL_TRESH_KM2.
        "drainage_threshold_km2": 4.05,
        "extract_dir": os.path.join(BASE_DIR, "Harare_extract"),
        "facilities_path": os.path.join(BASE_DIR, "Harare_extract", "harare_health_facilities.gpkg"),
        "roads_path": os.path.join(BASE_DIR, "Harare_extract", "harare_roads.gpkg"),
        "waterways_path": os.path.join(BASE_DIR, "Harare_extract", "harare_waterways.gpkg"),
        "raw_crossings_path": os.path.join(BASE_DIR, "Harare_extract", "harare_crossings.gpkg"),
        "crossings_path": os.path.join(BASE_DIR, "Harare_extract", "harare_crossings_distinct.gpkg"),
        "crossings_flag_csv": os.path.join(BASE_DIR, "Harare_extract", "harare_crossings_with_selfcomputed_floodplain.csv"),
        "floodplain_tif": os.path.join(BASE_DIR, "DEM", "harare_selfcomputed_floodplain.tif"),
        "merit_tif": os.path.join(BASE_DIR, "DEM", "harare_merit_hydro.tif"),
        "chirps_tif": os.path.join(BASE_DIR, "Rainfall", "harare_chirps_total.tif"),
        "event_start_date": "2025-12-01",
        "event_end_date": "2026-02-28",
        "utm_crs": "EPSG:32736",
        "event_label": "illustrative peak wet-season scenario (Dec 2025-Feb 2026) - not a documented event",
        "rainfall_col": "event_rainfall_mm",
        "flood_mechanism": "riverine",
    },
}

# Channel-initiation / HAND parameters, shared across sites (Nardi et al. GFPLAIN v1.0)
BL_TRESH_KM2 = 0.05
A_PARAM = 0.01
B_PARAM = 0.30

# Rainfall-intensity flood-gate thresholds (riverine mechanism only - see
# build_graphs). Standard Southern-Africa flash-flood/road-disruption
# benchmarks, not locally calibrated (Open Gap: still not real crossing-
# capacity data - see METHODS_LOG.md, 2026-08-27):
#   - a day this heavy triggers on its own
REGIONAL_FLOOD_THRESHOLD_MM = 50
#   - a lighter day still triggers IF the ground was already this wet
ANTECEDENT_TRIGGER_MM = 25
ANTECEDENT_SATURATION_MM = 40
D8_OFFSETS = {
    1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
    16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1),
}


def get_driving_speed(highway_type, surface):
    base = SPEED_LIMITS_BY_TYPE.get(highway_type, 15)
    if surface and str(surface) != "nan":
        factor = SURFACE_SPEED_FACTORS.get(str(surface).lower(), 0.7)
    else:
        factor = 0.5 if highway_type in ['track', 'path', 'unclassified', 'residential'] else (
            1.0 if highway_type in ['motorway', 'trunk', 'primary'] else 0.7)
    return max(base * factor, 3)


def highway_of(d):
    h = d.get("highway", "road")
    return h[0] if isinstance(h, list) else h


def surface_of(d):
    s = d.get("surface")
    return s[0] if isinstance(s, list) else s


class FloodAccessibilityPipeline:
    """One graph build, all downstream accessibility outputs. Config-driven -
    swap SITE_CONFIGS[site_id] for a new site, nothing else changes."""

    def __init__(self, site_id, skip_cache=False):
        if site_id not in SITE_CONFIGS:
            raise ValueError(f"Unknown site '{site_id}'. Known sites: {list(SITE_CONFIGS)}. "
                              f"Add a SITE_CONFIGS entry (bbox, event dates, output paths) - "
                              f"Phase A runs automatically on first use.")
        self.site_id = site_id
        self.cfg = SITE_CONFIGS[site_id]
        self.skip_cache = skip_cache
        self.log_lines = []
        os.makedirs(self.cfg["extract_dir"], exist_ok=True)
        os.makedirs(os.path.dirname(self.cfg["merit_tif"]), exist_ok=True)
        os.makedirs(os.path.dirname(self.cfg["chirps_tif"]), exist_ok=True)

    def log(self, msg):
        print(msg)
        self.log_lines.append(msg)

    def _cached(self, path):
        """Cache check: skip a stage if its output already exists, unless skip_cache."""
        exists = os.path.exists(path)
        if exists and not self.skip_cache:
            self.log(f"[cache] {os.path.basename(path)} exists, skipping")
        return exists and not self.skip_cache

    # =========================================================================
    # PHASE A: data acquisition (roads, DEM, floodplain, facilities)
    # =========================================================================

    def extract_roads_waterways(self):
        rp, wp = self.cfg["roads_path"], self.cfg["waterways_path"]
        if self._cached(rp) and self._cached(wp):
            return self
        self.log(f"[*] {self.site_id}: extracting roads + waterways from PBF ...")
        osm = pyrosm.OSM(PBF_PATH, bounding_box=self.cfg["bbox"])
        roads = osm.get_network(network_type="all", extra_attributes=["bridge", "ford", "layer", "surface"])
        roads.to_file(rp, driver="GPKG")
        self.log(f"[OK] {len(roads):,} road segments -> {rp}")

        waterways = osm.get_data_by_custom_criteria(
            custom_filter={"waterway": ["river", "stream"]}, filter_type="keep",
            keep_nodes=False, keep_ways=True, keep_relations=False)
        waterways.to_file(wp, driver="GPKG")
        self.log(f"[OK] {len(waterways):,} waterway segments -> {wp}")
        return self

    def extract_raw_crossings(self):
        out = self.cfg["raw_crossings_path"]
        if self._cached(out):
            return self
        self.log(f"[*] {self.site_id}: finding road x waterway crossings ...")
        roads = gpd.read_file(self.cfg["roads_path"])
        waterways = gpd.read_file(self.cfg["waterways_path"])
        utm = self.cfg["utm_crs"]
        roads_m = roads.to_crs(utm)
        waterways_m = waterways.to_crs(utm)
        cols = [c for c in ["highway", "bridge", "ford", "geometry"] if c in roads_m.columns]
        crossings = gpd.sjoin(roads_m[cols], waterways_m[["waterway", "geometry"]],
                               how="inner", predicate="intersects")
        crossings = crossings.drop(columns=["index_right"], errors="ignore").to_crs("EPSG:4326")
        crossings.to_file(out, driver="GPKG")
        self.log(f"[OK] {len(crossings)} raw road/waterway intersection pairs -> {out}")
        return self

    def fetch_merit_hydro(self):
        out = self.cfg["merit_tif"]
        if self._cached(out):
            return self
        self.log(f"[*] {self.site_id}: fetching MERIT Hydro (elv, dir, upa) from Earth Engine ...")
        ee.Initialize(project="your-earth-engine-project")
        region = ee.Geometry.Rectangle(self.cfg["bbox"])
        merit = ee.Image("MERIT/Hydro/v1_0_1").select(["elv", "dir", "upa"])
        url = merit.getDownloadURL({"region": region, "scale": 90, "crs": "EPSG:4326", "format": "GEO_TIFF"})
        resp = requests.get(url, timeout=300)
        resp.raise_for_status()
        with open(out, "wb") as f:
            f.write(resp.content)
        self.log(f"[OK] MERIT Hydro -> {out} ({len(resp.content):,} bytes)")
        return self

    def fetch_chirps(self):
        out = self.cfg["chirps_tif"]
        if self._cached(out):
            return self
        self.log(f"[*] {self.site_id}: fetching CHIRPS event rainfall "
                  f"({self.cfg['event_start_date']} to {self.cfg['event_end_date']}) ...")
        ee.Initialize(project="your-earth-engine-project")
        region = ee.Geometry.Rectangle(self.cfg["bbox"])
        chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterDate(
            self.cfg["event_start_date"], self.cfg["event_end_date"])
        event_total = chirps.sum().clip(region)
        url = event_total.getDownloadURL({"region": region, "scale": 5566, "crs": "EPSG:4326", "format": "GEO_TIFF"})
        resp = requests.get(url, timeout=300)
        resp.raise_for_status()
        with open(out, "wb") as f:
            f.write(resp.content)
        self.log(f"[OK] CHIRPS event rainfall -> {out} ({len(resp.content):,} bytes)")
        return self

    def fetch_chirps_thresholds(self):
        """CHIRPS-derived signals for a real rainfall-intensity flood gate,
        replacing the old "any rain at all this event" gate (event-total
        rainfall > 0mm) - a genuinely premature trigger: it only ever asked
        whether it rained, not whether it rained hard enough, or onto
        already-saturated ground, to actually disrupt a crossing (caught
        2026-08-27 - see METHODS_LOG.md for the full discussion, including
        why a literature-suggested Global IDF dataset (GSDR-IDF) turned out
        to have zero African stations and couldn't be used here).

        Two bands, both derived from CHIRPS (the same source already used
        and trusted elsewhere in this pipeline, so no new dataset/units to
        justify):
          - daily_max: the single heaviest CHIRPS day inside the event
            window - the standard 24h-accumulation flash-flood benchmark
            used across Southern Africa (~50mm/24h triggers localized
            disruption).
          - antecedent_3day: CHIRPS total for the 3 days immediately BEFORE
            the event window (deliberately not overlapping it) - an
            antecedent-precipitation-index proxy for "was the ground
            already wet," since the same daily total behaves very
            differently on saturated vs. dry ground. (ERA5-Land's own
            volumetric_soil_water_layer_1 band was checked and confirmed
            available in the same collection already used for the runoff
            coefficient, as a more direct physical alternative - not used
            here because it would need its own site-specific
            saturation-fraction cutoff we have no basis to defend yet,
            whereas an antecedent rainfall total in mm keeps the same,
            already-trusted units and source as the rest of this gate.)

        Fetched as one 2-band raster at CHIRPS's native ~5.5km scale,
        sampled per crossing in rank_and_dedupe_crossings - same pattern as
        every other per-crossing signal in this pipeline.
        """
        out = os.path.join(self.cfg["extract_dir"], f"{self.cfg['prefix']}_chirps_thresholds.tif")
        if self._cached(out):
            self._chirps_thresholds_tif = out
            return self
        self.log(f"[*] {self.site_id}: fetching CHIRPS daily-max + antecedent rainfall ...")
        ee.Initialize(project="your-earth-engine-project")
        region = ee.Geometry.Rectangle(self.cfg["bbox"])
        event_start = datetime.strptime(self.cfg["event_start_date"], "%Y-%m-%d")
        antecedent_start = (event_start - timedelta(days=3)).strftime("%Y-%m-%d")
        chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
        daily_max = chirps.filterDate(self.cfg["event_start_date"], self.cfg["event_end_date"]).max()
        antecedent_total = chirps.filterDate(antecedent_start, self.cfg["event_start_date"]).sum()
        img = ee.Image.cat([daily_max.rename("daily_max"), antecedent_total.rename("antecedent")]).clip(region)
        url = img.getDownloadURL({"region": region, "scale": 5566, "crs": "EPSG:4326", "format": "GEO_TIFF"})
        resp = requests.get(url, timeout=300)
        resp.raise_for_status()
        with open(out, "wb") as f:
            f.write(resp.content)
        self.log(f"[OK] CHIRPS daily-max/antecedent -> {out} ({len(resp.content):,} bytes)")
        self._chirps_thresholds_tif = out
        return self

    def fetch_runoff_coefficient(self):
        """Runoff coefficient C for flood_risk_index = C x rainfall x drainage
        area, from ERA5-Land Daily Aggregated (surface_runoff_sum /
        total_precipitation_sum) for the event's own start/end dates.

        Fetched as a 2-band raster (runoff, precip) at ERA5-Land's native
        11.1km resolution and sampled PER CROSSING, the same pattern already
        used for MERIT Hydro (90m) and CHIRPS (~5.5km) - not a single
        bbox-wide mean. An earlier version used `reduceRegion(mean)` to
        collapse the whole site to one C, on the reasoning that land cover
        doesn't vary meter-to-meter; that argument answered the wrong
        question - nobody needed meter-scale precision, just the actual
        11.1km pixel each crossing already falls in, which a multi-pixel
        bbox (roughly 6-30 ERA5-Land pixels across our sites) already
        provides. The mean-based version discarded real, available spatial
        variation for no resolution-based reason - a shortcut, not a
        necessity (caught 2026-08-27, see METHODS_LOG.md). Because C now
        varies per crossing instead of being a uniform multiplier, it is no
        longer mathematically inert for the top-5% ranking cross-check - it
        can genuinely shift which crossings rank highest, unlike before.
        """
        out = os.path.join(self.cfg["extract_dir"], f"{self.cfg['prefix']}_era5_runoff.tif")
        if self._cached(out):
            self._era5_runoff_tif = out
            return self
        self.log(f"[*] {self.site_id}: fetching ERA5-Land runoff/precipitation "
                  f"({self.cfg['event_start_date']} to {self.cfg['event_end_date']}) ...")
        ee.Initialize(project="your-earth-engine-project")
        region = ee.Geometry.Rectangle(self.cfg["bbox"])
        era5 = ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR").filterDate(
            self.cfg["event_start_date"], self.cfg["event_end_date"])
        runoff_total = era5.select("surface_runoff_sum").sum()
        precip_total = era5.select("total_precipitation_sum").sum()
        img = ee.Image.cat([runoff_total.rename("runoff"), precip_total.rename("precip")]).clip(region)
        url = img.getDownloadURL({"region": region, "scale": 11132, "crs": "EPSG:4326", "format": "GEO_TIFF"})
        resp = requests.get(url, timeout=300)
        resp.raise_for_status()
        with open(out, "wb") as f:
            f.write(resp.content)
        self.log(f"[OK] ERA5-Land runoff/precip -> {out} ({len(resp.content):,} bytes)")
        self._era5_runoff_tif = out
        return self

    def _sample_c_at_points(self, points):
        """Per-point runoff coefficient C = surface_runoff_sum /
        total_precipitation_sum, read from the cached ERA5-Land raster at
        each point's actual pixel (see fetch_runoff_coefficient). Falls back
        to C=1.0 (no attenuation - the same as the old implicit-C=1 default)
        wherever a point has zero/nodata precipitation this window or falls
        outside the raster, and clips to [0,1] since C is physically a
        fraction of rainfall."""
        tif = getattr(self, "_era5_runoff_tif", None) or os.path.join(
            self.cfg["extract_dir"], f"{self.cfg['prefix']}_era5_runoff.tif")
        with rasterio.open(tif) as src:
            runoff_band = src.read(1)
            precip_band = src.read(2)
            c_vals = []
            for pt in points:
                row, col = src.index(pt.x, pt.y)
                if 0 <= row < runoff_band.shape[0] and 0 <= col < runoff_band.shape[1]:
                    r, p = runoff_band[row, col], precip_band[row, col]
                    c = (r / p) if (p and p > 0) else 1.0
                else:
                    c = 1.0
                c_vals.append(float(np.clip(c, 0.0, 1.0)))
        return np.array(c_vals)

    def _sample_chirps_thresholds_at_points(self, points):
        """Per-point (daily_max, antecedent_3day) rainfall in mm, read from
        the cached CHIRPS thresholds raster (see fetch_chirps_thresholds).
        Falls back to (0, 0) - i.e. neither threshold can trigger - wherever
        a point falls outside the raster."""
        tif = getattr(self, "_chirps_thresholds_tif", None) or os.path.join(
            self.cfg["extract_dir"], f"{self.cfg['prefix']}_chirps_thresholds.tif")
        with rasterio.open(tif) as src:
            daily_max_band = src.read(1)
            antecedent_band = src.read(2)
            daily_max_vals, antecedent_vals = [], []
            for pt in points:
                row, col = src.index(pt.x, pt.y)
                if 0 <= row < daily_max_band.shape[0] and 0 <= col < daily_max_band.shape[1]:
                    dm, ant = daily_max_band[row, col], antecedent_band[row, col]
                else:
                    dm, ant = 0.0, 0.0
                daily_max_vals.append(float(dm) if dm == dm else 0.0)  # NaN-safe (nodata)
                antecedent_vals.append(float(ant) if ant == ant else 0.0)
        return np.array(daily_max_vals), np.array(antecedent_vals)

    def rank_and_dedupe_crossings(self):
        """Join MERIT upa + CHIRPS rainfall to raw crossings, compute
        flood_risk_index (validation cross-check, not the flood definition),
        then DBSCAN-dedupe OSM way-split duplicates into distinct locations."""
        out = self.cfg["crossings_path"]
        if self._cached(out):
            return self
        self.log(f"[*] {self.site_id}: ranking + deduping crossings ...")
        crossings = gpd.read_file(self.cfg["raw_crossings_path"])

        with rasterio.open(self.cfg["merit_tif"]) as src:
            upa_band = src.read(3)
            upa_vals = [upa_band[src.index(pt.x, pt.y)] if
                        (0 <= src.index(pt.x, pt.y)[0] < upa_band.shape[0] and
                         0 <= src.index(pt.x, pt.y)[1] < upa_band.shape[1]) else np.nan
                        for pt in crossings.geometry.centroid]
        with rasterio.open(self.cfg["chirps_tif"]) as src:
            rain_band = src.read(1)
            rain_vals = [rain_band[src.index(pt.x, pt.y)] if
                         (0 <= src.index(pt.x, pt.y)[0] < rain_band.shape[0] and
                          0 <= src.index(pt.x, pt.y)[1] < rain_band.shape[1]) else np.nan
                         for pt in crossings.geometry.centroid]
        crossings["upa_km2"] = upa_vals
        crossings["event_rainfall_mm"] = rain_vals
        c_vals = self._sample_c_at_points(list(crossings.geometry.centroid))
        crossings["runoff_coefficient_c"] = c_vals
        crossings["flood_risk_index"] = c_vals * crossings["upa_km2"] * crossings["event_rainfall_mm"]
        daily_max_vals, antecedent_vals = self._sample_chirps_thresholds_at_points(list(crossings.geometry.centroid))
        crossings["event_rainfall_daily_max_mm"] = daily_max_vals
        crossings["event_antecedent_3day_mm"] = antecedent_vals

        pts = crossings.copy()
        pts["geometry"] = pts.geometry.centroid
        pts_m = pts.to_crs(self.cfg["utm_crs"])
        coords = np.array([[g.x, g.y] for g in pts_m.geometry])
        clustering = DBSCAN(eps=30, min_samples=1).fit(coords)
        pts["cluster_id"] = clustering.labels_

        records = []
        for cid, group in pts.groupby("cluster_id"):
            records.append({
                "cluster_id": cid, "n_original_pairs": len(group),
                "highway": group["highway"].mode().iloc[0] if "highway" in group.columns else None,
                "waterway": group["waterway"].mode().iloc[0] if "waterway" in group.columns else None,
                "bridge": group["bridge"].mode().iloc[0] if "bridge" in group.columns and group["bridge"].notna().any() else None,
                "ford": group["ford"].mode().iloc[0] if "ford" in group.columns and group["ford"].notna().any() else None,
                "upa_km2": group["upa_km2"].max(),
                "event_rainfall_mm": group["event_rainfall_mm"].mean(),
                "runoff_coefficient_c": group["runoff_coefficient_c"].mean(),
                "flood_risk_index": group["flood_risk_index"].max(),
                "event_rainfall_daily_max_mm": group["event_rainfall_daily_max_mm"].max(),
                "event_antecedent_3day_mm": group["event_antecedent_3day_mm"].mean(),
                "geometry": group.geometry.iloc[0],
            })
        distinct = gpd.GeoDataFrame(records, crs="EPSG:4326")
        distinct.to_file(out, driver="GPKG")
        self.log(f"[OK] {len(crossings)} raw pairs -> {len(distinct)} distinct crossings -> {out}")
        return self

    def apply_runoff_coefficient(self):
        """Add a C-calibrated risk column to an ALREADY-cached crossings file
        (chimanimani/tokwe/beitbridge all had crossings_path before C existed).
        Writes flood_risk_index_c as a NEW column into crossings_path (the gpkg
        base file) - deliberately does not overwrite the existing
        flood_risk_index/tributary_risk_index column, since the deck and
        earlier log entries document that column's exact definition (implicit
        C=1) and changing its meaning under the same name would silently
        invalidate those descriptions. Must write to crossings_path
        specifically, not crossings_flag_csv directly: compute_floodplain's
        crossing-sampling step rebuilds crossings_flag_csv FROM crossings_path
        every run (even on a floodplain_tif cache hit), so a column added only
        to the flag CSV would be silently wiped the next time that runs. This
        is why apply_runoff_coefficient must also run BEFORE compute_floodplain
        in run_phase_a - see the ordering there.

        C is sampled PER CROSSING (see fetch_runoff_coefficient /
        _sample_c_at_points), not a single site-wide value - the marker file
        stores min/max/mean C actually applied, not one number, since there
        no longer is just one."""
        out_marker = os.path.join(self.cfg["extract_dir"], f"{self.cfg['prefix']}_flood_risk_index_c_applied.marker")
        if self._cached(out_marker):
            return self
        if not hasattr(self, "_era5_runoff_tif"):
            self.log("[!] No ERA5 runoff raster available (fetch_runoff_coefficient didn't run) - skipping")
            return self
        rainfall_col = self.cfg["rainfall_col"]

        gdf = gpd.read_file(self.cfg["crossings_path"])
        # rainfall_col is a legacy-file compatibility shim (each site's OLD
        # manually-built crossings file used a different rainfall column
        # name - idai_rainfall_mm, mar2026_rainfall_mm, ...). rank_and_dedupe_
        # crossings, this pipeline's OWN crossing builder, has always written
        # event_rainfall_mm regardless of that shim - just never actually
        # regenerated these 3 sites' crossings_path before 2026-08-27 (always
        # a cache hit on their pre-existing legacy files), so this mismatch
        # was latent. Prefer the fresh pipeline-native name when present.
        if rainfall_col not in gdf.columns and "event_rainfall_mm" in gdf.columns:
            rainfall_col = "event_rainfall_mm"
        c_vals = self._sample_c_at_points(list(gdf.geometry.centroid))
        gdf["runoff_coefficient_c"] = c_vals
        gdf["flood_risk_index_c"] = c_vals * gdf["upa_km2"] * gdf[rainfall_col]
        gdf.to_file(self.cfg["crossings_path"], driver="GPKG")
        with open(out_marker, "w") as f:
            f.write(f"C per-crossing: min={c_vals.min():.3f} mean={c_vals.mean():.3f} max={c_vals.max():.3f}\n")
        self.log(f"[OK] flood_risk_index_c (per-crossing C, mean={c_vals.mean():.3f}, "
                  f"range {c_vals.min():.3f}-{c_vals.max():.3f}) added for {len(gdf)} crossings -> "
                  f"{self.cfg['crossings_path']}")
        return self

    def compute_floodplain(self):
        """Self-computed floodplain: HAND (D8-traced, memoized) <= Leopold
        power-law water level. See implement_gfplain_hand.py for the derivation
        of A_PARAM/B_PARAM/BL_TRESH_KM2 - identical method, any bbox."""
        out = self.cfg["floodplain_tif"]
        flag_csv = self.cfg["crossings_flag_csv"]
        # Only the expensive HAND raster computation is cached - the crossing-
        # sampling step below is cheap, so it always re-runs to guarantee the
        # flag file/column exists even on a cache hit for the raster itself.
        if self._cached(out):
            self._sample_floodplain_at_crossings(out)
            return self
        self.log(f"[*] {self.site_id}: computing self-computed floodplain (HAND) ...")
        with rasterio.open(self.cfg["merit_tif"]) as src:
            elv = src.read(1).astype(np.float64)
            flowdir = src.read(2).astype(np.int32)
            upa = src.read(3).astype(np.float64)
            transform, crs = src.transform, src.crs
            nrows, ncols = elv.shape

        # Per-site override, Harare only: Hu & Demir (2021, Hydrology 8:65)
        # found 4 km^2 the effective drainage threshold for their HAND flood
        # mapping, citing Nobre et al. (2016)'s 4.05 km^2 as the literature
        # value - vs. this pipeline's own default BL_TRESH_KM2=0.05 km^2
        # (tuned for the other sites' stream-crossing detection, not flood
        # extent specifically). Harare's SITE_CONFIGS sets
        # drainage_threshold_km2 to follow that literature value; every
        # other site keeps the pipeline default via this same fallback.
        is_stream = upa >= self.cfg.get("drainage_threshold_km2", BL_TRESH_KM2)
        area_m2 = upa * 1e6
        water_level = A_PARAM * np.power(np.maximum(area_m2, 1.0), B_PARAM)

        target_row = np.full((nrows, ncols), -1, dtype=np.int32)
        target_col = np.full((nrows, ncols), -1, dtype=np.int32)
        for r0 in range(nrows):
            for c0 in range(ncols):
                if is_stream[r0, c0]:
                    target_row[r0, c0] = r0; target_col[r0, c0] = c0
                    continue
                if target_row[r0, c0] != -1:
                    continue
                path = []
                r, c = r0, c0
                steps = 0
                while steps < 2000:
                    if target_row[r, c] != -1:
                        tr, tc = target_row[r, c], target_col[r, c]; break
                    if is_stream[r, c]:
                        tr, tc = r, c; break
                    path.append((r, c))
                    d = flowdir[r, c]
                    if d not in D8_OFFSETS or d <= 0:
                        tr, tc = -1, -1; break
                    dr, dc = D8_OFFSETS[d]
                    nr, nc = r + dr, c + dc
                    if not (0 <= nr < nrows and 0 <= nc < ncols):
                        tr, tc = -1, -1; break
                    r, c = nr, nc; steps += 1
                else:
                    tr, tc = -1, -1
                for pr, pc in path:
                    target_row[pr, pc] = tr; target_col[pr, pc] = tc
            if r0 % 100 == 0:
                self.log(f"    row {r0}/{nrows} ...")

        resolved = target_row != -1
        hand = np.full((nrows, ncols), np.nan)
        threshold = np.full((nrows, ncols), np.nan)
        rr, cc = np.where(resolved)
        tr, tc = target_row[rr, cc], target_col[rr, cc]
        hand[rr, cc] = elv[rr, cc] - elv[tr, tc]
        threshold[rr, cc] = water_level[tr, tc]
        floodplain = np.where(resolved & (hand <= threshold), 1, np.where(resolved, 0, np.nan))
        self.log(f"[OK] Floodplain: {np.nansum(floodplain == 1):,.0f} cells "
                  f"({np.nansum(floodplain == 1) / resolved.sum() * 100:.1f}% of resolved area)")

        with rasterio.open(out, "w", driver="GTiff", height=nrows, width=ncols, count=1,
                            dtype=np.float32, crs=crs, transform=transform, nodata=np.nan) as dst:
            dst.write(floodplain.astype(np.float32), 1)
        self.log(f"[OK] Saved -> {out}")

        self._sample_floodplain_at_crossings(out)
        return self

    def _sample_floodplain_at_crossings(self, floodplain_tif_path):
        crossings = gpd.read_file(self.cfg["crossings_path"])
        with rasterio.open(floodplain_tif_path) as src:
            data = src.read(1)
            vals = []
            for pt in crossings.geometry.centroid:
                r, c = rasterio.transform.rowcol(src.transform, pt.x, pt.y)
                v = data[r, c] if (0 <= r < data.shape[0] and 0 <= c < data.shape[1]) else np.nan
                vals.append(v)
        crossings["selfcomputed_floodplain"] = vals
        if self.cfg["crossings_flag_csv"]:
            # Keep crossings_path as the geometry-only base; flag lives in a
            # separate CSV joined by cluster_id (matches the existing sites' layout).
            pd.DataFrame(crossings.drop(columns="geometry")).to_csv(self.cfg["crossings_flag_csv"], index=False)
        else:
            # No separate CSV configured for this site - write the flag straight
            # back into the crossings gpkg instead.
            crossings.to_file(self.cfg["crossings_path"], driver="GPKG")
        self.log(f"[OK] {int((crossings['selfcomputed_floodplain']==1).sum())}/{len(crossings)} crossings flagged flooded")

    def extract_facilities(self):
        """Sourced from the national MoH facility list (national_facility_
        capacity_estimates.csv, 1,674 facilities, bbox-filtered) rather than
        OSM tags - OSM `amenity=hospital/clinic` coverage is real but sparse
        (Beitbridge's from-scratch test found exactly 1 OSM-tagged facility
        for an entire border town, closed under flooding, and crashed
        run_mean_travel_time on an empty facility set - Open Gap identified
        2026-08-26, fixed 2026-08-27). The national list already carries
        workforce_est/workforce_imputed directly (see load_facilities), so
        this also removes the separate nearest-match capacity join for any
        newly-extracted site - facilities and their capacity are now the
        same source, not two joined ones."""
        out = self.cfg["facilities_path"]
        if self._cached(out):
            return self
        self.log(f"[*] {self.site_id}: extracting health facilities from the national MoH list (bbox-filtered) ...")
        west, south, east, north = self.cfg["bbox"]
        national = pd.read_csv(NATIONAL_CAPACITY_CSV)
        in_bbox = national[
            (national["LONGITUDE"] >= west) & (national["LONGITUDE"] <= east) &
            (national["LATITUDE"] >= south) & (national["LATITUDE"] <= north)
        ].copy()
        if len(in_bbox) == 0:
            raise RuntimeError(f"No MoH-listed health facilities found in {self.site_id}'s bbox - "
                                f"supply a facilities_path manually for this site.")
        facilities = gpd.GeoDataFrame(
            in_bbox, geometry=gpd.points_from_xy(in_bbox["LONGITUDE"], in_bbox["LATITUDE"]), crs="EPSG:4326")
        keep = [c for c in ["ID", "NAMEOFFACI", "DISTRICT", "TYPEOFFACI", "tier", "beds_est", "beds_imputed",
                             "workforce_est", "workforce_imputed", "CATHMENTPO", "geometry"] if c in facilities.columns]
        facilities = facilities[keep].rename(columns={"NAMEOFFACI": "name"})
        facilities.to_file(out, driver="GPKG")
        self.log(f"[OK] {len(facilities)} health facilities (national MoH list) -> {out}")
        return self

    def run_phase_a(self):
        (self.extract_roads_waterways()
             .extract_raw_crossings()
             .fetch_merit_hydro()
             .fetch_chirps()
             .fetch_chirps_thresholds()
             .fetch_runoff_coefficient()
             .rank_and_dedupe_crossings()
             .apply_runoff_coefficient()
             .compute_floodplain()
             .extract_facilities())
        return self

    # =========================================================================
    # PHASE B: accessibility computation
    # =========================================================================

    # -- Stage 1: routing graphs (baseline + flooded), driving mode ---------
    def build_graphs(self):
        self.log(f"[*] {self.site_id}: building driving graph ...")
        osm = pyrosm.OSM(PBF_PATH, bounding_box=self.cfg["bbox"])
        nodes, edges = osm.get_network(nodes=True, network_type="all", extra_attributes=["surface"])
        g_all = osm.to_graph(nodes, edges, graph_type="networkx")

        g_drive = g_all.copy()
        remove = []
        for u, v, k, data in g_drive.edges(keys=True, data=True):
            hw = highway_of(data)
            if hw in NON_DRIVEABLE_TYPES:
                remove.append((u, v, k)); continue
            length_km = max(data.get("length", 10), 10) / 1000.0
            data["length_km"] = length_km
            data["travel_time_min"] = (length_km / get_driving_speed(hw, surface_of(data))) * 60
        g_drive.remove_edges_from(remove)
        self.log(f"[OK] {g_drive.number_of_nodes():,} nodes, {g_drive.number_of_edges():,} edges")

        crossings = gpd.read_file(self.cfg["crossings_path"])
        if self.cfg["crossings_flag_csv"]:
            flag_df = pd.read_csv(self.cfg["crossings_flag_csv"])
            crossings["selfcomputed_floodplain"] = flag_df["selfcomputed_floodplain"].values

        # A crossing counts as flooded in THIS scenario only if it's BOTH
        # geomorphically inside the floodplain (static - could flood, given
        # its position relative to a stream of this size) AND its catchment
        # actually received rainfall INTENSE ENOUGH to disrupt it this event
        # window (dynamic - did it, and hard enough).
        #
        # Was: a single "did it rain at all this window (event total > 0mm)"
        # gate - fixed "would a drought month still show flooding" but was
        # itself premature: "yes it rained, therefore this floodplain
        # crossing flooded" doesn't distinguish a light shower from a
        # genuine flash-flood-triggering downpour. Replaced 2026-08-27 (see
        # METHODS_LOG.md) with a two-part rainfall-intensity rule, both legs
        # from CHIRPS (already the trusted source here, so no new dataset or
        # units to justify - a literature-suggested global IDF/return-period
        # dataset (GSDR-IDF) was checked and ruled out: zero African
        # stations, nearest is ~5,770km away):
        #   - REGIONAL_FLOOD_THRESHOLD_MM: the single heaviest day in the
        #     event window exceeds the standard Southern-Africa 24h flash-
        #     flood/road-disruption benchmark on its own, OR
        #   - a lighter day (> ANTECEDENT_TRIGGER_MM) still triggers if the
        #     3 days immediately before the event already delivered
        #     > ANTECEDENT_SATURATION_MM - an antecedent-precipitation-index
        #     proxy for "the ground was already wet," since the same rain
        #     behaves very differently on saturated vs. dry ground.
        # Still not a calibrated depth/discharge crossing-capacity threshold
        # (Open Gap #1) - these are standard regional intensity benchmarks,
        # not locally calibrated to any specific crossing's engineering.
        #
        # This gate ONLY applies to rainfall-driven (riverine) mechanisms.
        # Dam-spillage flooding is NOT locally rainfall-driven - a crossing
        # downstream of a spilling dam can flood from released reservoir water
        # while it's bone dry at that exact spot. Applying a local-rainfall
        # gate there would wrongly zero out the entire scenario in a dry
        # month, which is backwards for that mechanism. For dam_spillage
        # sites, floodplain membership alone remains the trigger - the
        # baseline-vs-flooded split already represents "did the dam release
        # happen," which isn't something local CHIRPS/ERA5 rain can answer.
        rainfall_col = self.cfg["rainfall_col"]
        in_floodplain = crossings["selfcomputed_floodplain"] == 1
        if self.cfg.get("flood_mechanism") != "riverine":
            had_rain_this_event = pd.Series(True, index=crossings.index)
        elif "event_rainfall_daily_max_mm" in crossings.columns and "event_antecedent_3day_mm" in crossings.columns:
            daily_max = crossings["event_rainfall_daily_max_mm"].fillna(0)
            antecedent = crossings["event_antecedent_3day_mm"].fillna(0)
            had_rain_this_event = (daily_max > REGIONAL_FLOOD_THRESHOLD_MM) | (
                (daily_max > ANTECEDENT_TRIGGER_MM) & (antecedent > ANTECEDENT_SATURATION_MM))
        elif rainfall_col in crossings.columns:
            self.log(f"[!] Rainfall-intensity columns not found (older cached crossings file) - "
                     f"falling back to the legacy '{rainfall_col} > 0' gate for this run")
            had_rain_this_event = crossings[rainfall_col].fillna(0) > 0
        else:
            self.log(f"[!] '{rainfall_col}' not found in crossings - skipping the event-rainfall gate for this run")
            had_rain_this_event = pd.Series(True, index=crossings.index)
        flooded_crossings = crossings[in_floodplain & had_rain_this_event].copy()
        n_geomorphic_only = int((in_floodplain & ~had_rain_this_event).sum())
        self.log(f"[i] {len(flooded_crossings)} flooded crossings this event "
                 f"({n_geomorphic_only} more are in the floodplain but received no rain this window - not flooded now)")

        edge_list = list(g_drive.edges(keys=True, data=True))
        mids, refs = [], []
        for u, v, k, data in edge_list:
            geom = data.get("geometry")
            if geom is not None:
                m = geom.interpolate(0.5, normalized=True)
                mids.append([m.x, m.y])
            else:
                ux, uy = g_drive.nodes[u]["x"], g_drive.nodes[u]["y"]
                vx, vy = g_drive.nodes[v]["x"], g_drive.nodes[v]["y"]
                mids.append([(ux + vx) / 2, (uy + vy) / 2])
            refs.append((u, v, k))
        tree = cKDTree(np.array(mids))
        g_flooded = g_drive.copy()
        flooded_set = set()
        for geom in flooded_crossings.geometry.centroid:
            _, idx = tree.query([geom.x, geom.y])
            flooded_set.add(refs[idx])
        for u, v, k in flooded_set:
            if g_flooded.has_edge(u, v, k): g_flooded.remove_edge(u, v, k)
            if g_flooded.has_edge(v, u, k): g_flooded.remove_edge(v, u, k)
        self.log(f"[OK] {len(flooded_set)} edges removed for flooded scenario")

        self.G_baseline = g_drive
        self.G_flooded = g_flooded
        self.flooded_crossing_points = flooded_crossings
        return self

    # -- Stage 2: facilities, closure, capacity ------------------------------
    def load_facilities(self):
        facilities = gpd.read_file(self.cfg["facilities_path"]).reset_index(drop=True)
        if "fac_code" not in facilities.columns:
            facilities["fac_code"] = [f"F{i+1}" for i in range(len(facilities))]

        with rasterio.open(self.cfg["floodplain_tif"]) as fp_src:
            in_floodplain = []
            for geom in facilities.geometry:
                row, col = fp_src.index(geom.x, geom.y)
                try:
                    v = fp_src.read(1, window=((row, row + 1), (col, col + 1)))[0, 0]
                except Exception:
                    v = 0
                in_floodplain.append(bool(v == 1))
        in_floodplain = np.array(in_floodplain)

        # Same two-part rainfall-intensity gate as crossings (build_graphs) -
        # a facility inside the floodplain during a drought month shouldn't
        # be "closed" any more than a crossing should be "flooded." Caught
        # 2026-08-27 while verifying the crossings fix at Beitbridge: the new
        # gate correctly zeroed out every flooded crossing there (0 edges
        # removed), yet the site's travel-time numbers didn't move at all -
        # because facility closure was still using the OLD, ungated
        # floodplain-only check, so 2 facilities stayed "closed" purely on
        # geomorphic membership regardless of whether this event actually
        # rained hard enough to flood them. Same riverine-only mechanism
        # branch as the crossing gate, same reasoning.
        if self.cfg.get("flood_mechanism") != "riverine":
            had_rain_this_event = np.ones(len(facilities), dtype=bool)
        else:
            daily_max, antecedent = self._sample_chirps_thresholds_at_points(list(facilities.geometry))
            had_rain_this_event = (daily_max > REGIONAL_FLOOD_THRESHOLD_MM) | (
                (daily_max > ANTECEDENT_TRIGGER_MM) & (antecedent > ANTECEDENT_SATURATION_MM))
        facilities["at_risk"] = list(in_floodplain & had_rain_this_event)
        closed = facilities.loc[facilities["at_risk"], "fac_code"].tolist()
        self.log(f"[i] Facility closure: {closed if closed else 'none'} at-risk in the flooded scenario")

        # extract_facilities() now sources directly from the national list,
        # so workforce_est/workforce_imputed already ride along - only a
        # manually-supplied facilities_path (an older cached site, or a
        # hand-edited override) needs this nearest-match join as a fallback.
        if "workforce_est" not in facilities.columns:
            national = pd.read_csv(NATIONAL_CAPACITY_CSV)
            national_tree = cKDTree(np.column_stack([national["LONGITUDE"], national["LATITUDE"]]))
            cap_rows = []
            for _, fac in facilities.iterrows():
                dist, idx = national_tree.query([fac.geometry.x, fac.geometry.y])
                match = national.iloc[idx]
                cap_rows.append({"fac_code": fac["fac_code"], "workforce_est": match["workforce_est"],
                                  "workforce_imputed": match["workforce_imputed"]})
            facilities = facilities.merge(pd.DataFrame(cap_rows), on="fac_code")
        facilities["S_j"] = facilities["workforce_est"].fillna(1).clip(lower=1)

        self.facilities = facilities
        return self

    # -- Stage 3: population grid --------------------------------------------
    def build_population_grid(self):
        west, south, east, north = self.cfg["bbox"]
        bbox_poly = Polygon([(west, south), (east, south), (east, north), (west, north)])
        hexagons = h3.geo_to_cells(bbox_poly, res=H3_RESOLUTION)
        hex_geoms, hex_ids = [], []
        for hid in hexagons:
            boundary = h3.cell_to_boundary(hid)
            hex_geoms.append(Polygon([(lon, lat) for lat, lon in boundary]))
            hex_ids.append(hid)
        hex_gdf = gpd.GeoDataFrame({"h3_id": hex_ids, "geometry": hex_geoms}, crs="EPSG:4326")
        hex_gdf = hex_gdf[hex_gdf.intersects(bbox_poly)].reset_index(drop=True)

        pop_raster = rasterio.open(POP_RASTER_PATH)
        populations = []
        for geom in hex_gdf.geometry:
            try:
                masked, _ = mask(pop_raster, [geom], crop=True)
                d = masked[0]
                if pop_raster.nodata is not None:
                    d = np.where(d == pop_raster.nodata, np.nan, d)
                s = np.nansum(d)
                populations.append(s if s > 0.1 else 0)
            except Exception:
                populations.append(0)
        hex_gdf["population"] = populations
        populated_hex = hex_gdf[hex_gdf["population"] > 0].copy()
        populated_hex["centroid"] = populated_hex.geometry.to_crs("EPSG:32736").centroid.to_crs("EPSG:4326")
        self.log(f"[OK] {len(populated_hex):,} populated hexagons, {populated_hex['population'].sum():,.0f} total population")
        self.populated_hex = populated_hex
        self.total_pop = populated_hex["population"].sum()
        return self

    # -- Stage 3b: vulnerability (RWI, validated locally against DHS) -------
    def join_vulnerability_index(self):
        """Nearest-match join of the winning vulnerability layer (decided
        once, nationally, by validate_rwi_against_dhs.py - not re-decided
        per site) onto self.populated_hex. Keeps both raw source columns
        where available (dhs_mean_wealth, rwi), not just the chosen one -
        stated honestly, same convention as workforce_est/workforce_imputed
        in load_facilities()."""
        with open(VULN_DECISION_PATH) as f:
            decision = json.load(f)
        source = decision["source"]
        self.log(f"[i] Vulnerability source: '{source}' (RWI-vs-DHS Pearson r={decision['pearson_r']:.3f}, "
                  f"see {os.path.basename(VULN_DECISION_PATH)})")

        centroids = np.column_stack([self.populated_hex["centroid"].x, self.populated_hex["centroid"].y])

        rwi = pd.read_csv(RWI_PATH)
        rwi_tree = cKDTree(rwi[["lon", "lat"]].values)
        rwi_dist, rwi_idx = rwi_tree.query(centroids)
        self.populated_hex["rwi"] = rwi.iloc[rwi_idx]["rwi"].values

        dhs = gpd.read_file(DHS_WEALTH_PATH)
        dhs_tree = cKDTree(np.column_stack([dhs.geometry.x, dhs.geometry.y]))
        dhs_dist, dhs_idx = dhs_tree.query(centroids)
        self.populated_hex["dhs_mean_wealth"] = dhs.iloc[dhs_idx]["mean_wealth"].values
        self.populated_hex["dhs_cluster_dist_km"] = dhs_dist * 111.0

        if source == "rwi":
            wealth = self.populated_hex["rwi"]
            self.populated_hex["vuln_source_dist_km"] = rwi_dist * 111.0
        else:
            wealth = self.populated_hex["dhs_mean_wealth"]
            self.populated_hex["vuln_source_dist_km"] = self.populated_hex["dhs_cluster_dist_km"]

        wmin, wmax = wealth.min(), wealth.max()
        self.populated_hex["vulnerability"] = 1 - (wealth - wmin) / (wmax - wmin) if wmax > wmin else 0.5
        self.log(f"[OK] Vulnerability joined, median match distance {self.populated_hex['vuln_source_dist_km'].median():.2f} km")
        return self

    def _nearest_node_lookup(self, G):
        ids = list(G.nodes())
        coords = np.array([[G.nodes[n]["x"], G.nodes[n]["y"]] for n in ids])
        tree_ = cKDTree(coords)
        def f(lon, lat):
            _, idx = tree_.query([lon, lat])
            return ids[idx]
        return f

    # -- Stage 4a: mean travel-time accessibility (v2 metric) ---------------
    def run_mean_travel_time(self):
        nn = self._nearest_node_lookup(self.G_baseline)
        fac_nodes = [nn(g.x, g.y) for g in self.facilities.geometry]
        open_fac_nodes = [nn(g.x, g.y) for g in self.facilities.loc[~self.facilities["at_risk"]].geometry]
        hex_nodes = [nn(pt.x, pt.y) for pt in self.populated_hex["centroid"]]

        base_t = nx.multi_source_dijkstra_path_length(self.G_baseline, sources=fac_nodes, weight="travel_time_min")
        # If every facility is closed (rare, but real - a small site can have
        # its only facility inside the floodplain), nothing is reachable at
        # all in the flooded scenario: everyone who had baseline access is cut
        # off. multi_source_dijkstra_path_length itself rejects an empty
        # source list, so handle that case explicitly rather than crash.
        if open_fac_nodes:
            flood_t = nx.multi_source_dijkstra_path_length(self.G_flooded, sources=open_fac_nodes, weight="travel_time_min")
        else:
            self.log("[!] All facilities closed in the flooded scenario - nobody has flooded-scenario access")
            flood_t = {}

        df = self.populated_hex[["h3_id", "population"]].copy()
        df["baseline_min"] = [base_t.get(n, np.nan) for n in hex_nodes]
        df["flooded_min"] = [flood_t.get(n, np.nan) for n in hex_nodes]
        df["cut_off"] = pd.notna(df["baseline_min"]) & pd.isna(df["flooded_min"])

        reach_base = df.dropna(subset=["baseline_min"])
        w_base = (reach_base["baseline_min"] * reach_base["population"]).sum() / reach_base["population"].sum()
        reach_both = df.dropna(subset=["baseline_min", "flooded_min"])
        w_flood = (reach_both["flooded_min"] * reach_both["population"]).sum() / reach_both["population"].sum() if len(reach_both) else np.nan
        pop_cutoff = df.loc[df["cut_off"], "population"].sum()

        result = {"baseline_min": w_base, "flooded_min": w_flood,
                  "pct_change": (w_flood - w_base) / w_base * 100, "pop_cutoff": pop_cutoff}
        self.log(f"[travel-time] {w_base:.1f} -> {w_flood:.1f} min ({result['pct_change']:+.1f}%), {pop_cutoff:,.0f} cut off")
        self.travel_time_result = result
        self._travel_time_df = df  # kept for run_distance_bands - same hex order as populated_hex
        return result

    # -- Stage 4a-ii: administrative (district) tabulation -------------------
    # Same population-weighted-mean formula as the regional summary above,
    # just grouped by district instead of over the whole populated_hex table -
    # closes the inherited plan's explicit "spreadsheet output by ward/SAL"
    # request at district granularity (ward/EA boundaries aren't in FAO GAUL,
    # still an open gap at finer resolution). A real polygon spatial join, not
    # a one-site-one-district assumption - confirmed during planning that the
    # Tokwe-Mukosi pilot bbox alone already spans two districts.
    def run_admin_tabulation(self):
        districts = gpd.read_file(ADMIN_BOUNDARIES_PATH)
        hex_pts = gpd.GeoDataFrame(
            self._travel_time_df.copy(),
            geometry=self.populated_hex["centroid"].values, crs=self.populated_hex.crs)
        joined = gpd.sjoin(hex_pts, districts[["ADM2_NAME", "geometry"]], predicate="within", how="left")

        rows = []
        for name, g in joined.groupby("ADM2_NAME"):
            pop = g["population"].sum()
            reach_base = g.dropna(subset=["baseline_min"])
            base_t = (reach_base["baseline_min"] * reach_base["population"]).sum() / reach_base["population"].sum() if len(reach_base) else np.nan
            reach_both = g.dropna(subset=["baseline_min", "flooded_min"])
            flood_t = (reach_both["flooded_min"] * reach_both["population"]).sum() / reach_both["population"].sum() if len(reach_both) else np.nan
            pop_cutoff = g.loc[g["cut_off"], "population"].sum()
            rows.append({
                "district": name, "population": pop, "baseline_min": base_t, "flooded_min": flood_t,
                "pct_change": (flood_t - base_t) / base_t * 100 if base_t else np.nan, "pop_cutoff": pop_cutoff,
            })
        rdf = pd.DataFrame(rows).sort_values("population", ascending=False).reset_index(drop=True)

        n_unmatched = int(joined["ADM2_NAME"].isna().sum())
        if n_unmatched:
            self.log(f"[!] {n_unmatched} hexes fell outside all district polygons (bbox edge effect) - excluded from district totals")

        out_path = os.path.join(self.cfg["extract_dir"], f"{self.cfg['prefix']}_admin_tabulation.csv")
        rdf.to_csv(out_path, index=False)
        self.log(f"[admin-tabulation] {len(rdf)} district(s):\n" + rdf.to_string(index=False))
        self.log(f"[OK] -> {out_path}")

        result = {"districts": rdf.to_dict("records")}
        self.admin_tabulation_result = result
        return result

    # -- Stage 4b: facility catchment shift ----------------------------------
    def run_catchment_shift(self):
        def assign(G, label):
            nn = self._nearest_node_lookup(G)
            candidates = self.facilities[~self.facilities["at_risk"]] if label == "flooded" else self.facilities
            per_fac_time = {}
            for _, fac in candidates.iterrows():
                per_fac_time[fac["fac_code"]] = nx.single_source_dijkstra_path_length(
                    G, nn(fac.geometry.x, fac.geometry.y), weight="travel_time_min")
            hex_nodes = [nn(pt.x, pt.y) for pt in self.populated_hex["centroid"]]
            assigned = []
            for n in hex_nodes:
                best_fac, best_t = None, np.inf
                for code, times in per_fac_time.items():
                    t = times.get(n)
                    if t is not None and t < best_t:
                        best_fac, best_t = code, t
                assigned.append(best_fac)
            return assigned

        base_assign = assign(self.G_baseline, "baseline")
        flood_assign = assign(self.G_flooded, "flooded")
        pop = self.populated_hex["population"].values
        switched = sum(p for a, b, p in zip(base_assign, flood_assign, pop) if b is not None and a != b)
        unreached = sum(p for a, b, p in zip(base_assign, flood_assign, pop) if a is not None and b is None)
        self.log(f"[catchment-shift] {switched:,.0f} people switched facility, {unreached:,.0f} lost access entirely")
        result = {"pop_switched": switched, "pop_unreached": unreached}
        self.catchment_result = result
        self._assigned_baseline = base_assign  # kept for run_distance_bands - same hex order as populated_hex
        self._assigned_flooded = flood_assign  # kept for run_catchment_shift_map - same hex order as populated_hex
        return result

    # -- Stage 4c: E2SFCA -----------------------------------------------------
    # Two decay functions computed side by side, not one replacing the other:
    # the continuous version (regression-tested, reported by default) and a
    # multi-zone version using Luo & Qi's own published weights (2009, Health
    # & Place 15(4)) - (1.00, 0.42, 0.09) for 0-10/10-20/20-30min, hard cutoff
    # beyond 30min. Note: the paper's own erratum states the Gaussian
    # derivation implies 0.03 for the third zone, but the paper's applied
    # results used 0.09 - 0.09 is what's implemented here, since that's what
    # the published, cited method actually computed, not the arithmetically
    # "corrected" value.
    ZONE_BREAKS_MIN = [10, 20, 30]
    ZONE_WEIGHTS = [1.00, 0.42, 0.09]

    @staticmethod
    def continuous_decay(t):
        return np.exp(-BETA * t)

    @classmethod
    def zone_decay(cls, t):
        w = np.zeros_like(t, dtype=float)
        prev = 0
        for cutoff, weight in zip(cls.ZONE_BREAKS_MIN, cls.ZONE_WEIGHTS):
            in_zone = (t > prev) & (t <= cutoff)
            w[in_zone] = weight
            prev = cutoff
        w[t <= 0] = cls.ZONE_WEIGHTS[0]
        return w

    def _e2sfca_compute(self, G, label, decay_fn):
        nn = self._nearest_node_lookup(G)
        candidates = self.facilities[~self.facilities["at_risk"]] if label == "flooded" else self.facilities
        hex_nodes = [nn(pt.x, pt.y) for pt in self.populated_hex["centroid"]]
        pops = self.populated_hex["population"].values
        R, per_fac_times = {}, {}
        for _, fac in candidates.iterrows():
            times = nx.single_source_dijkstra_path_length(G, nn(fac.geometry.x, fac.geometry.y), weight="travel_time_min")
            per_fac_times[fac["fac_code"]] = times
            t_arr = np.array([times.get(n, np.nan) for n in hex_nodes])
            w = np.where(np.isnan(t_arr), 0.0, decay_fn(t_arr))
            demand = np.sum(pops * w)
            R[fac["fac_code"]] = fac["S_j"] / demand if demand > 0 else 0.0
        A = np.zeros(len(hex_nodes))
        for code, times in per_fac_times.items():
            t_arr = np.array([times.get(n, np.nan) for n in hex_nodes])
            w = np.where(np.isnan(t_arr), 0.0, decay_fn(t_arr))
            A += R[code] * w
        return A, R

    def run_e2sfca(self):
        A_base, R_base = self._e2sfca_compute(self.G_baseline, "baseline", self.continuous_decay)
        A_flood, R_flood = self._e2sfca_compute(self.G_flooded, "flooded", self.continuous_decay)
        pop = self.populated_hex["population"].values
        score_base = (A_base * pop).sum() / self.total_pop
        score_flood = (A_flood * pop).sum() / self.total_pop
        pct = (score_flood - score_base) / score_base * 100 if score_base else np.nan
        self.log(f"[e2sfca] {score_base:.4f} -> {score_flood:.4f} ({pct:+.1f}%)")

        Az_base, Rz_base = self._e2sfca_compute(self.G_baseline, "baseline", self.zone_decay)
        Az_flood, Rz_flood = self._e2sfca_compute(self.G_flooded, "flooded", self.zone_decay)
        # NOTE: the population-weighted REGIONAL score is a known E2SFCA
        # invariant - Sum(P_i*A_i) telescopes to Sum(S_j) (total system
        # supply) for ANY decay function, as long as every facility has
        # nonzero demand (R_j = S_j/demand_j, so R_j*demand_j = S_j exactly).
        # score_mz_base/flood therefore equal score_base/flood almost
        # exactly by construction, not by coincidence - confirmed when this
        # was first built (bit-identical output caught what looked like a
        # bug but was this identity). The decay function only redistributes
        # WHERE accessibility lands, not the region-wide total - so the real
        # continuous-vs-multizone comparison has to be at the per-hex or
        # per-facility level, not the regional scalar.
        score_mz_base = (Az_base * pop).sum() / self.total_pop
        score_mz_flood = (Az_flood * pop).sum() / self.total_pop
        pct_mz = (score_mz_flood - score_mz_base) / score_mz_base * 100 if score_mz_base else np.nan
        mean_abs_diff_base = (np.abs(Az_base - A_base) * pop).sum() / self.total_pop
        mean_abs_diff_flood = (np.abs(Az_flood - A_flood) * pop).sum() / self.total_pop
        self.log(f"[e2sfca-multizone, Luo & Qi 2009 weights] regional score {score_mz_base:.4f} -> {score_mz_flood:.4f} "
                  f"({pct_mz:+.1f}%) - matches the continuous score by construction (E2SFCA invariant, see code comment); "
                  f"pop-weighted mean |A_multizone - A_continuous| per hex: {mean_abs_diff_base:.5f} baseline, "
                  f"{mean_abs_diff_flood:.5f} flooded - this is where the two methods actually differ")

        result = {"e2sfca_baseline": score_base, "e2sfca_flooded": score_flood, "pct_change": pct,
                  "e2sfca_multizone_baseline": score_mz_base, "e2sfca_multizone_flooded": score_mz_flood,
                  "pct_change_multizone": pct_mz,
                  "e2sfca_multizone_mean_abs_diff_baseline": mean_abs_diff_base,
                  "e2sfca_multizone_mean_abs_diff_flooded": mean_abs_diff_flood}
        self.e2sfca_result = result
        # Kept for run_priority_ranking - per-facility supply/demand ratio
        # (Rj, capacity-exceedance term) and per-hex accessibility (Ai), same
        # hex-order convention as _travel_time_df/_assigned_baseline. Purely
        # additive - e2sfca_result and every existing caller are unchanged.
        # Deliberately from the continuous run (regression-tested), not the
        # multi-zone one, so run_priority_ranking's behavior is unaffected.
        self._e2sfca_R_baseline, self._e2sfca_R_flooded = R_base, R_flood
        self._e2sfca_A_baseline, self._e2sfca_A_flooded = A_base, A_flood
        return result

    # -- Stage 4d: population per travel-time band ---------------------------
    # Directly from the original inherited plan (GIS analysis.docx): "Number
    # and percent of people living within a certain travel time of the
    # nearest facility... cutoffs at 5, 10, 15, 30, 60, 90, 120min... tables
    # of population per distance band, per facility type." Never built until
    # now - this closes that specific, explicitly-named gap. "Per facility
    # type" isn't meaningful at these pilot sites (2-6 facilities, mostly the
    # same OSM tag) - broken down per named facility (fac_code) instead,
    # which is more informative at this scale; still reports the overall
    # (all-facility) table the plan actually asked for.
    BAND_CUTOFFS_MIN = [5, 10, 15, 30, 60, 90, 120]

    def run_distance_bands(self):
        df = self._travel_time_df.copy()
        df["assigned_baseline"] = self._assigned_baseline
        bands = self.BAND_CUTOFFS_MIN + [np.inf]
        band_labels = [f"<= {b} min" for b in self.BAND_CUTOFFS_MIN] + ["> 120 min"]

        def band_table(time_col):
            rows = []
            reached = df.dropna(subset=[time_col])
            unreached_pop = df.loc[df[time_col].isna(), "population"].sum()
            prev = 0
            for cutoff, label in zip(bands, band_labels):
                in_band = reached[(reached[time_col] > prev) & (reached[time_col] <= cutoff)]
                pop_in_band = in_band["population"].sum()
                rows.append({"band": label, "population": pop_in_band,
                             "pct_of_total": 100 * pop_in_band / self.total_pop})
                prev = cutoff
            rows.append({"band": "unreachable", "population": unreached_pop,
                         "pct_of_total": 100 * unreached_pop / self.total_pop})
            return pd.DataFrame(rows)

        baseline_bands = band_table("baseline_min")
        flooded_bands = band_table("flooded_min")
        self.log("[distance-bands] baseline:\n" + baseline_bands.to_string(index=False))
        self.log("[distance-bands] flooded:\n" + flooded_bands.to_string(index=False))

        per_facility_rows = []
        for code in self.facilities["fac_code"]:
            sub = df[df["assigned_baseline"] == code]
            prev = 0
            for cutoff, label in zip(bands, band_labels):
                in_band = sub[(sub["baseline_min"] > prev) & (sub["baseline_min"] <= cutoff)]
                per_facility_rows.append({"fac_code": code, "band": label,
                                           "population": in_band["population"].sum()})
                prev = cutoff
        per_facility = pd.DataFrame(per_facility_rows)

        out_dir = self.cfg["extract_dir"]
        baseline_bands.to_csv(os.path.join(out_dir, f"{self.cfg['prefix']}_distance_bands_baseline.csv"), index=False)
        flooded_bands.to_csv(os.path.join(out_dir, f"{self.cfg['prefix']}_distance_bands_flooded.csv"), index=False)
        per_facility.to_csv(os.path.join(out_dir, f"{self.cfg['prefix']}_distance_bands_per_facility.csv"), index=False)
        self.log(f"[OK] Distance-band tables -> {out_dir}")

        result = {"baseline": baseline_bands.to_dict("records"),
                  "flooded": flooded_bands.to_dict("records")}
        self.distance_bands_result = result
        return result

    # -- Stage 5a: crisis-response priority ranking --------------------------
    # From the inherited plan (GIS analysis.docx): "Create a multi-criteria
    # priority ranking for existing facilities, based on: catchment
    # populations, socio-economic variables of catchment populations,
    # accessibility, degree of disruption in disturbed scenarios ... High
    # priority: large catchment population, that is highly vulnerable, with
    # low accessibility, experiences high capacity exceedance ..." Reuses
    # data already computed by earlier stages - no new Dijkstra runs.
    PRIORITY_WEIGHTS = {
        "catchment_population": 1.0, "vulnerability": 1.0, "low_accessibility": 1.0,
        "capacity_exceedance": 1.0, "disruption": 1.0, "flood_exposure": 1.0,
    }

    def run_priority_ranking(self):
        df = self._travel_time_df.copy()
        df["assigned_baseline"] = self._assigned_baseline
        df = df.merge(self.populated_hex[["h3_id", "vulnerability"]], on="h3_id", how="left")

        if len(self.flooded_crossing_points):
            flooded_gdf = gpd.GeoDataFrame(geometry=list(self.flooded_crossing_points.geometry.centroid),
                                            crs=self.populated_hex.crs)
            joined = gpd.sjoin(self.populated_hex[["h3_id", "geometry"]], flooded_gdf,
                                predicate="contains", how="inner")
            hexes_with_flooded_crossing = set(joined["h3_id"])
        else:
            hexes_with_flooded_crossing = set()

        rows = []
        for _, fac in self.facilities.iterrows():
            code = fac["fac_code"]
            sub = df[df["assigned_baseline"] == code]
            pop = sub["population"].sum()
            if pop > 0:
                vuln = (sub["vulnerability"] * sub["population"]).sum() / pop
                access = (sub["baseline_min"] * sub["population"]).sum() / pop
                # Cut-off hexes are treated as a 100% (ceiling) time increase -
                # a defensible cap, not a measured value, for population that
                # can no longer reach ANY facility at all. A hex essentially
                # co-located with its own (now-closed) facility has
                # baseline_min ~ 0, so a %-increase is undefined even when
                # it's NOT cut off (some other facility is still reachable,
                # just not instantly) - dividing by ~0 there produced +inf,
                # which np.nan_to_num's default posinf substitute then
                # overflowed back to +inf on multiplying by population,
                # corrupting the whole facility's priority_score to NaN
                # (found on the chimanimani_district run - never triggered at
                # pilot scale, where no closed facility's own hex happened to
                # stay reachable-but-slower rather than fully cut off).
                # Same defensible ceiling as the cut-off case, for the same
                # reason: not a measured percentage, a capped one.
                near_zero_base = sub["baseline_min"] < 1.0
                pct_increase = np.where(sub["cut_off"] | near_zero_base, 1.0,
                                         (sub["flooded_min"] - sub["baseline_min"]) / sub["baseline_min"])
                disruption = (np.nan_to_num(pct_increase, nan=1.0) * sub["population"]).sum() / pop
            else:
                vuln = access = disruption = 0.0

            R_j = self._e2sfca_R_baseline.get(code, 0.0)
            n_flooded_in_catchment = len(set(sub["h3_id"]) & hexes_with_flooded_crossing)
            flood_exposure_flag = bool(fac["at_risk"]) or (n_flooded_in_catchment > 0)

            rows.append({
                "fac_code": code, "name": fac.get("name") or code,
                "catchment_population": pop, "vulnerability_raw": vuln,
                "accessibility_raw_min": access, "R_j": R_j,
                "disruption_raw_pct": disruption * 100,
                "facility_at_risk": bool(fac["at_risk"]),
                "n_flooded_crossings_in_catchment": n_flooded_in_catchment,
                "flood_exposure_raw": int(flood_exposure_flag),
            })
        rdf = pd.DataFrame(rows)

        def norm(s):
            return (s - s.min()) / (s.max() - s.min()) if s.max() > s.min() else pd.Series(0.5, index=s.index)

        rdf["catchment_population_norm"] = norm(rdf["catchment_population"])
        rdf["vulnerability_norm"] = norm(rdf["vulnerability_raw"])
        rdf["low_accessibility_norm"] = norm(rdf["accessibility_raw_min"])
        rdf["capacity_exceedance_norm"] = 1 - norm(rdf["R_j"])  # low R = high competition = high exceedance
        rdf["disruption_norm"] = norm(rdf["disruption_raw_pct"])
        rdf["flood_exposure_norm"] = rdf["flood_exposure_raw"].astype(float)

        w = self.PRIORITY_WEIGHTS
        rdf["priority_score"] = sum(w[k] * rdf[f"{k}_norm"] for k in w) / sum(w.values())
        rdf = rdf.sort_values("priority_score", ascending=False).reset_index(drop=True)
        rdf["rank"] = rdf.index + 1

        out_dir = self.cfg["extract_dir"]
        csv_path = os.path.join(out_dir, f"{self.cfg['prefix']}_priority_ranking.csv")
        rdf.to_csv(csv_path, index=False)
        self.log("[priority-ranking]\n" + rdf[["rank", "fac_code", "priority_score", "catchment_population",
                                                "vulnerability_norm", "low_accessibility_norm",
                                                "capacity_exceedance_norm", "disruption_norm",
                                                "flood_exposure_norm"]].to_string(index=False))

        self._plot_priority_ranking_map(rdf)
        result = {"top_facility": rdf.iloc[0]["fac_code"], "top_score": float(rdf.iloc[0]["priority_score"]),
                  "ranking": rdf[["fac_code", "rank", "priority_score"]].to_dict("records")}
        self.priority_ranking_result = result
        return result

    def _plot_priority_ranking_map(self, rdf):
        import matplotlib.pyplot as plt
        fac = self.facilities.merge(rdf[["fac_code", "priority_score", "rank"]], on="fac_code")
        fig, ax = plt.subplots(figsize=(8, 8))
        self.populated_hex.boundary.plot(ax=ax, color="#e2e8f0", linewidth=0.3)
        sc = ax.scatter(fac.geometry.x, fac.geometry.y, s=80 + 400 * fac["priority_score"],
                         c=fac["priority_score"], cmap="YlOrRd", edgecolors="black", linewidths=0.8, zorder=5)
        for _, r in fac.iterrows():
            ax.annotate(f"{r['fac_code']} (#{int(r['rank'])})", (r.geometry.x, r.geometry.y),
                        textcoords="offset points", xytext=(6, 6), fontsize=8)
        plt.colorbar(sc, ax=ax, label="Priority score")
        ax.set_title(f"{self.cfg['prefix']}: facility crisis-response priority ranking")
        fig.tight_layout()
        out = os.path.join(self.cfg["extract_dir"], f"{self.cfg['prefix']}_priority_ranking_map.png")
        fig.savefig(out, dpi=150)
        plt.close(fig)
        self.log(f"[OK] -> {out}")

    # -- Stage 5b: underserved-area identification (CHW-placement input) ----
    # From the plan: "Identify underserved areas that would most benefit from
    # community health care workers." Pure filter on data already computed -
    # no new graph work.
    def run_underserved_areas(self):
        df = self._travel_time_df.copy()
        worst_band = self.BAND_CUTOFFS_MIN[-1]
        underserved = df[df["cut_off"] | (df["flooded_min"] > worst_band)].copy()
        underserved = underserved.merge(self.populated_hex[["h3_id", "geometry"]], on="h3_id", how="left")
        underserved_gdf = gpd.GeoDataFrame(underserved, geometry="geometry", crs=self.populated_hex.crs)

        out_dir = self.cfg["extract_dir"]
        csv_path = os.path.join(out_dir, f"{self.cfg['prefix']}_underserved_areas.csv")
        gpkg_path = os.path.join(out_dir, f"{self.cfg['prefix']}_underserved_areas.gpkg")
        underserved.drop(columns="geometry").to_csv(csv_path, index=False)
        underserved_gdf.to_file(gpkg_path, driver="GPKG")

        total_pop = float(underserved["population"].sum())
        self.log(f"[underserved-areas] {len(underserved)} hexes, {total_pop:,.0f} people "
                  f"cut off or > {worst_band}min under flooding -> {gpkg_path}")
        result = {"n_hexes": len(underserved), "population": total_pop}
        self.underserved_areas_result = result
        return result

    # -- Opt-in: crisis-response intervention scenarios (not in run_all) ----
    def _accessibility_metrics(self, G, facilities_df):
        """Shared by run_intervention_scenario only (baseline/flooded stage
        methods above are already regression-verified and left untouched) -
        one Dijkstra run per facility, reused for all three metrics instead
        of three separate copies."""
        nn = self._nearest_node_lookup(G)
        hex_nodes = [nn(pt.x, pt.y) for pt in self.populated_hex["centroid"]]
        pops = self.populated_hex["population"].values

        per_fac_times = {fac["fac_code"]: nx.single_source_dijkstra_path_length(
            G, nn(fac.geometry.x, fac.geometry.y), weight="travel_time_min")
            for _, fac in facilities_df.iterrows()}

        if per_fac_times:
            t_matrix = np.array([[times.get(n, np.nan) for n in hex_nodes] for times in per_fac_times.values()])
            min_t = np.nanmin(np.where(np.isnan(t_matrix), np.inf, t_matrix), axis=0)
            reach = np.isfinite(min_t)
            min_t = np.where(reach, min_t, np.nan)
            fac_codes = list(per_fac_times.keys())
            idx_min = np.argmin(np.where(np.isnan(t_matrix), np.inf, t_matrix), axis=0)
            assigned = [fac_codes[i] if reach[j] else None for j, i in enumerate(idx_min)]
        else:
            min_t = np.full(len(hex_nodes), np.nan)
            reach = np.zeros(len(hex_nodes), dtype=bool)
            assigned = [None] * len(hex_nodes)

        mean_t = (min_t[reach] * pops[reach]).sum() / pops[reach].sum() if reach.any() else np.nan

        def decay(t): return np.exp(-BETA * t)
        R = {}
        for _, fac in facilities_df.iterrows():
            t_arr = np.array([per_fac_times[fac["fac_code"]].get(n, np.nan) for n in hex_nodes])
            w = np.where(np.isnan(t_arr), 0.0, decay(t_arr))
            demand = np.sum(pops * w)
            R[fac["fac_code"]] = fac["S_j"] / demand if demand > 0 else 0.0
        A = np.zeros(len(hex_nodes))
        for code, times in per_fac_times.items():
            t_arr = np.array([times.get(n, np.nan) for n in hex_nodes])
            w = np.where(np.isnan(t_arr), 0.0, decay(t_arr))
            A += R[code] * w
        e2sfca_score = (A * pops).sum() / self.total_pop

        return {"mean_travel_time_min": mean_t, "pop_reachable": float(pops[reach].sum()),
                "assigned": assigned, "e2sfca_score": e2sfca_score}

    def run_intervention_scenario(self, interventions, scenario_name="intervention"):
        """Opt-in - NOT part of run_all(), needs a scenario-specific location
        choice. interventions = list of {"name", "lon", "lat", "capacity"}
        dicts - covers temp health points / new fixed or mobile clinics (same
        mechanism: a new facility node with a location and capacity). Re-runs
        accessibility on the FLOODED graph with the augmented facility set,
        compared against flooded-with-no-intervention computed the same way."""
        from shapely.geometry import Point
        new_rows = [{"fac_code": f"NEW{i+1}", "name": iv["name"], "at_risk": False,
                     "S_j": max(iv.get("capacity", 1), 1), "geometry": Point(iv["lon"], iv["lat"])}
                    for i, iv in enumerate(interventions)]
        new_gdf = gpd.GeoDataFrame(new_rows, crs=self.facilities.crs)
        open_baseline = self.facilities[~self.facilities["at_risk"]]
        facilities_intervened = pd.concat([open_baseline, new_gdf], ignore_index=True)

        no_iv = self._accessibility_metrics(self.G_flooded, open_baseline)
        with_iv = self._accessibility_metrics(self.G_flooded, facilities_intervened)
        pop_regained = with_iv["pop_reachable"] - no_iv["pop_reachable"]

        result = {
            "scenario_name": scenario_name, "interventions": interventions,
            "flooded_no_intervention": {k: v for k, v in no_iv.items() if k != "assigned"},
            "flooded_with_intervention": {k: v for k, v in with_iv.items() if k != "assigned"},
            "pop_regained_access": float(pop_regained),
            "travel_time_delta_min": with_iv["mean_travel_time_min"] - no_iv["mean_travel_time_min"],
            "e2sfca_delta": with_iv["e2sfca_score"] - no_iv["e2sfca_score"],
        }
        self.log(f"[intervention:{scenario_name}] pop regained access: {pop_regained:,.0f}, "
                  f"travel time {no_iv['mean_travel_time_min']:.1f} -> {with_iv['mean_travel_time_min']:.1f} min, "
                  f"E2SFCA {no_iv['e2sfca_score']:.4f} -> {with_iv['e2sfca_score']:.4f}")
        out_path = os.path.join(self.cfg["extract_dir"], f"{self.cfg['prefix']}_intervention_{scenario_name}_result.json")
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2, default=float)
        self.log(f"[OK] -> {out_path}")
        return result

    # =========================================================================
    # PHASE C: static site maps - deck/QA imagery, generated from THIS run's
    # own state so they can never silently drift out of sync with the actual
    # numbers again (previously five separate one-off/standalone scripts -
    # visualize_district_accessibility.py, facility_catchment_shift.py, and
    # three that no longer existed as files at all - none of them re-run
    # automatically when the pipeline logic changed, e.g. the 2026-08-27
    # rainfall-intensity gate change silently left every existing map stale
    # until caught and fixed here. Folded into the pipeline itself, called
    # from run_all(), matching the pattern _plot_priority_ranking_map already
    # used for the priority-ranking map.)
    # =========================================================================

    def run_roads_crossings_map(self):
        """Structural QA map: roads, waterways, and every distinct crossing
        colored by OSM tagging status. Not affected by the flood gate or
        rainfall at all - purely about what OSM does/doesn't tag."""
        import matplotlib.pyplot as plt
        roads = gpd.read_file(self.cfg["roads_path"])
        waterways = gpd.read_file(self.cfg["waterways_path"])
        crossings = gpd.read_file(self.cfg["crossings_path"])

        def tagged(col):
            if col not in crossings.columns:
                return pd.Series(False, index=crossings.index)
            s = crossings[col]
            return s.notna() & (s.astype(str).str.lower() != "no")
        is_bridge = tagged("bridge")
        is_ford = tagged("ford") & ~is_bridge
        is_untagged = ~is_bridge & ~is_ford

        fig, ax = plt.subplots(figsize=(10, 12))
        roads.plot(ax=ax, color="#cbd5e1", linewidth=0.4, zorder=1)
        waterways.plot(ax=ax, color="#60a5fa", linewidth=0.6, zorder=2)
        crossings[is_untagged].plot(ax=ax, color="#dc2626", markersize=12, zorder=3,
                                      label=f"Crossing: untagged (n={int(is_untagged.sum())})")
        crossings[is_ford].plot(ax=ax, color="#d97706", markersize=25, edgecolor="black", linewidths=0.5, zorder=4,
                                  label=f"Crossing: ford tagged (n={int(is_ford.sum())})")
        crossings[is_bridge].plot(ax=ax, color="#16a34a", markersize=25, edgecolor="black", linewidths=0.5, zorder=5,
                                    label=f"Crossing: bridge tagged (n={int(is_bridge.sum())})")
        ax.legend(loc="upper left", fontsize=9)
        ax.set_title(f"{self.cfg['prefix'].replace('_', ' ').title()}: Roads, Waterways & Crossings\n"
                      f"({len(roads):,} road segments, {len(waterways):,} waterway segments, "
                      f"{len(crossings):,} distinct crossings)", fontweight="bold")
        ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
        fig.tight_layout()
        out = os.path.join(self.cfg["extract_dir"], f"{self.cfg['prefix']}_roads_crossings_map.png")
        fig.savefig(out, dpi=130)
        plt.close(fig)
        self.log(f"[OK] -> {out}")
        return self

    def run_floodplain_map(self):
        """The self-computed HAND/Leopold floodplain extent. Purely
        geomorphic/terrain-based - not affected by the flood gate or
        rainfall-intensity thresholds at all, unlike the accessibility and
        catchment-shift maps below."""
        import matplotlib.pyplot as plt
        from matplotlib.colors import ListedColormap
        with rasterio.open(self.cfg["floodplain_tif"]) as src:
            data = src.read(1)
            bounds = src.bounds
        pct = float(np.nansum(data == 1) / np.sum(~np.isnan(data)) * 100)
        masked = np.ma.masked_where(data != 1, data)
        fig, ax = plt.subplots(figsize=(10, 12))
        ax.set_facecolor("white")
        ax.imshow(masked, extent=[bounds.left, bounds.right, bounds.bottom, bounds.top],
                   cmap=ListedColormap(["#1e3a5f"]), origin="upper")
        ax.set_title(f"{self.cfg['prefix'].replace('_', ' ').title()}: Self-Computed Floodplain\n"
                      f"(HAND + Leopold power law, MERIT Hydro 90m)\n"
                      f"Blue = floodplain ({pct:.1f}% of resolved area)", fontweight="bold")
        ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
        fig.tight_layout()
        out = os.path.join(self.cfg["extract_dir"], f"{self.cfg['prefix']}_floodplain_map.png")
        fig.savefig(out, dpi=130)
        plt.close(fig)
        self.log(f"[OK] -> {out}")
        return self

    def run_rainfall_map(self):
        """CHIRPS event-total rainfall - the causal INPUT layer, not a gated
        output, so also unaffected by the rainfall-intensity gate change."""
        import matplotlib.pyplot as plt
        with rasterio.open(self.cfg["chirps_tif"]) as src:
            data = src.read(1)
            bounds = src.bounds
        fig, ax = plt.subplots(figsize=(10, 9))
        im = ax.imshow(data, extent=[bounds.left, bounds.right, bounds.bottom, bounds.top],
                         cmap="Blues", origin="upper")
        plt.colorbar(im, ax=ax, label=f"Rainfall total, {self.cfg['event_start_date']} "
                                        f"to {self.cfg['event_end_date']} (mm)")
        ax.set_title(f"CHIRPS event rainfall — {self.cfg['event_label']}\n"
                      f"{self.cfg['prefix'].replace('_', ' ').title()}", fontweight="bold")
        ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
        fig.tight_layout()
        out = os.path.join(self.cfg["extract_dir"], f"{self.cfg['prefix']}_rainfall_map.png")
        fig.savefig(out, dpi=130)
        plt.close(fig)
        self.log(f"[OK] -> {out}")
        return self

    def run_accessibility_map(self):
        """Baseline/flooded/change 3-panel driving-accessibility map -
        directly reflects whatever the current flood gate produced, since it
        plots self._travel_time_df from THIS run."""
        import matplotlib.pyplot as plt
        df = self._travel_time_df.copy()
        hexes = self.populated_hex[["h3_id", "geometry"]].merge(df, on="h3_id")
        fig, axes = plt.subplots(1, 3, figsize=(16, 7))
        vmax = np.nanpercentile(hexes["flooded_min"].dropna(), 95)

        hexes.plot(column="baseline_min", ax=axes[0], cmap="YlOrRd", markersize=1, vmin=0, vmax=vmax, legend=True,
                    legend_kwds={"label": "Travel time (min)", "shrink": 0.7})
        axes[0].set_title("BASELINE\n(before flooding)", fontweight="bold")

        hexes.plot(column="flooded_min", ax=axes[1], cmap="YlOrRd", markersize=1, vmin=0, vmax=vmax, legend=True,
                    legend_kwds={"label": "Travel time (min)", "shrink": 0.7})
        cutoff = hexes[hexes["cut_off"]]
        # populated_hex's geometry is the actual hexagon polygon, not a point - geopandas
        # ignores marker/markersize entirely for polygons, so this is solid black hex fill,
        # not a marker glyph (caught 2026-08-28 on the catchment-shift map's mislabeled "×").
        if len(cutoff):  # .plot() on an empty GeoDataFrame errors (aspect-ratio calc on no points) -
            cutoff.plot(ax=axes[1], color="black")  # a real, zero-disruption scenario is a valid result
        axes[1].set_title(f"FLOODED\n(after) — {int(hexes['cut_off'].sum())} hexes cut off entirely", fontweight="bold")

        hexes["change"] = hexes["flooded_min"] - hexes["baseline_min"]
        hexes.plot(column="change", ax=axes[2], cmap="Reds", markersize=1, vmin=0, legend=True,
                    legend_kwds={"label": "Added minutes", "shrink": 0.7})
        axes[2].set_title("CHANGE\n(added travel time)", fontweight="bold")

        for ax in axes:
            ax.set_xlabel("Longitude"); ax.set_xticklabels([]); ax.set_yticklabels([])
        fig.suptitle(f"{self.cfg['prefix'].replace('_', ' ').title()} — driving accessibility, "
                      f"{self.travel_time_result['baseline_min']:.1f} → {self.travel_time_result['flooded_min']:.1f} min "
                      f"({self.travel_time_result['pct_change']:+.1f}%), "
                      f"{self.travel_time_result['pop_cutoff']:,.0f} people cut off",
                      fontsize=13, fontweight="bold", y=0.99)
        # tight_layout does NOT reserve space for a figure-level suptitle by
        # default - without the rect= here the suptitle overlaps whatever
        # tight_layout decided was the top of the subplot titles (caught
        # 2026-08-27 on tokwe's map: title text visibly overlapping "FLOODED
        # (after)..."). rect reserves the top 12% of the figure for it.
        fig.tight_layout(rect=[0, 0, 1, 0.88])
        out = os.path.join(self.cfg["extract_dir"], f"{self.cfg['prefix']}_accessibility_map.png")
        fig.savefig(out, dpi=130)
        plt.close(fig)
        self.log(f"[OK] -> {out}")
        return self

    def run_catchment_shift_map(self):
        """A real catchment map, not a status map. Baseline and flooded
        panels side by side, each hex colored by ITS ASSIGNED FACILITY (one
        consistent color per facility across both panels, matching the
        facility's own marker) - comparing the two panels directly shows
        which territory changed hands, the way an actual catchment/service-
        area map should. Hexes with no reachable facility at all ("lost
        access") are filled solid black, impossible to miss against the
        colored catchments. A facility-code -> name key sits alongside the
        maps, since fac_code alone (F1, F2, ...) means nothing to a reader
        who hasn't seen the underlying data.

        Replaces both the old facility_catchment_shift.py standalone script
        (duplicated its own graph-building logic, never applied ANY rainfall
        gate, no branch for chimanimani_district/beitbridge) and this
        method's own first version (2026-08-27), which only classified each
        hex as unchanged/switched/lost with no sense of WHICH facility or
        WHERE people who lost access actually are - direct feedback that it
        "doesn't make sense." Reuses self._assigned_baseline/_flooded
        (run_catchment_shift), the same authoritative, regression-tested
        assignment already used for every other crisis-response number."""
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        fac = self.facilities.reset_index(drop=True)
        codes = fac["fac_code"].tolist()
        names = [(fac.iloc[i].get("name") or codes[i]) for i in range(len(fac))]
        name_of = dict(zip(codes, names))
        # A big combined qualitative palette (60 colors) so even a 42-
        # facility site (chimanimani_district) gets a distinct color per
        # facility, not a repeating handful.
        palette = (list(plt.get_cmap("tab20").colors) + list(plt.get_cmap("tab20b").colors) +
                    list(plt.get_cmap("tab20c").colors))
        code_color = {c: palette[i % len(palette)] for i, c in enumerate(codes)}
        LOST_COLOR = "#111111"

        hexes = self.populated_hex[["h3_id", "geometry", "population"]].copy()
        hexes["baseline_fac"] = self._assigned_baseline
        hexes["flooded_fac"] = self._assigned_flooded

        lost_mask = hexes["flooded_fac"].isna()
        switched_mask = (~lost_mask) & (hexes["baseline_fac"] != hexes["flooded_fac"])
        n_lost, pop_lost = int(lost_mask.sum()), hexes.loc[lost_mask, "population"].sum()
        n_switched, pop_switched = int(switched_mask.sum()), hexes.loc[switched_mask, "population"].sum()

        # Legend ordered by baseline catchment size, most-served facility
        # first - reading order matches visual importance on the map.
        base_pop = hexes.groupby("baseline_fac")["population"].sum()
        legend_order = sorted(codes, key=lambda c: -base_pop.get(c, 0))
        two_col = len(codes) > 20
        fig = plt.figure(figsize=(17 if two_col else 14, 10))
        gs = fig.add_gridspec(1, 3, width_ratios=[5, 5, 2.6 if two_col else 1.7], wspace=0.04)
        ax0, ax1, axl = (fig.add_subplot(gs[0, i]) for i in range(3))
        axl.axis("off")

        for ax, col in [(ax0, "baseline_fac"), (ax1, "flooded_fac")]:
            for code in codes:
                sub = hexes[hexes[col] == code]
                if len(sub):
                    sub.plot(ax=ax, color=code_color[code], markersize=3)
            # Solid fill, not a marker glyph: populated_hex's geometry is the actual
            # hexagon polygon, not a point, and geopandas ignores marker/markersize
            # for polygons - an earlier version of this map claimed "marked x" in its
            # own title/legend while actually rendering solid black hex fill the whole
            # time (caught 2026-08-28). Text below now matches what's actually drawn.
            lost = hexes[hexes[col].isna()]
            if len(lost):
                lost.plot(ax=ax, color=LOST_COLOR, zorder=6)
            for i, row in fac.iterrows():
                ax.scatter(row.geometry.x, row.geometry.y, color=code_color[codes[i]], s=50,
                            edgecolor="black", linewidths=0.7, marker="^", zorder=7)
            ax.set_xticklabels([]); ax.set_yticklabels([])
        ax0.set_title("BASELINE\n(before flooding)", fontweight="bold", fontsize=12)
        # Kept short deliberately - "solid black = lost access" is explained once, by
        # the swatch in the facility key, not restated here (an earlier, longer version
        # of this title collided with the key's own heading - caught 2026-08-28).
        ax1.set_title(f"FLOODED (after)\n{n_lost:,} hexes / {pop_lost:,.0f} people lost access",
                       fontweight="bold", fontsize=12)

        rows_per_col = -(-len(legend_order) // (2 if two_col else 1))
        fontsize = 7 if len(codes) > 25 else (8 if len(codes) > 12 else 9)
        name_max = 16 if two_col else 26  # a long name (e.g. "ZRP Birchnough Bridge") bled into
        axl.text(0, 1.0, "Facility key\n(by baseline catchment size)", transform=axl.transAxes,  # column 2 at
                  fontsize=10, fontweight="bold", va="top")                                        # 2-col width -
        for i, code in enumerate(legend_order):                                                    # truncate + widen
            col_i, row_i = (i // rows_per_col, i % rows_per_col) if two_col else (0, i)
            x0 = col_i * 0.58
            y = 0.9 - row_i * (0.86 / max(rows_per_col, 1))
            axl.add_patch(mpatches.Rectangle((x0, y - 0.010), 0.035, 0.018, transform=axl.transAxes,
                                               color=code_color[code], clip_on=False))
            label = name_of[code]
            label = label[:name_max - 1] + "…" if len(label) > name_max else label
            axl.text(x0 + 0.05, y, f"{code}: {label}", transform=axl.transAxes,
                      fontsize=fontsize, va="center")
        axl.add_patch(mpatches.Rectangle((0, -0.028), 0.035, 0.018, transform=axl.transAxes,
                                           color=LOST_COLOR, clip_on=False))
        axl.text(0.05, -0.02, "lost access entirely (flooded scenario)", transform=axl.transAxes,
                  fontsize=fontsize, color=LOST_COLOR, fontweight="bold", va="center")

        fig.suptitle(f"{self.cfg['prefix'].replace('_', ' ').title()}: Facility Catchment, Baseline vs. Flooded\n"
                      f"{n_switched:,} hexes / {pop_switched:,.0f} people switched facility — "
                      f"{n_lost:,} hexes / {pop_lost:,.0f} people lost access entirely",
                      fontsize=13, fontweight="bold", y=0.99)
        fig.tight_layout(rect=[0, 0, 1, 0.88])
        out = os.path.join(self.cfg["extract_dir"], f"{self.cfg['prefix']}_catchment_shift_map.png")
        fig.savefig(out, dpi=130)
        plt.close(fig)
        self.log(f"[OK] -> {out}")
        return self

    def run_all(self):
        self.run_phase_a()
        self.run_roads_crossings_map()
        self.run_floodplain_map()
        self.run_rainfall_map()
        self.build_graphs()
        self.load_facilities()
        self.build_population_grid()
        self.join_vulnerability_index()
        self.run_mean_travel_time()
        self.run_accessibility_map()
        self.run_admin_tabulation()
        self.run_catchment_shift()
        self.run_catchment_shift_map()
        self.run_e2sfca()
        self.run_distance_bands()
        self.run_priority_ranking()
        self.run_underserved_areas()
        return {
            "site": self.site_id,
            "travel_time": self.travel_time_result,
            "admin_tabulation": self.admin_tabulation_result,
            "catchment_shift": self.catchment_result,
            "e2sfca": self.e2sfca_result,
            "distance_bands": self.distance_bands_result,
            "priority_ranking": self.priority_ranking_result,
            "underserved_areas": self.underserved_areas_result,
        }


if __name__ == "__main__":
    site = sys.argv[1] if len(sys.argv) > 1 else "chimanimani"
    skip_cache = "--skip-cache" in sys.argv
    pipeline = FloodAccessibilityPipeline(site, skip_cache=skip_cache)
    results = pipeline.run_all()
    out_path = os.path.join(BASE_DIR, f"{site}_pipeline_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=float)
    print(f"\n[OK] Full results -> {out_path}")

"""
SITE ONBOARDING CHECKLIST - now just config, Phase A runs itself:
  1. Add one SITE_CONFIGS entry: bbox, prefix, event_start_date/end_date,
     utm_crs, and the output paths (they don't need to exist yet - Phase A
     creates them on first run and caches them for every run after).
  2. Run `python flood_accessibility_pipeline.py <site_id>`. First run does
     the full Phase A sequence (roads/waterways extraction, crossing dedup +
     ranking, MERIT Hydro fetch, CHIRPS fetch, HAND floodplain, facility
     extraction) - this is the slow run (Earth Engine downloads + the HAND
     D8 trace, several minutes). Every run after reuses the cached files.
  3. Requires Earth Engine auth (ee.Initialize) to already be set up on this
     machine - the one Phase A dependency that can't be automated away.
  4. Not automated: JRC cross-check for large catchments (>~3,000km2) - run
     fetch_jrc_flood_hazard.py-equivalent manually if the new site has one,
     purely as validation, since it doesn't feed the pipeline's flood
     definition either way (see METHODS_LOG.md).
  5. No facility capacity work needed - national_facility_capacity_estimates.csv
     already covers any facility in Zimbabwe by nearest spatial match.
  6. If OSM has no tagged health facilities in the new bbox,
     extract_facilities() raises rather than silently producing an empty
     site - supply facilities_path pointing at an existing file instead.
"""
