"""
Flask backend for the Climate & Health Atlas (maps.placealert.org).

Reads climate_hiv_atlas.duckdb (built by build_db.py / build_hiv_burden.py /
extract_climate.py / extract_flood.py / build_climate_annual.py) and serves
it to the Leaflet frontend in templates/index.html.

Request: Dr. Makanga (CESHHAR), 2026-10-29 -- district-level overlap between
climate exposure (heat, heavy rain, flooding) and HIV burden over time,
and which districts are persistently in the highest-risk group.
"""
import os
import json
import duckdb
import pandas as pd
from flask import Flask, jsonify, render_template, request

import ai_agent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "climate_hiv_atlas.duckdb")

app = Flask(__name__)


def get_con():
    return duckdb.connect(DB_PATH, read_only=True)


@app.route("/")
def index():
    # Browsers/Cloudflare kept serving a stale cached copy of this page across
    # deploys (basemap swap, redesign, etc. all needed a hard refresh to show
    # up) -- force revalidation so future updates don't need that.
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/districts")
def districts():
    # The stored geometry is full-resolution (~376k vertices across all 62
    # districts -- fine for the Earth Engine extraction, which already
    # simplifies its own copy) but serving that raw to the browser was a
    # ~15MB GeoJSON payload that could stall or fail to load entirely on a
    # slow connection, with no visible error -- just an empty-looking map.
    # Simplified here to ~111m tolerance, imperceptible at any zoom this map
    # actually uses.
    import shapely.wkt
    con = get_con()
    rows = con.execute("SELECT district, area_km2, geometry_wkt FROM districts").fetchall()
    con.close()
    features = []
    for district, area_km2, wkt in rows:
        geom = shapely.wkt.loads(wkt).simplify(0.001)
        features.append({
            "type": "Feature",
            "geometry": json.loads(json.dumps(geom.__geo_interface__)),
            "properties": {"district": district, "area_km2": round(area_km2, 1)},
        })
    return jsonify({"type": "FeatureCollection", "features": features})


@app.route("/api/provinces")
def provinces():
    # Province boundaries are the dissolved union of their districts (FAO
    # GAUL 2015 level2's own ADM1_NAME) -- see add_province.py / build_db.py.
    # Same simplification tolerance as /api/districts for the same reason.
    import shapely.wkt
    con = get_con()
    rows = con.execute("SELECT province, area_km2, geometry_wkt FROM provinces").fetchall()
    con.close()
    features = []
    for province, area_km2, wkt in rows:
        geom = shapely.wkt.loads(wkt).simplify(0.001)
        features.append({
            "type": "Feature",
            "geometry": json.loads(json.dumps(geom.__geo_interface__)),
            "properties": {"province": province, "area_km2": round(area_km2, 1)},
        })
    return jsonify({"type": "FeatureCollection", "features": features})


@app.route("/api/national_timeseries")
def national_timeseries():
    """
    Whole-country area-weighted (climate) / test-weighted (HIV) annual
    series -- backs the trend strip above the map that builds up in sync
    with the year slider/autoplay, so the national trend is visible
    alongside the district/province spatial detail. Same weighting
    rationale as /api/layer/climate, /api/layer/hiv and
    /api/province_timeseries, just with no GROUP BY province (one national
    aggregate instead of ten).
    """
    con = get_con()
    climate = con.execute(
        "SELECT c.year, "
        "ROUND(SUM(c.heat_days * d.area_km2) / SUM(d.area_km2), 1) AS heat_days, "
        "ROUND(SUM(c.heatwave_days * d.area_km2) / SUM(d.area_km2), 1) AS heatwave_days, "
        "ROUND(SUM(c.heavy_rain_days * d.area_km2) / SUM(d.area_km2), 1) AS heavy_rain_days, "
        "ROUND(SUM(c.mean_tmax_c * d.area_km2) / SUM(d.area_km2), 1) AS mean_tmax_c, "
        "ROUND(SUM(c.total_precip_mm * d.area_km2) / SUM(d.area_km2), 1) AS total_precip_mm "
        "FROM climate_annual c JOIN districts d ON d.district = c.district "
        "GROUP BY c.year ORDER BY c.year"
    ).fetchall()
    hiv = con.execute(
        "SELECT h.survey_round, h.year, "
        "ROUND(SUM(h.prevalence_weighted_pct * h.n_tested) / SUM(h.n_tested), 2) AS prevalence_weighted_pct "
        "FROM hiv_burden h JOIN districts d ON d.district = h.district "
        "GROUP BY h.survey_round, h.year ORDER BY h.year"
    ).fetchall()
    malaria = con.execute(
        "SELECT m.year, "
        "ROUND(SUM(m.pf_parasite_rate * d.area_km2) / SUM(d.area_km2), 2) AS pf_parasite_rate, "
        "ROUND(SUM(m.pf_incidence_rate * d.area_km2) / SUM(d.area_km2), 2) AS pf_incidence_rate "
        "FROM malaria_annual m JOIN districts d ON d.district = m.district "
        "GROUP BY m.year ORDER BY m.year"
    ).fetchall()
    tb = con.execute(
        "SELECT year, ROUND(incidence_100k,1), ROUND(tbhiv_prct,1) FROM tb_national ORDER BY year"
    ).fetchall()
    # Access to care: population-weighted national travel time (dry baseline vs the year's expected
    # rain+flood-adjusted value); % change is taken from the two national means, not averaged from districts.
    access = con.execute(
        "SELECT year, "
        "ROUND(SUM(baseline_min * population) / SUM(population), 2) AS baseline_min, "
        "ROUND(SUM(flooded_min * population) / SUM(population), 2) AS flooded_min, "
        "ROUND(100.0 * (SUM(flooded_min * population) - SUM(baseline_min * population)) / SUM(baseline_min * population), 2) AS pct_change "
        "FROM access_annual GROUP BY year ORDER BY year"
    ).fetchall()
    con.close()
    return jsonify({
        "access": [dict(zip(["year", "baseline_min", "flooded_min", "pct_change"], r)) for r in access],
        "climate": [dict(zip(["year", "heat_days", "heatwave_days", "heavy_rain_days", "mean_tmax_c", "total_precip_mm"], r)) for r in climate],
        "hiv": [dict(zip(["survey_round", "year", "prevalence_weighted_pct"], r)) for r in hiv],
        "malaria": [dict(zip(["year", "pf_parasite_rate", "pf_incidence_rate"], r)) for r in malaria],
        "tb": [dict(zip(["year", "tb_incidence_100k", "tb_hiv_coinfection_pct"], r)) for r in tb],
    })


