"""
Shmron AI — the tool-using assistant for PALs Earth.

Unlike a plain chatbot, this agent can actually drive the extraction
pipeline on the user's behalf:
  - list_datasets:            introspects the real dataset catalog
  - propose_custom_dataset:   for anything not in the catalog, drafts a new
                               dataset definition and verifies it against the
                               real Earth Engine catalog (read-only) before
                               it's usable — no free-form code is ever
                               written or executed, only a validated config
                               dict in the same shape core.datasets.DATASETS
                               already uses.
  - submit_extraction_job:    runs the same create_job()/run_job_in_background()
                               path the manual Extract page uses, against the
                               user's uploaded coordinates for this session.
  - check_job_status:         polls job progress.

All state that must survive into the background worker process (custom
dataset definitions) is persisted on the job row itself, not just in
Streamlit session state.
"""
import json

from core.datasets import DATASETS
from core.ee_catalog import validate_dataset_candidate
from core.uploads import stage_uploaded_file
from core.windows import PREGNANCY_WINDOWS
from core.ee_catalog_search import search_catalog

MODEL = "claude-sonnet-4-6"
MAX_TOOL_ITERATIONS = 6
EE_PROJECT = "your-earth-engine-project"

# ── State backend ───────────────────────────────────────────────────────────
# This agent needs somewhere to keep per-user state across a turn: the staged
# upload, session-local custom datasets, the drafted plan and the last job id.
# Streamlit supplied st.session_state; Flask has no equivalent, so the store is
# injectable. Register one with set_state_provider(); with none registered we
# fall back to st.session_state so the Streamlit app behaves exactly as before.
_state_provider = None


def set_state_provider(fn):
    """fn() must return a dict-like that persists for one user's session."""
    global _state_provider
    _state_provider = fn


def _S():
    if _state_provider is not None:
        return _state_provider()
    import streamlit as st
    return st.session_state

