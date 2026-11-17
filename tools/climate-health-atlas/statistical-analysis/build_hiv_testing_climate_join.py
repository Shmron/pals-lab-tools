"""
Individual-level, climate-linked HIV-TESTING dataset for Zimbabwe --
the actual HIV outcome, not the maternal-health one built earlier today.

One row per woman interviewed in a DHS round that asked about HIV
testing (dhs_ir.women), with:
  - district via spatial join of her cluster's GPS (dhs_ge.clusters)
  - climate exposure in the 365 days before HER INTERVIEW DATE (v008,
    converted from DHS's century-month-code) -- 365 days because the
    primary outcome (v828) is itself a "in the past 12 months" recall
    question, so the exposure window should match the outcome's own
    recall frame, not an arbitrary window.
  - a secondary 90-day window for comparison/robustness.

Two outcomes:
  tested_12mo (v828): tested for HIV and received results in the past
    12 months -- the "responsive" marker identified earlier as better
    suited to detecting disruption than slow-moving prevalence.
    Available ZWIR52/62/72 (2005-06, 2010-11, 2015) -- same 3 rounds
    already in the dashboard.
  ever_tested (v781): tested for HIV at any point in her life.
    Available ZWIR42/52/62/72 (adds ~1999). Weaker design for a
    climate-exposure question -- a lifetime outcome doesn't have a
    single natural exposure window the way a 12-month-recall outcome
    does -- kept as a secondary/comparison outcome, not primary.

Kept in the same separate analysis directory as the birth-outcomes
pipeline; never touches the live climate_hiv_atlas app database.
"""
import os
import duckdb
import geopandas as gpd
import pandas as pd
from shapely import wkt as shapely_wkt

BASE_DIR = "/home/USER/PRECISE/climate_hiv_statistical_analysis"
ANALYSIS_DB = os.path.join(BASE_DIR, "birth_climate.duckdb")  # same analysis db, new tables
ATLAS_DB = "/home/USER/PRECISE/climate_hiv_atlas/climate_hiv_atlas.duckdb"
GE_DB = "/home/USER/PRECISE/dhs_ge.duckdb"
IR_DB = "/home/USER/PRECISE/dhs_ir.duckdb"

con = duckdb.connect(ANALYSIS_DB)
con.execute("INSTALL spatial; LOAD spatial;")
con.execute(f"ATTACH '{ATLAS_DB}' AS atlas (READ_ONLY)")
con.execute(f"ATTACH '{GE_DB}' AS gedb (READ_ONLY)")
con.execute(f"ATTACH '{IR_DB}' AS irdb (READ_ONLY)")

print("[1/5] Pulling ZW women with a usable HIV-testing outcome...")
women = con.execute("""
    SELECT survey_code, v001, v002, v003, v005 AS sample_weight, v006, v007, v008,
           v012 AS age, v106 AS education, v190 AS wealth_index, v024 AS region_code,
           v025 AS urban_rural, v021 AS psu,
           v781 AS ever_tested_raw, v828 AS tested_12mo_raw
    FROM irdb.women
    WHERE survey_code LIKE 'ZW%'
      AND (v828 IS NOT NULL OR v781 IN (0, 1))
""").fetchdf()
print(f"      {len(women)} women")

print("[2/5] Spatial-joining DHS clusters to districts (via GE cluster GPS)...")
districts = con.execute("SELECT district, province, geometry_wkt FROM atlas.districts").fetchdf()
districts["geometry"] = districts["geometry_wkt"].apply(shapely_wkt.loads)
districts_gdf = gpd.GeoDataFrame(districts, geometry="geometry", crs="EPSG:4326")

ge = con.execute("""
    SELECT survey_code AS ge_code, dhsclust AS v001, latnum, longnum
    FROM gedb.clusters WHERE survey_code LIKE 'ZW%'
""").fetchdf()
# GE and IR don't share a simple suffix convention for every round -- the
# 2010-11 wave is 'ZWGE61' in the geo file but 'ZWIR62' in the individual
# recode (verified against dhsyear/v007, not assumed). Explicit mapping,
# not a string replace, so a one-off naming quirk like this can't silently
# drop a whole round the way it did on the first pass of this script.
GE_TO_IR = {"ZWGE42": "ZWIR42", "ZWGE52": "ZWIR52", "ZWGE61": "ZWIR62", "ZWGE72": "ZWIR72"}
ge["survey_code"] = ge["ge_code"].map(GE_TO_IR)
ge = ge.dropna(subset=["survey_code"])
women = women.merge(ge[["survey_code", "v001", "latnum", "longnum"]], on=["survey_code", "v001"], how="left")
women = women.dropna(subset=["latnum", "longnum"])
women = women[~((women["latnum"] == 0) & (women["longnum"] == 0))]
print(f"      {len(women)} women with GPS")