@app.route("/api/meta")
def meta():
    con = get_con()
    climate_years = [r[0] for r in con.execute("SELECT DISTINCT year FROM climate_annual ORDER BY year").fetchall()]
    # Deliberately separate from climate_years: climate_daily can (and,
    # once extracted, does) hold a still-in-progress year that
    # climate_annual never will -- the year slider/annual choropleth
    # layers need a last-COMPLETE-year boundary (a partial year would
    # understate "heat days/year" etc. relative to every other year), but
    # the daily-calendar/typical-month views work at day/month
    # resolution, where a partial year is just "some months are missing",
    # not a problem. Keeping these as two separate lists is what lets the
    # Seasonality views reach the current year without ever letting a
    # partial year leak into an annual total.
    daily_years = [r[0] for r in con.execute("SELECT DISTINCT EXTRACT(year FROM date)::INTEGER FROM climate_daily ORDER BY 1").fetchall()]
    flood_years = [r[0] for r in con.execute("SELECT DISTINCT year FROM flood_annual ORDER BY year").fetchall()]
    access_years = [r[0] for r in con.execute("SELECT DISTINCT year FROM access_annual ORDER BY year").fetchall()]
    hiv_rounds = con.execute(
        "SELECT DISTINCT survey_round, year FROM hiv_burden ORDER BY year"
    ).fetchall()
    # Shorter than climate_years on purpose -- 2000 is Malaria Atlas Project's earliest
    # modelled year for this surface (no 1990s raster exists to extract), not a bug.
    malaria_years = [r[0] for r in con.execute("SELECT DISTINCT year FROM malaria_annual ORDER BY year").fetchall()]
    # National only (see tb_national's own comment in build_db.py for why) -- still its
    # own year axis since WHO's range (2000-2024) doesn't exactly match climate or malaria.
    tb_years = [r[0] for r in con.execute("SELECT DISTINCT year FROM tb_national ORDER BY year").fetchall()]
    layer_ranges = compute_layer_ranges(con)
    con.close()
    return jsonify({
        "climate_years": climate_years,
        "daily_years": daily_years,
        "flood_years": flood_years,
        "access_years": access_years,
        "hiv_rounds": [{"round": r, "year": y} for r, y in hiv_rounds],
        "malaria_years": malaria_years,
        "tb_years": tb_years,
        "layer_ranges": layer_ranges,
    })


# Climate/HIV layers are time series (one value per district per year/round), and the
# color scale used to be recomputed from whichever single frame was on screen -- so a
# mild year's worst district looked exactly as "maximally red" as the worst year's worst
# district, and a genuine multi-year worsening trend was invisible in the color alone
# (the legend numbers changed frame to frame, but nothing told you to read them that way).
# Fixing the scale to each layer's full time-series min/max instead means intensity
# visibly builds as you move the year slider, and the legend stays stable across frames
# so colors are directly comparable year to year. Same 0/1 floor-and-ceiling convention
# the old per-frame scale used (COLOR_RAMPS bottom/top), just computed once over all
# years/rounds rather than per request -- except mean_tmax_c (see ZERO_FLOOR_COLS below).
CLIMATE_RANGE_COLS = ["heat_days", "heatwave_days", "heavy_rain_days", "mean_tmax_c", "total_precip_mm"]

# Day-count/amount/rate columns can genuinely hit zero (a year with no heat days, no
# heavy-rain days, a drought with near-zero rainfall, a district with no malaria at all),
# so flooring the scale's lower bound at 0 is meaningful there. mean_tmax_c never goes
# anywhere near 0 degC in Zimbabwe (district-year means run ~22-32 degC) -- flooring it at
# 0 anyway wasted the bottom ~70% of the ramp on values that never occur, compressing every
# real year into a narrow top slice where they all looked the same color. Use its true
# observed range instead so year-to-year swings are visible.
#
# Deliberately NOT also forcing the ceiling up to some fixed value (the old per-frame JS
# code did `Math.max(...vals, 1)`, which only existed as an empty-array fallback, not a
# real floor -- translating it into "always ceiling at >=1" was harmless for day-counts/mm
# (their real max is always far above 1) but would have been actively wrong for malaria
# rates, which top out well under 1: forcing that ceiling to 1 would recompress the ramp
# exactly the way the un-fixed mean_tmax_c floor did, just from the top instead of the
# bottom. Ceiling is always each column's true observed max.
ZERO_FLOOR_COLS = {"heat_days", "heatwave_days", "heavy_rain_days", "total_precip_mm"}

