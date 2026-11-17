# Architecture

The tools in this repository share a simple deployment pattern, so each one can be run, tested and replaced on its own.

## Overview

```
Browser
   |
   v
Reverse proxy (nginx) -- static sites: websites/placealert-org, websites/portal-placealert-org
   |
   +--> Flask apps, one process per tool, each on its own local port
   |       climate-health-atlas, palsearth, harare-flood, precise-catalogue API,
   |       ecri-platform, ecri-poster-judging, dhs-portal, data-access services
   |
   +--> Local analytical stores (DuckDB / SQLite), built by scripts in each tool
   |
   +--> External services: Google Earth Engine, Anthropic API (assistants), OpenStreetMap, WorldPop, CHIRPS, ERA5-Land
```

## Conventions

- **One process per tool**, run as a systemd service (example unit files are in some tool folders) and reverse-proxied under a path or sub-domain.
- **Analytical data in DuckDB.** Build scripts (`build_*.py`, `extract_*.py`) turn source data into a local database; the app only reads it.
- **Thresholds applied late.** Where possible, raw daily values are stored and thresholds (for example heat-day definitions) are applied in SQL, so definitions can change without re-downloading data.
- **Assistants act through validated tools.** The AI assistants propose an action; code checks every field against real values (layer names, years, places) before anything runs, and returns an error rather than a guess. Assistants do not supply numbers themselves.
- **Secrets only in the environment.** No keys in code. See [DATA_AND_PRIVACY.md](DATA_AND_PRIVACY.md).

## Running a tool locally

1. Create a virtual environment and install the dependencies listed in the tool's README.
2. Copy `.env.example` to `.env` and set the variables that tool uses.
3. Build or provide the data the tool expects.
4. Start the app, for example `python app.py`, and open the local port it reports.

## Notes

- Some scripts keep the file or table names they had when first written (for example `climate_hiv_atlas.duckdb` in the Climate & Health Atlas). They were not renamed so that the code matches what is deployed.
- Example unit files and scripts mention generic placeholders such as `/home/USER` and `SERVER_IP` in place of real paths and addresses.
