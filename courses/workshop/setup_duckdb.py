"""
setup_duckdb.py — Load all workshop CSVs into exposures.duckdb.

Run from the workshop/ directory:
  python setup_duckdb.py
"""

from pathlib import Path
import duckdb

DATA_DIR  = Path(__file__).parent / "data"
DB_PATH   = DATA_DIR / "exposures.duckdb"


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"Removed existing {DB_PATH.name}")

    con = duckdb.connect(str(DB_PATH))

    # Attempt to load the spatial extension (non-blocking if unavailable).
    spatial_ok = False
    try:
        con.execute("INSTALL spatial; LOAD spatial;")
        spatial_ok = True
        print("spatial extension loaded")
    except Exception as e:
        print(f"spatial extension not available ({e}); spatial queries will be skipped")

    # ------------------------------------------------------------------ #
    # Core tables
    # ------------------------------------------------------------------ #

    tables = {
        "participants":  "participants.csv",
        "visits":        "visits.csv",
        "villages":      "villages.csv",
        "exposure_grid": "exposure_grid.csv",
        "participant_exposure_days_with_env": "participant_exposure_days_with_env.csv",
    }

    for table_name, csv_name in tables.items():
        csv_path = DATA_DIR / csv_name
        con.execute(f"""
            CREATE TABLE {table_name} AS
            SELECT * FROM read_csv_auto('{csv_path}', header=True)
        """)
        n = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        print(f"  {table_name:<45} {n:>10,} rows")

    # ------------------------------------------------------------------ #
    # Participant summary view
    # ------------------------------------------------------------------ #

    participants_with_exp = DATA_DIR / "participants_with_exposures.csv"
    con.execute(f"""
        CREATE VIEW participant_summary AS
        SELECT * FROM read_csv_auto('{participants_with_exp}', header=True)
    """)
    print(f"  view: participant_summary")

    # ------------------------------------------------------------------ #
    # Geometry columns (if spatial extension loaded)
    # ------------------------------------------------------------------ #

    if spatial_ok:
        try:
            for table, lon_col, lat_col in [
                ("visits", "longitude", "latitude"),
                ("exposure_grid", "lon", "lat"),
            ]:
                con.execute(f"ALTER TABLE {table} ADD COLUMN geom GEOMETRY")
                con.execute(f"UPDATE {table} SET geom = ST_Point({lon_col}, {lat_col})")
            print("  GEOMETRY columns added to visits and exposure_grid")
        except Exception as e:
            print(f"  GEOMETRY column creation skipped: {e}")

    con.close()
    print(f"\nexposures.duckdb built at {DB_PATH}")


if __name__ == "__main__":
    main()
