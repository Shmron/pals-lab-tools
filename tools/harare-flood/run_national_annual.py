"""
Drives the existing FloodAccessibilityPipeline at national (Zimbabwe-wide)
scale, once per target year, without modifying flood_accessibility_pipeline.py.

Three things the 4 small pilot sites never needed, handled here instead of
in the shared pipeline file:

1. MERIT Hydro at national extent, scale=90, would ask Earth Engine's
   getDownloadURL for ~1GB in one call (it caps around 48MB) -> tiled fetch
   + rasterio.merge mosaic, written straight to the path
   fetch_merit_hydro() already expects, so that method's own cache check
   just finds it done and skips re-fetching. Not needed for CHIRPS
   (~5.5km) or ERA5-Land (~11km) - both stay far under the limit at
   national extent.

2. Each year's flood "event window" is chosen automatically (that year's
   heaviest national 7-day CHIRPS total) instead of a human picking one
   historic event, so this can run unattended across a year range.

3. build_population_grid()'s per-hex rasterio.mask() Python loop measured
   at ~1.1ms/hex locally - fine for Harare's ~127k hexes, ~91 minutes for
   Zimbabwe's ~5M bbox-rectangle hexes. Replaced here with a mathematically
   equivalent vectorized rasterize+bincount zonal sum (same per-hex
   population totals, same output columns/shape FloodAccessibilityPipeline
   expects in self.populated_hex), reducing that stage to roughly a minute.
   Everything downstream of it (vulnerability join, routing, admin
   tabulation) is untouched.

Usage:
    python run_national_annual.py <year> [--skip-cache]
"""
import os
import sys
import glob
import tempfile

import ee
import requests
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.merge import merge as rasterio_merge
from rasterio.features import rasterize
from rasterio.mask import mask
from shapely.geometry import Polygon, box
import h3

EE_PROJECT = "your-earth-engine-project"
EE_SA_EMAIL = "ee-service-account@your-project.iam.gserviceaccount.com"
EE_SA_KEY = "/home/USER/PRECISE/palsearth/palsearth-sa-key.json"

# This server has no interactive `earthengine authenticate` session (the pilot
# sites' cached files were produced on the user's other machine, which did).
# PALSearth's own service account for this same EE project already lives on
# this server, so reuse it: patch ee.Initialize globally, before importing
# flood_accessibility_pipeline, so every bare `ee.Initialize(project=...)`
# call already inside that file (4 call sites, unmodified) transparently
# authenticates via the service account instead of failing.
_real_ee_initialize = ee.Initialize
def _ee_initialize_with_service_account(*args, **kwargs):
    creds = ee.ServiceAccountCredentials(EE_SA_EMAIL, EE_SA_KEY)
    return _real_ee_initialize(credentials=creds, project=EE_PROJECT)
ee.Initialize = _ee_initialize_with_service_account

import flood_accessibility_pipeline as fap

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NATIONAL_EXTRACT_DIR = os.path.join(BASE_DIR, "National_extract")
NATIONAL_BBOX = [25.237, -22.418, 33.056, -15.609]  # zimbabwe_districts.gpkg total_bounds, 0 margin
MERIT_TILE_DEG = 1.0  # grid step for tiled MERIT fetch -> ~110km tiles, well under the 48MB cap at 90m/3-band


def national_site_config(year, event_start, event_end):
    prefix = f"zimbabwe_{year}"
    extract_dir = os.path.join(NATIONAL_EXTRACT_DIR, str(year))
    return {
        "bbox": NATIONAL_BBOX,
        "prefix": prefix,
        "extract_dir": extract_dir,
        "facilities_path": os.path.join(extract_dir, f"{prefix}_health_facilities.gpkg"),
        "roads_path": os.path.join(extract_dir, f"{prefix}_roads.gpkg"),
        "waterways_path": os.path.join(extract_dir, f"{prefix}_waterways.gpkg"),
        "raw_crossings_path": os.path.join(extract_dir, f"{prefix}_crossings.gpkg"),
        "crossings_path": os.path.join(extract_dir, f"{prefix}_crossings_distinct.gpkg"),
        "crossings_flag_csv": os.path.join(extract_dir, f"{prefix}_crossings_with_selfcomputed_floodplain.csv"),
        "floodplain_tif": os.path.join(BASE_DIR, "DEM", f"{prefix}_selfcomputed_floodplain.tif"),
        "merit_tif": os.path.join(BASE_DIR, "DEM", f"{prefix}_merit_hydro.tif"),
        "chirps_tif": os.path.join(BASE_DIR, "Rainfall", f"{prefix}_chirps_total.tif"),
        "event_start_date": event_start,
        "event_end_date": event_end,
        "utm_crs": "EPSG:32736",
        "event_label": f"{year} heaviest national 7-day CHIRPS window ({event_start} to {event_end})",
        "rainfall_col": "event_rainfall_mm",
        "flood_mechanism": "riverine",
    }


