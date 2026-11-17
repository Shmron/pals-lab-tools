# Harare Flood & Access Tool

**Live:** <https://portal.placealert.org/harare-flood/>

Date-driven Flask app combining flood hazard (from Earth Engine rainfall and terrain data) with access to health facilities, including national annual runs and building flood damage estimates.

**Stack:** Flask, Folium/Leaflet, Google Earth Engine

## Key files

- `build_national_facilities.py`
- `flood_accessibility_pipeline.py`
- `rebuild_dhs_wealth_clusters.py`
- `run_national_annual.py`
- `serve.py`
- `validate_rwi_against_dhs.py`
- …and 6 more
- `flood-damage/`

## Python dependencies

Detected from the imports in this folder (versions are not pinned here):

`earthengine-api`, `Flask`, `folium`, `geopandas`, `h3`, `matplotlib`, `networkx`, `numpy`, `pandas`, `Pillow`, `pyrosm`, `rasterio`, `requests`, `scikit-learn`, `scipy`, `shapely`

## Configuration

Set these environment variables (see the root `.env.example`). Never commit real values.

- `EE_SA_EMAIL`
- `EE_SA_KEY`

## Data

This repository contains source code only. Databases, rasters, survey and participant data and generated outputs are deliberately excluded; see [docs/DATA_AND_PRIVACY.md](../../docs/DATA_AND_PRIVACY.md).

## Notes

- Large inputs (DEMs, population grids, road networks, run outputs) are not included.
- Shares its core pipeline with `tools/cascade-flood-model`.

Part of [PALs Lab Tools](../../README.md). Released under the [MIT licence](../../LICENSE).
