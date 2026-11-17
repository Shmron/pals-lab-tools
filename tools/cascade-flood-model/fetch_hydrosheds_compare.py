"""
Fetch HydroSHEDS (conditioned DEM + flow accumulation) for Chimanimani and
compare directly against the MERIT Hydro layer we've been using, at the same
462 crossing points.
"""
import os
import ee
import requests
import rasterio
import numpy as np
import geopandas as gpd
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "HydroSHEDS")
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
os.makedirs(OUT_DIR, exist_ok=True)

CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]

ee.Initialize(project="your-earth-engine-project")
region = ee.Geometry.Rectangle(CHIMANIMANI_BBOX)

# HydroSHEDS conditioned DEM (3 arc-sec, ~90m) + flow accumulation (from the
# separate HydroSHEDS Flow Accumulation product, also 3 arc-sec)
hydrosheds_dem = ee.Image("WWF/HydroSHEDS/03CONDEM")
hydrosheds_flowacc = ee.Image("WWF/HydroSHEDS/03DIR")  # flow direction; accumulation below

# HydroSHEDS doesn't ship a single "flow accumulation in km2" band on GEE the
# way MERIT Hydro does - the closest equivalent available is upstream drainage
# area via the HydroBASINS/HydroRIVERS network attributes, OR we derive
# accumulation ourselves from the conditioned DEM + flow direction. For a
# direct apples-to-apples check, pull what GEE actually offers: conditioned
# elevation and flow direction, at the same bbox/resolution as MERIT Hydro.
for band_name, img in [("dem", hydrosheds_dem), ("flowdir", hydrosheds_flowacc)]:
    band_img = img.clip(region).toFloat()
    url = band_img.getDownloadURL({
        "region": region, "scale": 90, "crs": "EPSG:4326", "format": "GEO_TIFF",
    })
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    out_path = os.path.join(OUT_DIR, f"chimanimani_hydrosheds_{band_name}.tif")
    with open(out_path, "wb") as f:
        f.write(resp.content)
    with rasterio.open(out_path) as src:
        data = src.read(1)
        valid = data[~np.isnan(data)]
        print(f"{band_name}: {len(resp.content):,} bytes, shape={data.shape}, "
              f"valid={len(valid)}/{data.size}, "
              f"min={np.nanmin(data) if len(valid) else 'n/a'}, "
              f"max={np.nanmax(data) if len(valid) else 'n/a'}")

# ---------------------------------------------------------------------------
# Compare elevation at our crossings: HydroSHEDS conditioned DEM vs MERIT Hydro elv
# ---------------------------------------------------------------------------
print("\n[*] Comparing HydroSHEDS vs MERIT Hydro elevation at our crossing points ...")
crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings_distinct.gpkg"))

with rasterio.open(os.path.join(OUT_DIR, "chimanimani_hydrosheds_dem.tif")) as hs_src, \
     rasterio.open(os.path.join(BASE_DIR, "DEM", "chimanimani_merit_hydro.tif")) as mh_src:
    hs_data = hs_src.read(1)
    mh_data = mh_src.read(1)  # band 1 = elv

    hs_vals, mh_vals = [], []
    for pt in crossings.geometry:
        r1, c1 = rasterio.transform.rowcol(hs_src.transform, pt.x, pt.y)
        r2, c2 = rasterio.transform.rowcol(mh_src.transform, pt.x, pt.y)
        hs_v = hs_data[r1, c1] if (0 <= r1 < hs_data.shape[0] and 0 <= c1 < hs_data.shape[1]) else np.nan
        mh_v = mh_data[r2, c2] if (0 <= r2 < mh_data.shape[0] and 0 <= c2 < mh_data.shape[1]) else np.nan
        hs_vals.append(hs_v)
        mh_vals.append(mh_v)

comparison = pd.DataFrame({
    "hydrosheds_elv_m": hs_vals,
    "merit_hydro_elv_m": mh_vals,
})
comparison["diff_m"] = comparison["hydrosheds_elv_m"] - comparison["merit_hydro_elv_m"]
print(comparison.describe().to_string())
print(f"\nHydroSHEDS valid at {comparison['hydrosheds_elv_m'].notna().sum()}/{len(comparison)} crossings")
print(f"MERIT Hydro valid at {comparison['merit_hydro_elv_m'].notna().sum()}/{len(comparison)} crossings")
print(f"Mean absolute elevation difference: {comparison['diff_m'].abs().mean():.1f} m")

comparison.to_csv(os.path.join(EXTRACT_DIR, "hydrosheds_vs_merit_comparison.csv"), index=False)