SYSTEM_PROMPT = """You are Shmron AI, the assistant for PALs Earth — a geospatial data
extraction platform. Researchers upload coordinates (villages, participants, health
facilities) and pull environmental exposure data (climate, air quality, vegetation,
terrain, soil, urban, socioeconomic) from satellite/reanalysis sources via Google Earth
Engine.

**Never tell the user what background they do or do not have, and never say things like "no GIS knowledge needed". Just explain clearly, in plain words, without commenting on their expertise.**

**Assume your user is an environmental epidemiologist or health researcher with NO GIS
or remote-sensing background** — they know their health outcomes and study design cold,
but "band", "reducer", "scale in metres", "reanalysis" mean little to them. Never make
them ask what something is before you've offered a plain-language read on it. When you
mention or propose a dataset, briefly say in one sentence what it actually measures and
why a health study would want it — don't just name the collection.

**Temperature unit:** ERA5-Land air temp, MERRA-2 T2M/T2MWET, and MODIS LST are all
natively stored in Kelvin. Default to Celsius (temp_unit="celsius") for any plan that
includes one of these, unless the user specifically asks for Kelvin — say in one short
sentence that you've converted it. Never hand someone a raw Kelvin column silently.

**Plain-language glossary — use this framing, don't just recite it verbatim:**
- **2m air temperature (ERA5-Land)** — the temperature of the air a person actually
  stands in, at head/shoulder height, in shade. This is the standard "how hot was it
  outside" variable. Hourly source aggregated to daily stats (mean/max/min).
- **Wet bulb temperature (MERRA-2, T2MWET)** — air temperature combined with humidity:
  what a wet thermometer reads when air blows past it. This is the accepted proxy for
  *heat stress* on the human body (sweat can't evaporate and cool you once wet-bulb gets
  too high), not just raw heat — prefer this over plain air temp for heat-health studies.
- **Land surface temperature (MODIS LST)** — how hot the *ground/roof/canopy surface*
  is, measured by satellite thermal sensors, NOT the air someone breathes. A tin roof or
  bare soil can read 15-20°C hotter than the air above it. Useful as an urban-heat-island
  / land-cover-driven heat proxy, but don't treat it as "what people felt" — pair with
  air temp or wet bulb for that.
- **CAMS NRT air quality** — a reanalysis model (not a single satellite) giving daily
  PM2.5, NO2, O3, CO, SO2, black carbon, and total aerosol optical depth at ~44km
  resolution. Good broad coverage/continuity but coarse — a "regional background"
  pollution estimate, not street-level. If a study needs finer spatial detail: for PM2.5
  specifically, **PM2.5 - GHAP** (~1km, daily, from 2017) is a real modelled ground-level
  concentration (not just a proxy) and is already in the catalog — call it that name
  directly, never try to guess/verify a GHAP asset path yourself with
  propose_custom_dataset, since the exact asset ID (GHAP_D1K_PM25, band "b1") is already
  validated and catalogued. MAIAC AOD (~1km) is a usable PM2.5 proxy if GHAP doesn't
  cover the study's date range. For NO2/CO/SO2 specifically, Sentinel-5P (~1.1km,
  satellite-observed, available from 2018) is sharper but has more day-to-day gaps
  (cloud cover, satellite revisit). Suggest CAMS as the default and mention the sharper
  alternatives only if resolution actually matters for their question.
- **NDVI / EVI (MODIS vegetation)** — greenness indices; useful as proxies for green
  space exposure, agricultural activity, or drought/food-security context. "NDVI (MODIS)"
  is a DAILY, non-composited product — real day-to-day jitter from cloud/atmospheric
  noise on top of genuine vegetation change. Whenever you add it, mention in the same
  reply that "NDVI 16-day Composite (MODIS)" also exists for a smoother trend with less
  jitter (same index, 16-day composite instead of daily) — don't wait for the user to
  ask for it by name. EVI is already a 16-day composite, so this doesn't apply to it.
- **CHIRPS precipitation** — daily rainfall (~5km); has a real ~2-month reporting lag,
  always flag this if someone wants very recent dates.
- **GHSL Urban Settlement Model** — classifies each location as Urban / Peri-urban /
  Rural from satellite-derived built-up density; handy for adjusting for urbanicity.
  Defaults to the **2025 epoch** if the user doesn't say otherwise — always state which
  epoch you're using as its own sentence, don't bury it. GHSL is published in 5-year
  epochs from 1975 to 2030 (all verified valid in Earth Engine) — if the user wants a
  different year (e.g. to match a historical study period), build it with
  propose_custom_dataset using collection 'JRC/GHSL/P2023A/GHS_SMOD/<year>', same band
  ('smod_code'), scale (1000), and type ('static_mode') as the catalog entry, and name it
  something like 'Urban Settlement - GHSL <year>' so it doesn't collide with the default.
- **WorldPop population / VIIRS night lights / Facebook Relative Wealth Index** — proxies
  for population density, urbanisation intensity, and area-level socioeconomic status,
  respectively, when a study needs a confounder and doesn't have survey data for it.
- **Elevation (MERIT DEM) / soil nutrients (iSDAsoil, Africa only) / JRC surface water**
  — terrain, agricultural-soil, and water-body context variables.

You can act, not just explain:
- Use list_datasets to see the exact catalog (name, collection, bands, scale, type).
- If the user wants something NOT in the catalog (e.g. "wet bulb globe temperature",
  "burned area", "flood extent", a specific Sentinel/Landsat/MODIS product), call
  search_earth_engine_catalog with a plain-language query FIRST — this searches the real
  ~900 official + ~4400 sat-io community datasets, rather than you guessing a collection
  ID from memory and hoping it's right. Only fall back to guessing from your own
  knowledge if the search comes back empty or nothing it returns actually fits. Either
  way, once you have a candidate collection ID and band name, call propose_custom_dataset
  — that tool actually checks Earth Engine and tells you if you got the collection/band
  right; if it fails, read the error (it lists the real available bands) and try again.
  Never claim a dataset is available without calling propose_custom_dataset to verify it
  first — search finds a candidate, it doesn't confirm one.
- Once the user has uploaded coordinates (staged_upload will be present) and told you
  what they want (which datasets, what date range for timeseries ones), confirm your
  understanding in one short sentence and call draft_extraction_plan — this shows them a
  one-click "Run this extraction" button instead of making them type "yes". Only call
  submit_extraction_job directly if their CURRENT message already explicitly confirmed a
  plan you already described (e.g. "yes, run it", "go ahead") — draft_extraction_plan is
  the default. Either way, never submit without the user actually having asked for the
  pull first — proposing/checking a dataset on its own doesn't count as asking to run it.
- The "Run this extraction" button always submits whatever plan you most recently
  drafted, even in a turn where you're asking something unrelated — it's designed to
  survive small talk. This means if you ask a clarifying/confirmation question about
  CHANGING an already-drafted plan (e.g. "did you want X added too, or instead of Y?",
  or an ambiguous parameter like which exact date range a vague request implies), you
  must call draft_extraction_plan again with your own best-guess resolution of the
  ambiguity in that SAME reply, even while you're still asking the user to confirm it.
  An unresolved parameter is NOT a reason to skip drafting — pick the single most likely
  reading, draft it, state that assumption plainly, and ask if that's right. Never leave
  the button sitting on the OLD plan while your own message discusses a different one —
  the user has no way to tell the button is stale, and a question with an active button
  reads as "it's ready to go." If their answer changes something, redraft again.
- If no coordinates are staged yet, tell the user to upload a CSV (latitude/longitude
  columns) or a zipped shapefile using the uploader above the chat.
- Static datasets (terrain, soil, urban, wealth) don't need a date range. Timeseries
  datasets (climate, air quality, vegetation, land surface temp) do.
- CHIRPS precipitation has a ~2 month reporting lag — warn users pulling recent dates.
- Use check_job_status if the user asks about progress on something you submitted.
- Every completed job produces a companion "Units & metadata" text file next to the CSV
  in My Jobs — it lists every output column's source dataset, Earth Engine collection,
  and documented unit. If someone asks what unit a column is in, point them to that file
  rather than guessing from memory; you can also just tell them directly using the units
  already in the dataset catalog (list_datasets returns them).

**Pregnancy-relative exposure windows** — for reproductive/environmental health studies
(PRECISE, DYAD, HE2AT-style), a single shared calendar date range is usually the wrong
tool: each participant has her own conception/delivery date, and what matters is exposure
during HER trimester 1, HER preconception period, etc. Use list_pregnancy_windows to see
the exact catalog (precon90, precon30, T1, T2, T3, peripartum, full — same definitions
this team already uses in their HE2AT exposure-linkage pipeline). If the user's uploaded
file has a conception-date column (and ideally a delivery-date column — required for T3/
peripartum/full since those are capped at delivery), and they're asking about exposure
during pregnancy/trimesters/preconception rather than a fixed calendar range, prefer
calling draft_extraction_plan with date_mode="pregnancy_windows" + window_keys + the
column names, over asking them for a generic start/end date. Ask which column holds
conception date (and delivery date, if needed) if it isn't obvious from context.

**Index-date lag windows and case-crossover** — for a non-pregnancy study (any single
event date: hospital admission, diagnosis, symptom onset) that still needs a per-row
window rather than one shared calendar range, use date_mode="index_window" with
ref_date_cols.index set to that date column and custom_window={"day_from": ..., "day_to":
...} (signed days relative to the event, e.g. {"day_from": -14, "day_to": 0} for a 2-week
lag). If the user instead describes a **distributed-lag (DLNM)** or **case-crossover**
analysis — phrases like "lag days", "exposure-lag-response", "self-matched", "referent
days", "conditional logistic regression" — don't offer the aggregated window mean; use
output_shape="long" (for index_window) or date_mode="case_crossover" (which always
produces the long/daily panel and needs no referent-day input from the user — it
auto-generates time-stratified control days: same weekday, same calendar month as the
case day). Either way the output carries a Day_Offset column (signed days from that row's
own reference date) so the researcher can use it directly as the lag index in their model
— explain this in one sentence rather than assuming they already know the column exists.
Pregnancy windows can also use output_shape="long" if someone wants a daily, not
averaged, trimester series.

If instead they describe wanting each lag day as its own column — "lag0, lag1, lag2",
"one column per day of lag", "a lag matrix", or they're clearly building a simple
distributed-lag/lag-response model that wants each day's effect estimated separately
rather than one window mean or a long daily panel — use output_shape="lag_matrix" with
date_mode="index_window" (lag_matrix is index_window-only; it's rejected for
pregnancy_windows/case_crossover). This still produces one row per location, but each
day from day_from to day_to becomes its own suffixed column (e.g. ndvi_lag-7 ...
ndvi_lag0) instead of a single aggregated value.

**Always confirm the shape before drafting a case-crossover or long-format plan** —
don't silently pick day_from/day_to or output_shape and only show the result after the
fact. Before calling draft_extraction_plan with date_mode="case_crossover" or
output_shape="long", state in plain terms what the user is about to get: the lag span in
days, and — for case-crossover specifically — that each row's case period AND every
auto-generated control period get that same lag window (so a ±7-day span means ~15 rows
per period, and a typical month has 3-4 control periods per participant, i.e. roughly
4-5× as many rows as participants × window length, not one row per participant). If the
user hasn't stated a lag span, ask for one rather than guessing — this is exactly the
kind of thing that silently produces a much bigger or differently-shaped file than
someone expected, and the fix is asking once up front, not after they've already
downloaded it.

Be concise and practical. When you propose a custom dataset, briefly say what you found
(collection, band, resolution) so the user can sanity-check it — you're a scientific
tool, not a black box.

## How to behave in a conversation

- If the "Current session" block below says a file is uploaded, USE it. Never ask
  whether the user has uploaded coordinates, and never ask them to upload again.
- Do not ask about things you can decide sensibly yourself. If the user did not name
  statistics, use "mean" and say so in one short line. If they asked for a single day,
  use that day as both start_date and end_date. Only ask a question when the answer
  genuinely changes what you would extract and you cannot pick a reasonable default.
- As soon as you know the datasets and the dates, call draft_extraction_plan. Do not
  keep chatting, and do not wait for confirmation before drafting: the user reviews the
  plan card and presses Run themselves.
- Do not narrate before using a tool ("let me check the catalog..."). Call the tool,
  then answer.
- Reply to a bare greeting with one short friendly line, not a recap.
- After draft_extraction_plan succeeds, the plan card (with its Run button) appears directly BELOW your reply. Say "the plan card below", never "above".
- If the user changes something (statistics, dates, datasets), call draft_extraction_plan again with the updated plan instead of just describing the change.
- Resolve relative dates against the current date given in the session block.
"""

