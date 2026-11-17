# ECRI Poster Judging

**Live:** <https://portal.placealert.org/ecri/judging/>

Score conference posters on five criteria, then vote on the winner. Adjudicators open it on any device and tap their number.

**Stack:** Flask, SQLite

## Key files

- `app.py`
- `poster_judging.py`
- `templates/`

## Python dependencies

Detected from the imports in this folder (versions are not pinned here):

`Flask`, `Werkzeug`

## Configuration

Set these environment variables (see the root `.env.example`). Never commit real values.

- `POSTER_BASE_URL`
- `POSTER_CHIEF_PASSWORD`
- `POSTER_DB`
- `POSTER_PREFIX`

## Data

This repository contains source code only. Databases, rasters, survey and participant data and generated outputs are deliberately excluded; see [docs/DATA_AND_PRIVACY.md](../../docs/DATA_AND_PRIVACY.md).

## Notes

- Set `POSTER_CHIEF_PASSWORD`; if it is unset a random password is generated at start-up.

Part of [PALs Lab Tools](../../README.md). Released under the [MIT licence](../../LICENSE).
