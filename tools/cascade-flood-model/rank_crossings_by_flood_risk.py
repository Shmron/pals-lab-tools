"""
Combine upstream drainage area (MERIT Hydro 'upa') with Cyclone Idai event
rainfall (CHIRPS) into a Rational-Method-style relative flood risk index per
crossing: risk ~ rainfall_mm * upstream_area_km2.

This is NOT a calibrated discharge estimate (no runoff coefficient, no channel
geometry) - it's a relative ranking to decide which crossings plausibly failed
during Idai, grounded in the actual rainfall that fell rather than a synthetic
area-only threshold.
"""
import os
import geopandas as gpd
import rasterio
import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
CHIRPS_PATH = os.path.join(BASE_DIR, "Rainfall", "chimanimani_chirps_idai_total.tif")

crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings_ranked.gpkg"))

# Sample CHIRPS event-total rainfall (mm) at each crossing point
with rasterio.open(CHIRPS_PATH) as src:
    rainfall_band = src.read(1)
    coords = [(pt.x, pt.y) for pt in crossings.geometry]
    rows_cols = [src.index(x, y) for x, y in coords]
    rainfall_values = []
    for row, col in rows_cols:
        if 0 <= row < rainfall_band.shape[0] and 0 <= col < rainfall_band.shape[1]:
            rainfall_values.append(float(rainfall_band[row, col]))
        else:
            rainfall_values.append(np.nan)

crossings["idai_rainfall_mm"] = rainfall_values

# Relative risk index: rainfall (mm) x upstream drainage area (km2)
# Proportional to Rational Method's i x A term (Q = C.i.A) - relative ranking only
crossings["flood_risk_index"] = crossings["idai_rainfall_mm"] * crossings["upa_km2"]

ranked = crossings.drop(columns=["geometry"], errors="ignore").sort_values(
    "flood_risk_index", ascending=False
)
ranked_path = os.path.join(EXTRACT_DIR, "chimanimani_crossings_flood_risk.csv")
ranked.to_csv(ranked_path, index=False)
print(f"[OK] Flood-risk-ranked crossings -> {ranked_path}\n")

print("Top 20 crossings by Idai-event flood risk index (rainfall x upstream area):")
print(ranked[["status", "highway", "waterway", "upa_km2", "idai_rainfall_mm", "flood_risk_index"]]
      .head(20).to_string(index=False))

print(f"\n--- Breakdown by tag status ---")
for status in ["bridge", "ford", "untagged"]:
    subset = ranked[ranked["status"] == status]
    if len(subset):
        print(f"{status:10s}: n={len(subset):3d}  median risk={subset['flood_risk_index'].median():.2f}  "
              f"max risk={subset['flood_risk_index'].max():.2f}")

# Save the geometry-attached version too, for mapping/scenario use
crossings_out = crossings.copy()
out_path = os.path.join(EXTRACT_DIR, "chimanimani_crossings_flood_risk.gpkg")
crossings_out.to_file(out_path, driver="GPKG")
print(f"\n[OK] Full layer (with geometry) -> {out_path}")