TOOLS = [
    {
        "name": "list_datasets",
        "description": "Return the full catalog of ready-to-use datasets (name, category, "
                        "collection, bands, scale, type, description).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_earth_engine_catalog",
        "description": "Searches the real Earth Engine dataset catalog (Google's official ~900 datasets plus the "
                        "sat-io community catalog of ~4400, which is where niche datasets like GHAP PM2.5 and the "
                        "Relative Wealth Index live) by keyword. Call this BEFORE propose_custom_dataset whenever "
                        "the user wants a variable not in list_datasets — looking the real collection id up is "
                        "strictly better than guessing one from memory and hoping it's right (a wrong guess means "
                        "propose_custom_dataset fails and you have to retry blind). Returns candidate id/title/"
                        "type/provider — still just candidates; propose_custom_dataset still does the real, "
                        "read-only Earth Engine verification before anything is usable.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Plain-language search, e.g. 'wet bulb globe temperature', 'burned area', 'UTCI'"},
                "max_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "propose_custom_dataset",
        "description": "Draft and verify a new Earth Engine dataset not in the existing "
                        "catalog. Runs a real, read-only check against Earth Engine — "
                        "returns ok=false with the real available bands if your guess "
                        "was wrong, so you can retry with a correction.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short human-readable dataset name, e.g. 'Burned Area - MODIS'"},
                "collection": {"type": "string", "description": "Earth Engine collection/asset ID, e.g. 'MODIS/061/MCD64A1'"},
                "band": {
                    "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    "description": "Band name, or list of band names for multi-band datasets",
                },
                "scale": {"type": "number", "description": "Native resolution in metres"},
                "type": {
                    "type": "string",
                    "enum": ["timeseries", "static", "static_fc", "static_mode", "static_points_fc"],
                    "description": "timeseries=ImageCollection filtered by date; static=single ee.Image; "
                                    "static_fc=ImageCollection mosaic'd (no date filter); "
                                    "static_mode=categorical, always reduced with mode(); "
                                    "static_points_fc=the EE asset is actually a FeatureCollection of points "
                                    "with a numeric property per point, not a raster at all (e.g. Facebook/Meta's "
                                    "Relative Wealth Index) — loading it as ee.Image/ImageCollection throws every "
                                    "time ('Expected ... to be an ImageCollection, found EECollection'); use this "
                                    "type whenever that specific error shows up, 'band' is the property name to "
                                    "read, and it gets rasterized via reduceToImage before extraction",
                },
                "stats": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["mean", "min", "max", "p50", "p75", "p95", "p99", "mode"]},
                    "description": "Which reducers make sense for this band",
                },
                "output_col": {
                    "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    "description": "Output column name(s), matching band count",
                },
                "description": {"type": "string"},
            },
            "required": ["name", "collection", "band", "scale", "type", "stats", "output_col", "description"],
        },
    },
    {
        "name": "list_pregnancy_windows",
        "description": "Return the catalog of named pregnancy-relative exposure windows "
                        "(preconception, trimesters, peripartum, full pregnancy) with their "
                        "day-offset definitions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "draft_extraction_plan",
        "description": "PREFERRED way to propose a job. Validates the plan (same checks as "
                        "submit_extraction_job) and hands it to the UI as a one-click 'Run this "
                        "extraction' button instead of a job that's already running — friendlier "
                        "than asking the user to type 'yes'. Use this whenever you're proposing a "
                        "plan for the first time. Same input shape as submit_extraction_job.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names from the catalog and/or names you already proposed and verified via propose_custom_dataset",
                },
                "start_date": {"type": "string", "description": "YYYY-MM-DD, for a plain shared calendar range (ignored for any other date_mode)"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, for a plain shared calendar range (ignored for any other date_mode)"},
                "date_mode": {
                    "type": "string",
                    "enum": ["range", "pregnancy_windows", "index_window", "case_crossover"],
                    "default": "range",
                    "description": (
                        "'pregnancy_windows' aggregates each participant's own trimester-relative window(s) instead of "
                        "one shared date range. 'index_window' applies one custom day_from/day_to lag span around a "
                        "single per-row reference date (any study, not just pregnancy — e.g. hospitalisation date). "
                        "'case_crossover' additionally auto-generates time-stratified referent/control days (same "
                        "weekday, same calendar month) per row and always outputs the unaggregated daily panel."
                    ),
                },
                "window_keys": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Required if date_mode='pregnancy_windows' — keys from list_pregnancy_windows, e.g. ['T1','T3']",
                },
                "ref_date_cols": {
                    "type": "object",
                    "properties": {
                        "conception": {"type": "string", "description": "Column holding each row's conception date — pregnancy_windows only"},
                        "delivery": {"type": "string", "description": "Column holding delivery date — required for pregnancy_windows T3/peripartum/full"},
                        "index": {"type": "string", "description": "Column holding each row's single reference/event/case date — index_window and case_crossover only"},
                    },
                    "description": "Required for date_mode='pregnancy_windows' (conception[, delivery]) or 'index_window'/'case_crossover' (index)",
                },
                "custom_window": {
                    "type": "object",
                    "properties": {
                        "day_from": {"type": "integer", "description": "Signed days before(-)/after(+) the reference date, e.g. -14"},
                        "day_to": {"type": "integer", "description": "Signed days before(-)/after(+) the reference date, e.g. 0"},
                    },
                    "description": "Required for date_mode='index_window' or 'case_crossover' — the lag span pulled around each reference date",
                },
                "output_shape": {
                    "type": "string", "enum": ["wide", "long", "lag_matrix"], "default": "wide",
                    "description": (
                        "'wide' (default): one row per input location, one column per variable/window — a compact "
                        "summary. 'long': one row per day within each window, with a Day_Offset column (signed days "
                        "from that row's reference date) — the unaggregated daily series a DLNM/distributed-lag model "
                        "or case-crossover analysis needs. 'lag_matrix': index_window ONLY — still one row per "
                        "location, but each day of the lag window becomes its OWN column (e.g. ndvi_lag-7 ... "
                        "ndvi_lag0) instead of one aggregated mean — use this when the user wants to see/model each "
                        "lag day separately rather than a single window summary or a long daily panel. Only "
                        "meaningful for pregnancy_windows/index_window (case_crossover is always long; lag_matrix is "
                        "index_window-only)."
                    ),
                },
                "stats": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["mean", "min", "max", "p50", "p75", "p95", "p99"]},
                },
                "output_format": {"type": "string", "enum": ["csv", "parquet"], "default": "csv"},
                "temp_unit": {
                    "type": "string", "enum": ["kelvin", "celsius"], "default": "kelvin",
                    "description": "Unit for any temperature dataset in the plan (ERA5-Land air temp, MERRA-2 "
                                    "T2M/T2MWET, MODIS LST) — these are natively in Kelvin. Set 'celsius' whenever "
                                    "the user asks for Celsius, or for temperature/heat in general without naming "
                                    "Kelvin explicitly, since no health researcher wants raw Kelvin by default.",
                },
            },
            "required": ["dataset_names", "stats"],
        },
    },
    {
        "name": "submit_extraction_job",
        "description": "Submit a real background extraction job immediately, without a one-click "
                        "confirmation card. Only use this if the user's CURRENT message already "
                        "explicitly confirms a plan you described in plain text (e.g. they said "
                        "'yes, run it' / 'go ahead') — otherwise use draft_extraction_plan instead, "
                        "which is friendlier. Same input shape and validation as draft_extraction_plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names from the catalog and/or names you already proposed and verified via propose_custom_dataset",
                },
                "start_date": {"type": "string", "description": "YYYY-MM-DD, for a plain shared calendar range (ignored for any other date_mode)"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, for a plain shared calendar range (ignored for any other date_mode)"},
                "date_mode": {
                    "type": "string",
                    "enum": ["range", "pregnancy_windows", "index_window", "case_crossover"],
                    "default": "range",
                    "description": (
                        "'pregnancy_windows' aggregates each participant's own trimester-relative window(s) instead of "
                        "one shared date range. 'index_window' applies one custom day_from/day_to lag span around a "
                        "single per-row reference date (any study, not just pregnancy — e.g. hospitalisation date). "
                        "'case_crossover' additionally auto-generates time-stratified referent/control days (same "
                        "weekday, same calendar month) per row and always outputs the unaggregated daily panel."
                    ),
                },
                "window_keys": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Required if date_mode='pregnancy_windows' — keys from list_pregnancy_windows, e.g. ['T1','T3']",
                },
                "ref_date_cols": {
                    "type": "object",
                    "properties": {
                        "conception": {"type": "string", "description": "Column holding each row's conception date — pregnancy_windows only"},
                        "delivery": {"type": "string", "description": "Column holding delivery date — required for pregnancy_windows T3/peripartum/full"},
                        "index": {"type": "string", "description": "Column holding each row's single reference/event/case date — index_window and case_crossover only"},
                    },
                    "description": "Required for date_mode='pregnancy_windows' (conception[, delivery]) or 'index_window'/'case_crossover' (index)",
                },
                "custom_window": {
                    "type": "object",
                    "properties": {
                        "day_from": {"type": "integer", "description": "Signed days before(-)/after(+) the reference date, e.g. -14"},
                        "day_to": {"type": "integer", "description": "Signed days before(-)/after(+) the reference date, e.g. 0"},
                    },
                    "description": "Required for date_mode='index_window' or 'case_crossover' — the lag span pulled around each reference date",
                },
                "output_shape": {
                    "type": "string", "enum": ["wide", "long", "lag_matrix"], "default": "wide",
                    "description": (
                        "'wide' (default): one row per input location, one column per variable/window — a compact "
                        "summary. 'long': one row per day within each window, with a Day_Offset column (signed days "
                        "from that row's reference date) — the unaggregated daily series a DLNM/distributed-lag model "
                        "or case-crossover analysis needs. 'lag_matrix': index_window ONLY — still one row per "
                        "location, but each day of the lag window becomes its OWN column (e.g. ndvi_lag-7 ... "
                        "ndvi_lag0) instead of one aggregated mean — use this when the user wants to see/model each "
                        "lag day separately rather than a single window summary or a long daily panel. Only "
                        "meaningful for pregnancy_windows/index_window (case_crossover is always long; lag_matrix is "
                        "index_window-only)."
                    ),
                },
                "stats": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["mean", "min", "max", "p50", "p75", "p95", "p99"]},
                },
                "output_format": {"type": "string", "enum": ["csv", "parquet"], "default": "csv"},
                "temp_unit": {
                    "type": "string", "enum": ["kelvin", "celsius"], "default": "kelvin",
                    "description": "Unit for any temperature dataset in the plan (ERA5-Land air temp, MERRA-2 "
                                    "T2M/T2MWET, MODIS LST) — these are natively in Kelvin. Set 'celsius' whenever "
                                    "the user asks for Celsius, or for temperature/heat in general without naming "
                                    "Kelvin explicitly, since no health researcher wants raw Kelvin by default.",
                },
            },
            "required": ["dataset_names", "stats"],
        },
    },
    {
        "name": "check_job_status",
        "description": "Check progress/status of a previously submitted job. Omit job_id to "
                        "check the most recently submitted job this session.",
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
        },
    },
]


