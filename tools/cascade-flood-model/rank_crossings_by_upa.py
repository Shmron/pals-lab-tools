"""
Rank the road x waterway crossings by upstream drainage area (MERIT Hydro 'upa'
band) - a standard proxy for flood/peak-discharge risk at each crossing.
"""
import os
import geopandas as gpd
import rasterio
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
DEM_PATH = os.path.join(BASE_DIR, "DEM", "chimanimani_merit_hydro.tif")

crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings.gpkg"))

# Representative point per crossing (centroid of the intersection geometry)
crossing_points = crossings.copy()
crossing_points["geometry"] = crossing_points.geometry.centroid


def classify(row):
    if row.get("bridge") not in (None, ""):
        return "bridge"
    if row.get("ford") not in (None, ""):
        return "ford"
    return "untagged"


crossing_points["status"] = crossing_points.apply(classify, axis=1)

# Sample MERIT Hydro band 3 (upa, upstream drainage area in km^2) at each crossing point
with rasterio.open(DEM_PATH) as src:
    upa_band = src.read(3)
    coords = [(pt.x, pt.y) for pt in crossing_points.geometry]
    rows_cols = [src.index(x, y) for x, y in coords]
    upa_values = []
    for row, col in rows_cols:
        if 0 <= row < upa_band.shape[0] and 0 <= col < upa_band.shape[1]:
            upa_values.append(float(upa_band[row, col]))
        else:
            upa_values.append(np.nan)

crossing_points["upa_km2"] = upa_values

# Save ranked table
ranked = crossing_points.drop(columns=["geometry"]).sort_values("upa_km2", ascending=False)
ranked_path = os.path.join(EXTRACT_DIR, "chimanimani_crossings_ranked.csv")
ranked.to_csv(ranked_path, index=False)
print(f"[OK] Ranked crossings -> {ranked_path}\n")

print(f"Total crossings ranked: {len(ranked)}")
print(f"\nTop 15 by upstream drainage area (highest flood-risk proxy):")
print(ranked[["status", "highway", "waterway", "upa_km2"]].head(15).to_string(index=False))

print(f"\n--- Breakdown by tag status ---")
for status in ["bridge", "ford", "untagged"]:
    subset = ranked[ranked["status"] == status]
    if len(subset):
        print(f"{status:10s}: n={len(subset):3d}  median upa={subset['upa_km2'].median():.2f} km2  "
              f"max upa={subset['upa_km2'].max():.2f} km2")

# Highlight the highest-risk UNTAGGED crossings specifically - these are the ones
# we currently have zero engineering information about, but the data says matter most
untagged_top = ranked[ranked["status"] == "untagged"].head(10)
print(f"\n--- Top 10 UNTAGGED crossings by upstream drainage area (highest unknown risk) ---")
print(untagged_top[["highway", "waterway", "upa_km2"]].to_string(index=False))

# Save the point layer too, with upa attached, for mapping
crossing_points_out = os.path.join(EXTRACT_DIR, "chimanimani_crossings_ranked.gpkg")
crossing_points.drop(columns=["index_right"], errors="ignore").to_file(crossing_points_out, driver="GPKG")
print(f"\n[OK] Ranked crossing points (for mapping) -> {crossing_points_out}")
