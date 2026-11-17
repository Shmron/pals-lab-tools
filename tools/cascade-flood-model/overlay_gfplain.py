"""
Overlay GFPLAIN250m geomorphic floodplain mask against our ranked crossings,
as an independent consistency check on the rainfall x drainage-area risk index.
"""
import os
import geopandas as gpd
import rasterio
from rasterio.windows import from_bounds
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
GFPLAIN_PATH = os.path.join(BASE_DIR, "GFPLAIN", "GFPLAIN TIFF", "AF.TIF")

CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]  # west, south, east, north

crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings_flood_risk.gpkg"))
ranked = crossings.sort_values("flood_risk_index", ascending=False)

print("[*] Opening GFPLAIN250m Africa raster (windowed read, not loading the whole continent) ...")
with rasterio.open(GFPLAIN_PATH) as src:
    print(f"    Full raster: {src.width}x{src.height}, CRS: {src.crs}, nodata: {src.nodata}")
    west, south, east, north = CHIMANIMANI_BBOX
    window = from_bounds(west, south, east, north, src.transform)
    data = src.read(1, window=window)
    win_transform = src.window_transform(window)
    print(f"    Clipped window: {data.shape}, unique values: {np.unique(data)}")

    def sample_gfplain(lon, lat):
        row, col = rasterio.transform.rowcol(win_transform, lon, lat)
        if 0 <= row < data.shape[0] and 0 <= col < data.shape[1]:
            return int(data[row, col])
        return None

    ranked = ranked.copy()
    ranked["gfplain_floodplain"] = [
        sample_gfplain(pt.x, pt.y) for pt in ranked.geometry.centroid
    ]

print("\nGFPLAIN value counts at all 462 crossings:")
print(ranked["gfplain_floodplain"].value_counts(dropna=False).to_string())

print("\nTop 20 crossings by flood_risk_index - does GFPLAIN agree?")
top20 = ranked.head(20)
print(top20[["status", "highway", "waterway", "upa_km2", "flood_risk_index", "gfplain_floodplain"]]
      .to_string(index=False))

n_agree = (top20["gfplain_floodplain"] == 1).sum()
print(f"\n[RESULT] {n_agree}/20 of our top-risk crossings are inside the GFPLAIN geomorphic floodplain")

# Also check across the full ranked set: does gfplain=1 correlate with higher risk index?
in_floodplain = ranked[ranked["gfplain_floodplain"] == 1]
not_in_floodplain = ranked[ranked["gfplain_floodplain"] == 0]
print(f"\nMedian flood_risk_index INSIDE GFPLAIN floodplain (n={len(in_floodplain)}): "
      f"{in_floodplain['flood_risk_index'].median():.2f}")
print(f"Median flood_risk_index OUTSIDE GFPLAIN floodplain (n={len(not_in_floodplain)}): "
      f"{not_in_floodplain['flood_risk_index'].median():.2f}")

out_path = os.path.join(EXTRACT_DIR, "chimanimani_crossings_with_gfplain.csv")
ranked.drop(columns=["geometry"], errors="ignore").to_csv(out_path, index=False)
print(f"\n[OK] Saved -> {out_path}")
