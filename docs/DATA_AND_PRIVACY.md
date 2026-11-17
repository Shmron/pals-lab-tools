# Data and privacy

This repository is **source code only**. That is deliberate, and it is how the lab protects participants and respects data-sharing agreements.

## What is not in this repository

- **Participant-level data**, including anything from the PRECISE cohort. The catalogues and APIs here serve such data only to authorised users, from private infrastructure.
- **Demographic and Health Survey (DHS) microdata.** DHS data must be requested from the [DHS Program](https://dhsprogram.com) and used under its terms. Tools such as the DHS Explorer and the HIV layers of the Climate & Health Atlas expect you to supply your own authorised copy.
- **Databases, rasters, vector layers and generated outputs** (DuckDB, SQLite, Parquet, GeoPackage, GeoTIFF and similar).
- **Credentials of any kind**: API keys, service-account keys, passwords, tokens, SMTP settings.
- **Personal contact details.** Addresses in the code are placeholders. Configure real ones through environment variables.

## How secrets are handled

- Configuration comes from environment variables. [`.env.example`](../.env.example) lists the variable names with empty values.
- `.env` and key files are ignored by [`.gitignore`](../.gitignore).
- CI runs a secret scan on every push and pull request.
- If you find a secret in this repository or its history, please report it privately (see [SECURITY.md](../SECURITY.md)).

## Using your own data

Each tool's README lists what it expects. As a rule, build any local databases with the scripts provided and keep them outside version control.

## Third-party data and terms

Tools use ERA5-Land, CHIRPS, the Malaria Atlas Project, WHO TB estimates, OpenStreetMap, WorldPop, FAO GAUL boundaries, Google Earth Engine and the DHS Program. Each has its own licence and citation requirements. The MIT licence in this repository covers the code only.

## What the tools do and do not claim

The Climate & Health Atlas documents its methods and checks on its in-app "Data & methodology" page. In short: the climate layers have not been compared with weather stations, district-level HIV estimates rest on small survey samples, and malaria and TB figures are used as published by their providers. Treat outputs as estimates.
