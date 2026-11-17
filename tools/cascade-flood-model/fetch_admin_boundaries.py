"""
Fetch Zimbabwe district (admin2) boundaries from FAO GAUL 2015 level2, once
nationally. Confirmed during planning: 62 districts, exact string match
"Chimanimani" for the pilot site's own district - no HDX scraping needed,
fits the project's existing GEE-fetch pattern (fetch_merit_hydro.py etc).

Output: Population/zimbabwe_districts.gpkg (ADM2_NAME, geometry)
"""
import os
import ee
import geopandas as gpd
from shapely.geometry import shape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "Population")
os.makedirs(OUT_DIR, exist_ok=True)
OUT_PATH = os.path.join(OUT_DIR, "zimbabwe_districts.gpkg")

ee.Initialize(project="your-earth-engine-project")

gaul2 = ee.FeatureCollection("FAO/GAUL/2015/level2")
zw = gaul2.filter(ee.Filter.eq("ADM0_NAME", "Zimbabwe"))

print("[*] Fetching Zimbabwe district boundaries (FAO GAUL 2015 level2) ...")
info = zw.getInfo()
features = info["features"]
print(f"[OK] {len(features)} districts")

records = []
for f in features:
    props = f["properties"]
    geom = shape(f["geometry"])
    records.append({"ADM2_NAME": props["ADM2_NAME"], "geometry": geom})

gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")
gdf.to_file(OUT_PATH, driver="GPKG")
print(f"[OK] Saved -> {OUT_PATH}")
print(sorted(gdf["ADM2_NAME"].tolist()))