MALARIA_RANGE_COLS = ["pf_parasite_rate", "pf_incidence_rate"]
# (db column, frontend layer key) -- the two differ (tbhiv_prct vs tb_hiv_coinfection_pct)
# because every other endpoint here reuses its frontend layer name directly as both the DB
# column and the JSON key (e.g. prevalence_weighted_pct), which lets the frontend do
# state.layerRanges[key] with no translation step. TB's DB columns were named for brevity
# (incidence_100k, tbhiv_prct); the frontend layer keys are prefixed tb_ so they read
# clearly next to pf_parasite_rate/pf_incidence_rate in the layer list. Mapped explicitly
# here rather than renaming the DB columns, so the translation lives in one place.
TB_RANGE_COLS = [("incidence_100k", "tb_incidence_100k"), ("tbhiv_prct", "tb_hiv_coinfection_pct")]

# Population-weighted (not area-weighted like CLIMATE_RANGE_COLS) when rolling up to
# province -- access_annual's district values are already population-weighted means
# (run_admin_tabulation's own formula), and access_annual carries its own population
# column directly, so no separate districts.area_km2 join is needed for this one.
ACCESS_RANGE_COLS = ["baseline_min", "flooded_min", "pct_change"]


def compute_layer_ranges(con):
    ranges = {}

    district_climate = con.execute(
        "SELECT " + ", ".join(f"MIN({c}), MAX({c})" for c in CLIMATE_RANGE_COLS) + " FROM climate_annual"
    ).fetchone()
    province_climate = con.execute(
        "WITH prov AS ("
        "  SELECT d.province, c.year, "
        + ", ".join(f"SUM(c.{c} * d.area_km2) / SUM(d.area_km2) AS {c}" for c in CLIMATE_RANGE_COLS)
        + "  FROM climate_annual c JOIN districts d ON d.district = c.district"
        "  GROUP BY d.province, c.year"
        ") SELECT " + ", ".join(f"MIN({c}), MAX({c})" for c in CLIMATE_RANGE_COLS) + " FROM prov"
    ).fetchone()
    for i, col in enumerate(CLIMATE_RANGE_COLS):
        d_lo, d_hi = district_climate[2 * i], district_climate[2 * i + 1]
        p_lo, p_hi = province_climate[2 * i], province_climate[2 * i + 1]
        if col in ZERO_FLOOR_COLS:
            ranges[col] = {"district": [min(d_lo, 0), d_hi], "province": [min(p_lo, 0), p_hi]}
        else:
            ranges[col] = {"district": [d_lo, d_hi], "province": [p_lo, p_hi]}

    d_lo, d_hi = con.execute("SELECT MIN(prevalence_weighted_pct), MAX(prevalence_weighted_pct) FROM hiv_burden").fetchone()
    p_lo, p_hi = con.execute(
        "WITH prov AS ("
        "  SELECT d.province, h.survey_round, "
        "    SUM(h.prevalence_weighted_pct * h.n_tested) / SUM(h.n_tested) AS prevalence_weighted_pct "
        "  FROM hiv_burden h JOIN districts d ON d.district = h.district "
        "  GROUP BY d.province, h.survey_round"
        ") SELECT MIN(prevalence_weighted_pct), MAX(prevalence_weighted_pct) FROM prov"
    ).fetchone()
    ranges["prevalence_weighted_pct"] = {"district": [min(d_lo, 0), d_hi], "province": [min(p_lo, 0), p_hi]}

    if con.execute("SELECT COUNT(*) FROM malaria_annual").fetchone()[0] > 0:
        district_malaria = con.execute(
            "SELECT " + ", ".join(f"MIN({c}), MAX({c})" for c in MALARIA_RANGE_COLS) + " FROM malaria_annual"
        ).fetchone()
        province_malaria = con.execute(
            "WITH prov AS ("
            "  SELECT d.province, m.year, "
            + ", ".join(f"SUM(m.{c} * d.area_km2) / SUM(d.area_km2) AS {c}" for c in MALARIA_RANGE_COLS)
            + "  FROM malaria_annual m JOIN districts d ON d.district = m.district"
            "  GROUP BY d.province, m.year"
            ") SELECT " + ", ".join(f"MIN({c}), MAX({c})" for c in MALARIA_RANGE_COLS) + " FROM prov"
        ).fetchone()
        for i, col in enumerate(MALARIA_RANGE_COLS):
            d_lo, d_hi = district_malaria[2 * i], district_malaria[2 * i + 1]
            p_lo, p_hi = province_malaria[2 * i], province_malaria[2 * i + 1]
            ranges[col] = {"district": [min(d_lo, 0), d_hi], "province": [min(p_lo, 0), p_hi]}

    if con.execute("SELECT COUNT(*) FROM tb_national").fetchone()[0] > 0:
        # National-only, so no area-weighting/join needed -- every district and
        # province gets the identical national value, so the range is the same at
        # both levels too (see /api/layer/tb).
        for db_col, layer_key in TB_RANGE_COLS:
            lo, hi = con.execute(f"SELECT MIN({db_col}), MAX({db_col}) FROM tb_national").fetchone()
            ranges[layer_key] = {"district": [min(lo, 0), hi], "province": [min(lo, 0), hi]}

    if con.execute("SELECT COUNT(*) FROM access_annual").fetchone()[0] > 0:
        district_access = con.execute(
            "SELECT " + ", ".join(f"MIN({c}), MAX({c})" for c in ACCESS_RANGE_COLS) + " FROM access_annual"
        ).fetchone()
        province_access = con.execute(
            "WITH prov AS ("
            "  SELECT d.province, a.year, "
            + ", ".join(f"SUM(a.{c} * a.population) / SUM(a.population) AS {c}" for c in ACCESS_RANGE_COLS)
            + "  FROM access_annual a JOIN districts d ON d.district = a.district"
            "  GROUP BY d.province, a.year"
            ") SELECT " + ", ".join(f"MIN({c}), MAX({c})" for c in ACCESS_RANGE_COLS) + " FROM prov"
        ).fetchone()
        for i, col in enumerate(ACCESS_RANGE_COLS):
            d_lo, d_hi = district_access[2 * i], district_access[2 * i + 1]
            p_lo, p_hi = province_access[2 * i], province_access[2 * i + 1]
            ranges[col] = {"district": [d_lo, d_hi], "province": [p_lo, p_hi]}

    return ranges


