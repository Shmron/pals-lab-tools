# Africa Road Network Density

**Live:** <https://portal.placealert.org/>

Africa-wide H3 hexagon map (resolution 7, about 5 km²) of road density from OpenStreetMap, a continent-scale proxy for traffic-related air-pollution exposure.

**Stack:** Python generators, MapLibre front-end

## Key files

- `generate_pwrnd.py`
- `generate_road_lines.py`
- `generate_roadnet.py`
- `index.html`
- `split_roadnet.py`

## Python dependencies

Detected from the imports in this folder (versions are not pinned here):

`geopandas`, `h3`, `numpy`, `osmium`, `pandas`, `pyproj`, `rasterio`, `requests`, `shapely`, `tqdm`

## Data

This repository contains source code only. Databases, rasters, survey and participant data and generated outputs are deliberately excluded; see [docs/DATA_AND_PRIVACY.md](../../docs/DATA_AND_PRIVACY.md).

## Notes

- The generated tile/data files are not included; the generator scripts reproduce them from OpenStreetMap.

Part of [PALs Lab Tools](../../README.md). Released under the [MIT licence](../../LICENSE).