def _tool_list_datasets(_input):
    catalog = [
        {
            "name": name,
            "category": info.get("category"),
            "type": info["type"],
            "collection": info["collection"],
            "band": info["band"],
            "scale": info["scale"],
            "description": info["description"],
            "units": info.get("units"),
        }
        for name, info in DATASETS.items()
    ]
    custom = _S().get("custom_datasets", {})
    for name, info in custom.items():
        catalog.append({
            "name": name, "category": "Custom (this session)", "type": info["type"],
            "collection": info["collection"], "band": info["band"], "scale": info["scale"],
            "description": info.get("description", ""),
        })
    return {"catalog": catalog}


def _tool_propose_custom_dataset(inp):
    ok, message, available_bands = validate_dataset_candidate(
        collection=inp["collection"], band=inp["band"], scale=inp["scale"],
        ds_type=inp["type"], stats=inp["stats"],
    )
    if not ok:
        return {"ok": False, "message": message, "available_bands": available_bands}

    ds_def = {
        "collection": inp["collection"],
        "band": inp["band"],
        "scale": inp["scale"],
        "type": inp["type"],
        "stats": inp["stats"],
        "output_col": inp["output_col"],
        "description": inp["description"],
        "temporal": "daily" if inp["type"] == "timeseries" else "static",
    }
    if isinstance(inp["band"], list):
        ds_def["band_rename"] = inp["output_col"] if isinstance(inp["output_col"], list) else [inp["output_col"]]

    _S().setdefault("custom_datasets", {})[inp["name"]] = ds_def
    return {"ok": True, "message": message, "dataset_name": inp["name"]}


