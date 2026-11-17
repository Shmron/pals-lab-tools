# PALs Earth

**Live:** <https://portal.placealert.org/palsearth/>

Point-and-extract environmental data for any location and date (NDVI, land-surface temperature, rainfall, elevation, soil and more), delivered analysis-ready. Built on Google Earth Engine, with an AI assistant and a cohort-exposure extraction module.

**Stack:** Flask, Google Earth Engine, DuckDB, Anthropic API (assistant)

## Key files

- `app.py`
- `catalog.py`
- `cohort_delivery.py`
- `cohort_query.py`
- `cutover.sh`
- `dev_restart.sh`
- `install.sh`
- `lock_geocode_proxy.sh`
- `nginx_cutover.py`
- `palsearth-flask.service`
- `rollback.sh`
- `serve.py`
- `state_store.py`
- `legacy-streamlit/`
- `static/`
- `templates/`

## Python dependencies

Detected from the imports in this folder (versions are not pinned here):

`anthropic`, `bcrypt`, `duckdb`, `earthengine-api`, `Flask`, `fpdf`, `geopandas`, `numpy`, `pandas`, `streamlit`, `waitress`

## Configuration

Set these environment variables (see the root `.env.example`). Never commit real values.

- `PALSEARTH_COOKIE_SECURE`
- `PALSEARTH_HOST`
- `PALSEARTH_PREFIX`
- `PALSEARTH_RECOVER_JOBS`
- `PALSEARTH_ROOT`
- `PALSEARTH_SECRET_KEY`
- `PALSEARTH_STATE_DB`
- `SMTP_APP_PASSWORD`

## Data

This repository contains source code only. Databases, rasters, survey and participant data and generated outputs are deliberately excluded; see [docs/DATA_AND_PRIVACY.md](../../docs/DATA_AND_PRIVACY.md).

## Notes

- `legacy-streamlit/` is the original Streamlit version, kept for reference.
- Earth Engine needs your own project and service-account key (see `.env.example`). No keys are stored in this repository.

Part of [PALs Lab Tools](../../README.md). Released under the [MIT licence](../../LICENSE).