@app.route("/api/layer/climate")
def layer_climate():
    year = request.args.get("year", type=int)
    level = request.args.get("level", "district")
    con = get_con()
    if level == "province":
        # Area-weighted mean across the province's districts -- these are
        # intensities/day-counts, not totals, so a plain sum across
        # districts would be meaningless. No population data is loaded in
        # this app, so area is the only weighting available; a province
        # with one small hot district and one large mild one will read
        # closer to the mild one's climate than a population weighting
        # would. See the methodology modal.
        rows = con.execute(
            "SELECT d.province, "
            "ROUND(SUM(c.heat_days * d.area_km2) / SUM(d.area_km2), 1), "
            "ROUND(SUM(c.heatwave_days * d.area_km2) / SUM(d.area_km2), 1), "
            "ROUND(SUM(c.heavy_rain_days * d.area_km2) / SUM(d.area_km2), 1), "
            "ROUND(SUM(c.mean_tmax_c * d.area_km2) / SUM(d.area_km2), 1), "
            "ROUND(SUM(c.total_precip_mm * d.area_km2) / SUM(d.area_km2), 1) "
            "FROM climate_annual c JOIN districts d ON d.district = c.district "
            "WHERE c.year = ? GROUP BY d.province", [year]
        ).fetchall()
        cols = ["province", "heat_days", "heatwave_days", "heavy_rain_days", "mean_tmax_c", "total_precip_mm"]
    else:
        rows = con.execute(
            "SELECT district, heat_days, heatwave_days, heavy_rain_days, "
            "ROUND(mean_tmax_c,1), ROUND(total_precip_mm,1) "
            "FROM climate_annual WHERE year = ?", [year]
        ).fetchall()
        cols = ["district", "heat_days", "heatwave_days", "heavy_rain_days", "mean_tmax_c", "total_precip_mm"]
    con.close()
    return jsonify([dict(zip(cols, r)) for r in rows])


@app.route("/api/layer/tb")
def layer_tb():
    """
    TB is national-only (see tb_national's comment in build_db.py), but the frontend's
    choropleth/legend/styling code is written generically against "one value per
    district/province" -- rather than special-case the map renderer for the one layer
    that has no spatial variation, broadcast the same national value to every
    district/province row here instead, so every district renders identically (by
    construction, not a bug) and nothing downstream needs to know TB is different.
    """
    year = request.args.get("year", type=int)
    level = request.args.get("level", "district")
    con = get_con()
    row = con.execute(
        "SELECT ROUND(incidence_100k,1), ROUND(tbhiv_prct,1) FROM tb_national WHERE year = ?", [year]
    ).fetchone()
    incidence_100k, tbhiv_prct = row if row else (None, None)
    if level == "province":
        names = [r[0] for r in con.execute("SELECT province FROM provinces").fetchall()]
        cols = ["province", "tb_incidence_100k", "tb_hiv_coinfection_pct"]
    else:
        names = [r[0] for r in con.execute("SELECT district FROM districts").fetchall()]
        cols = ["district", "tb_incidence_100k", "tb_hiv_coinfection_pct"]
    con.close()
    return jsonify([dict(zip(cols, [name, incidence_100k, tbhiv_prct])) for name in names])


@app.route("/api/layer/malaria")
def layer_malaria():
    year = request.args.get("year", type=int)
    level = request.args.get("level", "district")
    con = get_con()
    if level == "province":
        # Same area-weighting convention as /api/layer/climate -- no population data
        # loaded in this app, so area is the only weighting available. See the
        # methodology modal.
        rows = con.execute(
            "SELECT d.province, "
            "ROUND(SUM(m.pf_parasite_rate * d.area_km2) / SUM(d.area_km2), 2), "
            "ROUND(SUM(m.pf_incidence_rate * d.area_km2) / SUM(d.area_km2), 2) "
            "FROM malaria_annual m JOIN districts d ON d.district = m.district "
            "WHERE m.year = ? GROUP BY d.province", [year]
        ).fetchall()
        cols = ["province", "pf_parasite_rate", "pf_incidence_rate"]
    else:
        rows = con.execute(
            "SELECT district, ROUND(pf_parasite_rate,2), ROUND(pf_incidence_rate,2) "
            "FROM malaria_annual WHERE year = ?", [year]
        ).fetchall()
        cols = ["district", "pf_parasite_rate", "pf_incidence_rate"]
    con.close()
    return jsonify([dict(zip(cols, r)) for r in rows])


@app.route("/api/layer/flood")
def layer_flood():
    year = request.args.get("year", type=int)
    con = get_con()
    rows = con.execute(
        "SELECT district, ROUND(water_fraction_pct,2) FROM flood_annual WHERE year = ?", [year]
    ).fetchall()
    con.close()
    return jsonify([{"district": d, "water_fraction_pct": w} for d, w in rows])


