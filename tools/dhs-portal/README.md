# DHS Explorer

**Live:** <https://dhs.placealert.org>

Access-controlled portal and API for exploring Demographic and Health Survey data held by the lab.

**Stack:** Flask, DuckDB, Leaflet, Anthropic API (assistant)

## Key files

- `api.py`
- `dhs_access.py`
- `dhs_codebook.py`
- `web/`

## Python dependencies

Detected from the imports in this folder (versions are not pinned here):

`anthropic`, `bcrypt`, `duckdb`, `Flask`, `flask-cors`, `openpyxl`

## Configuration

Set these environment variables (see the root `.env.example`). Never commit real values.

- `ANTHROPIC_API_KEY`
- `SMTP_APP_PASSWORD`

## Data

This repository contains source code only. Databases, rasters, survey and participant data and generated outputs are deliberately excluded; see [docs/DATA_AND_PRIVACY.md](../../docs/DATA_AND_PRIVACY.md).

## Notes

- **No DHS data is included.** DHS microdata must be obtained from the DHS Program under its terms of use.

Part of [PALs Lab Tools](../../README.md). Released under the [MIT licence](../../LICENSE).