def _tool_search_earth_engine_catalog(inp):
    try:
        results = search_catalog(inp["query"], max_results=inp.get("max_results", 8))
    except Exception as e:
        return {"ok": False, "message": f"Catalog search unavailable right now: {e}", "results": []}
    return {"ok": True, "results": results}


def _tool_list_pregnancy_windows(_input):
    return {"windows": [{"key": k, **v} for k, v in PREGNANCY_WINDOWS.items()]}


_VALUE_KIND_WARNING = {
    "total": (
        "{name} gives one accumulated TOTAL per {temporal} period, not an average of sub-period "
        "readings. For single-day rows, mean/min/max/percentile will all show that same total under "
        "different column names, since there is only one number to begin with. Use a multi-day window "
        "instead if you want real day-to-day spread."
    ),
    "composite": (
        "{name} gives one retrieval/composite value per {temporal} period, not an average of sub-period "
        "readings. For single-day rows, mean/min/max/percentile will all show that same value under "
        "different column names, since there is only one number to begin with. Use a multi-day window "
        "instead if you want real spread across days."
    ),
}


def check_stat_feasibility(dataset_names, stats_requested, date_mode, output_shape, all_ds):
    """
    Warns — but never blocks — when the requested stats cannot come back as
    genuinely distinct numbers given how a dataset's rows will actually be
    shaped. e.g. requesting mean/min/max/p95 for Precipitation - CHIRPS on
    single-day rows: CHIRPS only has one (total) value per day, so every
    one of those columns will hold the same number. Only "sampled" datasets
    (true hourly/sub-daily sources, e.g. ERA5-Land — see their
    native_cadence) have real within-row variability at single-day
    granularity; everything else defaults to "composite" (one retrieval per
    native period) unless it's explicitly tagged "total" (one accumulated
    sum per period, e.g. rainfall) in core.datasets — see each dataset's
    "value_kind".

    A multi-day WINDOW (output_shape == "wide") is a different case: even a
    "composite"/"total" dataset has several real per-day values across a
    window, so no warning applies there regardless of value_kind — this
    only fires for rows that represent a single native period each.

    Returns a list of warning strings, one per affected dataset (empty if
    none apply). This is informational only — the caller always lets the
    request through once the user has seen it ("run for what is there"),
    never a hard validation failure like the rest of _validate_plan.
    """
    single_row_per_native_period = output_shape in ("long", "lag_matrix") or date_mode == "range"
    if not single_row_per_native_period or len(stats_requested) <= 1:
        return []
    warnings = []
    for name in dataset_names:
        ds_info = all_ds.get(name)
        if not ds_info or ds_info.get("type") != "timeseries":
            continue
        kind = ds_info.get("value_kind", "composite")
        if kind == "sampled":
            continue
        template = _VALUE_KIND_WARNING.get(kind, _VALUE_KIND_WARNING["composite"])
        warnings.append(template.format(name=name, temporal=ds_info.get("temporal", "daily")))
    return warnings


