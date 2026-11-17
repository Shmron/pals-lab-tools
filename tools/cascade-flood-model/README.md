# CASCADE Flood-Accessibility Model

Pipeline for estimating how flooding changes travel time to health facilities: floodplain and hazard extraction, road-network routing, facility catchments (E2SFCA), population weighting and crossing risk ranking, developed on pilot sites in Zimbabwe. `METHODS_LOG.md` records the method decisions and why they were made.

**Stack:** Python, GeoPandas/rasterio, NetworkX, Google Earth Engine

## Key files

- `METHODS_LOG.md`
- `build_routing_graph.py`
- `building_flood_damage.py`
- `chimanimani_facility_dashboard.py`
- `chimanimani_facility_dashboard_walking.py`
- `chimanimani_v2_scenario.py`
- `compute_flooded_travel_times.py`
- `compute_site_flood_depth.py`
- `dedupe_crossings_and_bridges.py`
- `e2sfca_scenario.py`
- `extract_chimanimani_facilities.py`
- `extract_chimanimani_roads.py`
- `extract_dhs_wealth_clusters.py`
- `extract_tokwemukosi.py`
- `facility_capacity_estimate.py`
- `facility_catchment_shift.py`
- `fetch_admin_boundaries.py`
- `fetch_chirps_idai.py`
- `fetch_hydrosheds_compare.py`
- `fetch_jrc_flood_hazard.py`
- `fetch_jrc_tokwe.py`
- `fetch_merit_hydro.py`
- `fetch_relative_wealth_index.py`
- `fetch_sar_flood_idai.py`
- `filter_landslides_zimbabwe.py`
- …and 29 more

## Python dependencies

Detected from the imports in this folder (versions are not pinned here):

`branca`, `earthengine-api`, `folium`, `geopandas`, `h3`, `matplotlib`, `networkx`, `numpy`, `pandas`, `Pillow`, `pyrosm`, `rasterio`, `requests`, `scikit-learn`, `scipy`, `shapely`

## Configuration

Set these environment variables (see the root `.env.example`). Never commit real values.

- `EE_SA_EMAIL`
- `EE_SA_KEY`

## Data

This repository contains source code only. Databases, rasters, survey and participant data and generated outputs are deliberately excluded; see [docs/DATA_AND_PRIVACY.md](../../docs/DATA_AND_PRIVACY.md).

## Notes

- All input and output data (rasters, extracts, maps, results) are excluded. Scripts expect local data folders you create.
- The GFPLAIN text files used for reference during development are third-party material and are not included.

Part of [PALs Lab Tools](../../README.md). Released under the [MIT licence](../../LICENSE).
