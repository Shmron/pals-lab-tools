"""
Builds an individual-level, climate-linked birth-outcomes dataset for
Zimbabwe: one row per DHS birth (1990+, Birth Recode rounds with GPS --
ZWBR42/52/62/72), with climate exposure in the 90 days before birth
attached by district, and maternal/household controls merged in from the
matching Individual Recode (IR) record.

This is a statistical-analysis pipeline, kept deliberately separate from
the live climate_hiv_atlas Flask app/db (different deliverable, different
update cadence -- never touches the production duckdb file).

Design, in order:
1. Pull ZW births with GPS + a usable outcome (ANC visits m14 or place of
   delivery m15) from dhs_br_geo.cohort_births, birth_date_approx>=1990
   (climate_daily's own coverage start).
2. Spatial-join each birth's DHS cluster GPS to a Zimbabwe district
   (clusters repeat across many births -- join distinct clusters once).
3. Attach 90-days-pre-birth climate exposure per district via a date-range
   join against climate_daily, using each district's own TX90p heat
   threshold (same convention the dashboard uses, not a flat cutoff).
4. Merge mother/household controls from dhs_ir.women via
   (survey_code, v001, v002, v003) -- same cluster/household/respondent
   key DHS uses to link BR and IR records for the same woman.
5. Derive the two outcomes and write an analysis-ready table + CSV.

Caveats carried forward (same as the dashboard's own documented ones):
DHS cluster GPS is displaced ~2km urban / ~5km rural (10km for 1% of
rural clusters) for anonymity, so district assignment is approximate near
boundaries. birth_date_approx is DHS's own reconstructed date (month
precision -- DHS collects month+year of birth, not exact day; the "15th"
seen in the data is a DHS convention for mid-month, not a real day of
birth), so the 90-day exposure window is month-resolution, not day-exact.
"""
import os
import duckdb
import geopandas as gpd
import pandas as pd
from shapely import wkt as shapely_wkt

BASE_DIR = "/home/USER/PRECISE/climate_hiv_statistical_analysis"
os.makedirs(BASE_DIR, exist_ok=True)
ANALYSIS_DB = os.path.join(BASE_DIR, "birth_climate.duckdb")
ATLAS_DB = "/home/USER/PRECISE/climate_hiv_atlas/climate_hiv_atlas.duckdb"
BRGEO_DB = "/home/USER/PRECISE/dhs_br_geo.duckdb"
IR_DB = "/home/USER/PRECISE/dhs_ir.duckdb"

con = duckdb.connect(ANALYSIS_DB)
con.execute("INSTALL spatial; LOAD spatial;")
con.execute(f"ATTACH '{ATLAS_DB}' AS atlas (READ_ONLY)")
con.execute(f"ATTACH '{BRGEO_DB}' AS brgeo (READ_ONLY)")
con.execute(f"ATTACH '{IR_DB}' AS irdb (READ_ONLY)")

print("[1/5] Pulling ZW births 1990+ with a usable outcome and GPS...")
births = con.execute("""
    SELECT survey_code, v001, v002, v003, latnum, longnum, urban_rura,
           v025 AS urban_rural, v023 AS stratum, v005 AS sample_weight,
           v021 AS psu, dhsyear, birth_date_approx, m14 AS anc_visits_raw,
           m15 AS delivery_place_raw
    FROM brgeo.cohort_births
    WHERE survey_code LIKE 'ZW%'
      AND birth_date_approx >= '1990-01-01'
      AND (m14 IS NOT NULL OR m15 IS NOT NULL)
      AND latnum IS NOT NULL AND longnum IS NOT NULL
      AND NOT (latnum = 0 AND longnum = 0)
""").fetchdf()
print(f"      {len(births)} births")

print("[2/5] Spatial-joining DHS clusters to districts...")
districts = con.execute("SELECT district, province, geometry_wkt FROM atlas.districts").fetchdf()
districts["geometry"] = districts["geometry_wkt"].apply(shapely_wkt.loads)
districts_gdf = gpd.GeoDataFrame(districts, geometry="geometry", crs="EPSG:4326")

clusters = births[["survey_code", "v001", "latnum", "longnum"]].drop_duplicates()
clusters["geometry"] = clusters.apply(lambda r: shapely_wkt.loads(f"POINT({r.longnum} {r.latnum})"), axis=1)
clusters_gdf = gpd.GeoDataFrame(clusters, geometry="geometry", crs="EPSG:4326")
joined = gpd.sjoin(clusters_gdf, districts_gdf[["district", "province", "geometry"]], how="left", predicate="within")
unmatched = joined["district"].isna().sum()
print(f"      {len(clusters)} distinct clusters, {unmatched} unmatched to any district (likely just outside a polygon boundary)")

