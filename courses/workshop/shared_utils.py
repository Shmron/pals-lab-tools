"""
shared_utils.py — Shared logic for the GeoAI workshop.

Imported by generate_cohort.py, generate_base_data.py, and all student notebooks.
Implement once, reuse everywhere so that data generation and notebook exercises
produce identical results.
"""

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree


# ---------------------------------------------------------------------------
# Nearest-grid-cell join
# ---------------------------------------------------------------------------

def find_nearest_grid_cells(points_df: pd.DataFrame, grid_df: pd.DataFrame) -> pd.DataFrame:
    """Attach the nearest grid_id to every row in points_df.

    All locations must be within the Zimbabwe bounding box (lon 25.5–32.8,
    lat -22.2 to -15.6). At 0.25° grid spacing (~27 km/cell) the nearest cell
    is always within ~14 km — the best available environmental estimate for
    that location.

    Parameters
    ----------
    points_df : DataFrame with columns ``latitude``, ``longitude``
    grid_df   : DataFrame with columns ``lat``, ``lon``, ``grid_id``

    Returns
    -------
    points_df with a new ``grid_id`` column (original index preserved).
    """
    grid_coords = grid_df[["lat", "lon"]].values
    tree = cKDTree(grid_coords)

    point_coords = points_df[["latitude", "longitude"]].values
    _, indices = tree.query(point_coords)

    result = points_df.copy()
    result["grid_id"] = grid_df["grid_id"].iloc[indices].values
    return result


# ---------------------------------------------------------------------------
# Visit-to-day location assignment
# ---------------------------------------------------------------------------

def assign_visit_location_to_days(
    participant_days_df: pd.DataFrame,
    visits_df: pd.DataFrame,
) -> pd.DataFrame:
    """Assign every gestational day to the nearest-in-time visit location.

    For each participant, three visits (visit_1, visit_2, delivery) provide
    location anchors. Two midpoint breakpoints split the pregnancy into three
    windows; each window inherits the location of the bounding visit.

    Breakpoint rule: a day that falls exactly on a breakpoint is assigned to
    the *later* visit (i.e. breakpoints are right-inclusive).

    Parameters
    ----------
    participant_days_df : DataFrame with columns ``PID``, ``date``
    visits_df           : DataFrame with columns ``PID``, ``visit_type``,
                          ``visit_date``, ``village``, ``latitude``, ``longitude``

    Returns
    -------
    participant_days_df with new columns:
        ``village``, ``latitude``, ``longitude``, ``source_visit_type``
    """
    visits_df = visits_df.copy()
    visits_df["visit_date"] = pd.to_datetime(visits_df["visit_date"])

    result_rows = []

    for pid, days in participant_days_df.groupby("PID"):
        pv = visits_df[visits_df["PID"] == pid].sort_values("visit_date").reset_index(drop=True)
        if len(pv) < 3:
            # Fewer than 3 visits — assign all days to the earliest available visit.
            row = pv.iloc[0]
            days = days.copy()
            days["village"] = row["village"]
            days["latitude"] = row["latitude"]
            days["longitude"] = row["longitude"]
            days["source_visit_type"] = row["visit_type"]
            result_rows.append(days)
            continue

        v1, v2, vd = pv.iloc[0], pv.iloc[1], pv.iloc[2]

        # Midpoint breakpoints (floor to whole days).
        bp1 = v1["visit_date"] + (v2["visit_date"] - v1["visit_date"]) // 2
        bp2 = v2["visit_date"] + (vd["visit_date"] - v2["visit_date"]) // 2

        days = days.copy()
        dates = pd.to_datetime(days["date"])

        cond_v1 = dates < bp1
        cond_v2 = (dates >= bp1) & (dates < bp2)
        cond_vd = dates >= bp2

        for col in ["village", "latitude", "longitude", "source_visit_type"]:
            days[col] = None

        src_col = "visit_type"
        for mask, visit_row in [(cond_v1, v1), (cond_v2, v2), (cond_vd, vd)]:
            days.loc[mask, "village"] = visit_row["village"]
            days.loc[mask, "latitude"] = visit_row["latitude"]
            days.loc[mask, "longitude"] = visit_row["longitude"]
            days.loc[mask, "source_visit_type"] = visit_row[src_col]

        result_rows.append(days)

    return pd.concat(result_rows, ignore_index=True)


# ---------------------------------------------------------------------------
# Trimester bucketing
# ---------------------------------------------------------------------------

def assign_trimester(gestational_day: pd.Series) -> pd.Series:
    """Return trimester number (1, 2, or 3) for each gestational day.

    Trimester 1: days 0–90
    Trimester 2: days 91–180
    Trimester 3: days 181+
    """
    return pd.cut(
        gestational_day,
        bins=[-1, 90, 180, 9999],
        labels=[1, 2, 3],
    ).astype(int)


# ---------------------------------------------------------------------------
# Trimester-level exposure summaries
# ---------------------------------------------------------------------------

def compute_trimester_summaries(exposure_days_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate daily exposures to one row per participant with trimester suffixes.

    Input must have columns:
        PID, trimester, temp_mean_c, temp_max_c, pm25_ugm3, ndvi, precipitation_mm

    Returns a wide DataFrame with columns like:
        PID, temp_mean_t1, temp_mean_t2, temp_mean_t3,
             temp_max_t1, ..., pm25_mean_t1, ..., ndvi_mean_t1, ...,
             precip_total_t1, ...
    """
    agg = (
        exposure_days_df
        .groupby(["PID", "trimester"])
        .agg(
            temp_mean=("temp_mean_c", "mean"),
            temp_max=("temp_max_c", "max"),
            pm25_mean=("pm25_ugm3", "mean"),
            ndvi_mean=("ndvi", "mean"),
            precip_total=("precipitation_mm", "sum"),
        )
        .reset_index()
    )

    pivot = agg.pivot(index="PID", columns="trimester")
    pivot.columns = [f"{metric}_t{t}" for metric, t in pivot.columns]
    pivot = pivot.reset_index()

    # Ensure all trimester columns exist even for participants missing a trimester.
    expected_cols = [
        f"{m}_t{t}"
        for m in ["temp_mean", "temp_max", "pm25_mean", "ndvi_mean", "precip_total"]
        for t in [1, 2, 3]
    ]
    for col in expected_cols:
        if col not in pivot.columns:
            pivot[col] = np.nan

    return pivot
