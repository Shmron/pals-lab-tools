# ECRI 2026 Conference Platform

**Live:** <https://portal.placealert.org/ecri/>

Flask platform for the ECRI 2026 conference: submissions, review workflow and organiser/staff tools.

**Stack:** Flask, SQLite

## Key files

- `app.py`
- `static/`
- `templates/`

## Python dependencies

Detected from the imports in this folder (versions are not pinned here):

`Flask`, `itsdangerous`, `openpyxl`, `Pillow`, `poster_judging`, `qrcode`, `reportlab`, `Werkzeug`

## Configuration

Set these environment variables (see the root `.env.example`). Never commit real values.

- `SMTP_APP_PASSWORD`

## Data

This repository contains source code only. Databases, rasters, survey and participant data and generated outputs are deliberately excluded; see [docs/DATA_AND_PRIVACY.md](../../docs/DATA_AND_PRIVACY.md).

## Notes

- Configure the secret key and SMTP settings through environment variables (see `.env.example`).

Part of [PALs Lab Tools](../../README.md). Released under the [MIT licence](../../LICENSE).
