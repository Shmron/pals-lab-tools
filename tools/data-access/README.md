# Data Access Request Services

Small services that handle requests for API tokens and fast DuckDB access to the lab research database, and for the road-network geodata APIs: request forms, approval flow and token issuing.

**Stack:** Flask

## Key files

- `duckdb_request.py`
- `geodata_api_request.py`

## Python dependencies

Detected from the imports in this folder (versions are not pinned here):

`access_db`, `bcrypt`, `Flask`, `fpdf`, `Werkzeug`

## Configuration

Set these environment variables (see the root `.env.example`). Never commit real values.

- `CATALOGUE_ACCESS_CODE`
- `FLASK_SECRET_KEY`
- `HE2AT_ACCESS_CODE`
- `SMTP_APP_PASSWORD`

## Data

This repository contains source code only. Databases, rasters, survey and participant data and generated outputs are deliberately excluded; see [docs/DATA_AND_PRIVACY.md](../../docs/DATA_AND_PRIVACY.md).

## Notes

- Contact addresses in the code are placeholders; configure real values through environment variables.

Part of [PALs Lab Tools](../../README.md). Released under the [MIT licence](../../LICENSE).