def find_wettest_week(year):
    """That year's heaviest 7-day national-mean CHIRPS rainfall total -
    automatable equivalent of the pilot sites' manually-chosen event
    windows, using the same CHIRPS source already trusted elsewhere in
    this pipeline."""
    ee.Initialize(project=EE_PROJECT)
    region = ee.Geometry.Rectangle(NATIONAL_BBOX)
    chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterDate(f"{year}-01-01", f"{year+1}-01-01")

    def daily_mean(img):
        val = img.reduceRegion(ee.Reducer.mean(), region, scale=5566).get("precipitation")
        return img.set("date", img.date().format("YYYY-MM-dd")).set("mean_mm", val)

    feats = chirps.map(daily_mean).toList(400)
    info = ee.FeatureCollection(ee.List(feats).map(lambda f: ee.Feature(None, {
        "date": ee.Image(f).get("date"), "mean_mm": ee.Image(f).get("mean_mm")}))).getInfo()
    rows = [(f["properties"]["date"], f["properties"]["mean_mm"]) for f in info["features"]]
    df = pd.DataFrame(rows, columns=["date", "mean_mm"]).dropna()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")
    rolling = df["mean_mm"].rolling(7).sum()
    end = rolling.idxmax()
    start = end - pd.Timedelta(days=6)
    print(f"[*] {year}: wettest 7-day window = {start.date()} to {(end + pd.Timedelta(days=1)).date()} "
          f"({rolling.max():.1f}mm national mean)")
    return start.strftime("%Y-%m-%d"), (end + pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def fetch_national_merit_hydro(out_path):
    if os.path.exists(out_path):
        print(f"[cache] {out_path} exists, skipping MERIT tiling")
        return
    ee.Initialize(project=EE_PROJECT)
    west, south, east, north = NATIONAL_BBOX
    merit = ee.Image("MERIT/Hydro/v1_0_1").select(["elv", "dir", "upa"])

    tmp_dir = tempfile.mkdtemp(prefix="merit_tiles_")
    tile_paths = []
    x = west
    tile_n = 0
    while x < east:
        y = south
        x2 = min(x + MERIT_TILE_DEG, east)
        while y < north:
            y2 = min(y + MERIT_TILE_DEG, north)
            region = ee.Geometry.Rectangle([x, y, x2, y2])
            url = merit.getDownloadURL({"region": region, "scale": 90, "crs": "EPSG:4326", "format": "GEO_TIFF"})
            resp = requests.get(url, timeout=300)
            resp.raise_for_status()
            tile_path = os.path.join(tmp_dir, f"tile_{tile_n}.tif")
            with open(tile_path, "wb") as f:
                f.write(resp.content)
            tile_paths.append(tile_path)
            print(f"[OK] MERIT tile {tile_n} [{x:.2f},{y:.2f},{x2:.2f},{y2:.2f}] ({len(resp.content):,} bytes)")
            tile_n += 1
            y = y2
        x = x2

    srcs = [rasterio.open(p) for p in tile_paths]
    mosaic, out_transform = rasterio_merge(srcs)
    meta = srcs[0].meta.copy()
    meta.update({"height": mosaic.shape[1], "width": mosaic.shape[2], "transform": out_transform})
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(mosaic)
    for s in srcs:
        s.close()
    for p in tile_paths:
        os.remove(p)
    os.rmdir(tmp_dir)
    print(f"[OK] {tile_n} MERIT tiles mosaicked -> {out_path}")


def build_national_population_grid(pipeline):
    """Vectorized equivalent of FloodAccessibilityPipeline.build_population_grid:
    same h3 res-9 hex grid over cfg['bbox'], same per-hex population sum
    from POP_RASTER_PATH, same output columns (h3_id, geometry, population,
    centroid) and self.total_pop - just rasterize+bincount instead of a
    per-hex rasterio.mask() Python loop (~1.1ms/hex measured locally,
    ~91min at national hex counts vs this approach's ~1min)."""
    west, south, east, north = pipeline.cfg["bbox"]
    bbox_poly = Polygon([(west, south), (east, south), (east, north), (west, north)])
    hexagons = h3.geo_to_cells(bbox_poly, res=fap.H3_RESOLUTION)
    hex_ids = list(hexagons)
    hex_geoms = [Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(hid)]) for hid in hex_ids]
    print(f"[*] {len(hex_geoms):,} candidate hexes over the national bbox")

    with rasterio.open(fap.POP_RASTER_PATH) as pop_raster:
        pop_arr = pop_raster.read(1).astype(np.float64)
        if pop_raster.nodata is not None:
            pop_arr = np.where(pop_arr == pop_raster.nodata, 0, pop_arr)
        pop_arr = np.where(pop_arr < 0, 0, pop_arr)
        shapes = [(geom, i + 1) for i, geom in enumerate(hex_geoms)]
        hex_id_raster = rasterize(shapes, out_shape=pop_arr.shape, transform=pop_raster.transform,
                                   fill=0, dtype=np.int32)

    sums = np.bincount(hex_id_raster.ravel(), weights=pop_arr.ravel(), minlength=len(hex_geoms) + 1)
    hex_pop = sums[1:]

    hex_gdf = gpd.GeoDataFrame({"h3_id": hex_ids, "geometry": hex_geoms, "population": hex_pop}, crs="EPSG:4326")
    populated_hex = hex_gdf[hex_gdf["population"] > 0].copy()
    populated_hex["centroid"] = populated_hex.geometry.to_crs("EPSG:32736").centroid.to_crs("EPSG:4326")
    print(f"[OK] {len(populated_hex):,} populated hexagons, {populated_hex['population'].sum():,.0f} total population")

    pipeline.populated_hex = populated_hex
    pipeline.total_pop = populated_hex["population"].sum()
    return pipeline


