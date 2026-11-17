"""
Loads harare_flood_flask/run_national_annual.py's per-year admin-tabulation
CSVs (district-level population-weighted mean travel time to nearest open
health facility, baseline vs. flood scenario) into access_annual.

Each year's flood scenario is that year's heaviest national 7-day CHIRPS
window (see run_national_annual.find_wettest_week) -- not a separately
chosen event, so this sits on the same CHIRPS foundation climate_daily/
climate_annual already use.
"""
import os
import glob
import duckdb
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "climate_hiv_atlas.duckdb")
TABULATION_DIR = "/home/USER/PRECISE/harare_flood_flask"


def main():
    con = duckdb.connect(DB_PATH)
    con.execute("DELETE FROM access_annual")

    paths = sorted(glob.glob(os.path.join(TABULATION_DIR, "*_admin_tabulation.csv")))
    if not paths:
        raise SystemExit(f"[FAIL] no *_admin_tabulation.csv found in {TABULATION_DIR}")

    total_rows = 0
    for path in paths:
        year = int(os.path.basename(path).split("_")[0])
        df = pd.read_csv(path)
        df["year"] = year
        df = df[["district", "year", "population", "baseline_min", "flooded_min", "pct_change", "pop_cutoff"]]
        con.executemany(
            "INSERT OR REPLACE INTO access_annual VALUES (?, ?, ?, ?, ?, ?, ?)",
            df.itertuples(index=False, name=None),
        )
        total_rows += len(df)
        print(f"[OK] {year}: {len(df)} districts <- {path}")

    print(f"[OK] access_annual loaded: {total_rows} rows, "
          f"{con.execute('SELECT COUNT(DISTINCT year) FROM access_annual').fetchone()[0]} year(s)")
    con.close()


if __name__ == "__main__":
    main()
