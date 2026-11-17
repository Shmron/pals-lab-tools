"""
Natural-language assistant for the Climate & Health Atlas: "show me X" or "is Y
correlated with Z" turns into either (a) a validated, reproducible setting of this app's
own real map/correlation controls, or (b) a chart built from this app's own already
-extracted tables -- never a number invented by the model.

Architecture follows the same pattern already proven by Shmron (PALs Earth's assistant,
see palsearth*/core/ai_agent.py): the model proposes an action via a tool call, and a
separate _validate_* function checks every field against real, live values (actual layer
keys, actual year/round lists pulled from the database, actual district/province names)
before anything is applied or queried. A tool call that fails validation returns a plain
error back to the model to retry or explain, not a best-guess fallback -- and the system
prompt tells the model explicitly to say "I don't have that" rather than approximate when
nothing in these tables answers the question.

Two tools, deliberately not more:
  - set_view: drives the app's own existing choropleth or correlation view (layer/level/
    time/place, or health-var x climate-var for the correlation view). This is the
    "reproducible" path -- the result is a real, ordinary state of the app itself,
    reachable by clicking, not a one-off render.
  - chart_query: for comparisons the existing views can't show directly (e.g. a
    cross-district scatter, or one place's full history as a standalone chart) --
    pulls real rows from this app's own tables via the same aggregation rules
    /api/layer/* already uses (area-weighted for province, national-flat for TB),
    returns data for the frontend to chart, never pre-rendered or guessed.
"""
import json
import os

import duckdb
from anthropic import Anthropic

from core.apikey import get_anthropic_key

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "climate_hiv_atlas.duckdb")
MODEL = "claude-sonnet-5"

# Single source of truth for every chartable/settable metric: which table it lives in,
# how to weight it at province level (None = already-identical-everywhere, like TB), and
# its time axis. Mirrors app.py's CLIMATE_RANGE_COLS/MALARIA_RANGE_COLS/TB_RANGE_COLS --
# kept as a separate registry here (not imported from app.py) because this module's job
# is answering natural-language queries about the data, a different concern from app.py's
# job of serving the map's own API, even though the underlying facts must stay in sync.
METRICS = {
    "heat_days": {"table": "climate_annual", "col": "heat_days", "weight": "area", "time": "annual", "label": "Extreme heat days/year"},
    "heatwave_days": {"table": "climate_annual", "col": "heatwave_days", "weight": "area", "time": "annual", "label": "Heatwave days/year"},
    "mean_tmax_c": {"table": "climate_annual", "col": "mean_tmax_c", "weight": "area", "time": "annual", "label": "Mean daily max temp, degC"},
    "heavy_rain_days": {"table": "climate_annual", "col": "heavy_rain_days", "weight": "area", "time": "annual", "label": "Heavy rain days/year"},
    "total_precip_mm": {"table": "climate_annual", "col": "total_precip_mm", "weight": "area", "time": "annual", "label": "Total annual precipitation, mm"},
    "prevalence_weighted_pct": {"table": "hiv_burden", "col": "prevalence_weighted_pct", "weight": "n_tested", "time": "hiv_round", "label": "HIV prevalence, % (DHS, weighted)"},
    "pf_parasite_rate": {"table": "malaria_annual", "col": "pf_parasite_rate", "weight": "area", "time": "annual", "label": "Malaria prevalence, PfPR2-10, %"},
    "pf_incidence_rate": {"table": "malaria_annual", "col": "pf_incidence_rate", "weight": "area", "time": "annual", "label": "Malaria incidence, per 1,000/yr"},
    "tb_incidence_100k": {"table": "tb_national", "col": "incidence_100k", "weight": None, "time": "tb_annual", "label": "TB incidence, per 100,000/yr (national)"},
    "tb_hiv_coinfection_pct": {"table": "tb_national", "col": "tbhiv_prct", "weight": None, "time": "tb_annual", "label": "TB-HIV coinfection, % (national)"},
}
CLIMATE_METRICS = [k for k, v in METRICS.items() if v["table"] == "climate_annual"]
HEALTH_METRICS_FOR_CORRELATION = ["prevalence_weighted_pct", "pf_parasite_rate", "pf_incidence_rate"]


