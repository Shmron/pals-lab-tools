"""
generate_cohort.py — Two-pass synthetic pregnancy cohort generator.

Pass 1: villages, stub participants, visits, exposure_grid
Pass 2: timeline assignment → exposure join → outcome assignment

All outputs written to workshop/data/.

Run from the workshop/ directory:
  python generate_cohort.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from datetime import date, timedelta

from shared_utils import (
    find_nearest_grid_cells,
    assign_visit_location_to_days,
    assign_trimester,
    compute_trimester_summaries,
)

RNG = np.random.default_rng(42)
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

SEED = 42
N_PARTICIPANTS = 60
MIGRATION_RATE = 0.30

LON_MIN, LON_MAX = 25.5, 32.8
LAT_MIN, LAT_MAX = -22.2, -15.6
GRID_STEP = 0.25

ENROLLMENT_START = date(2023, 1, 1)
ENROLLMENT_WINDOW_DAYS = 365


# ---------------------------------------------------------------------------
# Villages
# ---------------------------------------------------------------------------

VILLAGE_SPECS = [
    ("Riverside",   "Matabeleland",  -20.8, 27.8),
    ("Hilltop",     "Matabeleland",  -21.1, 28.1),
    ("Greenfield",  "Midlands",      -19.2, 29.4),
    ("Ironstone",   "Midlands",      -18.9, 30.0),
    ("Sunridge",    "Harare",        -17.8, 31.2),
    ("Westgate",    "Harare",        -17.5, 30.9),
    ("Eastview",    "Manicaland",    -19.0, 32.1),
    ("Ferndale",    "Manicaland",    -19.4, 31.8),
    ("Kopje",       "Bulawayo",      -20.1, 28.6),
    ("Ntabazinduna","Bulawayo",      -20.4, 28.9),
]


def generate_villages() -> pd.DataFrame:
    rows = []
    for name, district, lat, lon in VILLAGE_SPECS:
        rows.append(
            dict(
                village=name,
                district=district,
                latitude=round(lat + RNG.uniform(-0.05, 0.05), 4),
                longitude=round(lon + RNG.uniform(-0.05, 0.05), 4),
            )
        )
    df = pd.DataFrame(rows)
    df.to_csv(DATA_DIR / "villages.csv", index=False)
    print(f"villages.csv           — {len(df)} rows")
    return df


# ---------------------------------------------------------------------------
# Participants (stub — no outcome yet)
# ---------------------------------------------------------------------------

def generate_participants() -> pd.DataFrame:
    rows = []
    for i in range(1, N_PARTICIPANTS + 1):
        enroll_offset = int(RNG.integers(0, ENROLLMENT_WINDOW_DAYS))
        enrollment_date = ENROLLMENT_START + timedelta(days=enroll_offset)

        ga_at_enroll_weeks = int(RNG.integers(4, 21))   # 4–20 weeks
        ga_at_enroll_days  = ga_at_enroll_weeks * 7

        conception_date = enrollment_date - timedelta(days=ga_at_enroll_days)
        edd = conception_date + timedelta(days=280)

        has_delivery = RNG.random() < 0.85
        if has_delivery:
            offset = int(RNG.integers(-21, 22))
            actual_delivery = edd + timedelta(days=offset)
            # Preterm: ~10% of those with delivery (before 259 days gestation = 37 weeks)
            if RNG.random() < 0.10:
                preterm_days = int(RNG.integers(224, 259))  # 32–37 weeks
                actual_delivery = conception_date + timedelta(days=preterm_days)
        else:
            actual_delivery = None

        rows.append(
            dict(
                PID=f"P{i:03d}",   # string — deliberate teaching point
                enrollment_date=enrollment_date,
                conception_date=conception_date,
                expected_delivery_date=edd,
                actual_delivery_date=actual_delivery,
                migrated=None,      # filled after visits
                outcome=None,       # filled in pass 2
                birthweight_g=None, # filled in pass 2
            )
        )
    df = pd.DataFrame(rows)
    # Ensure PID stays as string even if read back from CSV with default dtypes.
    df["PID"] = df["PID"].astype(str)
    return df


# ---------------------------------------------------------------------------
# Visits  (3 per participant; ~30% migrate across districts)
# ---------------------------------------------------------------------------

def generate_visits(participants_df: pd.DataFrame, villages_df: pd.DataFrame) -> pd.DataFrame:
    village_names    = villages_df["village"].tolist()
    village_district = villages_df.set_index("village")["district"].to_dict()
    village_lat      = villages_df.set_index("village")["latitude"].to_dict()
    village_lon      = villages_df.set_index("village")["longitude"].to_dict()

    rows = []
    migrated_flags = {}

    for _, p in participants_df.iterrows():
        pid            = p["PID"]
        conception     = pd.Timestamp(p["conception_date"])
        delivery_dt    = p["actual_delivery_date"]
        edd            = pd.Timestamp(p["expected_delivery_date"])
        delivery_date  = pd.Timestamp(delivery_dt) if pd.notna(delivery_dt) else edd

        home_village = RNG.choice(village_names)
        home_district = village_district[home_village]

        is_migrant = RNG.random() < MIGRATION_RATE
        migrated_flags[pid] = False

        assigned_villages = {
            "visit_1": home_village,
            "visit_2": home_village,
            "delivery": home_village,
        }

        if is_migrant:
            # Pick a village in a *different* district.
            other_district_villages = [
                v for v in village_names if village_district[v] != home_district
            ]
            if other_district_villages:
                migrant_village = RNG.choice(other_district_villages)
                # Migrate at visit_2, possibly at delivery too.
                assigned_villages["visit_2"] = migrant_village
                if RNG.random() < 0.5:
                    assigned_villages["delivery"] = migrant_village
                migrated_flags[pid] = True

        # Visit dates.
        v1_date  = conception + pd.Timedelta(days=int(RNG.integers(30, 91)))
        v2_date  = conception + pd.Timedelta(days=int(RNG.integers(150, 221)))
        del_date = delivery_date

        for vtype, vdate, vvillage in [
            ("visit_1",  v1_date,  assigned_villages["visit_1"]),
            ("visit_2",  v2_date,  assigned_villages["visit_2"]),
            ("delivery", del_date, assigned_villages["delivery"]),
        ]:
            rows.append(
                dict(
                    PID=pid,
                    visit_type=vtype,
                    visit_date=vdate.date(),
                    village=vvillage,
                    latitude=village_lat[vvillage],
                    longitude=village_lon[vvillage],
                )
            )

    df = pd.DataFrame(rows)
    return df, migrated_flags


# ---------------------------------------------------------------------------
# Exposure grid (daily, full study period, seasonal cycle)
# ---------------------------------------------------------------------------

def generate_exposure_grid(start_date: date, end_date: date) -> pd.DataFrame:
    lons = np.arange(LON_MIN, LON_MAX + 0.01, GRID_STEP)
    lats = np.arange(LAT_MIN, LAT_MAX + 0.01, GRID_STEP)
    dates = pd.date_range(start_date, end_date, freq="D")

    n_lons, n_lats, n_dates = len(lons), len(lats), len(dates)
    print(f"  Generating exposure grid: {n_lons} lons × {n_lats} lats × {n_dates} days …")

    rng = np.random.default_rng(SEED)

    # Pre-build spatial grid (no date dimension yet)
    grid_lons, grid_lats = np.meshgrid(lons, lats)
    grid_lons = grid_lons.ravel()   # shape (n_cells,)
    grid_lats = grid_lats.ravel()
    n_cells = len(grid_lons)

    lat_norm = (grid_lats - LAT_MIN) / (LAT_MAX - LAT_MIN)   # 0=south, 1=north
    lon_norm = (grid_lons - LON_MIN) / (LON_MAX - LON_MIN)   # 0=west, 1=east

    # Day-of-year seasonal signal (one per date, broadcast across cells)
    doy = dates.day_of_year.values.astype(float)  # 1–366
    # Dry season peaks July-September (doy ~180-270); sinusoid centred on doy=225
    seasonal_temp   =  3.0 * np.sin(2 * np.pi * (doy - 60)  / 365)   # °C amplitude
    seasonal_pm25   =  8.0 * np.sin(2 * np.pi * (doy - 60)  / 365)   # µg/m³
    seasonal_ndvi   = -0.15 * np.sin(2 * np.pi * (doy - 60) / 365)   # NDVI inverse phase
    seasonal_precip = -1.5  * np.sin(2 * np.pi * (doy - 60) / 365)   # mm

    rows = []
    gid = 1
    for ci in range(n_cells):
        lat  = grid_lats[ci]
        lon  = grid_lons[ci]
        lnrm = lat_norm[ci]
        lorm = lon_norm[ci]

        temp_spatial  = 20 + 9 * lnrm
        pm25_spatial  = 28 - 14 * lorm
        ndvi_spatial  = 0.40 + 0.25 * lnrm
        precip_spatial = 1.0 + 3.0 * lnrm

        grid_id = f"G{gid:05d}"
        gid += 1

        noise_temp   = rng.normal(0, 1.2,  n_dates)
        noise_pm25   = rng.normal(0, 3.0,  n_dates)
        noise_ndvi   = rng.normal(0, 0.03, n_dates)
        noise_precip = rng.normal(0, 0.8,  n_dates)

        temp_mean   = temp_spatial  + seasonal_temp   + noise_temp
        temp_max    = temp_mean + rng.uniform(3, 6, n_dates)
        pm25        = np.clip(pm25_spatial  + seasonal_pm25   + noise_pm25,   1.0, 120.0)
        ndvi        = np.clip(ndvi_spatial  + seasonal_ndvi   + noise_ndvi,   0.05, 0.95)
        precip      = np.clip(precip_spatial + seasonal_precip + noise_precip, 0.0, None)

        # Relative humidity via Magnus-Tetens from simulated dewpoint.
        dewpoint_offset = rng.uniform(3, 8, n_dates)
        td = temp_mean - dewpoint_offset
        rh = 100 * np.exp((17.625 * td) / (243.04 + td)) / np.exp((17.625 * temp_mean) / (243.04 + temp_mean))
        rh = np.clip(rh, 0, 100)

        for di, dt in enumerate(dates):
            rows.append(
                dict(
                    grid_id=grid_id,
                    lon=round(float(lon), 4),
                    lat=round(float(lat), 4),
                    date=dt.date(),
                    temp_mean_c=round(float(temp_mean[di]), 2),
                    temp_max_c=round(float(temp_max[di]), 2),
                    pm25_ugm3=round(float(pm25[di]), 2),
                    ndvi=round(float(ndvi[di]), 3),
                    precipitation_mm=round(float(precip[di]), 2),
                    rh_mean=round(float(rh[di]), 1),
                )
            )

    df = pd.DataFrame(rows)
    return df


# ---------------------------------------------------------------------------
# Pass 2 — timeline, exposure join, outcome assignment
# ---------------------------------------------------------------------------

def build_pregnancy_days(participants_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, p in participants_df.iterrows():
        start = pd.Timestamp(p["conception_date"])
        end_dt = p["actual_delivery_date"]
        end = pd.Timestamp(end_dt) if pd.notna(end_dt) else pd.Timestamp(p["expected_delivery_date"])

        for gday, dt in enumerate(pd.date_range(start, end)):
            rows.append(dict(PID=p["PID"], date=dt.date(), gestational_day=gday))
    return pd.DataFrame(rows)


def assign_outcomes(participants_df: pd.DataFrame, trimester_summaries: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(SEED + 1)
    df = participants_df.merge(trimester_summaries[["PID", "temp_mean_t3", "pm25_mean_t3"]], on="PID", how="left")

    outcomes = []
    weights = []
    for _, row in df.iterrows():
        t3_temp = row["temp_mean_t3"] if pd.notna(row["temp_mean_t3"]) else 25.0
        t3_pm25 = row["pm25_mean_t3"] if pd.notna(row["pm25_mean_t3"]) else 20.0

        p_preterm    = 0.08 + max(0, (t3_temp - 27.0)) * 0.015
        p_stillbirth = 0.02 + max(0, (t3_pm25 - 30.0)) * 0.002

        p_preterm    = min(p_preterm,    0.30)
        p_stillbirth = min(p_stillbirth, 0.10)

        r = rng.random()
        if r < p_stillbirth:
            outcome = "stillbirth"
            bw = None
        elif r < p_stillbirth + p_preterm:
            outcome = "preterm_birth"
            bw = int(rng.normal(2200, 300))
        else:
            bw_base = 3100 - max(0, (t3_pm25 - 20.0)) * 15
            bw = int(rng.normal(bw_base, 400))
            bw = max(500, bw)
            if bw < 2500:
                outcome = "low_birth_weight"
            else:
                outcome = "live_birth_term"

        outcomes.append(outcome)
        weights.append(bw)

    df["outcome"]      = outcomes
    df["birthweight_g"] = weights
    # For stillbirths, no birthweight.
    df.loc[df["outcome"] == "stillbirth", "birthweight_g"] = None
    return df[participants_df.columns.tolist() + ["outcome", "birthweight_g"]].drop_duplicates("PID")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # ---- Pass 1 ----
    print("Pass 1: generating villages, participants, visits …")
    villages_df = generate_villages()

    participants_stub = generate_participants()

    visits_df, migrated_flags = generate_visits(participants_stub, villages_df)
    visits_df.to_csv(DATA_DIR / "visits.csv", index=False)
    print(f"visits.csv             — {len(visits_df)} rows")

    # Update migrated flag.
    participants_stub["migrated"] = participants_stub["PID"].map(migrated_flags)
    participants_stub.drop(columns=["outcome", "birthweight_g"], inplace=True)

    # Study period bounds.
    conceptions = pd.to_datetime(participants_stub["conception_date"])
    deliveries  = pd.to_datetime(
        participants_stub["actual_delivery_date"].fillna(participants_stub["expected_delivery_date"])
    )
    study_start = conceptions.min().date()
    study_end   = deliveries.max().date()
    print(f"  Study period: {study_start} → {study_end}")

    exposure_grid = generate_exposure_grid(study_start, study_end)
    exposure_grid.to_csv(DATA_DIR / "exposure_grid.csv", index=False)
    print(f"exposure_grid.csv      — {len(exposure_grid):,} rows")

    # ---- Pass 2 ----
    print("Pass 2: timeline, exposure join, outcome assignment …")

    # Build per-day timeline.
    preg_days = build_pregnancy_days(participants_stub)
    print(f"  Pregnancy days table: {len(preg_days):,} rows")

    # Assign visit locations to each day.
    preg_days = assign_visit_location_to_days(preg_days, visits_df)
    preg_days["trimester"] = assign_trimester(preg_days["gestational_day"])
    preg_days.to_csv(DATA_DIR / "participant_exposure_days.csv", index=False)
    print(f"participant_exposure_days.csv — {len(preg_days):,} rows")

    # Nearest-grid-cell join.
    unique_locs = preg_days[["latitude", "longitude"]].drop_duplicates().copy()
    grid_ref = exposure_grid[["grid_id", "lat", "lon"]].drop_duplicates(subset=["grid_id"])
    unique_locs = find_nearest_grid_cells(unique_locs, grid_ref)
    preg_days = preg_days.merge(unique_locs[["latitude", "longitude", "grid_id"]], on=["latitude", "longitude"], how="left")

    # Date merge to attach environmental values.
    env_cols = ["grid_id", "date", "temp_mean_c", "temp_max_c", "pm25_ugm3", "ndvi", "precipitation_mm", "rh_mean"]
    preg_days_env = preg_days.merge(
        exposure_grid[env_cols].rename(columns={"date": "date_str"}),
        left_on=["grid_id", "date"],
        right_on=["grid_id", "date_str"],
        how="left",
    ).drop(columns=["date_str"], errors="ignore")

    preg_days_env.to_csv(DATA_DIR / "participant_exposure_days_with_env.csv", index=False)
    print(f"participant_exposure_days_with_env.csv — {len(preg_days_env):,} rows")

    # Trimester summaries.
    trimester_summaries = compute_trimester_summaries(preg_days_env)

    # Assign outcomes.
    participants_final = assign_outcomes(participants_stub, trimester_summaries)
    participants_final.to_csv(DATA_DIR / "participants.csv", index=False)
    print(f"participants.csv       — {len(participants_final)} rows")
    print("  Outcome distribution:")
    print(participants_final["outcome"].value_counts().to_string(header=False))

    # Participants with exposures (wide summary for notebook 06).
    participants_with_exposures = participants_final.merge(trimester_summaries, on="PID", how="left")
    participants_with_exposures.to_csv(DATA_DIR / "participants_with_exposures.csv", index=False)
    print(f"participants_with_exposures.csv — {len(participants_with_exposures)} rows")

    print("\nAll data files written to workshop/data/")


if __name__ == "__main__":
    main()
