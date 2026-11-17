"""
Pulls daily district-mean tmax (ERA5-Land) and precipitation (CHIRPS) for
Zimbabwe's 62 districts, 1990-2025 plus whatever of the current year is
available, into climate_daily, then derives heat/heatwave/heavy-rain day
counts into climate_annual.

END_YEAR now includes the current, still-in-progress year on purpose --
climate_daily is meant to hold daily rows regardless of whether the year
is complete, so the daily-calendar/typical-month views (day/month
resolution, where "some recent months are missing" is just a gap, not a
misleading total) can reach the current year. extract_range()'s own "no
dates -> empty month" handling means requesting months that don't exist
yet in ERA5-Land/CHIRPS is harmless, not an error.

The safety this used to rely on living here (END_YEAR = last complete
year) now lives in build_climate_annual.py instead: it only promotes a
(district, year) into climate_annual -- which feeds the year slider and
every annual choropleth layer -- if that year actually has a full set of
days in climate_daily. A partial current year sits in climate_daily for
the daily/monthly views and is automatically excluded from climate_annual
until it's actually complete, rather than requiring this script to track
two different "how far did I get" numbers. ERA5-Land and CHIRPS are
confirmed to carry data up to the time of any given run (checked
2026-11-04: ERA5-Land through 2026-10-26, CHIRPS through 2026-09-30).

One reduceRegions() call per year per dataset (68 calls total), each
returning one feature per district with ~365 daily band values -- far
cheaper than looping per-district. Scale is set explicitly to each
dataset's native resolution so large district polygons don't push EE
into a finer default scale (ERA5-Land ~11132m, CHIRPS ~5566m).

Thresholds (heat day >=35C, heavy rain day >=20mm, heatwave >=3 consecutive
heat days) are applied in SQL over the raw daily values stored here, not
baked into the extraction -- so they can change later without re-pulling
from Earth Engine.
"""
import os
import sys
import time
import duckdb
import ee
import geopandas as gpd
from shapely.geometry import mapping

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "climate_hiv_atlas.duckdb")
SA_KEY = "/home/USER/PRECISE/palsearth/palsearth-sa-key.json"
SA_EMAIL = "ee-service-account@your-project.iam.gserviceaccount.com"

START_YEAR = 1990
END_YEAR = 2026

ERA5_SCALE = 11132
CHIRPS_SCALE = 5566

HEAT_THRESHOLD_C = 35.0
HEAVY_RAIN_THRESHOLD_MM = 20.0
HEATWAVE_MIN_RUN = 3


def ee_init():
    creds = ee.ServiceAccountCredentials(SA_EMAIL, SA_KEY)
    ee.Initialize(credentials=creds, project="your-earth-engine-project")


def load_districts_fc(con, simplify_tolerance_deg=0.01):
    # Full-res district polygons total ~376k vertices -- sending that inline on
    # every reduceRegions call blows the 10MB request-payload limit. Simplified
    # to ~0.01deg (~1.1km) here, well under ERA5-Land/CHIRPS native resolution
    # (11km/5.5km) so it doesn't affect the zonal means. The districts table
    # itself (used for map rendering) keeps full precision -- this only affects
    # the geometry shipped to Earth Engine.
    df = con.execute("SELECT district, geometry_wkt FROM districts").fetchdf()
    gdf = gpd.GeoDataFrame(df, geometry=gpd.GeoSeries.from_wkt(df["geometry_wkt"]), crs="EPSG:4326")
    gdf["geometry"] = gdf.geometry.simplify(simplify_tolerance_deg)
    feats = [ee.Feature(ee.Geometry(mapping(row.geometry)), {"district": row.district}) for row in gdf.itertuples()]
    return ee.FeatureCollection(feats)


def extract_range(districts_fc, start, end, collection_id, band, scale):
    coll = ee.ImageCollection(collection_id).filterDate(start, end).select(band)
    dates = coll.aggregate_array("system:time_start").getInfo()
    if not dates:
        return {}
    band_names = [f"d{i}" for i in range(len(dates))]
    stacked = coll.toBands().rename(band_names)
    result = stacked.reduceRegions(collection=districts_fc, reducer=ee.Reducer.mean(), scale=scale, tileScale=4).getInfo()

    from datetime import datetime, timezone
    iso_dates = [datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat() for ts in dates]

    out = {}
    for feat in result["features"]:
        props = feat["properties"]
        district = props["district"]
        out[district] = {iso_dates[i]: props.get(f"d{i}") for i in range(len(iso_dates))}
    return out


def main():
    start_year = int(sys.argv[1]) if len(sys.argv) > 1 else START_YEAR
    ee_init()
    con = duckdb.connect(DB_PATH)
    districts_fc = load_districts_fc(con)
    n_districts = con.execute("SELECT COUNT(*) FROM districts").fetchone()[0]
    print(f"[OK] EE initialised, {n_districts} district features, resuming from {start_year}")

    for year in range(start_year, END_YEAR + 1):
        year_rows = 0
        t0 = time.time()
        for month in range(1, 13):
            start = f"{year}-{month:02d}-01"
            end_month = month + 1
            end_year = year
            if end_month > 12:
                end_month = 1
                end_year += 1
            end = f"{end_year}-{end_month:02d}-01"

            tmax_by_district = extract_range(districts_fc, start, end, "ECMWF/ERA5_LAND/DAILY_AGGR", "temperature_2m_max", ERA5_SCALE)
            precip_by_district = extract_range(districts_fc, start, end, "UCSB-CHG/CHIRPS/DAILY", "precipitation", CHIRPS_SCALE)

            rows = []
            all_districts = set(tmax_by_district) | set(precip_by_district)
            for district in all_districts:
                tmax_series = tmax_by_district.get(district, {})
                precip_series = precip_by_district.get(district, {})
                all_dates = set(tmax_series) | set(precip_series)
                for d in all_dates:
                    tmax_k = tmax_series.get(d)
                    tmax_c = (tmax_k - 273.15) if tmax_k is not None else None
                    precip_mm = precip_series.get(d)
                    rows.append((district, d, tmax_c, precip_mm))

            if rows:
                con.executemany("INSERT OR REPLACE INTO climate_daily VALUES (?, ?, ?, ?)", rows)
            year_rows += len(rows)

        print(f"[OK] {year}: {year_rows} district-day rows in {time.time()-t0:.1f}s", flush=True)

    con.close()


if __name__ == "__main__":
    main()