cluster_district = joined[["survey_code", "v001", "district", "province"]]
births = births.merge(cluster_district, on=["survey_code", "v001"], how="left")
births = births.dropna(subset=["district"])
print(f"      {len(births)} births retained after district match")

print("[3/5] Attaching 90-days-pre-birth climate exposure per district...")
con.register("births_tmp", births)
con.execute("DROP TABLE IF EXISTS births_with_district")
con.execute("CREATE TABLE births_with_district AS SELECT *, row_number() OVER () AS birth_row_id FROM births_tmp")

exposure = con.execute("""
    SELECT b.birth_row_id,
           COUNT(*) FILTER (WHERE c.tmax_c >= t.heat_threshold_c) AS heat_days_90d,
           COUNT(*) FILTER (WHERE c.precip_mm >= 20) AS heavy_rain_days_90d,
           ROUND(AVG(c.tmax_c), 1) AS mean_tmax_90d,
           ROUND(SUM(c.precip_mm), 1) AS total_precip_90d
    FROM births_with_district b
    JOIN atlas.climate_daily c
      ON c.district = b.district
     AND c.date BETWEEN (b.birth_date_approx - INTERVAL 90 DAY) AND b.birth_date_approx
    JOIN atlas.district_thresholds t ON t.district = b.district
    GROUP BY b.birth_row_id
""").fetchdf()
print(f"      exposure computed for {len(exposure)} births")

print("[4/5] Merging maternal/household controls from the matching IR record...")
# dhs_ir.women's survey_code is already e.g. 'ZWIR72' -- the BR->IR swap
# belongs on the BIRTHS side ('ZWBR72' -> 'ZWIR72') to match it, not here.
ir_controls = con.execute("""
    SELECT survey_code, v001, v002, v003,
           v012 AS mother_age, v106 AS mother_education, v190 AS wealth_index,
           v024 AS region_code
    FROM irdb.women
    WHERE survey_code LIKE 'ZW%'
""").fetchdf()

final = con.execute("""
    SELECT b.*, e.heat_days_90d, e.heavy_rain_days_90d, e.mean_tmax_90d, e.total_precip_90d,
           replace(b.survey_code, 'BR', 'IR') AS ir_survey_code
    FROM births_with_district b
    JOIN exposure e ON e.birth_row_id = b.birth_row_id
""").fetchdf()
final = final.merge(ir_controls, left_on=["ir_survey_code", "v001", "v002", "v003"],
                     right_on=["survey_code", "v001", "v002", "v003"], how="left", suffixes=("", "_ir"))
final = final.drop(columns=["ir_survey_code", "survey_code_ir"])

# Derived outcomes, standard DHS conventions:
# facility_delivery: m15 >=20 = any health facility (govt or private);
# 11/12 = respondent's/other home; 98 = don't know -> missing.
final["facility_delivery"] = None
final.loc[final["delivery_place_raw"].isin([11, 12]), "facility_delivery"] = 0
final.loc[(final["delivery_place_raw"] >= 20) & (final["delivery_place_raw"] < 90), "facility_delivery"] = 1
# anc_4plus: WHO's old minimum-contact standard (4+ visits); 98 = don't know -> missing.
final["anc_4plus"] = None
anc_mask = (final["anc_visits_raw"] >= 0) & (final["anc_visits_raw"] < 90)
final.loc[anc_mask, "anc_4plus"] = (final.loc[anc_mask, "anc_visits_raw"] >= 4).astype(int)

print(f"[5/5] Writing {len(final)} rows to {ANALYSIS_DB} (table: analysis_births) and a CSV copy...")
con.execute("DROP TABLE IF EXISTS analysis_births")
con.register("final_tmp", final)
con.execute("CREATE TABLE analysis_births AS SELECT * FROM final_tmp")
final.to_csv(os.path.join(BASE_DIR, "analysis_births.csv"), index=False)

print()
print("=== Sanity checks ===")
print("Rows by round:", final["survey_code"].value_counts().to_dict())
print("facility_delivery non-null:", final["facility_delivery"].notna().sum(), "/ mean:", final["facility_delivery"].astype(float).mean())
print("anc_4plus non-null:", final["anc_4plus"].notna().sum(), "/ mean:", final["anc_4plus"].astype(float).mean())
print("heat_days_90d: min/mean/max =", final["heat_days_90d"].min(), final["heat_days_90d"].mean(), final["heat_days_90d"].max())
print("mother_age non-null:", final["mother_age"].notna().sum(), "/ wealth_index non-null:", final["wealth_index"].notna().sum())
con.close()
