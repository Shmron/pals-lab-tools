"""
Creates climate_hiv_atlas.duckdb and loads district boundaries.

District source: FAO GAUL 2015 level2 (62 districts), same source CASCADE's
fetch_admin_boundaries.py uses -- reused here from
~/PRECISE/harare_flood_flask/Population/zimbabwe_districts.gpkg rather than
re-fetching from Earth Engine.
"""
import json
import os
import duckdb
import geopandas as gpd
from shapely.ops import unary_union

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "climate_hiv_atlas.duckdb")
DISTRICTS_GPKG = "/home/USER/PRECISE/harare_flood_flask/Population/zimbabwe_districts.gpkg"
# district -> province (FAO GAUL 2015 level2's own ADM1_NAME), fetched once
# via Earth Engine alongside fetch_admin_boundaries.py's ADM2_NAME pull --
# see add_province.py for how this file was generated.
PROVINCE_MAP = os.path.join(BASE_DIR, "district_province_map.json")

con = duckdb.connect(DB_PATH)
con.execute("INSTALL spatial; LOAD spatial;")

province_of = json.load(open(PROVINCE_MAP))

gdf = gpd.read_file(DISTRICTS_GPKG)[["ADM2_NAME", "geometry"]].rename(columns={"ADM2_NAME": "district"})
gdf["area_km2"] = gdf.to_crs(epsg=32736).geometry.area / 1e6  # UTM 36S, good enough for ZW-wide areas
gdf["province"] = gdf["district"].map(province_of)
missing = gdf[gdf["province"].isna()]
if not missing.empty:
    raise SystemExit(f"[FAIL] no province mapped for: {missing['district'].tolist()}")

con.execute("DROP TABLE IF EXISTS districts")
con.execute("""
    CREATE TABLE districts (
        district VARCHAR PRIMARY KEY,
        province VARCHAR,
        area_km2 DOUBLE,
        geometry_wkt VARCHAR
    )
""")
for _, row in gdf.iterrows():
    con.execute(
        "INSERT INTO districts VALUES (?, ?, ?, ?)",
        [row["district"], row["province"], row["area_km2"], row.geometry.wkt],
    )

print(f"[OK] districts table loaded: {con.execute('SELECT COUNT(*) FROM districts').fetchone()[0]} rows")

# Province boundaries are just the dissolved union of their districts -- no
# separate Earth Engine fetch needed.
con.execute("DROP TABLE IF EXISTS provinces")
con.execute("""
    CREATE TABLE provinces (
        province VARCHAR PRIMARY KEY,
        area_km2 DOUBLE,
        geometry_wkt VARCHAR
    )
""")
for province, group in gdf.groupby("province"):
    dissolved = unary_union(group.geometry.tolist())
    con.execute(
        "INSERT INTO provinces VALUES (?, ?, ?)",
        [province, group["area_km2"].sum(), dissolved.wkt],
    )
print(f"[OK] provinces table loaded: {con.execute('SELECT COUNT(*) FROM provinces').fetchone()[0]} rows")

# Raw daily climate series (filled in by extract_climate.py)
con.execute("""
    CREATE TABLE IF NOT EXISTS climate_daily (
        district VARCHAR,
        date DATE,
        tmax_c DOUBLE,
        precip_mm DOUBLE,
        PRIMARY KEY (district, date)
    )
""")

# Materialized annual aggregates, derived from climate_daily (fast API reads,
# thresholds can change later without re-extracting from Earth Engine)
con.execute("""
    CREATE TABLE IF NOT EXISTS climate_annual (
        district VARCHAR,
        year INTEGER,
        heat_days INTEGER,
        heatwave_days INTEGER,
        heavy_rain_days INTEGER,
        mean_tmax_c DOUBLE,
        total_precip_mm DOUBLE,
        PRIMARY KEY (district, year)
    )
""")

con.execute("""
    CREATE TABLE IF NOT EXISTS flood_annual (
        district VARCHAR,
        year INTEGER,
        water_fraction_pct DOUBLE,
        PRIMARY KEY (district, year)
    )
""")

# District-level flood/rain-adjusted accessibility: population-weighted mean travel time
# to the nearest open health facility, baseline vs. flood scenario, from
# harare_flood_flask/run_national_annual.py's run_admin_tabulation() output (see
# build_access.py). One row per district per year -- flood scenario for each year is that
# year's heaviest national 7-day CHIRPS rainfall window, same source already trusted
# elsewhere in this app (climate_daily/climate_annual), not a separately-sourced event.
con.execute("""
    CREATE TABLE IF NOT EXISTS access_annual (
        district VARCHAR,
        year INTEGER,
        population DOUBLE,
        baseline_min DOUBLE,
        flooded_min DOUBLE,
        pct_change DOUBLE,
        pop_cutoff DOUBLE,
        PRIMARY KEY (district, year)
    )
""")

con.execute("""
    CREATE TABLE IF NOT EXISTS hiv_burden (
        district VARCHAR,
        survey_round VARCHAR,
        year INTEGER,
        n_tested INTEGER,
        n_positive INTEGER,
        prevalence_pct DOUBLE,
        prevalence_weighted_pct DOUBLE,
        PRIMARY KEY (district, survey_round)
    )
""")

# Zimbabwe's DHS rounds never ran a malaria testing module (checked directly against
# dhs_pr/hr/kr/ir.duckdb -- hml35/hml32 test-result columns are empty in every round, only
# bednet-ownership counts exist), so this comes from Malaria Atlas Project's modelled 5km
# rasters (WCS from data.malariaatlas.org) instead, aggregated to district the same way
# climate_annual is -- see extract_malaria.py. Starts 2000, not 1990: that's MAP's earliest
# modelled year, no earlier surface exists to extract.
#
# Units: pf_parasite_rate is PfPR2-10 as a PERCENT (0-100, scaled up from MAP's raw 0-1
# proportion -- same convention as hiv_burden.prevalence_weighted_pct), pf_incidence_rate
# is cases PER 1,000 POPULATION PER YEAR (scaled from MAP's raw per-1 rate), matching the
# units MAP's own Data Explorer CSV export uses.
con.execute("""
    CREATE TABLE IF NOT EXISTS malaria_annual (
        district VARCHAR,
        year INTEGER,
        pf_parasite_rate DOUBLE,
        pf_incidence_rate DOUBLE,
        PRIMARY KEY (district, year)
    )
""")

# National only, not per-district like every other table here -- checked directly (full
# text search of DHS_Codebook.xlsx across all 15 recode types, plus a column-name sweep
# of every dhs_*.duckdb file on this server for tb_/tuberc* patterns): zero TB data exists
# anywhere in Zimbabwe's DHS records, unlike HIV (AR biomarker) or malaria (no DHS data,
# but MAP's modelled rasters exist). WHO's Global TB Programme only publishes
# country-level incidence/TB-HIV-coinfection estimates, no subnational breakdown -- see
# extract_tb.py. incidence_100k: TB cases per 100,000 population/year. tbhiv_prct: percent
# of incident TB cases that are also HIV-positive (the figure most relevant to this app's
# climate x HIV theme).
con.execute("""
    CREATE TABLE IF NOT EXISTS tb_national (
        year INTEGER PRIMARY KEY,
        incidence_100k DOUBLE,
        tbhiv_prct DOUBLE
    )
""")

print("[OK] schema ready:", con.execute("SHOW TABLES").fetchall())
con.close()
