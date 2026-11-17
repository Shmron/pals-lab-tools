"""
Generate workshop data for GeoAI Climate, Environment & Health course.
Uses real Zimbabwe DHS 2015 (ZWBR72) birth records as the health dataset
and generates realistic climate exposure grids calibrated to Zimbabwe's
actual seasonal patterns and 2015 El Niño drought conditions.

Outputs (written to workshop/data/):
  zw_births.csv          — individual birth records (400 clusters, ZW 2015)
  zw_clusters.csv        — cluster-level summary with outcome rates
  zw_climate_monthly.csv — monthly environmental data per cluster (2011–2015)
  zw_births_env.csv      — births joined to trimester-level environmental exposures
  exposures_zw.duckdb    — DuckDB with all tables for NB06
  health_facilities.csv  — Zimbabwe health facility locations (revised)
"""

import numpy as np
import pandas as pd
import duckdb
import pathlib
import sys

WORKSHOP_DIR = pathlib.Path(__file__).parent
DATA_DIR = WORKSHOP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_GEO = pathlib.Path(__file__).parent.parent / "dhs_br_geo.duckdb"
DB_BR  = pathlib.Path(__file__).parent.parent / "dhs_br.duckdb"

RNG = np.random.default_rng(42)

# ── 1. Extract real birth records ────────────────────────────────────────────

print("Extracting Zimbabwe DHS 2015 birth records ...")

con_geo = duckdb.connect(str(DB_GEO), read_only=True)
con_br  = duckdb.connect(str(DB_BR),  read_only=True)

geo = con_geo.execute("""
SELECT caseid, bidx, v001 AS cluster_id, v005 AS sample_weight,
       b3 AS birth_cmc, b4 AS child_sex,
       b5 AS child_alive, b7 AS age_at_death_months,
       b11 AS preceding_birth_interval, b20 AS gestational_months,
       m18 AS birth_size_reported, m19 AS birth_weight_g,
       m14 AS anc_total_visits,
       m15 AS place_of_delivery,
       latnum AS cluster_lat, longnum AS cluster_lon,
       urban_rura, dhsregna AS province
FROM br_geo
WHERE country_code='ZW' AND survey_code='ZWBR72'
  AND latnum IS NOT NULL AND ABS(latnum) > 0.01
""").fetchdf()

br = con_br.execute("""
SELECT caseid, bidx,
       v012 AS mother_age,
       v106 AS mother_education,
       v190 AS wealth_quintile,
       bord  AS birth_order,
       v025  AS residence_type
FROM births WHERE survey_code='ZWBR72'
""").fetchdf()

births = geo.merge(br, on=["caseid", "bidx"], how="left")
con_geo.close()
con_br.close()

# ── 2. Derive clean outcome variables ────────────────────────────────────────

# Century-month code (CMC) → year and month: CMC = (year-1900)*12 + month
births["birth_year"]  = 1900 + (births["birth_cmc"] - 1) // 12
births["birth_month"] = ((births["birth_cmc"] - 1) % 12) + 1

# Conception year/month (~9 months before birth)
births["conception_cmc"]   = births["birth_cmc"] - 9
births["conception_year"]  = 1900 + (births["conception_cmc"] - 1) // 12
births["conception_month"] = ((births["conception_cmc"] - 1) % 12) + 1

# Low birth weight (WHO: <2500 g; exclude "don't know" = 9999)
births["birth_weight_g"] = births["birth_weight_g"].where(
    (births["birth_weight_g"] > 100) & (births["birth_weight_g"] != 9999)
)
births["low_birth_weight"] = (births["birth_weight_g"] < 2500).astype("Int8")
births.loc[births["birth_weight_g"].isna(), "low_birth_weight"] = pd.NA

# Neonatal death (died within 28 days; DHS: b7 == 0 = died in month 0)
births["neonatal_death"] = (
    (births["child_alive"] == 0) & (births["age_at_death_months"] == 0)
).astype(int)