clusters = women[["survey_code", "v001", "latnum", "longnum"]].drop_duplicates()
clusters["geometry"] = clusters.apply(lambda r: shapely_wkt.loads(f"POINT({r.longnum} {r.latnum})"), axis=1)
clusters_gdf = gpd.GeoDataFrame(clusters, geometry="geometry", crs="EPSG:4326")
joined = gpd.sjoin(clusters_gdf, districts_gdf[["district", "province", "geometry"]], how="left", predicate="within")
print(f"      {len(clusters)} distinct clusters, {joined['district'].isna().sum()} unmatched")

women = women.merge(joined[["survey_code", "v001", "district", "province"]], on=["survey_code", "v001"], how="left")
women = women.dropna(subset=["district"])
print(f"      {len(women)} women retained after district match")

print("[3/5] Converting DHS century-month-code (v008) to an interview date...")
# DHS CMC: v008 = (year-1900)*12 + month. Day set to 15 (DHS's own
# mid-month convention, same as birth_date_approx in the births pipeline).
women["interview_year"] = 1900 + ((women["v008"] - 1) // 12)
women["interview_month"] = ((women["v008"] - 1) % 12) + 1
women["interview_date"] = pd.to_datetime(
    dict(year=women["interview_year"], month=women["interview_month"], day=15)
)

print("[4/5] Attaching climate exposure (365d and 90d pre-interview)...")
con.register("women_tmp", women.drop(columns=["geometry"], errors="ignore"))
con.execute("DROP TABLE IF EXISTS women_with_district")
con.execute("CREATE TABLE women_with_district AS SELECT *, row_number() OVER () AS woman_row_id FROM women_tmp")

exposure = con.execute("""
    SELECT w.woman_row_id,
           COUNT(*) FILTER (WHERE c.tmax_c >= t.heat_threshold_c AND c.date >= w.interview_date - INTERVAL 365 DAY) AS heat_days_365d,
           COUNT(*) FILTER (WHERE c.precip_mm >= 20 AND c.date >= w.interview_date - INTERVAL 365 DAY) AS heavy_rain_days_365d,
           COUNT(*) FILTER (WHERE c.tmax_c >= t.heat_threshold_c AND c.date >= w.interview_date - INTERVAL 90 DAY) AS heat_days_90d,
           COUNT(*) FILTER (WHERE c.precip_mm >= 20 AND c.date >= w.interview_date - INTERVAL 90 DAY) AS heavy_rain_days_90d
    FROM women_with_district w
    JOIN atlas.climate_daily c ON c.district = w.district AND c.date BETWEEN (w.interview_date - INTERVAL 365 DAY) AND w.interview_date
    JOIN atlas.district_thresholds t ON t.district = w.district
    GROUP BY w.woman_row_id
""").fetchdf()
print(f"      exposure computed for {len(exposure)} women")

final = con.execute("""
    SELECT w.*, e.heat_days_365d, e.heavy_rain_days_365d, e.heat_days_90d, e.heavy_rain_days_90d
    FROM women_with_district w JOIN exposure e ON e.woman_row_id = w.woman_row_id
""").fetchdf()

final["ever_tested"] = None
final.loc[final["ever_tested_raw"].isin([0, 1]), "ever_tested"] = final.loc[final["ever_tested_raw"].isin([0, 1]), "ever_tested_raw"]

# tested_12mo (v828, "received results in the past 12 months") is only
# ASKED of women who said yes to ever_tested (v781=1) -- DHS's own skip
# pattern, confirmed against the raw data (v828 is null for every v781=0
# row). Taking v828 at face value as the outcome -- as this script's first
# version did -- silently restricts the population to already-tested
# women and reads as a 96% past-year testing rate, which is not a real
# population figure. The correct population-level construction: never-
# tested women are a real 0 (not missing), ever-tested women get v828's
# answer, and only true don't-know on v781 itself is left missing.
final["tested_12mo"] = None
final.loc[final["ever_tested_raw"] == 0, "tested_12mo"] = 0
final.loc[final["ever_tested_raw"] == 1, "tested_12mo"] = pd.to_numeric(final.loc[final["ever_tested_raw"] == 1, "tested_12mo_raw"], errors="coerce")

print(f"[5/5] Writing {len(final)} rows (table: analysis_hiv_testing) + CSV...")
con.execute("DROP TABLE IF EXISTS analysis_hiv_testing")
con.register("final_tmp", final)
con.execute("CREATE TABLE analysis_hiv_testing AS SELECT * FROM final_tmp")
final.to_csv(os.path.join(BASE_DIR, "analysis_hiv_testing.csv"), index=False)

print("\n=== Sanity checks ===")
print("Rows by round:", final["survey_code"].value_counts().to_dict())
print("tested_12mo non-null:", final["tested_12mo"].notna().sum(), "/ mean:", final["tested_12mo"].mean())
print("ever_tested non-null:", final["ever_tested"].notna().sum(), "/ mean:", pd.to_numeric(final["ever_tested"]).mean())
print("heat_days_365d: min/mean/max =", final["heat_days_365d"].min(), final["heat_days_365d"].mean(), final["heat_days_365d"].max())
print("age non-null:", final["age"].notna().sum(), "/ wealth_index non-null:", final["wealth_index"].notna().sum())
con.close()
