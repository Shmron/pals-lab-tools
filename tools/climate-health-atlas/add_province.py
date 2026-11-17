"""
One-off migration: adds a `province` column to `districts` (from FAO GAUL
2015 level2's own ADM1_NAME, via district_province_map.json) and a new
`provinces` table holding dissolved province boundaries + summed area.

Run once against the live climate_hiv_atlas.duckdb, then build_db.py is
updated to do the same on any future full rebuild.
"""
import json
import os

import duckdb
import shapely.wkt
from shapely.ops import unary_union

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "climate_hiv_atlas.duckdb")
MAP_PATH = os.path.join(BASE_DIR, "district_province_map.json")

mapping = json.load(open(MAP_PATH))

con = duckdb.connect(DB_PATH)

cols = [r[1] for r in con.execute("PRAGMA table_info('districts')").fetchall()]
if "province" not in cols:
    con.execute("ALTER TABLE districts ADD COLUMN province VARCHAR")

for district, province in mapping.items():
    con.execute("UPDATE districts SET province = ? WHERE district = ?", [province, district])

missing = con.execute("SELECT district FROM districts WHERE province IS NULL").fetchall()
if missing:
    raise SystemExit(f"[FAIL] no province mapped for: {missing}")
print("[OK] districts.province populated for all rows")

rows = con.execute("SELECT district, province, area_km2, geometry_wkt FROM districts").fetchall()
by_province = {}
for district, province, area_km2, wkt in rows:
    by_province.setdefault(province, {"area": 0.0, "geoms": []})
    by_province[province]["area"] += area_km2
    by_province[province]["geoms"].append(shapely.wkt.loads(wkt))

con.execute("DROP TABLE IF EXISTS provinces")
con.execute("""
    CREATE TABLE provinces (
        province VARCHAR PRIMARY KEY,
        area_km2 DOUBLE,
        geometry_wkt VARCHAR
    )
""")
for province, d in by_province.items():
    dissolved = unary_union(d["geoms"])
    con.execute("INSERT INTO provinces VALUES (?, ?, ?)", [province, d["area"], dissolved.wkt])

print(f"[OK] provinces table built: {con.execute('SELECT COUNT(*) FROM provinces').fetchone()[0]} rows")
con.close()