def get_con():
    return duckdb.connect(DB_PATH, read_only=True)


# ---------------------------------------------------------------------------
# System prompt -- built fresh per request (cheap, all local SELECTs) so the valid
# year/round lists and district/province names can never drift out of sync with the
# live database, the same reason app.py's /api/meta computes these at request time
# instead of hardcoding them.
# ---------------------------------------------------------------------------

METHODOLOGY = """
## Data sources and methods (the ONLY grounding for any answer about where a number comes from or how it was calculated -- do not improvise beyond this)

**Climate (heat & rain):** District-mean values from ERA5-Land (~11km) and CHIRPS
(~5.5km), 1990-2025. An extreme heat day is defined PER DISTRICT, not a flat national
threshold: a day whose max temp exceeds that district's own 90th percentile across its
full 1990-2025 record (WMO/ETCCDI "TX90p" convention) -- so "extreme heat days" means a
different absolute temperature in Harare vs. Beitbridge by design. A heatwave is 3+
consecutive such days in the same calendar year. Heavy rain day is a flat >=20mm
(CHIRPS). Large districts get an area-averaged value that can smooth over local extremes.

**HIV burden:** Zimbabwe DHS HIV test results (AR recode), for the 2005-06, 2010-11 and
2015 survey rounds ONLY -- no other rounds have HIV testing loaded, and there is no
district-level HIV data for any other year. Weighted by each person's DHS sample weight.
Each tested person is assigned to a district via their DHS cluster's GPS coordinates,
which DHS displaces ~2-10km for anonymity, so district assignment near boundaries is
approximate.

**Malaria:** Zimbabwe's DHS rounds never ran a malaria testing module (checked directly:
every malaria test-result column in every DHS recode on file is empty; only bednet
-ownership counts exist). Values are instead Malaria Atlas Project modelled estimates
(Pf parasite rate PfPR2-10, and Pf incidence per 1,000/yr), pulled from MAP's public
raster service at ~5km resolution and averaged per district. 2000-2025 only -- MAP has
no earlier modelled year for this surface.

**TB:** No district-level TB data exists for Zimbabwe at all -- DHS never tests for TB,
and WHO's Global TB Programme (the only public source) publishes TB incidence and TB-HIV
coinfection rate at NATIONAL level only, 2000-2024, no subnational breakdown. This is why
the TB layer colors the whole country one flat shade per year on the map -- it is a
national figure, not a spatial pattern, and there is no way to say which district has
"more TB" because that number does not exist.

**Province aggregation:** Climate and malaria values are area-weighted means across a
province's districts (no population layer is loaded, so area is the only weighting
available -- a province with one small extreme district and one large mild one reads
closer to the mild one). HIV is a test-count-weighted mean. TB needs no weighting -- it
is the same single national value at every level.
""".strip()


def _valid_years(con, metric_key):
    m = METRICS[metric_key]
    if m["time"] == "hiv_round":
        return [r[0] for r in con.execute("SELECT DISTINCT survey_round FROM hiv_burden ORDER BY year").fetchall()]
    if m["time"] == "tb_annual":
        return [r[0] for r in con.execute(f"SELECT DISTINCT year FROM {m['table']} ORDER BY year").fetchall()]
    return [r[0] for r in con.execute(f"SELECT DISTINCT year FROM {m['table']} ORDER BY year").fetchall()]


