"""
Turns a user's cohort/subset/exposure picks into either:
  (a) a direct DuckDB query over already-computed exposure columns
      ("use existing exposures" — assign_existing_exposures), or
  (b) a synthetic locations CSV in the exact shape PALs Earth's own
      core/extractor.py already expects from an uploaded file
      (materialize_locations_csv), handed off unchanged to the existing
      GEE extraction/worker pipeline for a NEW exposure variable.

Every column name used anywhere in here comes from a cohort's entry in
core.cohort_catalog.COHORTS — nothing here is PRECISE-specific, so a future
cohort with a different schema works without touching this file.
"""
import os
import uuid
import duckdb
import pandas as pd

from core.cohort_catalog import get_cohort
from core.windows import (
    PREGNANCY_WINDOWS,
    compute_window_dates,
    compute_custom_window_dates,
    build_case_crossover_referents,
)

MATERIALIZED_DIR = '/home/USER/PRECISE/cohort_exposures/materialized'
os.makedirs(MATERIALIZED_DIR, exist_ok=True)


def _connect(cohort):
    return duckdb.connect(cohort["source"]["path"], read_only=True)


def _quote(col):
    return '"' + col.replace('"', '""') + '"'


def _where_clause(cohort, filters, id_list):
    """
    Builds the WHERE clause for a filtered subset. Every column in
    cohort['site_fallback_columns'] (Village / health_facility for PRECISE)
    is OR'd together into one bracketed clause — picking a village must not
    silently exclude village-less participants identified only by facility
    — while every other filterable column (notably Country, the access-
    scope column) stays AND'd as a hard restriction.
    """
    filters = dict(filters or {})
    fallback_cols = cohort.get("site_fallback_columns", [])
    clauses, params = [], []

    fb_clauses, fb_params = [], []
    for col in fallback_cols:
        values = [v for v in (filters.pop(col, None) or []) if v not in (None, "")]
        if not values:
            continue
        placeholders = ",".join(["?"] * len(values))
        fb_clauses.append(f"{_quote(col)} IN ({placeholders})")
        fb_params.extend(values)
    if fb_clauses:
        clauses.append("(" + " OR ".join(fb_clauses) + ")")
        params.extend(fb_params)

    for col, values in filters.items():
        if col not in cohort["filterable_columns"]:
            continue
        values = [v for v in (values or []) if v not in (None, "")]
        if not values:
            continue
        placeholders = ",".join(["?"] * len(values))
        clauses.append(f"{_quote(col)} IN ({placeholders})")
        params.extend(values)

    id_list = [i for i in (id_list or []) if i not in (None, "")]
    if id_list:
        placeholders = ",".join(["?"] * len(id_list))
        clauses.append(f"{_quote(cohort['id_column'])} IN ({placeholders})")
        params.extend(id_list)
    where = " AND ".join(clauses) if clauses else "1=1"
    return where, params