@app.route("/api/layer/access")
def layer_access():
    """Population-weighted mean travel time to the nearest open health
    facility, baseline vs. that year's flood scenario (see build_access.py).
    Province rollup is population-weighted (access_annual carries its own
    population column), unlike climate/malaria's area-weighting -- a real
    weighting is available here, so use it instead of the area-weighted
    fallback those layers are stuck with."""
    year = request.args.get("year", type=int)
    level = request.args.get("level", "district")
    con = get_con()
    if level == "province":
        rows = con.execute(
            "SELECT d.province, "
            "ROUND(SUM(a.baseline_min * a.population) / SUM(a.population), 1), "
            "ROUND(SUM(a.flooded_min * a.population) / SUM(a.population), 1), "
            "ROUND(SUM(a.pct_change * a.population) / SUM(a.population), 1), "
            "SUM(a.pop_cutoff) "
            "FROM access_annual a JOIN districts d ON d.district = a.district "
            "WHERE a.year = ? GROUP BY d.province", [year]
        ).fetchall()
        cols = ["province", "baseline_min", "flooded_min", "pct_change", "pop_cutoff"]
    else:
        rows = con.execute(
            "SELECT district, ROUND(baseline_min,1), ROUND(flooded_min,1), ROUND(pct_change,1), pop_cutoff "
            "FROM access_annual WHERE year = ?", [year]
        ).fetchall()
        cols = ["district", "baseline_min", "flooded_min", "pct_change", "pop_cutoff"]
    con.close()
    return jsonify([dict(zip(cols, r)) for r in rows])


@app.route("/api/layer/hiv")
def layer_hiv():
    survey_round = request.args.get("round")
    level = request.args.get("level", "district")
    con = get_con()
    if level == "province":
        # Province totals are a straight sum of tests/positives (that's
        # just arithmetic); prevalence_weighted_pct is a test-count-weighted
        # mean of each district's own DHS-sample-weighted prevalence, which
        # is the best estimate available without re-weighting raw DHS
        # records at the province level.
        rows = con.execute(
            "SELECT d.province, SUM(h.n_tested), SUM(h.n_positive), "
            "ROUND(100.0 * SUM(h.n_positive) / SUM(h.n_tested), 2), "
            "ROUND(SUM(h.prevalence_weighted_pct * h.n_tested) / SUM(h.n_tested), 2) "
            "FROM hiv_burden h JOIN districts d ON d.district = h.district "
            "WHERE h.survey_round = ? GROUP BY d.province", [survey_round]
        ).fetchall()
        cols = ["province", "n_tested", "n_positive", "prevalence_pct", "prevalence_weighted_pct"]
    else:
        rows = con.execute(
            "SELECT district, n_tested, n_positive, ROUND(prevalence_pct,2), ROUND(prevalence_weighted_pct,2) "
            "FROM hiv_burden WHERE survey_round = ?", [survey_round]
        ).fetchall()
        cols = ["district", "n_tested", "n_positive", "prevalence_pct", "prevalence_weighted_pct"]
    con.close()
    return jsonify([dict(zip(cols, r)) for r in rows])


@app.route("/api/district_timeseries")
def district_timeseries():
    """
    Full per-year/per-round history for one district, across all three data
    sources -- backs the click-to-inspect detail panel. This is the view
    that actually answers "how has this changed over time" for a single
    place, rather than forcing the viewer to flip through year-by-year
    snapshots and remember what they saw.
    """
    district = request.args.get("district")
    con = get_con()
    climate = con.execute(
        "SELECT year, heat_days, heatwave_days, heavy_rain_days, "
        "ROUND(mean_tmax_c,1) AS mean_tmax_c, ROUND(total_precip_mm,1) AS total_precip_mm "
        "FROM climate_annual WHERE district = ? ORDER BY year", [district]
    ).fetchall()
    hiv = con.execute(
        "SELECT survey_round, year, n_tested, ROUND(prevalence_weighted_pct,2) AS prevalence_weighted_pct "
        "FROM hiv_burden WHERE district = ? ORDER BY year", [district]
    ).fetchall()
    malaria = con.execute(
        "SELECT year, ROUND(pf_parasite_rate,2), ROUND(pf_incidence_rate,2) "
        "FROM malaria_annual WHERE district = ? ORDER BY year", [district]
    ).fetchall()
    threshold_row = con.execute(
        "SELECT ROUND(heat_threshold_c,1) FROM district_thresholds WHERE district = ?", [district]
    ).fetchone()
    con.close()
    return jsonify({
        "district": district,
        "heat_threshold_c": threshold_row[0] if threshold_row else None,
        "climate": [dict(zip(["year", "heat_days", "heatwave_days", "heavy_rain_days", "mean_tmax_c", "total_precip_mm"], r)) for r in climate],
        "hiv": [dict(zip(["survey_round", "year", "n_tested", "prevalence_weighted_pct"], r)) for r in hiv],
        "malaria": [dict(zip(["year", "pf_parasite_rate", "pf_incidence_rate"], r)) for r in malaria],
    })


