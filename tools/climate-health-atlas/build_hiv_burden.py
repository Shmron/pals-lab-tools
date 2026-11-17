"""
Builds district-level HIV prevalence per DHS survey round.

Joins AR (HIV test results, hiv03: 0=negative/1=positive, others excluded as
indeterminate/missing per DHS convention) to GE (cluster GPS, hivclust ==
dhsclust) within each round, then spatially joins cluster points to district
polygons (FAO GAUL 2015 level2).

Caveat carried into hiv_burden: DHS displaces cluster GPS ~2km urban/5km rural
(10km for 1% of rural clusters) for anonymity, so district assignment is
approximate near boundaries -- same caveat noted in dhs_codebook.py's GE entry.
"""
import os
import duckdb
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "climate_hiv_atlas.duckdb")
PRECISE_DIR = "/home/USER/PRECISE"

# AR survey_code -> (GE survey_code, display label, representative year)
ROUNDS = {
    "ZWAR51": ("ZWGE52", "2005-06", 2005),
    "ZWAR61": ("ZWGE61", "2010-11", 2010),
    "ZWAR71": ("ZWGE72", "2015", 2015),
}

con = duckdb.connect(DB_PATH)
districts = gpd.GeoDataFrame(
    con.execute("SELECT district, geometry_wkt FROM districts").fetchdf(),
    geometry=gpd.GeoSeries.from_wkt(con.execute("SELECT geometry_wkt FROM districts").fetchdf()["geometry_wkt"]),
    crs="EPSG:4326",
)[["district", "geometry"]]

ar_con = duckdb.connect(os.path.join(PRECISE_DIR, "dhs_ar.duckdb"), read_only=True)
ge_con = duckdb.connect(os.path.join(PRECISE_DIR, "dhs_ge.duckdb"), read_only=True)

con.execute("DELETE FROM hiv_burden")

for ar_code, (ge_code, label, year) in ROUNDS.items():
    ar = ar_con.execute(
        "SELECT hivclust, hiv03, hiv05 FROM hiv_results "
        "WHERE survey_code = ? AND hiv03 IN (0, 1)",
        [ar_code],
    ).fetchdf()
    ge = ge_con.execute(
        "SELECT dhsclust, latnum, longnum FROM clusters "
        "WHERE survey_code = ? AND NOT (latnum = 0 AND longnum = 0)",
        [ge_code],
    ).fetchdf()

    merged = ar.merge(ge, left_on="hivclust", right_on="dhsclust", how="inner")
    merged["weight"] = merged["hiv05"] / 1_000_000.0
    merged["is_positive"] = (merged["hiv03"] == 1).astype(int)

    points = gpd.GeoDataFrame(
        merged,
        geometry=[Point(xy) for xy in zip(merged["longnum"], merged["latnum"])],
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(points, districts, how="inner", predicate="within")

    agg = joined.groupby("district").apply(
        lambda g: pd.Series({
            "n_tested": len(g),
            "n_positive": int(g["is_positive"].sum()),
            "prevalence_pct": 100.0 * g["is_positive"].sum() / len(g),
            "prevalence_weighted_pct": 100.0 * (g["is_positive"] * g["weight"]).sum() / g["weight"].sum(),
        }),
        include_groups=False,
    ).reset_index()

    for _, row in agg.iterrows():
        con.execute(
            "INSERT INTO hiv_burden VALUES (?, ?, ?, ?, ?, ?, ?)",
            [row["district"], label, year, int(row["n_tested"]), int(row["n_positive"]),
             row["prevalence_pct"], row["prevalence_weighted_pct"]],
        )

    unmatched = len(points) - len(joined)
    print(f"[OK] {ar_code} ({label}): {len(points)} tested points -> "
          f"{agg.shape[0]} districts covered, {unmatched} points fell outside all district polygons")

print(con.execute("SELECT survey_round, COUNT(*) districts, SUM(n_tested) n, "
                   "ROUND(AVG(prevalence_weighted_pct),2) avg_prev FROM hiv_burden GROUP BY survey_round").fetchdf())
con.close()