# Small for gestational age proxy (mother-reported size: 4=smaller, 5=very small)
births["small_at_birth"] = births["birth_size_reported"].isin([4, 5]).astype(int)

# Preterm proxy (gestational months < 9; b20 only in most recent births)
births["preterm"] = pd.NA
mask_ga = births["gestational_months"].notna()
births.loc[mask_ga, "preterm"] = (births.loc[mask_ga, "gestational_months"] < 9).astype("Int8")

# ANC 4+ visits (WHO recommendation; m14=number of ANC visits; coded 0-20, 98=dk)
births["anc_4plus"] = pd.NA
anc_mask = births["anc_total_visits"].notna() & (births["anc_total_visits"] < 90)
births.loc[anc_mask, "anc_4plus"] = (births.loc[anc_mask, "anc_total_visits"] >= 4).astype("Int8")

# Facility delivery (codes 20+ = facility; 11/12 = home)
births["facility_delivery"] = pd.NA
del_mask = births["place_of_delivery"].notna()
births.loc[del_mask, "facility_delivery"] = (
    births.loc[del_mask, "place_of_delivery"] >= 20
).astype("Int8")

# Encode categoricals
births["sex"]       = births["child_sex"].map({1: "male", 2: "female"})
births["urban"]     = births["residence_type"].map({1: 1, 2: 0})
births["education"] = births["mother_education"].map(
    {0: "none", 1: "primary", 2: "secondary", 3: "higher"}
)
births["province"]  = births["province"].str.strip()

# ── 3. Filter to 2011–2015 births (recent, birth weight recorded) ────────────

recent = births[births["birth_year"] >= 2011].copy()
print(f"  Total births: {len(births):,}  |  2011-2015: {len(recent):,}")
print(f"  LBW (2011-15): {recent['low_birth_weight'].sum():.0f}/"
      f"{recent['low_birth_weight'].notna().sum():.0f} "
      f"({100*recent['low_birth_weight'].mean():.1f}%)")
print(f"  Neonatal deaths: {recent['neonatal_death'].sum()} "
      f"({100*recent['neonatal_death'].mean():.1f}%)")
print(f"  Clusters: {recent['cluster_id'].nunique()}")

# ── 4. Build cluster reference table ─────────────────────────────────────────

cluster_cols = ["cluster_id", "cluster_lat", "cluster_lon", "province",
                "urban_rura", "urban"]

cluster_ref = (
    recent.groupby("cluster_id")
    .agg(
        cluster_lat=("cluster_lat", "first"),
        cluster_lon=("cluster_lon", "first"),
        province=("province", "first"),
        urban_rura=("urban_rura", "first"),
        urban=("urban", "first"),
        n_births=("birth_cmc", "count"),
        lbw_n=("low_birth_weight", "sum"),
        lbw_denom=("low_birth_weight", lambda x: x.notna().sum()),
        neonatal_deaths=("neonatal_death", "sum"),
        small_births=("small_at_birth", "sum"),
    )
    .reset_index()
)
cluster_ref["lbw_rate"] = (cluster_ref["lbw_n"] / cluster_ref["lbw_denom"]).round(3)
cluster_ref["neonatal_mr"] = (cluster_ref["neonatal_deaths"] / cluster_ref["n_births"]).round(3)

# ── 5. Generate realistic monthly climate data per cluster ───────────────────
# Calibrated to Zimbabwe seasonal norms and 2015 El Niño:
#   - Rainy / warm:  Oct–Apr  (higher rainfall, higher temperature)
#   - Dry / cool:    May–Sep  (minimal rain, cooler nights)
# El Niño 2015 effect: ~20% rainfall deficit, +0.5–1 °C anomaly, reduced NDVI
# Spatial gradients:
#   - Southern ZW (lower lat) → hotter, drier
#   - Eastern Highlands (lon > 32) → cooler, wetter
#   - Urban clusters → slightly higher LST (urban heat island)

print("Generating monthly climate data ...")