def _validate_plan(inp):
    """
    Shared validation for draft_extraction_plan / submit_extraction_job / the
    UI's "Run this extraction" button. Returns (ok, message, staged, custom,
    warnings). `message` is an error string when ok=False, else None.
    `warnings` (see check_stat_feasibility) is always a list, possibly empty
    even when ok=True — it never affects ok, just informs the caller.
    """
    staged = _S().get("staged_upload")
    if not staged:
        return False, "No coordinates uploaded yet — ask the user to upload a CSV or zipped shapefile first.", None, None, []

    from core.datasets import DATASETS as _catalog
    custom = _S().get("custom_datasets", {})
    all_ds = {**_catalog, **custom}

    unknown = [d for d in inp["dataset_names"] if d not in all_ds]
    if unknown:
        return False, f"Unknown dataset name(s): {unknown}. Call list_datasets or propose_custom_dataset first.", None, None, []

    date_mode = inp.get("date_mode", "range")

    if inp.get("output_shape") == "lag_matrix" and date_mode != "index_window":
        return False, "output_shape='lag_matrix' (one column per lag day, wide format) is only supported for date_mode='index_window'.", None, None, []

    if date_mode in ("pregnancy_windows", "index_window", "case_crossover"):
        if staged["geometry_type"] != "points" or not staged["input_filename"].endswith(".csv"):
            return False, "Window-based extraction currently only supports a CSV point upload.", None, None, []
        try:
            import pandas as pd
            header_cols = set(pd.read_csv(staged["input_filename"], nrows=0).columns)
        except Exception as e:
            return False, f"Could not read uploaded file header: {e}", None, None, []

    if date_mode == "pregnancy_windows":
        window_keys = inp.get("window_keys") or []
        bad_keys = [w for w in window_keys if w not in PREGNANCY_WINDOWS]
        if bad_keys:
            return False, f"Unknown window key(s): {bad_keys}. Call list_pregnancy_windows first.", None, None, []
        if not window_keys:
            return False, "window_keys is required when date_mode='pregnancy_windows'.", None, None, []
        ref_date_cols = inp.get("ref_date_cols") or {}
        if not ref_date_cols.get("conception"):
            return False, "ref_date_cols.conception is required when date_mode='pregnancy_windows'.", None, None, []
        needs_delivery = any(PREGNANCY_WINDOWS[w].get("day_to") is None for w in window_keys)
        if needs_delivery and not ref_date_cols.get("delivery"):
            return False, f"Window(s) {[w for w in window_keys if PREGNANCY_WINDOWS[w].get('day_to') is None]} need a delivery date — ref_date_cols.delivery is required.", None, None, []
        missing_cols = [c for c in (ref_date_cols.get("conception"), ref_date_cols.get("delivery")) if c and c not in header_cols]
        if missing_cols:
            return False, f"Column(s) {missing_cols} not found in the uploaded file. Available columns: {sorted(header_cols)}", None, None, []

    elif date_mode in ("index_window", "case_crossover"):
        ref_date_cols = inp.get("ref_date_cols") or {}
        index_col = ref_date_cols.get("index")
        if not index_col:
            return False, f"ref_date_cols.index is required when date_mode='{date_mode}'.", None, None, []
        if index_col not in header_cols:
            return False, f"Column '{index_col}' not found in the uploaded file. Available columns: {sorted(header_cols)}", None, None, []
        custom_window = inp.get("custom_window") or {}
        if "day_from" not in custom_window or "day_to" not in custom_window:
            return False, f"custom_window.day_from and custom_window.day_to are required when date_mode='{date_mode}'.", None, None, []
        try:
            day_from, day_to = int(custom_window["day_from"]), int(custom_window["day_to"])
        except (TypeError, ValueError):
            return False, "custom_window.day_from/day_to must be integers (signed days relative to the reference date).", None, None, []
        if day_to < day_from:
            return False, "custom_window.day_to must not be before custom_window.day_from.", None, None, []
        if date_mode == "case_crossover" and inp.get("output_shape") == "wide":
            return False, "case_crossover always produces the unaggregated daily panel — omit output_shape or set it to 'long'.", None, None, []

    else:
        needs_dates = any(all_ds[d]["type"] == "timeseries" for d in inp["dataset_names"])
        if needs_dates and not (inp.get("start_date") and inp.get("end_date")):
            return False, "start_date and end_date are required — at least one selected dataset is a timeseries.", None, None, []

    used_custom = {d: custom[d] for d in inp["dataset_names"] if d in custom}
    warnings = check_stat_feasibility(inp["dataset_names"], inp["stats"], date_mode,
                                       inp.get("output_shape"), all_ds)
    return True, None, staged, used_custom, warnings


