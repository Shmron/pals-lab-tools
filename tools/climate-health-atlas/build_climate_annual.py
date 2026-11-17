"""
Derives climate_annual (heat/heatwave/heavy-rain day counts) from the raw
climate_daily table. Thresholds live here, not in extract_climate.py, so
they can be changed and re-run without touching Earth Engine again.

Heat day definition (changed 2026-11-02, was a flat >=35C nationally):
now PERCENTILE-based per district, following the WMO/ETCCDI TX90p
convention -- a heat day is a day whose max temp exceeds THAT DISTRICT'S
OWN 90th percentile of daily max temp over 1990-2025. A flat national
threshold misrepresents local climate: 35C in Harare (usually low-to-mid
20s) is genuinely extreme; the same 35C in Beitbridge (already-hot
lowveld) is an ordinary summer day. Each district's p90 is computed once
into district_thresholds and reused for both heat_days and the heatwave
consecutive-run detection below. Heavy-rain days keep the flat CHIRPS
>=20mm threshold -- only heat was asked to change to percentiles.

Heatwave detection uses a gaps-and-islands pattern: within each
(district, year), consecutive heat days get the same "island" id
(date minus a running row count); islands of length >=3 count as a
heatwave. Runs are computed per calendar year (not across the Dec31/Jan1
boundary) -- a deliberate simplification, noted here rather than hidden.

MIN_DAYS_FOR_COMPLETE_YEAR guards against a still-in-progress year (now
deliberately present in climate_daily -- see extract_climate.py) leaking
into climate_annual as if it were a real full year: "heat days/year" or
"total annual precipitation" computed over 9 months would understate both
relative to every other year in the trend, and the year slider would show
a misleading dip at the end that's an artifact of incompleteness, not a
real signal. 360 (not 365) leaves room for the odd sporadic EE gap on a
genuinely complete year without that triggering the same exclusion.
"""
import os
import duckdb

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "climate_hiv_atlas.duckdb")

HEAT_PERCENTILE = 0.90
HEAVY_RAIN_THRESHOLD_MM = 20.0
HEATWAVE_MIN_RUN = 3
MIN_DAYS_FOR_COMPLETE_YEAR = 360

con = duckdb.connect(DB_PATH)

con.execute("""
    CREATE TABLE IF NOT EXISTS district_thresholds (
        district VARCHAR PRIMARY KEY,
        heat_percentile DOUBLE,
        heat_threshold_c DOUBLE
    )
""")
con.execute("DELETE FROM district_thresholds")
con.execute(f"""
    INSERT INTO district_thresholds
    SELECT district, {HEAT_PERCENTILE}, QUANTILE_CONT(tmax_c, {HEAT_PERCENTILE})
    FROM climate_daily
    WHERE tmax_c IS NOT NULL
    GROUP BY district
""")
n_thresh = con.execute("SELECT COUNT(*) FROM district_thresholds").fetchone()[0]
print(f"[OK] district_thresholds: {n_thresh} districts")
print(con.execute("SELECT district, ROUND(heat_threshold_c,1) FROM district_thresholds "
                   "ORDER BY heat_threshold_c LIMIT 3").fetchdf())
print(con.execute("SELECT district, ROUND(heat_threshold_c,1) FROM district_thresholds "
                   "ORDER BY heat_threshold_c DESC LIMIT 3").fetchdf())

SQL = f"""
WITH base AS (
    SELECT c.district, c.date, c.tmax_c, c.precip_mm,
           EXTRACT(year FROM c.date)::INTEGER AS year, t.heat_threshold_c
    FROM climate_daily c
    JOIN district_thresholds t USING (district)
),
heat_flagged AS (
    SELECT district, year, date,
           date - (ROW_NUMBER() OVER (PARTITION BY district, year ORDER BY date) * INTERVAL 1 DAY) AS island
    FROM base
    WHERE tmax_c >= heat_threshold_c
),
heat_runs AS (
    SELECT district, year, island, COUNT(*) AS run_length
    FROM heat_flagged
    GROUP BY district, year, island
),
heatwave_agg AS (
    SELECT district, year, SUM(run_length) AS heatwave_days
    FROM heat_runs
    WHERE run_length >= {HEATWAVE_MIN_RUN}
    GROUP BY district, year
),
yearly AS (
    SELECT district, year,
           COUNT(*) FILTER (WHERE tmax_c >= heat_threshold_c) AS heat_days,
           COUNT(*) FILTER (WHERE precip_mm >= {HEAVY_RAIN_THRESHOLD_MM}) AS heavy_rain_days,
           AVG(tmax_c) AS mean_tmax_c,
           SUM(precip_mm) AS total_precip_mm
    FROM base
    GROUP BY district, year
    HAVING COUNT(*) >= {MIN_DAYS_FOR_COMPLETE_YEAR}
)
SELECT y.district, y.year, y.heat_days,
       COALESCE(h.heatwave_days, 0) AS heatwave_days,
       y.heavy_rain_days, y.mean_tmax_c, y.total_precip_mm
FROM yearly y
LEFT JOIN heatwave_agg h USING (district, year)
"""

con.execute("DELETE FROM climate_annual")
con.execute(f"INSERT INTO climate_annual {SQL}")
n = con.execute("SELECT COUNT(*) FROM climate_annual").fetchone()[0]
print(f"[OK] climate_annual: {n} district-year rows")
print(con.execute("SELECT year, COUNT(*) districts, ROUND(AVG(heat_days),1) avg_heat_days, "
                   "ROUND(AVG(heatwave_days),1) avg_heatwave_days, ROUND(AVG(heavy_rain_days),1) avg_heavy_rain "
                   "FROM climate_annual GROUP BY year ORDER BY year").fetchdf())
con.close()
