# Africa Road Proximity

**Live:** <https://portal.placealert.org/>

Distance to the nearest highway and major road for every H3 cell in Africa, with population-weighted admin summaries (WorldPop) as a proxy for traffic pollution and road access.

**Stack:** Python generators, MapLibre front-end

## Key files

- `filter_euclidean_pop.py`
- `generate_euclidean.py`
- `generate_pweuclidean.py`
- `index.html`

## Python dependencies

Detected from the imports in this folder (versions are not pinned here):

`geopandas`, `h3`, `numpy`, `osmium`, `rasterio`, `requests`, `scipy`, `shapely`, `tqdm`

## Data

This repository contains source code only. Databases, rasters, survey and participant data and generated outputs are deliberately excluded; see [docs/DATA_AND_PRIVACY.md](../../docs/DATA_AND_PRIVACY.md).

## Notes

- Generated data files are not included.

Part of [PALs Lab Tools](../../README.md). Released under the [MIT licence](../../LICENSE).
