"""
Per-district annual flood-frequency proxy from JRC Global Surface Water
YearlyHistory (1984-2021, 30m, band waterClass: 0=no data/1=not water/
2=seasonal water/3=permanent water).

water_fraction_pct = % of district area classified as seasonal or permanent
water that year -- "how often each area has been under water" per the
original brief, summarised to district per year.

Reduced at 300m (10x native 30m) rather than native scale: a reduceRegions
mean reducer at 300m still correctly estimates the water-covered fraction
(it averages the finer water-class pixels under the hood), but keeps pixel
counts for big rural districts tractable. One call for the whole 1984-2021
collection (single band per year, not daily) so no monthly chunking needed.

NOT CURRENTLY USED (2026-11-02): verified via 3 independent checks --
native 30m vs 300m reduceRegion (both ~60% for Beitbridge), and the
separate long-term JRC "occurrence" composite (also ~63%) -- that JRC GSW's
water classification is unreliable for Zimbabwe's terrain, giving
implausible 60-77% "water fraction" for both arid lowveld districts
(Beitbridge) and the capital (Harare, 66%), where ground truth is low
single digits. This is a known JRC GSW weakness (bright/arid soil and
built-up surfaces misclassified as water), not a bug in this script --
geometry area, pixel counts and scale were all independently confirmed
correct. The app.py endpoints/tables for flood are left in place but
unused by the frontend until a better flood proxy (e.g. CHIRPS-based
extreme rainfall accumulation) replaces this.
"""
import os
import time
import duckdb
import ee

from extract_climate import ee_init, load_districts_fc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "climate_hiv_atlas.duckdb")

GSW_SCALE = 300
START_YEAR = 1990
END_YEAR = 2021


def extract_flood_year(districts_fc, year):
    img = ee.ImageCollection("JRC/GSW1_4/YearlyHistory").filterDate(f"{year}-01-01", f"{year + 1}-01-01").first()
    water_mask = img.select("waterClass").gte(2)  # seasonal or permanent
    result = water_mask.reduceRegions(
        collection=districts_fc, reducer=ee.Reducer.mean(), scale=GSW_SCALE, tileScale=4
    ).getInfo()
    return {f["properties"]["district"]: f["properties"].get("mean") for f in result["features"]}


def main():
    ee_init()
    con = duckdb.connect(DB_PATH)
    districts_fc = load_districts_fc(con)
    con.execute("DELETE FROM flood_annual")

    for year in range(START_YEAR, END_YEAR + 1):
        t0 = time.time()
        fractions = extract_flood_year(districts_fc, year)
        rows = [(district, year, frac * 100.0 if frac is not None else None) for district, frac in fractions.items()]
        con.executemany("INSERT OR REPLACE INTO flood_annual VALUES (?, ?, ?)", rows)
        print(f"[OK] {year}: {len(rows)} districts in {time.time()-t0:.1f}s", flush=True)

    con.close()


if __name__ == "__main__":
    main()