def run_year(year, skip_cache=False):
    event_start, event_end = find_wettest_week(year)
    site_id = f"zimbabwe_{year}"
    fap.SITE_CONFIGS[site_id] = national_site_config(year, event_start, event_end)

    cfg = fap.SITE_CONFIGS[site_id]
    os.makedirs(cfg["extract_dir"], exist_ok=True)
    os.makedirs(os.path.dirname(cfg["merit_tif"]), exist_ok=True)
    os.makedirs(os.path.dirname(cfg["chirps_tif"]), exist_ok=True)

    fetch_national_merit_hydro(cfg["merit_tif"])

    pipeline = fap.FloodAccessibilityPipeline(site_id, skip_cache=skip_cache)
    pipeline.run_phase_a()
    pipeline.build_graphs()
    pipeline.load_facilities()
    build_national_population_grid(pipeline)
    pipeline.join_vulnerability_index()
    pipeline.run_mean_travel_time()
    tabulation = pipeline.run_admin_tabulation()  # dict {"districts": [...]}; also already
    # written to cfg["extract_dir"]/{prefix}_admin_tabulation.csv by run_admin_tabulation
    # itself - this just also drops a year-named copy at BASE_DIR for build_access.py to read.

    out_csv = os.path.join(BASE_DIR, f"{year}_admin_tabulation.csv")
    pd.DataFrame(tabulation["districts"]).to_csv(out_csv, index=False)
    print(f"[OK] year {year} done -> {out_csv}")
    return pipeline


if __name__ == "__main__":
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    skip_cache = "--skip-cache" in sys.argv
    run_year(year, skip_cache=skip_cache)