def generate_cluster_climate(cluster_id, lat, lon, urban, n_months=60):
    """Monthly climate for one cluster, 2011-01 through 2015-12."""
    years  = sorted([2011]*12 + [2012]*12 + [2013]*12 + [2014]*12 + [2015]*12)
    months = list(range(1, 13)) * 5

    rows = []
    for yr, mo in zip(years, months):

        # -- Temperature (LST day, °C) --
        # seasonal cycle: peak ~Dec/Jan (summer), trough ~Jul (winter)
        season_temp  = 26.0 - 5.0 * np.cos(2 * np.pi * (mo - 1) / 12)
        lat_gradient = (lat + 22) * 0.3          # south = warmer
        elev_adjust  = -max(0, (lon - 31.5) * 1.2)  # eastern highlands = cooler
        uhi           = 1.2 * urban               # urban heat island
        elnino_2015   = 0.8 if yr == 2015 else 0.0
        base_temp     = season_temp + lat_gradient + elev_adjust + uhi + elnino_2015
        lst_day_c     = round(base_temp + RNG.normal(0, 0.8), 2)

        # -- Rainfall (mm/month) --
        # rainy: Oct(10)–Apr(4); peak Jan/Feb
        if mo in [11, 12, 1, 2, 3]:
            rain_base = 120.0 + 30 * np.sin(np.pi * (mo % 12) / 4)
        elif mo in [4, 10]:
            rain_base = 55.0
        else:
            rain_base = 8.0
        # spatial: south drier, east wetter
        rain_lat  = (lat + 22) * (-3.0)   # south = drier
        rain_lon  = max(0, (lon - 31.5) * 20)  # eastern highlands = wetter
        elnino_rain = -0.25 * rain_base if yr == 2015 else 0.0
        rain_mm   = max(0, rain_base + rain_lat + rain_lon + elnino_rain
                        + RNG.normal(0, rain_base * 0.15))
        chirps_mm = round(rain_mm, 1)

        # -- NDVI --
        ndvi_base  = 0.35 + 0.25 * np.sin(2 * np.pi * (mo - 3) / 12)
        ndvi_lat   = (lat + 22) * (-0.008)    # south = sparser veg
        ndvi_lon   = max(0, (lon - 31.5) * 0.04)  # eastern highlands greener
        ndvi_urban = -0.08 * urban
        elnino_ndvi = -0.06 if yr == 2015 else 0.0
        ndvi = min(0.85, max(0.05,
                   ndvi_base + ndvi_lat + ndvi_lon + ndvi_urban + elnino_ndvi
                   + RNG.normal(0, 0.02)))
        ndvi = round(ndvi, 3)

        # -- Heat stress index (approximate Wet Bulb Globe Temp proxy) --
        humidity_factor = chirps_mm / 150  # rough humidity proxy
        heat_index = round(lst_day_c + 2.5 * humidity_factor + RNG.normal(0, 0.3), 2)

        rows.append({
            "cluster_id": cluster_id, "year": yr, "month": mo,
            "lst_day_c": lst_day_c, "chirps_mm": chirps_mm,
            "ndvi": ndvi, "heat_index": heat_index,
        })
    return rows

all_climate = []
for _, row in cluster_ref.iterrows():
    all_climate.extend(generate_cluster_climate(
        row["cluster_id"], row["cluster_lat"], row["cluster_lon"], row["urban"]
    ))

climate = pd.DataFrame(all_climate)
print(f"  Climate rows: {len(climate):,}  ({climate['cluster_id'].nunique()} clusters × 60 months)")

# ── 6. Assign trimester-level exposures to each birth ────────────────────────

print("Assigning trimester exposures ...")

# Zimbabwe seasons
def season(month):
    if month in [10, 11, 12, 1, 2, 3]:
        return "rainy"
    elif month in [4, 5]:
        return "post_rainy"
    else:
        return "dry"

