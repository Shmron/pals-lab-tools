"""
1. Collapse the 462 road x waterway intersection PAIRS into distinct physical
   crossing LOCATIONS (cluster nearby duplicates from OSM way-splitting).
2. Build a standalone bridges/fords layer directly from the road tags, with
   OSM's bridge sub-type categorization (bridge=low_water_crossing is
   functionally ford-like despite being tagged under 'bridge').
"""
import os
import geopandas as gpd
import pandas as pd
import numpy as np
from sklearn.cluster import DBSCAN

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")

CLUSTER_DISTANCE_M = 30  # crossings within 30m are treated as the same physical location

# ---------------------------------------------------------------------------
# Part 1: deduplicate intersection pairs into distinct locations
# ---------------------------------------------------------------------------
crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings_flood_risk.gpkg"))
print(f"[i] Starting from {len(crossings)} intersection PAIRS")

pts = crossings.copy()
pts["geometry"] = pts.geometry.centroid
pts_m = pts.to_crs("EPSG:32736")  # UTM 36S, meters

coords = np.array([[g.x, g.y] for g in pts_m.geometry])
clustering = DBSCAN(eps=CLUSTER_DISTANCE_M, min_samples=1).fit(coords)
pts["cluster_id"] = clustering.labels_
n_clusters = pts["cluster_id"].nunique()
print(f"[OK] Collapsed into {n_clusters} distinct physical crossing locations "
      f"(within {CLUSTER_DISTANCE_M}m of each other = same location)")


def best_status(statuses, bridges, fords):
    # Priority: ford or bridge=low_water_crossing (functionally at-grade/floodable)
    # > generic bridge > untagged. Report the specific tag value too.
    for b in bridges:
        if b == "low_water_crossing":
            return "ford-like (bridge=low_water_crossing)", b
    if any(pd.notna(f) and f != "" for f in fords):
        f_val = next(f for f in fords if pd.notna(f) and f != "")
        return "ford", f_val
    if any(pd.notna(b) and b != "" for b in bridges):
        b_val = next(b for b in bridges if pd.notna(b) and b != "")
        return "bridge", b_val
    return "untagged", None


records = []
for cid, group in pts.groupby("cluster_id"):
    status, tag_value = best_status(
        group["status"].tolist(),
        group["bridge"].tolist() if "bridge" in group.columns else [None] * len(group),
        group["ford"].tolist() if "ford" in group.columns else [None] * len(group),
    )
    rep_geom = group.geometry.iloc[0]
    records.append({
        "cluster_id": cid,
        "n_original_pairs": len(group),
        "status": status,
        "tag_value": tag_value,
        "highway": group["highway"].mode().iloc[0] if "highway" in group.columns else None,
        "waterway": group["waterway"].mode().iloc[0] if "waterway" in group.columns else None,
        "upa_km2": group["upa_km2"].max(),
        "idai_rainfall_mm": group["idai_rainfall_mm"].mean(),
        "geometry": rep_geom,
    })

distinct = gpd.GeoDataFrame(records, crs="EPSG:4326")
distinct["flood_risk_index"] = distinct["idai_rainfall_mm"] * distinct["upa_km2"]
distinct = distinct.sort_values("flood_risk_index", ascending=False)

print(f"\nHow many original pairs collapsed per location (top offenders):")
print(distinct.nlargest(5, "n_original_pairs")[["status", "n_original_pairs", "upa_km2"]].to_string(index=False))

print(f"\nDistinct locations by status:")
print(distinct["status"].value_counts().to_string())

print(f"\nTop 20 distinct crossing locations by flood risk index:")
print(distinct.head(20)[["status", "tag_value", "highway", "waterway", "upa_km2",
                           "idai_rainfall_mm", "flood_risk_index", "n_original_pairs"]].to_string(index=False))

out_path = os.path.join(EXTRACT_DIR, "chimanimani_crossings_distinct.gpkg")
distinct.to_file(out_path, driver="GPKG")
print(f"\n[OK] Saved {len(distinct)} distinct crossing locations -> {out_path}")

# ---------------------------------------------------------------------------
# Part 2: standalone bridges/fords layer, directly from road tags
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("STANDALONE BRIDGES/FORDS LAYER (from road segment tags directly)")
print("=" * 70)
roads = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_roads.gpkg"))

bridges = roads[roads["bridge"].notna() & (roads["bridge"] != "")].copy()
fords = roads[roads["ford"].notna() & (roads["ford"] != "")].copy()

print(f"\n[i] {len(bridges)} road segments tagged bridge=* "
      f"(the segment geometry itself IS the bridge alignment - not a separate point)")
print(bridges["bridge"].value_counts().to_string())

print(f"\n[i] {len(fords)} road segments tagged ford=* "
      f"(geometry is the specific point/short stretch that crosses at-grade)")
print(fords["ford"].value_counts().to_string())

n_low_water = (bridges["bridge"] == "low_water_crossing").sum()
if n_low_water:
    print(f"\n[!] {n_low_water} bridge(s) tagged 'low_water_crossing' specifically - "
          f"functionally ford-like (gets submerged) despite being under the 'bridge' key, "
          f"not a generic elevated bridge=yes")

bridges_out = os.path.join(EXTRACT_DIR, "chimanimani_bridges.gpkg")
fords_out = os.path.join(EXTRACT_DIR, "chimanimani_fords.gpkg")
if len(bridges):
    bridges.to_file(bridges_out, driver="GPKG")
    print(f"\n[OK] Bridges layer -> {bridges_out}")
if len(fords):
    fords.to_file(fords_out, driver="GPKG")
    print(f"[OK] Fords layer -> {fords_out}")
