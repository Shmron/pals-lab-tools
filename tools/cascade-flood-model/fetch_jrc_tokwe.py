"""
Fetch JRC Global River Flood Hazard (RP10, RP20 depth) for the Tokwe-Mukosi
bbox, sample at each of our 114 crossings, and flag which ones exceed a
0.3m impassability threshold - the same kind of physical criterion we
couldn't get from our own index at Chimanimani.
"""
import os
import ee
import requests
import rasterio
import numpy as np
import geopandas as gpd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "JRC_FloodHazard")
EXTRACT_DIR = os.path.join(BASE_DIR, "TokweMukosi_extract")
os.makedirs(OUT_DIR, exist_ok=True)

TOKWE_BBOX = [30.25, -21.45, 30.65, -20.85]
IMPASSABLE_DEPTH_M = 0.3

ee.Initialize(project="your-earth-engine-project")
region = ee.Geometry.Rectangle(TOKWE_BBOX)
jrc = ee.ImageCollection("JRC/CEMS_GLOFAS/FloodHazard/v2_1").mosaic()

for band in ["RP10_depth", "RP20_depth"]:
    img = jrc.select(band).clip(region).toFloat()
    url = img.getDownloadURL({"region": region, "scale": 90, "crs": "EPSG:4326", "format": "GEO_TIFF"})
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    out_path = os.path.join(OUT_DIR, f"tokwe_jrc_{band}.tif")
    with open(out_path, "wb") as f:
        f.write(resp.content)
    with rasterio.open(out_path) as src:
        data = src.read(1)
        valid = data[~np.isnan(data)]
        print(f"{band}: {len(resp.content):,} bytes, valid={len(valid)}/{data.size}, "
              f"range={np.nanmin(data) if len(valid) else 'n/a'}-{np.nanmax(data) if len(valid) else 'n/a'}")

crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "tokwe_crossings.gpkg"))
crossings["geometry"] = crossings.geometry.centroid

with rasterio.open(os.path.join(OUT_DIR, "tokwe_jrc_RP10_depth.tif")) as src10, \
     rasterio.open(os.path.join(OUT_DIR, "tokwe_jrc_RP20_depth.tif")) as src20:
    d10 = src10.read(1)
    d20 = src20.read(1)
    rp10_vals, rp20_vals = [], []
    for pt in crossings.geometry:
        r, c = rasterio.transform.rowcol(src10.transform, pt.x, pt.y)
        v10 = d10[r, c] if (0 <= r < d10.shape[0] and 0 <= c < d10.shape[1]) else np.nan
        r2, c2 = rasterio.transform.rowcol(src20.transform, pt.x, pt.y)
        v20 = d20[r2, c2] if (0 <= r2 < d20.shape[0] and 0 <= c2 < d20.shape[1]) else np.nan
        rp10_vals.append(v10)
        rp20_vals.append(v20)

crossings["rp10_depth_m"] = rp10_vals
crossings["rp20_depth_m"] = rp20_vals
crossings["jrc_valid"] = crossings["rp10_depth_m"].notna() & (crossings["rp10_depth_m"] > -1e30)

n_valid = crossings["jrc_valid"].sum()
print(f"\n[i] JRC RP10 valid at {n_valid}/{len(crossings)} crossings")

valid_crossings = crossings[crossings["jrc_valid"]].copy()
valid_crossings["impassable_rp10"] = valid_crossings["rp10_depth_m"] > IMPASSABLE_DEPTH_M
n_impassable = valid_crossings["impassable_rp10"].sum()
print(f"[i] Of those, {n_impassable} exceed the {IMPASSABLE_DEPTH_M}m impassability threshold at RP10 "
      f"(a ~10-year return period - conservative, given two separate rain pulses hit this season)")

print("\nCrossings with valid JRC depth, sorted by RP10 depth:")
print(valid_crossings[["highway", "waterway", "rp10_depth_m", "rp20_depth_m", "impassable_rp10"]]
      .sort_values("rp10_depth_m", ascending=False).head(20).to_string(index=False))

out_path = os.path.join(EXTRACT_DIR, "tokwe_crossings_jrc.gpkg")
crossings.to_file(out_path, driver="GPKG")
print(f"\n[OK] Saved -> {out_path}")
