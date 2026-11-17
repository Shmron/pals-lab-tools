"""
Rebuilds Population/zimbabwe_dhs2015_wealth_clusters.gpkg from Zimbabwe's
actual 2015 DHS data already on this server, since the original gpkg is
gone. Not a substitute dataset -- the real source.

Joins dhs_ge_parquet/ZWGE72.parquet (400 cluster GPS points: dhsclust,
latnum, longnum) to dhs_ir_parquet/ZWIR72.parquet (v001=cluster number,
v191=wealth index factor score combined, scaled by 1e5 per DHS convention)
via dhsclust == v001, averaging v191 per cluster -> mean_wealth.

400 clusters in both files, confirming this is the same 2015 Zimbabwe DHS
round flood_accessibility_pipeline.py's docstring cites ("r=0.837, n=400
clusters").
"""
import os
import pandas as pd
import geopandas as gpd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GE_PATH = "/home/USER/PRECISE/dhs_ge_parquet/ZWGE72.parquet"
IR_PATH = "/home/USER/PRECISE/dhs_ir_parquet/ZWIR72.parquet"
OUT_PATH = os.path.join(BASE_DIR, "Population", "zimbabwe_dhs2015_wealth_clusters.gpkg")


def main():
    ge = pd.read_parquet(GE_PATH, columns=["dhsclust", "latnum", "longnum"])
    ir = pd.read_parquet(IR_PATH, columns=["v001", "v191"])

    # DHS wealth factor score is stored scaled by 1e5 (negative values are
    # valid -- it's a relative factor score, not a bounded index).
    cluster_wealth = ir.groupby("v001")["v191"].mean().div(1e5).rename("mean_wealth")

    merged = ge.merge(cluster_wealth, left_on="dhsclust", right_index=True, how="inner")
    print(f"[*] {len(merged)} clusters matched (GE: {len(ge)}, IR clusters: {ir['v001'].nunique()})")

    gdf = gpd.GeoDataFrame(
        merged[["dhsclust", "mean_wealth"]],
        geometry=gpd.points_from_xy(merged["longnum"], merged["latnum"]),
        crs="EPSG:4326",
    )
    gdf.to_file(OUT_PATH, driver="GPKG")
    print(f"[OK] {len(gdf)} clusters -> {OUT_PATH}")
    print(gdf["mean_wealth"].describe())


if __name__ == "__main__":
    main()