def get_trimester_exp(cid, birth_yr, birth_mo, birth_wt_ok):
    """Return T1/T2/T3 monthly means for temperature, rainfall, NDVI."""
    # Approximate month offsets from conception (month 0 = T1 start)
    t1_months = [(birth_yr * 12 + birth_mo - 9 + k - 1) for k in range(1, 4)]
    t2_months = [(birth_yr * 12 + birth_mo - 9 + k - 1) for k in range(4, 7)]
    t3_months = [(birth_yr * 12 + birth_mo - 9 + k - 1) for k in range(7, 10)]

    def to_ym(ym):
        yr  = ym // 12
        mo  = ym % 12 + 1
        return yr, mo

    clim_cl = climate[climate["cluster_id"] == cid].copy()
    clim_cl["ym"] = clim_cl["year"] * 12 + clim_cl["month"] - 1

    result = {}
    for label, mlist in [("t1", t1_months), ("t2", t2_months), ("t3", t3_months)]:
        sub = clim_cl[clim_cl["ym"].isin(mlist)]
        if len(sub) > 0:
            result[f"lst_{label}"]     = sub["lst_day_c"].mean()
            result[f"rain_{label}"]    = sub["chirps_mm"].mean()
            result[f"ndvi_{label}"]    = sub["ndvi"].mean()
            result[f"heat_{label}"]    = sub["heat_index"].mean()
        else:
            result[f"lst_{label}"] = result[f"rain_{label}"] = \
            result[f"ndvi_{label}"] = result[f"heat_{label}"] = np.nan

    # Trimester with maximum heat stress
    result["hot_t3"] = (result.get("lst_t3", 0) > 27.5)
    # Drought: T2/T3 rainfall < 60 mm/month (dry-season pregnancy)
    result["drought_exposed"] = (
        result.get("rain_t2", 999) < 60 or result.get("rain_t3", 999) < 60
    )
    # Season of conception
    result["conception_season"] = season(to_ym(t1_months[0])[1])
    return result

# Build climate index for fast lookup: cluster_id → (year, month) → row
clim_idx = climate.set_index(["cluster_id", "year", "month"])

# Vectorised: join by cluster_id + year + month for T3 (quickest path)
# For the workshop we pre-compute for all recent births
exp_rows = []
sample = recent[["caseid", "bidx", "cluster_id", "birth_year", "birth_month",
                  "birth_weight_g"]].copy()

for _, row in sample.iterrows():
    r = get_trimester_exp(
        row["cluster_id"], row["birth_year"], row["birth_month"],
        row["birth_weight_g"]
    )
    r["caseid"] = row["caseid"]
    r["bidx"]   = row["bidx"]
    exp_rows.append(r)

exp_df = pd.DataFrame(exp_rows)
print(f"  Exposure rows: {len(exp_df):,}")

# ── 7. Merge everything and create the final birth dataset ───────────────────

keep_cols = [
    "caseid", "bidx", "cluster_id", "cluster_lat", "cluster_lon",
    "province", "urban", "urban_rura",
    "birth_year", "birth_month", "conception_year", "conception_month",
    "sex", "birth_order", "mother_age", "education", "wealth_quintile",
    "birth_weight_g", "low_birth_weight", "neonatal_death",
    "small_at_birth", "preterm", "anc_total_visits", "anc_4plus",
    "facility_delivery", "preceding_birth_interval",
]
births_out = recent[keep_cols].merge(exp_df, on=["caseid", "bidx"], how="left")

# Add season of birth
births_out["birth_season"] = births_out["birth_month"].apply(season)

# Sample weight (DHS)
births_out["sample_weight"] = recent["sample_weight"].values / 1_000_000

print(f"\nFinal births table: {len(births_out):,} rows, {births_out.shape[1]} columns")
print(f"  LBW: {births_out['low_birth_weight'].sum():.0f}/"
      f"{births_out['low_birth_weight'].notna().sum():.0f}")

# ── 8. Generate Zimbabwe health facilities ───────────────────────────────────

print("Building health facilities table ...")

