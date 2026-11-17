"""
Pulls Zimbabwe's national TB incidence (per 100,000 population/year) and TB-HIV
coinfection rate (% of incident TB cases that are also HIV-positive) from WHO's Global
TB Programme, 2000-2024, into tb_national.

National only, not per-district like every other table in this app: checked directly
(full text search of DHS_Codebook.xlsx across all 15 DHS recode types, plus a
column-name sweep of every dhs_*.duckdb file on this server for tb_/tuberc* patterns)
and there is zero TB data anywhere in Zimbabwe's DHS records -- DHS tests for HIV via
blood draw and tests for malaria via RDT in some rounds, but never tests for TB (that
needs sputum/GeneXpert, not part of the DHS protocol). WHO's own published estimates are
the only usable source, and WHO only models/publishes these at country level -- no
subnational breakdown exists publicly, unlike HIV (DHS, district-level via GPS-tagged
clusters) or malaria (Malaria Atlas Project, 5km rasters aggregatable to district).

Endpoint is WHO's own public CSV export (extranet.who.int/tme/generateCSV.asp?ds=estimates),
the same one backing the Global TB Report -- no API key needed, confirmed working
2026-11-05. Full dataset is ~1MB/all countries/all years; filtered to Zimbabwe here.
"""
import os

import duckdb
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "climate_hiv_atlas.duckdb")

WHO_TB_CSV_URL = "https://extranet.who.int/tme/generateCSV.asp?ds=estimates"
ISO3 = "ZWE"

resp = requests.get(WHO_TB_CSV_URL, timeout=60)
resp.raise_for_status()

import csv
import io

reader = csv.DictReader(io.StringIO(resp.text))
rows = [r for r in reader if r["iso3"] == ISO3]
if not rows:
    raise SystemExit(f"[FAIL] no rows found for iso3={ISO3} in WHO TB estimates CSV")

con = duckdb.connect(DB_PATH)
con.execute("DELETE FROM tb_national")
for r in rows:
    con.execute(
        "INSERT INTO tb_national VALUES (?, ?, ?)",
        [int(r["year"]), float(r["e_inc_100k"]), float(r["e_tbhiv_prct"]) if r["e_tbhiv_prct"] else None],
    )
con.close()

years = sorted(int(r["year"]) for r in rows)
print(f"[DONE] tb_national: {len(rows)} rows, years {years[0]}-{years[-1]}")