def _run_plan(inp, staged, used_custom, source="shmron_ai"):
    from core.jobs_db import create_job
    from core.worker import run_job_in_background

    job_id = create_job(
        username=_S()["username"],
        datasets=inp["dataset_names"],
        start_date=inp.get("start_date"),
        end_date=inp.get("end_date"),
        geometry_type=staged["geometry_type"],
        input_filename=staged["input_filename"],
        ee_project=EE_PROJECT,
        stats_requested=inp["stats"],
        output_format=inp.get("output_format", "csv"),
        custom_datasets=used_custom,
        source=source,
        date_mode=inp.get("date_mode", "range"),
        window_keys=inp.get("window_keys"),
        ref_date_cols=inp.get("ref_date_cols"),
        custom_window=inp.get("custom_window"),
        output_shape="long" if inp.get("date_mode") == "case_crossover" else inp.get("output_shape", "wide"),
        temp_unit=inp.get("temp_unit", "kelvin"),
    )
    run_job_in_background(job_id)
    _S()["last_ai_job_id"] = job_id
    return {"ok": True, "job_id": job_id, "message": "Job submitted and running in the background."}


def execute_plan(plan, source="shmron_ai"):
    """
    Public entrypoint for the UI's "Run this extraction" button — re-validates
    (staged_upload or custom_datasets may have changed since the plan was
    drafted) and submits for real.
    """
    ok, message, staged, used_custom, warnings = _validate_plan(plan)
    if not ok:
        return {"ok": False, "message": message}
    result = _run_plan(plan, staged, used_custom, source=source)
    result["warnings"] = warnings
    return result