@app.route("/api/province_timeseries")
def province_timeseries():
    """
    Same per-year/per-round history as /api/district_timeseries, but
    aggregated across a province's districts: area-weighted for climate,
    test-count-weighted for HIV (see /api/layer/climate and /api/layer/hiv
    for why). The 90th-percentile heat threshold is inherently a
    per-district statistic (each district's own 1990-2025 record), so for a
    province we report the range across its districts rather than a single
    number.
    """
    province = request.args.get("province")
    con = get_con()
    climate = con.execute(
        "SELECT c.year, "
        "ROUND(SUM(c.heat_days * d.area_km2) / SUM(d.area_km2), 1) AS heat_days, "
        "ROUND(SUM(c.heatwave_days * d.area_km2) / SUM(d.area_km2), 1) AS heatwave_days, "
        "ROUND(SUM(c.heavy_rain_days * d.area_km2) / SUM(d.area_km2), 1) AS heavy_rain_days, "
        "ROUND(SUM(c.mean_tmax_c * d.area_km2) / SUM(d.area_km2), 1) AS mean_tmax_c, "
        "ROUND(SUM(c.total_precip_mm * d.area_km2) / SUM(d.area_km2), 1) AS total_precip_mm "
        "FROM climate_annual c JOIN districts d ON d.district = c.district "
        "WHERE d.province = ? GROUP BY c.year ORDER BY c.year", [province]
    ).fetchall()
    hiv = con.execute(
        "SELECT h.survey_round, h.year, SUM(h.n_tested) AS n_tested, "
        "ROUND(SUM(h.prevalence_weighted_pct * h.n_tested) / SUM(h.n_tested), 2) AS prevalence_weighted_pct "
        "FROM hiv_burden h JOIN districts d ON d.district = h.district "
        "WHERE d.province = ? GROUP BY h.survey_round, h.year ORDER BY h.year", [province]
    ).fetchall()
    malaria = con.execute(
        "SELECT m.year, "
        "ROUND(SUM(m.pf_parasite_rate * d.area_km2) / SUM(d.area_km2), 2) AS pf_parasite_rate, "
        "ROUND(SUM(m.pf_incidence_rate * d.area_km2) / SUM(d.area_km2), 2) AS pf_incidence_rate "
        "FROM malaria_annual m JOIN districts d ON d.district = m.district "
        "WHERE d.province = ? GROUP BY m.year ORDER BY m.year", [province]
    ).fetchall()
    threshold_row = con.execute(
        "SELECT ROUND(MIN(t.heat_threshold_c),1), ROUND(MAX(t.heat_threshold_c),1) "
        "FROM district_thresholds t JOIN districts d ON d.district = t.district "
        "WHERE d.province = ?", [province]
    ).fetchone()
    con.close()
    return jsonify({
        "province": province,
        "heat_threshold_range_c": list(threshold_row) if threshold_row and threshold_row[0] is not None else None,
        "climate": [dict(zip(["year", "heat_days", "heatwave_days", "heavy_rain_days", "mean_tmax_c", "total_precip_mm"], r)) for r in climate],
        "hiv": [dict(zip(["survey_round", "year", "n_tested", "prevalence_weighted_pct"], r)) for r in hiv],
        "malaria": [dict(zip(["year", "pf_parasite_rate", "pf_incidence_rate"], r)) for r in malaria],
    })


@app.route("/api/province_districts")
def province_districts():
    """
    Every district's own annual series inside one province, so the province
    detail panel can draw one line per district (thin) under the
    province's area-weighted line (bold). Same column set/rounding as
    /api/district_timeseries so the frontend can treat them identically.
    """
    province = request.args.get("province")
    con = get_con()
    names = [r[0] for r in con.execute(
        "SELECT district FROM districts WHERE province = ? ORDER BY district", [province]).fetchall()]
    out = {}
    for n in names:
        climate = con.execute(
            "SELECT year, heat_days, heatwave_days, heavy_rain_days, ROUND(mean_tmax_c,1), ROUND(total_precip_mm,1) "
            "FROM climate_annual WHERE district = ? ORDER BY year", [n]).fetchall()
        hiv = con.execute(
            "SELECT survey_round, year, ROUND(prevalence_weighted_pct,2) FROM hiv_burden WHERE district = ? ORDER BY year", [n]).fetchall()
        malaria = con.execute(
            "SELECT year, ROUND(pf_parasite_rate,2), ROUND(pf_incidence_rate,2) FROM malaria_annual WHERE district = ? ORDER BY year", [n]).fetchall()
        out[n] = {
            "climate": [dict(zip(["year", "heat_days", "heatwave_days", "heavy_rain_days", "mean_tmax_c", "total_precip_mm"], r)) for r in climate],
            "hiv": [dict(zip(["survey_round", "year", "prevalence_weighted_pct"], r)) for r in hiv],
            "malaria": [dict(zip(["year", "pf_parasite_rate", "pf_incidence_rate"], r)) for r in malaria],
        }
    con.close()
    return jsonify({"province": province, "districts": out})


