"""
Extract cluster-level wealth-index ground truth from the user's own local
Zimbabwe DHS 2015 download (ZWHR72FL household recode + ZWGE72FL geolocated
cluster GPS points). Round 72 = 2015, the most recent Zimbabwe DHS available
(no later full DHS exists) - earlier rounds (42/52/61) are NOT merged in
since DHS wealth-index construction isn't methodologically comparable across
rounds.

hv271 ("Wealth index factor score combined", continuous) is averaged per
cluster (hv001) and joined to that cluster's GPS point (hv001 == DHSCLUST).
GPS coordinates are DHS-displaced for respondent privacy (rural clusters up
to 5km, 1% up to 10km; urban up to 2km - see the GE folder's own
GPS_Displacement_README.txt) - a real, bounded positional uncertainty, kept
in mind rather than treated as exact.

Output: Population/zimbabwe_dhs2015_wealth_clusters.gpkg
"""
import os
import geopandas as gpd
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "Population")
os.makedirs(OUT_DIR, exist_ok=True)
OUT_PATH = os.path.join(OUT_DIR, "zimbabwe_dhs2015_wealth_clusters.gpkg")

DHS_DIR = r"PATH/TO/LOCAL/DATA"
HR_DTA = os.path.join(DHS_DIR, "HR", "ZWHR72DT", "ZWHR72FL.DTA")
GE_SHP = os.path.join(DHS_DIR, "GE", "ZWGE72FL", "ZWGE72FL.shp")

print("[*] Loading household recode (ZWHR72FL) hv001/hv271 ...")
hr = pd.read_stata(HR_DTA, columns=["hv001", "hv271"], convert_categoricals=False)
print(f"[OK] {len(hr):,} households across {hr['hv001'].nunique()} clusters")

cluster_wealth = hr.groupby("hv001")["hv271"].mean().reset_index()
cluster_wealth.columns = ["DHSCLUST", "mean_wealth"]

print("[*] Loading cluster GPS points (ZWGE72FL) ...")
ge = gpd.read_file(GE_SHP)[["DHSCLUST", "LATNUM", "LONGNUM", "URBAN_RURA", "ADM1NAME", "geometry"]]
print(f"[OK] {len(ge)} cluster points, survey year {ge['DHSCLUST'].count()} rows")

n_missing_gps = int((ge["LATNUM"] == 0).sum())
if n_missing_gps:
    print(f"[!] Dropping {n_missing_gps} cluster(s) with LATNUM==0 (DHS's sentinel for suppressed/missing coordinates)")
ge = ge[ge["LATNUM"] != 0].copy()

merged = ge.merge(cluster_wealth, on="DHSCLUST", how="inner")
print(f"[OK] {len(merged)} clusters with both GPS and wealth data (of {len(ge)} geolocated, {len(cluster_wealth)} with wealth)")

merged.to_file(OUT_PATH, driver="GPKG")
print(f"[OK] Saved -> {OUT_PATH}")
print(f"\nWealth score (hv271) range: {merged['mean_wealth'].min():.0f} to {merged['mean_wealth'].max():.0f}")
print(f"Urban/rural split: {merged['URBAN_RURA'].value_counts().to_dict()}")