def build_system_prompt(con):
    districts = [r[0] for r in con.execute("SELECT district FROM districts ORDER BY district").fetchall()]
    provinces = [r[0] for r in con.execute("SELECT province FROM provinces ORDER BY province").fetchall()]
    metric_lines = []
    for key, m in METRICS.items():
        years = _valid_years(con, key)
        span = f"{years[0]}-{years[-1]}" if m["time"] != "hiv_round" else ", ".join(years)
        metric_lines.append(f"- `{key}` ({m['label']}): valid time values are {span}")

    return f"""You are the natural-language assistant for the Zimbabwe Climate & Health
Atlas (a choropleth map covering climate, HIV, malaria and TB indicators across
Zimbabwe's 62 districts / 10 provinces, 1990-2025 depending on the indicator).

{METHODOLOGY}

## Every metric this app has, and its valid time range -- these are the ONLY valid
values for a layer/metric and a time_value. Do not accept or guess a year/round outside
what's listed here; if the user asks for one, say plainly it isn't available and state
the actual valid range instead.
{chr(10).join(metric_lines)}

## Daily records (separate from the metrics above)
Every metric above (heat_days, mean_tmax_c, total_precip_mm, etc.) is a YEAR-level
figure -- an annual count or mean. Real per-DAY temperature and rainfall records also
exist, 1990-present, one row per district per day (tmax_c, precip_mm), for questions
about a single day's extreme (e.g. "what was the hottest day ever, and where/when") --
use find_daily_extreme for these, not find_extreme. Don't confuse the two: a district's
highest mean_tmax_c year (an annual average) is NOT the same thing as its hottest single
day, and will read much lower -- say so explicitly if a user's question could be
mistaken for the annual-mean version of the question.

## Valid districts (62): {", ".join(districts)}

## Valid provinces (10): {", ".join(provinces)}

## Hard rules
1. NEVER state a specific number, value, trend, or comparison you did not just retrieve
   from a tool call in this turn. If you have not called a tool, you do not have a real
   number to report; say what you could check instead, or call the tool.
2. If a request falls outside what these tables can answer (wrong year, a place not in
   the list, an indicator that doesn't exist, asking for something below national level
   for TB, asking for something before a metric's earliest valid year), say so plainly
   and state what IS available instead. Never approximate, interpolate, or guess to fill
   the gap.
3. The FIRST time a source or a distinction matters in a conversation (e.g. "this is a
   single-day record, not an annual average" or which agency a number is from), say it.
   Once it has been established, do not repeat the same caveat or source attribution
   again for the same kind of figure later in the same conversation; the user has
   already read it. Treat every reply as part of an ongoing conversation, not a
   standalone fact sheet that re-explains its own provenance each time.
4. Prefer set_view when the request maps onto a real layer/view this app already has
   (single indicator over time/place, or two variables against each other via the
   correlation view) -- that gives a reproducible, clickable result, not just a chat
   answer. Use chart_query for a snapshot comparison across places (scatter/bar, one
   year), or a time series for one place, the whole country (level="national"), or
   two metrics together (line). chart_query's tool result includes every data point it
   fetched, so you can read it directly to answer follow-up questions about that same
   chart (e.g. "which year in that series was highest") without a second call. For a
   TRUE all-time extreme, still prefer find_extreme/find_daily_extreme over eyeballing a
   chart_query result -- they answer it exactly, not approximately. find_extreme can
   also be scoped to one place via its optional `place` field (e.g. "Beitbridge's own
   hottest year", not the nationwide record) -- use that instead of scanning a
   chart_query series by hand whenever the question is specifically about one place's
   own extreme. Use find_daily_extreme instead of find_extreme when the question is
   about one specific DAY's temperature/rainfall extreme, not a year's mean -- see
   "Daily records" above.
5. Keep explanations plain-language and brief. You're talking to public health
   researchers, not GIS specialists; explain a term in one clause if you use it, don't
   assume familiarity with "reducer", "tercile", "raster", etc.
6. Write in plain prose, not markdown. No **bold**, no *italics*, no bullet-point lists
   for a short answer, and no em dashes -- use a period or comma instead of a dash to
   join clauses. Write the way you'd say it out loud, as complete plain sentences.
"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "set_view",
        "description": (
            "Set the map to a single indicator's choropleth, or to the climate x health "
            "correlation view (two indicators at once). This is the preferred tool "
            "whenever the request is about one or two of this app's own indicators -- "
            "the result is a real, reproducible state of the app itself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["single", "correlation"]},
                "metric": {"type": "string", "description": "Required for mode=single. One of the metric keys listed in the system prompt."},
                "health_metric": {"type": "string", "description": "Required for mode=correlation. One of prevalence_weighted_pct, pf_parasite_rate, pf_incidence_rate."},
                "climate_metric": {"type": "string", "description": "Required for mode=correlation. One of the climate_annual metric keys."},
                "level": {"type": "string", "enum": ["district", "province"]},
                "time_value": {"type": "string", "description": "A year (as a string, e.g. '2015') or, for prevalence_weighted_pct, a DHS round string (e.g. '2015')."},
                "place": {"type": "string", "description": "Optional: a specific district or province name to also open the detail panel for."},
            },
            "required": ["mode", "level", "time_value"],
        },
    },
    {
        "name": "chart_query",
        "description": (
            "Fetch real data for a chart the existing map views can't show directly: a "
            "snapshot comparison across all districts/provinces for one or two metrics "
            "(scatter/bar), or one place's (or the whole country's) full history for one "
            "or two metrics over time (line). Returns raw data points only -- the app "
            "renders the chart from them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "enum": ["scatter", "bar", "line"]},
                "x_metric": {"type": "string", "description": "Metric key. For line charts, this is the primary metric plotted over time."},
                "y_metric": {"type": "string", "description": "Metric key. Required for scatter (the second axis). Optional for line (a second metric plotted alongside x_metric as a combined two-line chart, e.g. rainfall and temperature together -- only valid when both share the same time axis, i.e. both annual or both DHS-round metrics). Unused for bar."},
                "level": {"type": "string", "enum": ["district", "province", "national"], "description": "'national' is only valid with chart_type='line' -- one time series (or two, with y_metric) for the whole country, area/test-count-weighted the same way the main map's own national trend strip is computed. There is no national snapshot to compare across places, so scatter/bar never use 'national'."},
                "time_value": {"type": "string", "description": "Required for scatter/bar: a year or round. Omit for line (uses the metric's full valid range)."},
                "place": {"type": "string", "description": "Required for line charts when level is 'district' or 'province': a single district or province name. Omit when level='national'."},
            },
            "required": ["chart_type", "x_metric", "level"],
        },
    },
    {
        "name": "find_extreme",
        "description": (
            "Find the single highest or lowest ANNUAL value of one metric, either across "
            "EVERY valid place and EVERY valid year/round at once (omit place -- e.g. "
            "'which year and district had the most heat days ever'), or restricted to "
            "ONE specific place's own history (pass place -- e.g. 'which was Beitbridge's "
            "own hottest year', not the national record). Every metric here is a "
            "year-level figure (e.g. mean_tmax_c is a district's MEAN daily max temp "
            "across a whole year) -- for a true single-DAY temperature or rainfall "
            "extreme, use find_daily_extreme instead, which hits real per-day records, "
            "not an annual average."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description": "Metric key."},
                "level": {"type": "string", "enum": ["district", "province"]},
                "direction": {"type": "string", "enum": ["max", "min"]},
                "place": {"type": "string", "description": "Optional: restrict the search to this one district/province's own time series, instead of scanning every place."},
            },
            "required": ["metric", "level", "direction"],
        },
    },
    {
        "name": "find_daily_extreme",
        "description": (
            "Find the single highest or lowest DAY-LEVEL temperature or rainfall ever "
            "recorded, across all districts and all days 1990-present -- e.g. 'what was "
            "the hottest day ever in Zimbabwe, and where'. Hits real per-day records "
            "(climate_daily), not an annual average like find_extreme's mean_tmax_c -- "
            "use this whenever the question is about a single day's extreme, not a "
            "year's mean. District level only (no province-level daily aggregate exists)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["tmax_c", "precip_mm"], "description": "tmax_c = daily max temp, degC. precip_mm = daily rainfall, mm."},
                "direction": {"type": "string", "enum": ["max", "min"]},
            },
            "required": ["metric", "direction"],
        },
    },
]


# ---------------------------------------------------------------------------
# Validation -- every field checked against the live database, not the model's say-so.
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    pass


def _check_metric(key):
    if key not in METRICS:
        raise ValidationError(f"'{key}' is not a real metric in this app. Valid metric keys: {', '.join(METRICS)}")


def _check_level(level):
    if level not in ("district", "province"):
        raise ValidationError("level must be 'district' or 'province'.")


def _check_place(con, level, place):
    if place is None:
        return
    table = "districts" if level == "district" else "provinces"
    col = "district" if level == "district" else "province"
    row = con.execute(f"SELECT 1 FROM {table} WHERE {col} = ?", [place]).fetchone()
    if not row:
        valid = [r[0] for r in con.execute(f"SELECT {col} FROM {table} ORDER BY {col}").fetchall()]
        same_initial = [v for v in valid if place and v[:1].lower() == place[:1].lower()]
        suggestions = (same_initial or valid)[:5]
        raise ValidationError(f"'{place}' is not a real {level} name. Some valid names: {', '.join(suggestions)}")


def _check_time_value(con, metric_key, time_value):
    valid = [str(v) for v in _valid_years(con, metric_key)]
    if str(time_value) not in valid:
        raise ValidationError(
            f"'{time_value}' is not a valid time value for {metric_key}. "
            f"Valid values: {valid[0]}-{valid[-1]}" if METRICS[metric_key]["time"] != "hiv_round" else f"Valid DHS rounds: {', '.join(valid)}"
        )


def validate_set_view(con, inp):
    mode = inp.get("mode")
    level = inp.get("level")
    _check_level(level)
    _check_place(con, level, inp.get("place"))
    if mode == "single":
        metric = inp.get("metric")
        if not metric:
            raise ValidationError("mode=single requires metric.")
        _check_metric(metric)
        _check_time_value(con, metric, inp.get("time_value"))
    elif mode == "correlation":
        health = inp.get("health_metric")
        climate = inp.get("climate_metric")
        if health not in HEALTH_METRICS_FOR_CORRELATION:
            raise ValidationError(f"health_metric must be one of {HEALTH_METRICS_FOR_CORRELATION}.")
        if climate not in CLIMATE_METRICS:
            raise ValidationError(f"climate_metric must be one of {CLIMATE_METRICS}.")
        _check_time_value(con, health, inp.get("time_value"))
    else:
        raise ValidationError("mode must be 'single' or 'correlation'.")
    return True


def validate_chart_query(con, inp):
    level = inp.get("level")
    chart_type = inp.get("chart_type")
    # "national" only makes sense for a line (one series over time for the whole
    # country) -- scatter/bar compare VALUES ACROSS PLACES at one point in time, and
    # there is exactly one national value, nothing to compare it against.
    if level == "national":
        if chart_type != "line":
            raise ValidationError("level='national' only works with chart_type='line' (one time series for the whole country). Use district or province for scatter/bar.")
    else:
        _check_level(level)
        _check_place(con, level, inp.get("place"))
    x = inp.get("x_metric")
    _check_metric(x)
    if chart_type == "scatter":
        y = inp.get("y_metric")
        if not y:
            raise ValidationError("scatter requires y_metric.")
        _check_metric(y)
        if not inp.get("time_value"):
            raise ValidationError("scatter requires time_value (a snapshot year/round).")
        _check_time_value(con, x, inp["time_value"])
        _check_time_value(con, y, inp["time_value"])
    elif chart_type == "bar":
        if not inp.get("time_value"):
            raise ValidationError("bar requires time_value (a snapshot year/round).")
        _check_time_value(con, x, inp["time_value"])
    elif chart_type == "line":
        if level != "national" and not inp.get("place"):
            raise ValidationError("line requires place (a single district/province to trace over time), unless level='national'.")
        if inp.get("y_metric"):
            _check_metric(inp["y_metric"])
    else:
        raise ValidationError("chart_type must be 'scatter', 'bar', or 'line'.")
    return True


def validate_find_extreme(con, inp):
    _check_metric(inp.get("metric"))
    _check_level(inp.get("level"))
    _check_place(con, inp.get("level"), inp.get("place"))
    if inp.get("direction") not in ("max", "min"):
        raise ValidationError("direction must be 'max' or 'min'.")
    return True


def validate_find_daily_extreme(con, inp):
    if inp.get("metric") not in ("tmax_c", "precip_mm"):
        raise ValidationError("metric must be 'tmax_c' or 'precip_mm'.")
    if inp.get("direction") not in ("max", "min"):
        raise ValidationError("direction must be 'max' or 'min'.")
    return True


# ---------------------------------------------------------------------------
# Data fetching -- same aggregation rules /api/layer/* already uses (see app.py),
# generalised across all four tables via the METRICS registry. Only ever called after
# the corresponding validate_* has passed, so metric/level/place/time_value are already
# known-real by this point.
# ---------------------------------------------------------------------------

def _time_col(metric_key):
    return "survey_round" if METRICS[metric_key]["time"] == "hiv_round" else "year"


def fetch_snapshot(con, metric_key, level, time_value):
    """One value per district/province for one metric at one point in time."""
    m = METRICS[metric_key]
    table, col, weight = m["table"], m["col"], m["weight"]
    tcol = _time_col(metric_key)

    if weight is None:  # TB: single national value, broadcast to every place (see /api/layer/tb)
        row = con.execute(f"SELECT {col} FROM {table} WHERE {tcol} = ?", [time_value]).fetchone()
        value = row[0] if row else None
        name_table, name_col = ("districts", "district") if level == "district" else ("provinces", "province")
        names = [r[0] for r in con.execute(f"SELECT {name_col} FROM {name_table}").fetchall()]
        return {name: value for name in names}

    if level == "district":
        rows = con.execute(f"SELECT district, {col} FROM {table} WHERE {tcol} = ?", [time_value]).fetchall()
        return {r[0]: r[1] for r in rows}

    weight_expr = "d.area_km2" if weight == "area" else "t.n_tested"
    rows = con.execute(
        f"SELECT d.province, SUM(t.{col} * {weight_expr}) / SUM({weight_expr}) "
        f"FROM {table} t JOIN districts d ON d.district = t.district "
        f"WHERE t.{tcol} = ? GROUP BY d.province", [time_value]
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def fetch_timeseries(con, metric_key, level, place):
    """One metric's full valid-year/round history for a district, a province, or (place
    ignored) the whole country -- same area/test-count weighting app.py's own
    /api/national_timeseries and /api/province_timeseries use, just generalised across
    all four tables via the METRICS registry instead of one query per table in app.py.
    """
    m = METRICS[metric_key]
    table, col, weight = m["table"], m["col"], m["weight"]
    tcol = _time_col(metric_key)

    if weight is None:  # TB: same national series regardless of place/level
        rows = con.execute(f"SELECT {tcol}, {col} FROM {table} ORDER BY year").fetchall()
        return [{"time": r[0], "value": r[1]} for r in rows]

    if level == "district":
        rows = con.execute(f"SELECT {tcol}, {col} FROM {table} WHERE district = ? ORDER BY year", [place]).fetchall()
        return [{"time": r[0], "value": r[1]} for r in rows]

    weight_expr = "d.area_km2" if weight == "area" else "t.n_tested"
    if level == "national":
        rows = con.execute(
            f"SELECT t.{tcol}, SUM(t.{col} * {weight_expr}) / SUM({weight_expr}) "
            f"FROM {table} t JOIN districts d ON d.district = t.district "
            f"GROUP BY t.{tcol} ORDER BY MIN(t.year)"
        ).fetchall()
    else:  # province
        rows = con.execute(
            f"SELECT t.{tcol}, SUM(t.{col} * {weight_expr}) / SUM({weight_expr}) "
            f"FROM {table} t JOIN districts d ON d.district = t.district "
            f"WHERE d.province = ? GROUP BY t.{tcol} ORDER BY MIN(t.year)", [place]
        ).fetchall()
    return [{"time": r[0], "value": r[1]} for r in rows]


def run_chart_query(con, inp):
    chart_type = inp["chart_type"]
    level = inp["level"]
    if chart_type == "line":
        place = "Zimbabwe (national)" if level == "national" else inp["place"]
        series = fetch_timeseries(con, inp["x_metric"], level, inp.get("place"))
        result = {
            "chart_type": "line", "level": level, "place": place,
            "x_metric": inp["x_metric"], "x_label": METRICS[inp["x_metric"]]["label"],
            "points": series,
        }
        # Optional second metric -- e.g. "rainfall and temperature together" -- plotted
        # as a combined dual-axis line chart by the frontend (very different units/
        # scales, e.g. degC vs mm, so they can't share one y-axis).
        if inp.get("y_metric"):
            result["y_metric"] = inp["y_metric"]
            result["y_label"] = METRICS[inp["y_metric"]]["label"]
            result["y_points"] = fetch_timeseries(con, inp["y_metric"], level, inp.get("place"))
        return result
    snapshot_x = fetch_snapshot(con, inp["x_metric"], level, inp["time_value"])
    if chart_type == "bar":
        points = [{"place": k, "value": v} for k, v in snapshot_x.items() if v is not None]
        points.sort(key=lambda p: p["value"], reverse=True)
        return {
            "chart_type": "bar", "level": level, "time_value": inp["time_value"],
            "x_metric": inp["x_metric"], "x_label": METRICS[inp["x_metric"]]["label"],
            "points": points,
        }
    # scatter
    snapshot_y = fetch_snapshot(con, inp["y_metric"], level, inp["time_value"])
    places = [p for p in snapshot_x if snapshot_x[p] is not None and snapshot_y.get(p) is not None]
    points = [{"place": p, "x": snapshot_x[p], "y": snapshot_y[p]} for p in places]
    return {
        "chart_type": "scatter", "level": level, "time_value": inp["time_value"],
        "x_metric": inp["x_metric"], "x_label": METRICS[inp["x_metric"]]["label"],
        "y_metric": inp["y_metric"], "y_label": METRICS[inp["y_metric"]]["label"],
        "points": points,
    }


def run_find_extreme(con, inp):
    """
    True all-time extreme: scans every valid place AND every valid year/round for one
    metric in a single query, not just one year (chart_query's bar/scatter) or one place
    (chart_query's line) -- the gap that made "which year and district was hottest ever"
    unanswerable before this existed (the model would correctly refuse rather than guess,
    but now it has a real way to answer instead of just explaining it can't).
    """
    metric_key, level, direction = inp["metric"], inp["level"], inp["direction"]
    scope_place = inp.get("place")  # None = scan every place; set = this one place's own history only
    m = METRICS[metric_key]
    table, col, weight = m["table"], m["col"], m["weight"]
    tcol = _time_col(metric_key)
    order = "DESC" if direction == "max" else "ASC"

    if weight is None:  # TB: national only, no place dimension to scope to
        row = con.execute(f"SELECT {tcol}, {col} FROM {table} WHERE {col} IS NOT NULL ORDER BY {col} {order} LIMIT 1").fetchone()
        place, time_val, value = ("Zimbabwe (national)", row[0], row[1]) if row else (None, None, None)
    elif level == "district":
        if scope_place:
            row = con.execute(
                f"SELECT district, {tcol}, {col} FROM {table} WHERE district = ? AND {col} IS NOT NULL ORDER BY {col} {order} LIMIT 1",
                [scope_place],
            ).fetchone()
        else:
            row = con.execute(f"SELECT district, {tcol}, {col} FROM {table} WHERE {col} IS NOT NULL ORDER BY {col} {order} LIMIT 1").fetchone()
        place, time_val, value = row if row else (None, None, None)
    else:
        weight_expr = "d.area_km2" if weight == "area" else "t.n_tested"
        province_filter = "WHERE province = ?" if scope_place else ""
        params = [scope_place] if scope_place else []
        row = con.execute(
            f"WITH prov AS ("
            f"  SELECT d.province, t.{tcol} AS time_val, SUM(t.{col} * {weight_expr}) / SUM({weight_expr}) AS value "
            f"  FROM {table} t JOIN districts d ON d.district = t.district GROUP BY d.province, t.{tcol}"
            f") SELECT province, time_val, value FROM prov {province_filter} "
            + ("AND" if province_filter else "WHERE") + f" value IS NOT NULL ORDER BY value {order} LIMIT 1",
            params,
        ).fetchone()
        place, time_val, value = row if row else (None, None, None)

    return {
        "metric": metric_key, "metric_label": m["label"], "level": level, "direction": direction,
        "place": place, "time": time_val, "value": value,
    }


DAILY_METRIC_LABELS = {"tmax_c": "Daily max temp, degC", "precip_mm": "Daily precipitation, mm"}


def run_find_daily_extreme(con, inp):
    """
    True single-day extreme from climate_daily's real per-day records (833k+ rows,
    1990-present) -- the gap find_extreme can't close, since every metric there is a
    year-level figure (mean_tmax_c is a MEAN across a whole year, nowhere near a single
    day's spike). District level only -- no province-level daily aggregate exists, and
    building one on the fly over 833k rows for a single lookup isn't worth it here.
    """
    metric, direction = inp["metric"], inp["direction"]
    order = "DESC" if direction == "max" else "ASC"
    row = con.execute(
        f"SELECT district, date, {metric} FROM climate_daily WHERE {metric} IS NOT NULL ORDER BY {metric} {order} LIMIT 1"
    ).fetchone()
    district, date_val, value = row if row else (None, None, None)
    return {
        "metric": metric, "metric_label": DAILY_METRIC_LABELS[metric], "direction": direction,
        "place": district, "time": str(date_val) if date_val is not None else None, "value": value,
    }


# ---------------------------------------------------------------------------
# Agent loop -- same shape as Shmron's run_agent_turn: Claude proposes a tool call,
# we validate for real before doing anything, feed the outcome back, repeat until the
# model stops calling tools. actions accumulates every validated tool call this turn so
# the frontend can apply/render all of them (usually just one).
# ---------------------------------------------------------------------------

def run_agent_turn(client, con, messages):
    system_prompt = build_system_prompt(con)
    actions = []

    for _ in range(6):  # hard cap -- a well-behaved turn never needs more than 1-2 tool calls
        # max_tokens=1024 (the old value) silently truncated real answers mid-sentence:
        # Sonnet 5 runs adaptive thinking by default (budget_tokens is removed/rejected
        # on this model), and thinking tokens count against max_tokens same as the
        # visible answer -- a tight cap left too little room once thinking ate its share.
        # effort="low" keeps this fast and cheap for what's actually a routing +
        # brief-explanation task, not deep reasoning (confirmed against real test
        # queries -- correctness didn't depend on higher effort), while the generous
        # max_tokens means hitting the cap is no longer the default outcome.
        resp = client.messages.create(
            model=MODEL, max_tokens=16000, system=system_prompt, tools=TOOLS, messages=messages,
            output_config={"effort": "low"},
        )
        # Plain dicts, not SDK content-block objects -- this messages list round-trips
        # through Flask/JSON to the frontend and back every turn (so the conversation
        # persists across requests without server-side session state), and the Anthropic
        # API itself accepts plain dicts matching its schema just as well as SDK objects.
        content_dicts = [b.model_dump() for b in resp.content]
        messages.append({"role": "assistant", "content": content_dicts})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in resp.content if b.type == "text")
            return text, actions

        tool_results = []
        for tu in tool_uses:
            try:
                if tu.name == "set_view":
                    validate_set_view(con, tu.input)
                    actions.append({"type": "set_view", **tu.input})
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "View set."})
                elif tu.name == "chart_query":
                    validate_chart_query(con, tu.input)
                    data = run_chart_query(con, tu.input)
                    actions.append({"type": "chart_query", **data})
                    # The full fetched data, not just a point count -- a count-only
                    # result left the model unable to answer any follow-up question
                    # about data it had just pulled (e.g. "which year in that chart was
                    # highest"), forcing it to either guess or wrongly claim the tool
                    # couldn't give it real values. Small enough to always include in
                    # full (at most ~62 points for a district-level bar/scatter, ~36 for
                    # an annual line) -- no reason to summarize away real numbers here.
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(data)})
                elif tu.name == "find_extreme":
                    validate_find_extreme(con, tu.input)
                    data = run_find_extreme(con, tu.input)
                    actions.append({"type": "find_extreme", **data})
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(data)})
                elif tu.name == "find_daily_extreme":
                    validate_find_daily_extreme(con, tu.input)
                    data = run_find_daily_extreme(con, tu.input)
                    actions.append({"type": "find_daily_extreme", **data})
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(data)})
                else:
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"Unknown tool {tu.name}.", "is_error": True})
            except ValidationError as e:
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": str(e), "is_error": True})
        messages.append({"role": "user", "content": tool_results})

    return "I had trouble resolving that request -- could you rephrase it?", actions


def new_client():
    key = get_anthropic_key()
    if not key:
        return None
    return Anthropic(api_key=key)
    return True