@app.route("/api/persistent_risk")
def persistent_risk():
    """
    Composite, time-averaged risk ranking: min-max normalises mean daily max
    temp and heavy-rain days (averaged across all available years per
    district), and HIV prevalence (averaged across the 3 survey rounds),
    then averages the three normalised scores. Answers "which districts are
    persistently in the highest-risk group" directly, rather than asking the
    viewer to eyeball several separate layers.

    Uses mean_tmax_c, not heat_days, for the heat component -- heat_days is
    each district's own count of days above *that district's own* 90th
    percentile, so by construction every district averages ~36.5 days/year
    long-run (10% of 365), with no real cross-district spread (confirmed
    2026-11-03: all 62 districts average 36.53 to 2dp). Min-max normalising
    a column with no real spread divides by a near-zero denominator that is
    pure floating-point noise, which produced a different "top district"
    on almost every repeated call despite identical underlying data -- a
    real bug, not a display issue. mean_tmax_c has genuine cross-district
    variance and is the metric the app's own methodology modal already
    recommends for comparing absolute heat between districts.

    Flood (JRC Global Surface Water) is deliberately excluded here -- see
    extract_flood.py's header note: verified against 3 independent methods
    to be unreliable for Zimbabwe's terrain (bright/arid soil and urban
    surfaces misclassified as water), not a bug in the extraction. Dropped
    2026-11-02 pending a better flood proxy.
    """
    level = request.args.get("level", "district")
    con = get_con()
    climate = con.execute(
        "SELECT district, AVG(mean_tmax_c) heat, AVG(heavy_rain_days) rain FROM climate_annual GROUP BY district"
    ).fetchdf()
    hiv = con.execute(
        "SELECT district, AVG(prevalence_weighted_pct) hiv, AVG(n_tested) n_tested FROM hiv_burden GROUP BY district"
    ).fetchdf()
    districts_meta = con.execute("SELECT district, province, area_km2 FROM districts").fetchdf()
    con.close()

    df = climate.merge(hiv, on="district").merge(districts_meta, on="district")

    if level == "province":
        # Roll the already-per-district, already-averaged-across-years heat
        # and rain up to province by area; roll HIV up by each district's
        # average test count -- same weighting rationale as /api/layer/*.
        def weighted(group, col, weight_col):
            w = group[weight_col]
            return (group[col] * w).sum() / w.sum()

        rows = []
        for province, g in df.groupby("province"):
            rows.append({
                "province": province,
                "heat": weighted(g, "heat", "area_km2"),
                "rain": weighted(g, "rain", "area_km2"),
                "hiv": weighted(g, "hiv", "n_tested"),
            })
        work = pd.DataFrame(rows)
        key = "province"
    else:
        work = df[["district", "heat", "rain", "hiv"]].copy()
        key = "district"

    def norm(col):
        lo, hi = work[col].min(), work[col].max()
        # A spread that's floating-point noise (not a real difference) must
        # be treated the same as no spread at all -- dividing by it would
        # amplify that noise into an unstable, run-to-run-different score
        # (this is what the mean_tmax_c swap above fixes for heat_days, but
        # the guard stays as protection against the same class of bug in
        # any future column with little real variance).
        return (work[col] - lo) / (hi - lo) if (hi - lo) > 1e-6 else work[col] * 0

    work["heat_score"] = norm("heat")
    work["rain_score"] = norm("rain")
    work["hiv_score"] = norm("hiv")
    work["composite_score"] = work[["heat_score", "rain_score", "hiv_score"]].mean(axis=1)

    out = work.sort_values("composite_score", ascending=False)[
        [key, "heat", "rain", "hiv", "composite_score"]
    ].round(2)
    return jsonify(out.to_dict(orient="records"))


@app.route("/api/daily")
def daily():
    """
    Day-by-day tmax/precip for one district or province, one calendar
    month at a time -- backs the detail panel's daily calendar view.
    climate_daily only stores district rows, so a province request
    area-weights across its districts per day (same convention as every
    other province aggregate in this app). Also returns the district's
    (or province's districts' min/max) TX90p heat threshold, so the
    frontend can shade days as "hot for this place" consistently with
    every other heat layer, rather than an unrelated colour scale.
    """
    name = request.args.get("name")
    level = request.args.get("level", "district")
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    con = get_con()
    if level == "province":
        rows = con.execute(
            "SELECT c.date, "
            "ROUND(SUM(c.tmax_c * d.area_km2) / SUM(d.area_km2), 1) AS tmax_c, "
            "ROUND(SUM(c.precip_mm * d.area_km2) / SUM(d.area_km2), 1) AS precip_mm "
            "FROM climate_daily c JOIN districts d ON d.district = c.district "
            "WHERE d.province = ? AND EXTRACT(year FROM c.date) = ? AND EXTRACT(month FROM c.date) = ? "
            "GROUP BY c.date ORDER BY c.date", [name, year, month]
        ).fetchall()
        threshold_row = con.execute(
            "SELECT ROUND(MIN(t.heat_threshold_c),1), ROUND(MAX(t.heat_threshold_c),1) "
            "FROM district_thresholds t JOIN districts d ON d.district = t.district WHERE d.province = ?", [name]
        ).fetchone()
    else:
        rows = con.execute(
            "SELECT date, ROUND(tmax_c,1), ROUND(precip_mm,1) FROM climate_daily "
            "WHERE district = ? AND EXTRACT(year FROM date) = ? AND EXTRACT(month FROM date) = ? "
            "ORDER BY date", [name, year, month]
        ).fetchall()
        threshold_row = con.execute(
            "SELECT ROUND(heat_threshold_c,1), ROUND(heat_threshold_c,1) FROM district_thresholds WHERE district = ?", [name]
        ).fetchone()
    con.close()
    return jsonify({
        "days": [{"date": str(r[0]), "tmax_c": r[1], "precip_mm": r[2]} for r in rows],
        "heat_threshold_range_c": list(threshold_row) if threshold_row and threshold_row[0] is not None else None,
    })