province_coords = {
    "Harare":               (-17.83, 31.05),
    "Bulawayo":             (-20.15, 28.58),
    "Manicaland":           (-18.97, 32.65),
    "Mashonaland Central":  (-17.00, 31.08),
    "Mashonaland East":     (-18.30, 31.90),
    "Mashonaland West":     (-17.50, 29.75),
    "Masvingo":             (-20.06, 30.83),
    "Matabeleland North":   (-18.65, 27.55),
    "Matabeleland South":   (-21.18, 29.00),
    "Midlands":             (-19.50, 29.80),
}

fac_rows = []
fid = 1
for prov, (plat, plon) in province_coords.items():
    n_hosp = 1 if prov in ["Harare", "Bulawayo"] else 1
    n_rural = RNG.integers(2, 5)
    # 1 district hospital
    fac_rows.append({
        "facility_id": f"ZW{fid:03d}", "facility_type": "district_hospital",
        "province": prov,
        "latitude":  round(plat + RNG.uniform(-0.1, 0.1), 4),
        "longitude": round(plon + RNG.uniform(-0.1, 0.1), 4),
        "catchment_pop": int(RNG.integers(40000, 120000)),
        "beds": int(RNG.integers(60, 200)),
        "has_maternity": 1,
    })
    fid += 1
    # Rural health centres
    for _ in range(n_rural):
        fac_rows.append({
            "facility_id": f"ZW{fid:03d}", "facility_type": "rural_health_centre",
            "province": prov,
            "latitude":  round(plat + RNG.uniform(-1.5, 1.5), 4),
            "longitude": round(plon + RNG.uniform(-1.5, 1.5), 4),
            "catchment_pop": int(RNG.integers(3000, 15000)),
            "beds": int(RNG.integers(10, 40)),
            "has_maternity": 1,
        })
        fid += 1
    # Urban clinics for Harare and Bulawayo
    if prov in ["Harare", "Bulawayo"]:
        for _ in range(3):
            fac_rows.append({
                "facility_id": f"ZW{fid:03d}", "facility_type": "urban_clinic",
                "province": prov,
                "latitude":  round(plat + RNG.uniform(-0.2, 0.2), 4),
                "longitude": round(plon + RNG.uniform(-0.2, 0.2), 4),
                "catchment_pop": int(RNG.integers(8000, 30000)),
                "beds": 0, "has_maternity": 0,
            })
            fid += 1

facilities = pd.DataFrame(fac_rows)
print(f"  Facilities: {len(facilities)}")

# ── 9. Save outputs ──────────────────────────────────────────────────────────

print("Writing data files ...")

births_out.to_csv(DATA_DIR / "zw_births.csv", index=False)
print(f"  zw_births.csv: {len(births_out):,} rows")

cluster_ref.to_csv(DATA_DIR / "zw_clusters.csv", index=False)
print(f"  zw_clusters.csv: {len(cluster_ref):,} rows")

climate.to_csv(DATA_DIR / "zw_climate_monthly.csv", index=False)
print(f"  zw_climate_monthly.csv: {len(climate):,} rows")

births_out.to_csv(DATA_DIR / "zw_births_env.csv", index=False)
print(f"  zw_births_env.csv: {len(births_out):,} rows")

facilities.to_csv(DATA_DIR / "health_facilities.csv", index=False)
print(f"  health_facilities.csv: {len(facilities):,} rows")

# ── 10. Build DuckDB database ────────────────────────────────────────────────

db_path = DATA_DIR / "exposures_zw.duckdb"
if db_path.exists():
    db_path.unlink()

con = duckdb.connect(str(db_path))
con.execute("CREATE TABLE births AS SELECT * FROM births_out")
con.execute("CREATE TABLE clusters AS SELECT * FROM cluster_ref")
con.execute("CREATE TABLE climate AS SELECT * FROM climate")
con.execute("CREATE TABLE facilities AS SELECT * FROM facilities")
con.execute("CREATE TABLE cluster_exposures AS SELECT * FROM exp_df")
con.close()
print(f"  exposures_zw.duckdb: written")

print("\nDone. Summary:")
print(births_out[["low_birth_weight","neonatal_death","small_at_birth","anc_4plus"]].describe().T)
