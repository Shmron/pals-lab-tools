"""
Fetch the Relative Wealth Index (RWI) for Zimbabwe from Google Earth Engine -
a 2.4km-resolution satellite/connectivity-derived wealth surface, itself
built and globally validated against DHS ground truth (Chi et al. 2021 /
Facebook Data for Good, hosted in the awesome-gee-community-catalog).

Fetched once nationally (not per-site) since it's a dense point layer, not a
per-site raster like MERIT Hydro/CHIRPS. Whether this or the local DHS 2015
cluster data (extract_dhs_wealth_clusters.py) becomes the pipeline's actual
vulnerability layer is decided by validate_rwi_against_dhs.py, not here.

Output: Population/zimbabwe_rwi.csv (lon, lat, rwi)
"""
import os
import ee

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "Population")
os.makedirs(OUT_DIR, exist_ok=True)
OUT_PATH = os.path.join(OUT_DIR, "zimbabwe_rwi.csv")

# Zimbabwe bbox, generous margin (national extract, not a site bbox)
ZW_BBOX = [25.0, -22.5, 33.1, -15.5]

ee.Initialize(project="your-earth-engine-project")

rwi = ee.FeatureCollection("projects/sat-io/open-datasets/facebook/relative_wealth_index")
region = ee.Geometry.Rectangle(ZW_BBOX)
zw_rwi = rwi.filterBounds(region)

print("[*] Requesting Zimbabwe RWI point count from Earth Engine ...")
n = zw_rwi.size().getInfo()
print(f"[OK] {n:,} RWI points intersect the Zimbabwe bbox")

print("[*] Exporting to CSV via getDownloadURL ...")
url = zw_rwi.select(["rwi"]).getDownloadURL("csv")

import requests
resp = requests.get(url, timeout=180)
resp.raise_for_status()
raw_path = os.path.join(OUT_DIR, "zimbabwe_rwi_raw.csv")
with open(raw_path, "wb") as f:
    f.write(resp.content)
print(f"[OK] Saved raw -> {raw_path} ({len(resp.content):,} bytes)")

# Earth Engine's CSV export only carries a GeoJSON ".geo" column, not
# separate lon/lat properties (select() can't pull geometry coordinates as
# columns) - parse the point coordinates out of it locally instead of
# round-tripping through GEE again.
import json
import pandas as pd
df = pd.read_csv(raw_path)
coords = df[".geo"].apply(lambda g: json.loads(g)["coordinates"])
df["lon"] = coords.apply(lambda c: c[0])
df["lat"] = coords.apply(lambda c: c[1])
df = df[["lon", "lat", "rwi"]]
df.to_csv(OUT_PATH, index=False)
print(f"[OK] Saved -> {OUT_PATH}")
print(f"\n{len(df):,} rows, columns: {list(df.columns)}")
print(df.describe())