def distinct_values(cohort_key, column, filters=None, limit=1000):
    """
    filters (optional) scopes the options themselves — e.g. passing
    {'Country': ['Kenya']} so a Kenya-scoped account's Village/health_facility
    dropdowns never even list another country's site names in the first
    place, not just blocked from submitting them.
    """
    cohort = get_cohort(cohort_key)
    if column not in cohort["filterable_columns"]:
        raise ValueError(f"{column!r} is not a filterable column for {cohort_key!r}")
    where, params = _where_clause(cohort, filters, None)
    con = _connect(cohort)
    tbl = cohort["source"]["table"]
    rows = con.execute(
        f"SELECT DISTINCT {_quote(column)} FROM {tbl} "
        f"WHERE {_quote(column)} IS NOT NULL AND ({where}) ORDER BY 1 LIMIT {int(limit)}",
        params
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def distinct_values_with_scope(cohort_key, column, filters=None, limit=2000):
    """
    Like distinct_values, but also returns each option's value for the
    cohort's access_scope_column (e.g. Country) alongside it, as
    [(value, scope_value), ...] — so a filter UI can tag each checkbox with
    which scope it belongs to and narrow a column like Village down to
    just the currently-selected Country(ies) client-side, instead of
    showing every site across every country regardless of what's picked.
    Only meaningful for columns listed in the cohort's
    "site_columns_scoped_by_country"; raises otherwise, since nothing
    guarantees any other column maps cleanly 1:1 to the scope column.
    """
    cohort = get_cohort(cohort_key)
    if column not in cohort.get("site_columns_scoped_by_country", []):
        raise ValueError(f"{column!r} is not registered as scoped-by-country for {cohort_key!r}")
    scope_col = cohort.get("access_scope_column")
    where, params = _where_clause(cohort, filters, None)
    con = _connect(cohort)
    tbl = cohort["source"]["table"]
    rows = con.execute(
        f"SELECT DISTINCT {_quote(column)}, {_quote(scope_col)} FROM {tbl} "
        f"WHERE {_quote(column)} IS NOT NULL AND ({where}) ORDER BY 1 LIMIT {int(limit)}",
        params
    ).fetchall()
    con.close()
    return [(r[0], r[1]) for r in rows]


def count_participants(cohort_key, filters=None, id_list=None):
    cohort = get_cohort(cohort_key)
    where, params = _where_clause(cohort, filters, id_list)
    con = _connect(cohort)
    tbl = cohort["source"]["table"]
    idcol = _quote(cohort["id_column"])
    n = con.execute(
        f"SELECT COUNT(DISTINCT {idcol}) FROM {tbl} WHERE {where}", params
    ).fetchone()[0]
    con.close()
    return n


def _participant_meta(cohort, filters, id_list, extra_cols):
    """One row per participant: id + any extra (reference-date / lat/lon)
    columns, collapsed with MAX() in case a value repeats identically across
    that participant's daily rows (harmless) or is NULL on some rows."""
    where, params = _where_clause(cohort, filters, id_list)
    con = _connect(cohort)
    tbl = cohort["source"]["table"]
    idcol = cohort["id_column"]
    cols = [idcol] + extra_cols
    agg_select = ", ".join(
        _quote(c) if c == idcol else f"MAX({_quote(c)}) AS {_quote(c)}" for c in cols
    )
    df = con.execute(
        f"SELECT {agg_select} FROM {tbl} WHERE {where} GROUP BY {_quote(idcol)}", params
    ).fetchdf()
    con.close()
    return df


def _daily_rows(cohort, filters, id_list, columns):
    where, params = _where_clause(cohort, filters, id_list)
    con = _connect(cohort)
    tbl = cohort["source"]["table"]
    col_sql = ", ".join(_quote(c) for c in columns)
    df = con.execute(f"SELECT {col_sql} FROM {tbl} WHERE {where}", params).fetchdf()
    con.close()
    return df


def _resolve_window_fn(cohort, date_mode, ref_date_cols, custom_window):
    """Returns a function taking a participant meta-row and a window key,
    returning (start, end) or None — same contract core.extractor.py's own
    windowed-extraction path uses, just reused here directly."""
    if date_mode == "pregnancy_windows":
        if not cohort.get("pregnancy_window_support"):
            raise ValueError(f"{cohort['label']} does not support pregnancy windows.")
        conception_col = ref_date_cols.get("conception")
        delivery_col = ref_date_cols.get("delivery")
        if not conception_col:
            raise ValueError("A conception-date column is required for pregnancy-window assignment.")

        def fn(row, win_key):
            conception = pd.to_datetime(row.get(conception_col))
            delivery = pd.to_datetime(row.get(delivery_col)) if delivery_col else pd.NaT
            return compute_window_dates(conception, delivery, win_key)

        return fn

    if date_mode in ("index_window", "case_crossover"):
        index_col = ref_date_cols.get("index")
        if not index_col:
            raise ValueError("A reference/event-date column is required.")
        day_from, day_to = custom_window.get("day_from"), custom_window.get("day_to")
        if day_from is None or day_to is None:
            raise ValueError("A day_from/day_to lag span is required.")

        def fn(row, win_key):
            index_date = pd.to_datetime(row.get(index_col))
            if win_key == "case" or win_key == "custom":
                return compute_custom_window_dates(index_date, day_from, day_to)
            # win_key is a control referent date pre-computed by the caller.
            return compute_custom_window_dates(win_key, day_from, day_to)

        return fn

    raise ValueError(f"Unknown date_mode: {date_mode!r}")


def assign_existing_exposures(cohort_key, filters, id_list, exposure_columns,
                               date_mode, ref_date_cols, window_keys=None,
                               custom_window=None, output_shape="wide"):
    """
    Mode A — pulls values already computed in the cohort's own daily table
    for the selected subset, reshaped per the chosen date mode/output shape.
    Returns a pandas.DataFrame ready to write out as CSV. No Earth Engine
    call — this is a reshape of data that already exists.
    """
    cohort = get_cohort(cohort_key)
    idcol = cohort["id_column"]
    daycol = cohort["daily_date_column"]
    ref_date_cols = ref_date_cols or {}
    custom_window = custom_window or {}

    meta_cols = sorted(set(v for v in ref_date_cols.values() if v))
    meta = _participant_meta(cohort, filters, id_list, meta_cols)
    if meta.empty:
        raise ValueError("No participants match that subset.")

    daily = _daily_rows(cohort, filters, id_list, [idcol, daycol] + exposure_columns)
    daily[daycol] = pd.to_datetime(daily[daycol])
    # Group once up front (O(N)) instead of re-scanning the whole frame for
    # every participant inside the loop below (O(participants x N) — fine
    # for a one-village test, minutes-long for a whole-country query).
    daily_by_pid = {pid: df for pid, df in daily.groupby(idcol, sort=False)}

    win_fn = _resolve_window_fn(cohort, date_mode, ref_date_cols, custom_window)

    if date_mode == "pregnancy_windows":
        keys = window_keys or []
        if not keys:
            raise ValueError("No windows selected.")
    elif date_mode == "case_crossover":
        keys = None  # resolved per participant below (case + referents)
    else:
        keys = ["custom"]

    wide_rows, long_rows = [], []
    for _, prow in meta.iterrows():
        pid = prow[idcol]
        p_daily = daily_by_pid.get(pid)
        if p_daily is None or p_daily.empty:
            continue

        periods = []  # list of (label, start, end, anchor)
        if date_mode == "case_crossover":
            index_date = pd.to_datetime(prow.get(ref_date_cols.get("index")))
            wd = win_fn(prow, "case")
            if wd:
                periods.append(("case", wd[0], wd[1], index_date))
            for ref_date in build_case_crossover_referents(index_date):
                wdr = compute_custom_window_dates(ref_date, custom_window["day_from"], custom_window["day_to"])
                if wdr:
                    periods.append((f"control_{ref_date.strftime('%Y%m%d')}", wdr[0], wdr[1], ref_date))
        else:
            for wk in keys:
                wd = win_fn(prow, wk)
                if wd:
                    anchor = pd.to_datetime(prow.get(ref_date_cols.get("conception") or ref_date_cols.get("index")))
                    periods.append((wk, wd[0], wd[1], anchor))

        for label, start, end, anchor in periods:
            mask = (p_daily[daycol] >= start) & (p_daily[daycol] <= end)
            period_rows = p_daily.loc[mask]
            if period_rows.empty:
                continue
            if output_shape == "long":
                for _, drow in period_rows.iterrows():
                    rec = {idcol: pid, "Window": label,
                           "Exposure_Date": drow[daycol],
                           "Day_Offset": (drow[daycol] - anchor).days}
                    if date_mode == "case_crossover":
                        rec["Role"] = "case" if label == "case" else "control"
                    for col in exposure_columns:
                        rec[col] = drow[col]
                    long_rows.append(rec)
            else:
                rec = {idcol: pid, "Window": label}
                means = period_rows[exposure_columns].mean(numeric_only=True)
                for col in exposure_columns:
                    rec[f"{col}_{label}"] = means.get(col)
                wide_rows.append((pid, label, rec))

    if output_shape == "long":
        if not long_rows:
            raise ValueError("No exposure rows fell inside the computed windows for this subset.")
        out = pd.DataFrame(long_rows)
        front = ["Exposure_Date", "Window", "Day_Offset"] + (["Role"] if date_mode == "case_crossover" else [])
        out = out[[idcol] + front + [c for c in out.columns if c not in front + [idcol]]]
        return out.sort_values([idcol, "Window", "Exposure_Date"]).reset_index(drop=True)

    if not wide_rows:
        raise ValueError("No exposure rows fell inside the computed windows for this subset.")
    merged = {}
    for pid, label, rec in wide_rows:
        merged.setdefault(pid, {idcol: pid}).update(
            {k: v for k, v in rec.items() if k not in (idcol, "Window")}
        )
    out = pd.DataFrame(list(merged.values()))
    return out.sort_values(idcol).reset_index(drop=True)


def materialize_locations_csv(cohort_key, filters, id_list, ref_date_cols):
    """
    Mode B support — writes a small CSV of [id, lat, lon, <ref date cols>],
    one row per participant in the filtered subset, in exactly the shape
    core/extractor.py already reads from a manually uploaded PALs Earth
    file (it auto-detects lat/lon by common column names, and a per-row
    date column by whatever name is passed in ref_date_cols). The existing
    extractor/worker pipeline is used completely unmodified from here on.
    """
    cohort = get_cohort(cohort_key)
    idcol = cohort["id_column"]
    latcol, loncol = cohort["location_columns"]["lat"], cohort["location_columns"]["lon"]
    ref_cols = sorted(set(v for v in (ref_date_cols or {}).values() if v))
    meta = _participant_meta(cohort, filters, id_list, [latcol, loncol] + ref_cols)
    if meta.empty:
        raise ValueError("No participants match that subset.")
    out_path = os.path.join(MATERIALIZED_DIR, f"{cohort_key}_{uuid.uuid4().hex[:8]}.csv")
    meta.to_csv(out_path, index=False)
    return out_path, len(meta)
