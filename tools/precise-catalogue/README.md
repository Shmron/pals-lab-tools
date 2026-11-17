# PRECISE Network Catalogue and API

**Live:** <https://portal.placealert.org/catalogue/>

Geospatial catalogue of environmental and social determinants of maternal health across the PRECISE sites, with a built-in AI assistant and an authenticated API. Access is controlled.

**Stack:** Flask, DuckDB, Anthropic API (assistant)

## Key files

- `access_db.py`
- `accessibility_engine.py`
- `api.py`
- `catalogue_notify.py`
- `index.html`
- `merge_outcomes.py`
- `precise_db.py`
- `setup_db.py`

## Python dependencies

Detected from the imports in this folder (versions are not pinned here):

`anthropic`, `bcrypt`, `duckdb`, `earthengine-api`, `Flask`, `flask-cors`, `geopandas`, `h3`, `matplotlib`, `numpy`, `pandas`, `pyarrow`, `requests`, `scipy`, `shapely`, `statsmodels`

## Configuration

Set these environment variables (see the root `.env.example`). Never commit real values.

- `ANTHROPIC_API_KEY`
- `CATALOGUE_ACCESS_CODE`
- `HE2AT_ACCESS_CODE`
- `SMTP_APP_PASSWORD`
- `WORKSHOP_KEY`

## Data

This repository contains source code only. Databases, rasters, survey and participant data and generated outputs are deliberately excluded; see [docs/DATA_AND_PRIVACY.md](../../docs/DATA_AND_PRIVACY.md).

## Notes

- **No participant-level data is in this repository.** The catalogue serves it only to authorised users.

Part of [PALs Lab Tools](../../README.md). Released under the [MIT licence](../../LICENSE).
