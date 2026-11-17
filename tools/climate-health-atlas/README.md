# Climate & Health Atlas

**Live:** <https://maps.placealert.org>

Interactive atlas of Zimbabwe's 62 districts and 10 provinces linking climate (heat and rain), HIV, malaria, TB and flood-related access to care. Year-by-year animation, per-district and per-province trend charts with a 3-year average and full-period trend line, a short plain-language summary under each chart, a climate x health comparison map, a composite ranking, and a natural-language assistant that drives the map's own controls from its own tables. The in-app "Data & methodology" page documents every method and the quality checks that were run.

**Stack:** Flask, DuckDB, Leaflet, Google Earth Engine, Anthropic API (assistant)

## Key files

- `add_province.py`
- `ai_agent.py`
- `app.py`
- `build_access.py`
- `build_climate_annual.py`
- `build_db.py`
- `build_hiv_burden.py`
- `climate-hiv-atlas.service`
- `extract_climate.py`
- `extract_flood.py`
- `extract_malaria.py`
- `extract_tb.py`
- `core/`
- `static/`
- `statistical-analysis/`
- `templates/`

## Python dependencies

Detected from the imports in this folder (versions are not pinned here):

`anthropic`, `duckdb`, `earthengine-api`, `Flask`, `geopandas`, `numpy`, `pandas`, `rasterio`, `requests`, `shapely`, `statsmodels`

## Configuration

The assistant needs an Anthropic API key. `core/apikey.py` reads `ANTHROPIC_API_KEY` from the environment (or an env file). No key is stored in this repository. Set `PORT` to choose the port (default 8506 in the provided service file).

## Data

This repository contains source code only. Databases, rasters, survey and participant data and generated outputs are deliberately excluded; see [docs/DATA_AND_PRIVACY.md](../../docs/DATA_AND_PRIVACY.md).

## Notes

- The database file keeps its original name, `climate_hiv_atlas.duckdb`, from before the atlas grew beyond HIV. It is built by the `build_*.py` / `extract_*.py` scripts and is **not** included.
- `statistical-analysis/` holds separate DHS-based models (births and HIV testing against climate) that are independent of the live app.
- Sources: ERA5-Land, CHIRPS, Zimbabwe DHS, Malaria Atlas Project, WHO TB estimates, FAO GAUL 2015 boundaries.

Part of [PALs Lab Tools](../../README.md). Released under the [MIT licence](../../LICENSE).
