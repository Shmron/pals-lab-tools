"""
Filter the NASA Global Landslide Catalog to Zimbabwe / Chimanimani, and check
specifically for Cyclone Idai (March 2019) events.
"""
import os
import json
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IN_PATH = os.path.join(BASE_DIR, "Landslides", "Global_Landslide_Catalog_Export_rows.json")
OUT_DIR = os.path.join(BASE_DIR, "Landslides")

CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]  # west, south, east, north

with open(IN_PATH, encoding="utf-8") as f:
    raw = json.load(f)

cols = [c.get("fieldName") for c in raw["meta"]["view"]["columns"]]
df = pd.DataFrame(raw["data"], columns=cols)
print(f"[i] Total global landslide records: {len(df):,}")

df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce")

zw = df[df["country_name"].str.strip().str.lower() == "zimbabwe"].copy()
print(f"[i] Zimbabwe records: {len(zw)}")
if len(zw):
    print(zw[["event_date", "admin_division_name", "location_description",
               "landslide_trigger", "fatality_count", "latitude", "longitude"]].to_string(index=False))

# Idai window: mid-March 2019, generous +/- buffer
idai = df[(df["event_date"] >= "2019-03-10") & (df["event_date"] <= "2019-03-31")].copy()
print(f"\n[i] Global records in the Idai window (2019-03-10 to 2019-03-31): {len(idai)}")
if len(idai):
    print(idai[["event_date", "country_name", "admin_division_name", "location_description",
                 "fatality_count", "latitude", "longitude"]].to_string(index=False))

# Anything within/near the Chimanimani bbox specifically (small buffer since catalog
# coordinates are often approximate / gazeteer-snapped, not precise)
west, south, east, north = CHIMANIMANI_BBOX
buffer = 0.3  # ~30km buffer given catalog coordinate imprecision
near_chimanimani = df[
    (df["longitude"] >= west - buffer) & (df["longitude"] <= east + buffer) &
    (df["latitude"] >= south - buffer) & (df["latitude"] <= north + buffer)
].copy()
print(f"\n[i] Records within ~30km of Chimanimani bbox (any date): {len(near_chimanimani)}")
if len(near_chimanimani):
    print(near_chimanimani[["event_date", "country_name", "admin_division_name",
                             "location_description", "fatality_count",
                             "latitude", "longitude"]].to_string(index=False))

# Save whatever we found
combined = pd.concat([zw, idai, near_chimanimani]).drop_duplicates(subset=["event_id"])
if len(combined):
    combined = combined.dropna(subset=["latitude", "longitude"])
    gdf = gpd.GeoDataFrame(
        combined,
        geometry=[Point(xy) for xy in zip(combined["longitude"], combined["latitude"])],
        crs="EPSG:4326",
    )
    out_path = os.path.join(OUT_DIR, "zimbabwe_idai_landslides.gpkg")
    gdf.drop(columns=["created_meta", "updated_meta"], errors="ignore").to_file(out_path, driver="GPKG")
    print(f"\n[OK] Saved {len(gdf)} relevant records -> {out_path}")
else:
    print("\n[X] No relevant records found (Zimbabwe country match, Idai date window, or geographic proximity)")
