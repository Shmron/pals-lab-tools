"""
Seed Harare_extract/harare_health_facilities.gpkg from the user's own curated
facility list (Harare_healthfacilities_classified_v2.csv), bbox-filtered to
the "harare" SITE_CONFIGS entry, BEFORE Phase A runs - so extract_facilities()
sees the gpkg already cached and never falls back to the national MoH list.
"""
import os
import geopandas as gpd
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_CSV = r"PATH/TO/LOCAL/DATA"
OUT_GPKG = os.path.join(BASE_DIR, "Harare_extract", "harare_health_facilities.gpkg")

BBOX = [30.66, -18.22, 31.70, -17.17]  # west, south, east, north - matches SITE_CONFIGS["harare"] (widened 2026-10-19 to cover all 152 facilities)

df = pd.read_csv(SRC_CSV)
west, south, east, north = BBOX
in_bbox = df[
    (df["longitude"] >= west) & (df["longitude"] <= east) &
    (df["latitude"] >= south) & (df["latitude"] <= north)
].reset_index(drop=True)

out_of_bbox = ~(
    (df["longitude"] >= west) & (df["longitude"] <= east) &
    (df["latitude"] >= south) & (df["latitude"] <= north)
)
excluded = df[out_of_bbox]

in_bbox["fac_code"] = [f"F{i+1}" for i in range(len(in_bbox))]
in_bbox["workforce_est"] = 1  # dummy - capacity-weighted stages (E2SFCA/priority-ranking) are out of scope here

gdf = gpd.GeoDataFrame(
    in_bbox[["fac_code", "name", "amenity", "classification", "CEmONC", "workforce_est"]],
    geometry=gpd.points_from_xy(in_bbox["longitude"], in_bbox["latitude"]),
    crs="EPSG:4326",
)
os.makedirs(os.path.dirname(OUT_GPKG), exist_ok=True)
gdf.to_file(OUT_GPKG, driver="GPKG")

print(f"[OK] {len(gdf)}/{len(df)} facilities in Harare metro bbox -> {OUT_GPKG}")
print(f"[OK] {len(excluded)} facilities excluded (outside bbox), e.g.:")
print(excluded[["name", "latitude", "longitude"]].head(8).to_string(index=False))
print("\nType breakdown (in bbox):")
print(gdf["amenity"].value_counts())
print("\nClassification breakdown (in bbox):")
print(gdf["classification"].value_counts())