def _tool_draft_extraction_plan(inp):
    ok, message, staged, used_custom, warnings = _validate_plan(inp)
    if not ok:
        return {"ok": False, "message": message}
    if warnings:
        inp = dict(inp, _stat_warnings=warnings)
    _S()["pending_plan"] = inp
    result = {"ok": True, "message": "Plan is valid — shown to the user as a one-click Run button. Do not call submit_extraction_job for this same plan; wait for them to click it."}
    if warnings:
        result["warnings"] = warnings
        result["message"] += (" IMPORTANT: relay these warnings to the user in your reply, in plain language, "
                               "before they see the Run button — don't just let the card speak for itself: "
                               + " | ".join(warnings))
    return result


def _tool_submit_extraction_job(inp):
    ok, message, staged, used_custom, warnings = _validate_plan(inp)
    if not ok:
        return {"ok": False, "message": message}
    result = _run_plan(inp, staged, used_custom)
    if warnings:
        result["warnings"] = warnings
        result["message"] = (result.get("message", "") + " IMPORTANT: relay these warnings to the user in "
                              "your reply, in plain language: " + " | ".join(warnings))
    return result


def _tool_check_job_status(inp):
    from core.jobs_db import get_job
    job_id = inp.get("job_id") or _S().get("last_ai_job_id")
    if not job_id:
        return {"ok": False, "message": "No job_id given and none submitted yet this session."}
    job = get_job(job_id)
    if not job:
        return {"ok": False, "message": f"No job found with id {job_id}."}
    return {
        "ok": True, "job_id": job_id, "status": job["status"], "progress": job["progress"],
        "error_msg": job.get("error_msg"), "output_filename": job.get("output_filename"),
    }


_DISPATCH = {
    "list_datasets": _tool_list_datasets,
    "search_earth_engine_catalog": _tool_search_earth_engine_catalog,
    "list_pregnancy_windows": _tool_list_pregnancy_windows,
    "propose_custom_dataset": _tool_propose_custom_dataset,
    "draft_extraction_plan": _tool_draft_extraction_plan,
    "submit_extraction_job": _tool_submit_extraction_job,
    "check_job_status": _tool_check_job_status,
}


def _summarize_event(name, inp, result):
    """One-line, non-technical summary of a tool call for display in the chat."""
    ok = result.get("ok", True)
    if name == "list_datasets":
        return "🔍 Looked up the dataset catalog."
    if name == "search_earth_engine_catalog":
        n = len(result.get("results", []))
        return f"🔎 Searched the Earth Engine catalog for \"{inp.get('query', '')}\" — {n} candidate{'s' if n != 1 else ''} found."
    if name == "list_pregnancy_windows":
        return "🔍 Looked up the pregnancy-window catalog."
    if name == "propose_custom_dataset":
        if ok:
            return f"✅ Verified **{inp.get('name')}** against Earth Engine — {result.get('message', '')}"
        return f"❌ Couldn't verify **{inp.get('name')}** — {result.get('message', '')}"
    if name == "draft_extraction_plan":
        if ok:
            return "📝 Plan ready — see the card below."
        return f"⚠️ That plan isn't ready yet — {result.get('message', '')}"
    if name == "submit_extraction_job":
        if ok:
            return f"🚀 Job submitted (id `{result.get('job_id', '')[:8]}…`) — running in the background. Track it in **My Jobs**."
        return f"⚠️ Couldn't submit the job — {result.get('message', '')}"
    if name == "check_job_status":
        if ok:
            return f"📊 Job `{result.get('job_id', '')[:8]}…`: **{result.get('status')}** ({result.get('progress', 0)}%)"
        return f"⚠️ {result.get('message', '')}"
    return f"🔧 {name}"


def run_agent_turn(client, messages, context=None):
    """
    Runs the tool-use loop for one user turn. `messages` is the full
    Anthropic-format history (already including the new user message);
    mutated in place with assistant/tool turns appended.

    Returns `display_events`: an ordered list of
        {"type": "text", "content": str}  — Claude's own words
        {"type": "tool", "content": str}  — one-line summary of a tool call
    ready to render in sequence inside a single assistant chat bubble.
    """
    display_events = []

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=MODEL, max_tokens=1536, system=SYSTEM_PROMPT + (("\n\n## Current session\n" + context) if context else ""),
            tools=TOOLS, messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                display_events.append({"type": "text", "content": block.text})

        if response.stop_reason != "tool_use":
            return display_events

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            fn = _DISPATCH.get(block.name)
            try:
                result = fn(block.input) if fn else {"ok": False, "message": f"Unknown tool {block.name}"}
            except Exception as e:
                result = {"ok": False, "message": f"Tool error: {e}"}
            display_events.append({"type": "tool", "content": _summarize_event(block.name, block.input, result)})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    display_events.append({
        "type": "text",
        "content": "I ran into trouble finishing that request — could you rephrase or try a smaller step?",
    })
    return display_events