@app.route("/api/monthly_climatology")
def monthly_climatology():
    """
    "What does a typical month look like here" -- mean daily max temp and
    total/rainy-day counts per calendar month, averaged across all
    available years (1990-2025), not a single year's trend. Answers a
    different question than the year-by-year layers: not "how has this
    changed over time" but "when in the year does it usually happen".

    rainy_days here is a plain >0.2mm "any measurable rain" day count,
    distinct from the heavy_rain_days layer's >=20mm threshold -- this is
    a seasonality measure (how many days of the month typically see any
    rain at all), not a repeat of the heavy-rain exposure metric.

    compare_year (optional): also returns that ONE year's actual per-month
    values alongside the long-run average, as "year_data" -- backs the
    "how did this year compare to typical" overlay in the detail panel.
    Unlike the annual choropleth layers/year slider, this is safe to run
    for a still-in-progress year (e.g. partial 2026) since each month
    either has a complete value or isn't returned at all -- there's no
    "partial year total" to misread the way there would be on an annual
    layer.
    """
    name = request.args.get("name")
    level = request.args.get("level", "district")
    compare_year = request.args.get("compare_year", type=int)
    con = get_con()
    if level == "province":
        climatology_sql = (
            "WITH daily_w AS ("
            "  SELECT c.date, "
            "         SUM(c.tmax_c * d.area_km2) / SUM(d.area_km2) AS tmax_c, "
            "         SUM(c.precip_mm * d.area_km2) / SUM(d.area_km2) AS precip_mm "
            "  FROM climate_daily c JOIN districts d ON d.district = c.district "
            "  WHERE d.province = ? GROUP BY c.date"
            "), monthly AS ("
            "  SELECT EXTRACT(year FROM date) yr, EXTRACT(month FROM date) mo, "
            "         AVG(tmax_c) mean_tmax, SUM(precip_mm) total_precip, "
            "         COUNT(*) FILTER (WHERE precip_mm > 0.2) rainy_days "
            "  FROM daily_w GROUP BY yr, mo"
            ") SELECT mo, ROUND(AVG(mean_tmax),1), ROUND(AVG(total_precip),1), ROUND(AVG(rainy_days),1) "
            "FROM monthly GROUP BY mo ORDER BY mo"
        )
        climatology_rows = con.execute(climatology_sql, [name]).fetchall()
        year_rows = None
        if compare_year:
            year_sql = (
                "WITH daily_w AS ("
                "  SELECT c.date, "
                "         SUM(c.tmax_c * d.area_km2) / SUM(d.area_km2) AS tmax_c, "
                "         SUM(c.precip_mm * d.area_km2) / SUM(d.area_km2) AS precip_mm "
                "  FROM climate_daily c JOIN districts d ON d.district = c.district "
                "  WHERE d.province = ? AND EXTRACT(year FROM c.date) = ? GROUP BY c.date"
                ") SELECT EXTRACT(month FROM date) mo, ROUND(AVG(tmax_c),1), ROUND(SUM(precip_mm),1), "
                "COUNT(*) FILTER (WHERE precip_mm > 0.2) "
                "FROM daily_w GROUP BY mo ORDER BY mo"
            )
            year_rows = con.execute(year_sql, [name, compare_year]).fetchall()
    else:
        climatology_sql = (
            "WITH monthly AS ("
            "  SELECT EXTRACT(year FROM date) yr, EXTRACT(month FROM date) mo, "
            "         AVG(tmax_c) mean_tmax, SUM(precip_mm) total_precip, "
            "         COUNT(*) FILTER (WHERE precip_mm > 0.2) rainy_days "
            "  FROM climate_daily WHERE district = ? GROUP BY yr, mo"
            ") SELECT mo, ROUND(AVG(mean_tmax),1), ROUND(AVG(total_precip),1), ROUND(AVG(rainy_days),1) "
            "FROM monthly GROUP BY mo ORDER BY mo"
        )
        climatology_rows = con.execute(climatology_sql, [name]).fetchall()
        year_rows = None
        if compare_year:
            year_sql = (
                "SELECT EXTRACT(month FROM date) mo, ROUND(AVG(tmax_c),1), ROUND(SUM(precip_mm),1), "
                "COUNT(*) FILTER (WHERE precip_mm > 0.2) "
                "FROM climate_daily WHERE district = ? AND EXTRACT(year FROM date) = ? "
                "GROUP BY mo ORDER BY mo"
            )
            year_rows = con.execute(year_sql, [name, compare_year]).fetchall()
    con.close()
    cols = ["month", "mean_tmax_c", "total_precip_mm", "rainy_days"]
    return jsonify({
        "climatology": [dict(zip(cols, r)) for r in climatology_rows],
        "year_data": [dict(zip(cols, r)) for r in year_rows] if year_rows is not None else None,
    })


@app.route("/api/ai/chat", methods=["POST"])
def ai_chat():
    """
    Natural-language assistant (see ai_agent.py for the actual logic/validation gate).
    Stateless on purpose -- no server-side session -- the frontend holds the full
    conversation and resends it every turn (messages[-1] is the new user turn); this
    endpoint just runs one agent turn and hands back the updated history plus whatever
    validated actions (map views / chart data) resulted, for the frontend to apply/render.
    """
    client = ai_agent.new_client()
    if client is None:
        return jsonify({"error": "AI assistant isn't configured right now (no API key available). Try again shortly, or use the map controls directly."}), 503

    body = request.get_json(force=True, silent=True) or {}
    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        return jsonify({"error": "messages is required and must be a non-empty list."}), 400

    con = ai_agent.get_con()
    try:
        text, actions = ai_agent.run_agent_turn(client, con, messages)
    except Exception as e:
        return jsonify({"error": f"The assistant hit an error and couldn't finish: {e}"}), 502
    finally:
        con.close()

    return jsonify({"text": text, "actions": actions, "messages": messages})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8506))
    app.run(host="127.0.0.1", port=port)
