"""
generate_base_data.py — Generate base workshop data for notebooks 01–03.

Produces:
  workshop/data/health_facilities.csv  (~30 geocoded health facilities)
  workshop/data/climate_grid.csv       (0.25° grid snapshot, 2024-07-15)

Run from the workshop/ directory:
  python generate_base_data.py
"""

import numpy as np
import pandas as pd
from pathlib import Path

RNG = np.random.default_rng(42)
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Zimbabwe bounding box
LON_MIN, LON_MAX = 25.5, 32.8
LAT_MIN, LAT_MAX = -22.2, -15.6

DISTRICTS = ["Harare", "Bulawayo", "Midlands", "Manicaland", "Matabeleland"]

DISTRICT_BOXES = {
    "Harare":        (31.0, 32.0, -18.0, -17.0),
    "Bulawayo":      (28.2, 29.2, -20.5, -19.8),
    "Midlands":      (29.0, 30.5, -19.5, -18.5),
    "Manicaland":    (31.5, 32.5, -19.5, -18.5),
    "Matabeleland":  (27.0, 28.5, -21.5, -20.0),
}

FACILITY_TYPES = ["hospital", "clinic", "health_post"]
TYPE_WEIGHTS = [0.15, 0.55, 0.30]


def generate_facilities() -> pd.DataFrame:
    rows = []
    fid = 1
    for district, (lon0, lon1, lat0, lat1) in DISTRICT_BOXES.items():
        n = 6 if district in ("Harare", "Bulawayo") else 4
        for _ in range(n):
            ftype = RNG.choice(FACILITY_TYPES, p=TYPE_WEIGHTS)
            lat = RNG.uniform(lat0, lat1)
            lon = RNG.uniform(lon0, lon1)
            catchment = int(RNG.integers(500, 15_000))
            rows.append(
                dict(
                    facility_id=f"F{fid:03d}",
                    facility_type=ftype,
                    district=district,
                    latitude=round(lat, 6),
                    longitude=round(lon, 6),
                    catchment_pop=catchment,
                )
            )
            fid += 1

    # Introduce 2 rows with missing coordinates to make it a teaching point.
    for _ in range(2):
        rows.append(
            dict(
                facility_id=f"F{fid:03d}",
                facility_type="clinic",
                district="Midlands",
                latitude=None,
                longitude=None,
                catchment_pop=int(RNG.integers(200, 3000)),
            )
        )
        fid += 1

    df = pd.DataFrame(rows)
    # Deliberately write facility_id as an integer-looking string to illustrate
    # the PID-as-float bug in notebook 01.
    return df


def generate_climate_grid(snapshot_date: str = "2024-07-15") -> pd.DataFrame:
    """Single-date 0.25° grid snapshot covering the Zimbabwe bounding box."""
    lons = np.arange(LON_MIN, LON_MAX + 0.01, 0.25)
    lats = np.arange(LAT_MIN, LAT_MAX + 0.01, 0.25)

    rows = []
    gid = 1
    for lat in lats:
        for lon in lons:
            # Spatial gradients (same logic as exposure_grid in generate_cohort.py):
            # temp increases toward the north (lower absolute lat value)
            # PM2.5 higher in western half
            # NDVI decreases with latitude (lower absolute lat = less vegetation)
            lat_norm = (lat - LAT_MIN) / (LAT_MAX - LAT_MIN)   # 0=south, 1=north
            lon_norm = (lon - LON_MIN) / (LON_MAX - LON_MIN)   # 0=west, 1=east

            temp_base = 22 + 8 * lat_norm
            pm25_base = 25 - 12 * lon_norm
            ndvi_base = 0.55 + 0.2 * lat_norm
            precip_base = 0.5 + 2.0 * lat_norm

            noise = RNG.normal(0, 1)
            temp = round(float(temp_base + noise * 0.8), 2)
            pm25 = round(float(max(2.0, pm25_base + noise * 2.5)), 2)
            ndvi = round(float(np.clip(ndvi_base + RNG.normal(0, 0.04), 0.05, 0.95)), 3)
            precip = round(float(max(0, precip_base + RNG.normal(0, 0.3))), 2)

            rows.append(
                dict(
                    grid_id=f"G{gid:04d}",
                    lon=round(float(lon), 4),
                    lat=round(float(lat), 4),
                    date=snapshot_date,
                    temp_mean_c=temp,
                    pm25_ugm3=pm25,
                    ndvi=ndvi,
                    precipitation_mm=precip,
                )
            )
            gid += 1

    return pd.DataFrame(rows)


def main():
    facilities = generate_facilities()
    facilities.to_csv(DATA_DIR / "health_facilities.csv", index=False)
    print(f"health_facilities.csv  — {len(facilities)} rows")

    grid = generate_climate_grid()
    grid.to_csv(DATA_DIR / "climate_grid.csv", index=False)
    print(f"climate_grid.csv       — {len(grid)} rows  ({grid['lon'].nunique()} lons × {grid['lat'].nunique()} lats)")


if __name__ == "__main__":
    main()
