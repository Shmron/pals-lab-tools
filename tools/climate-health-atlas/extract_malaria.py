"""
Pulls district-mean Plasmodium falciparum parasite rate (PfPR2-10, the standard
2-10-year-old prevalence indicator) and Pf incidence rate (cases per 1,000/year) from
the Malaria Atlas Project, 2000-2025, into malaria_annual.

Why this isn't an Earth Engine pull like climate/flood: checked EE's own "Malaria Atlas
Project" publisher catalog (developers.google.com/earth-engine/datasets/publisher/
malariaatlasproject) and it only carries MAP's environmental *covariates* (EVI, land
surface temp, accessibility, friction surface) -- the actual PfPR/incidence output
rasters are not mirrored into EE. They're served from MAP's own GeoServer instead
(data.malariaatlas.org/geoserver), over WCS 2.0.1 -- the same backend the R
`malariaAtlas::getRaster()` package hits, just called directly here since this
pipeline is Python. No API key needed; it's a public GeoServer.

Why 2000, not 1990 like everything else in this app: 2000 is MAP's earliest modelled
year for this surface (confirmed via DescribeCoverage's TimeDomain) -- there is no
earlier raster to extract, so malaria_years is deliberately a shorter axis than
climate_years/hivRounds in the frontend, not a bug.

Zonal stats (plain pixel mean per district, ~5km pixels at this resolution so each of
Zimbabwe's 62 districts covers many pixels) mirror how climate_annual's district values
are produced -- province-level area-weighting is then done the same way app.py already
does it for climate/HIV (SUM(value*area)/SUM(area) in SQL), not duplicated here.
"""
import os
import time

import duckdb
import geopandas as gpd
import numpy as np
import rasterio
import rasterio.features
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "climate_hiv_atlas.duckdb")

WCS_BASE = "https://data.malariaatlas.org/geoserver/Malaria/ows"
# Latest MAP release as of 2026-11-05 (checked via owslib WebCoverageService listing
# against the Malaria workspace) -- re-check periodically, MAP ships a new vintage
# every few months and old ones stay queryable under their own tag.
COVERAGES = {
    "pf_parasite_rate": "Malaria__202608_Global_Pf_Parasite_Rate",
    "pf_incidence_rate": "Malaria__202608_Global_Pf_Incidence_Rate",
}
START_YEAR = 2000
END_YEAR = 2025
NODATA = -9999.0

con = duckdb.connect(DB_PATH)
districts = gpd.GeoDataFrame(
    con.execute("SELECT district, area_km2, geometry_wkt FROM districts").fetchdf(),
    geometry=gpd.GeoSeries.from_wkt(con.execute("SELECT geometry_wkt FROM districts").fetchdf()["geometry_wkt"]),
    crs="EPSG:4326",
)[["district", "area_km2", "geometry"]]

# Padded a little past the tight district union so edge districts aren't clipped by a
# bbox that exactly matches their own boundary (floating-point edge pixels).
minx, miny, maxx, maxy = districts.total_bounds
pad = 0.15
minx, miny, maxx, maxy = minx - pad, miny - pad, maxx + pad, maxy + pad


def fetch_year(coverage_id, year):
    url = (
        f"{WCS_BASE}?service=WCS&version=2.0.1&request=GetCoverage"
        f"&coverageId={coverage_id}"
        f"&subset=Lat({miny},{maxy})&subset=Long({minx},{maxx})"
        f'&subset=time("{year}-01-01T00:00:00.000Z")'
        f"&format=image/tiff"
    )
    for attempt in range(3):
        resp = requests.get(url, timeout=120)
        if resp.ok and resp.content[:2] in (b"II", b"MM"):  # valid TIFF magic bytes
            return resp.content
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"GetCoverage failed for {coverage_id} {year}: {resp.status_code} {resp.text[:200]}")


def district_means(tiff_bytes):
    # rasterstats pulls in pyogrio, which conflicts with this conda env's GDAL install
    # (version mismatch -> ValueError at import). Zonal mean is simple enough to do
    # directly: rasterize each district's polygon index into a label array matching the
    # downloaded raster's grid, then average band-1 pixels per label. One rasterize call
    # for all 62 districts, not one mask per district.
    with rasterio.io.MemoryFile(tiff_bytes) as memfile:
        with memfile.open() as src:
            band1 = src.read(1)
            transform = src.transform
            out_shape = (src.height, src.width)
    # Largest-area district first, smallest last: rasterize's overlap rule is
    # last-shape-wins, so without this, Chitungwiza (47.7 km^2, Zimbabwe's smallest
    # district by far and an enclave right next to much-larger Harare) loses every pixel
    # it shares with its larger neighbour and ends up with zero labelled pixels at all --
    # confirmed: happened for all 26 years once all_touched=True (below) made more
    # pixels count as "shared" in the first place. Processing small/enclave districts
    # last means they're the one that wins the shared pixels, not the other way round.
    order = districts["area_km2"].sort_values(ascending=False).index
    shapes = [(districts.geometry[i], i + 1) for i in order]
    # all_touched=True, not the default pixel-center-contains rule -- small districts
    # are tight enough relative to this raster's ~5km pixels that the default rule can
    # miss every pixel center depending on that year's exact grid alignment (confirmed
    # for Chitungwiza, 2009 only, before the ordering fix above was also needed).
    # all_touched catches any pixel the polygon touches at all, which is what you want
    # for small-area zonal stats anyway -- the slight over-inclusion at district borders
    # doesn't matter at this resolution relative to never getting a value at all.
    labels = rasterio.features.rasterize(
        shapes, out_shape=out_shape, transform=transform, fill=0, dtype="int32", all_touched=True
    )
    valid = band1 != NODATA
    means = []
    for i in range(len(districts)):
        mask = valid & (labels == i + 1)
        means.append(float(band1[mask].mean()) if mask.any() else None)
    return means


con.execute("DELETE FROM malaria_annual")
n_rows = 0
for year in range(START_YEAR, END_YEAR + 1):
    pfpr_means = district_means(fetch_year(COVERAGES["pf_parasite_rate"], year))
    incid_means = district_means(fetch_year(COVERAGES["pf_incidence_rate"], year))
    for i, district in enumerate(districts["district"]):
        # The raw raster stores both as per-1 proportions (PfPR e.g. 0-0.3, incidence
        # e.g. 0-0.05). Scaled here to match this app's existing convention -- HIV
        # prevalence is stored as prevalence_pct (0-100), not a 0-1 fraction -- and to
        # match the "cases per thousand" units MAP's own Data Explorer CSV export uses,
        # so a district's number here is directly comparable to what you'd see there.
        # Scaling at storage time, not display time, matters because the color-ramp
        # thresholds and legend ticks read these same stored values directly (see
        # app.py's compute_layer_ranges) -- there's no separate display-only transform.
        pfpr = pfpr_means[i] * 100 if pfpr_means[i] is not None else None
        incid = incid_means[i] * 1000 if incid_means[i] is not None else None
        con.execute(
            "INSERT INTO malaria_annual VALUES (?, ?, ?, ?)",
            [district, year, pfpr, incid],
        )
        n_rows += 1
    print(f"[OK] {year}: {len([v for v in pfpr_means if v is not None])}/{len(districts)} districts with PfPR data")

con.close()
print(f"[DONE] malaria_annual: {n_rows} rows ({END_YEAR - START_YEAR + 1} years x {len(districts)} districts)")
