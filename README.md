# PALs Lab Tools

Open-source code for the geospatial climate-and-health tools built at **Place Alert Labs (PALs Lab)**: research pipelines, exposure models, data portals, courses and the two websites that bring them together.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/Shmron/pals-lab-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/Shmron/pals-lab-tools/actions/workflows/ci.yml)

- Website: <https://placealert.org>
- PALs Portal (the front door to every live tool): <https://portal.placealert.org>

> This repository holds **source code only**. No databases, survey data, participant data, API keys or credentials are included. See [docs/DATA_AND_PRIVACY.md](docs/DATA_AND_PRIVACY.md).

## The tools

| Tool | What it does | Live | Code |
|---|---|---|---|
| **Climate & Health Atlas** | Maps Zimbabwe's 62 districts and 10 provinces, linking heat, rain, HIV, malaria, TB and flood-related access to care, year by year, with trend charts and a validated AI assistant | [maps.placealert.org](https://maps.placealert.org) | [`tools/climate-health-atlas`](tools/climate-health-atlas) |
| **PALs Earth** | Point-and-extract environmental data (NDVI, land-surface temperature, rainfall, elevation, soil and more) for any location and date, on Google Earth Engine | [portal](https://portal.placealert.org/palsearth/) | [`tools/palsearth`](tools/palsearth) |
| **HarmonAIze** | AI-assisted geocoding and coordinate validation for messy facility and address records | [portal](https://portal.placealert.org/harmonaize/) | [Shmron/HarmonAIze](https://github.com/Shmron/HarmonAIze) (separate repo, see note below) |
| **Harare Flood & Access Tool** | Flood hazard combined with access to health facilities | [portal](https://portal.placealert.org/harare-flood/) | [`tools/harare-flood`](tools/harare-flood) |
| **CASCADE Flood-Accessibility Model** | The pipeline and method log behind flood-related access estimates | n/a | [`tools/cascade-flood-model`](tools/cascade-flood-model) |
| **HE²AT Environmental Exposure Catalogue** | 35+ environmental exposure variables for maternal and newborn health in Sub-Saharan Africa (environmental data only) | [portal](https://portal.placealert.org/heat-catalogue/) | [`tools/exposure-catalogue`](tools/exposure-catalogue) |
| **PRECISE Network Catalogue and API** | Environmental and social determinants of maternal health across the PRECISE sites, with an AI assistant (access controlled) | [portal](https://portal.placealert.org/catalogue/) | [`tools/precise-catalogue`](tools/precise-catalogue) |
| **Africa Road Network Density** | Africa-wide H3 map of road density from OpenStreetMap | [portal](https://portal.placealert.org) | [`tools/africa-road-density`](tools/africa-road-density) |
| **Africa Road Proximity** | Distance to the nearest highway and major road for every H3 cell in Africa | [portal](https://portal.placealert.org) | [`tools/africa-road-proximity`](tools/africa-road-proximity) |
| **Cohort Exposures** | Attach environmental exposures to cohort records (now part of PALs Earth) | [portal](https://portal.placealert.org/cohort-exposures/) | [`tools/cohort-exposures`](tools/cohort-exposures) |
| **ECRI 2026 Platform** | Conference platform: submissions, review and organiser tools | [portal](https://portal.placealert.org/ecri/) | [`tools/ecri-platform`](tools/ecri-platform) |
| **ECRI Poster Judging** | Score conference posters on five criteria and vote for a winner, on any device | [portal](https://portal.placealert.org/ecri/judging/) | [`tools/ecri-poster-judging`](tools/ecri-poster-judging) |
| **DHS Explorer** | Access-controlled portal and API for DHS data held by the lab | [dhs.placealert.org](https://dhs.placealert.org) | [`tools/dhs-portal`](tools/dhs-portal) |
| **Data access services** | Request and approval flows for API tokens, DuckDB access and geodata APIs | n/a | [`tools/data-access`](tools/data-access) |

### Courses

Free, certificate-bearing course material: [`courses/geoai-workshop`](courses/geoai-workshop) and [`courses/workshop`](courses/workshop) (GeoAI for Climate, Environment & Health).

### Websites

The static source of [placealert.org](https://placealert.org) ([`websites/placealert-org`](websites/placealert-org)) and the PALs Portal ([`websites/portal-placealert-org`](websites/portal-placealert-org)). Pages for tools developed by partners are not included.

## Repository layout

```
tools/        one folder per tool, each with its own README
courses/      course notebooks and facilitator material (outputs cleared)
websites/     static site source
docs/         architecture, data and privacy notes
.github/      CI, issue and pull request templates
```

## Getting started

Each tool is independent. In a tool's folder:

1. Read its `README.md` for the stack, key files and configuration.
2. Create a Python environment and install the dependencies listed there (versions are not pinned yet).
3. Copy [`.env.example`](.env.example) to `.env` and fill in your own values. **Never commit `.env`.**
4. Provide your own data (databases, rasters, survey files). See [docs/DATA_AND_PRIVACY.md](docs/DATA_AND_PRIVACY.md) for what is and is not shared.

Tools that use Google Earth Engine need your own Earth Engine project and service account. The code reads these from environment variables.

## About HarmonAIze

HarmonAIze lives in its own repository, [Shmron/HarmonAIze](https://github.com/Shmron/HarmonAIze), which is a fork of the upstream open-source project [drnicholasbrink/HarmonAIze](https://github.com/drnicholasbrink/HarmonAIze). Please credit the upstream project when citing it.

## Data sources and credit

Tools here build on openly available data and models, including ERA5-Land (ECMWF), CHIRPS (UCSB Climate Hazards Center), the Malaria Atlas Project, WHO TB estimates, OpenStreetMap, WorldPop, FAO GAUL boundaries, Google Earth Engine, and the Demographic and Health Surveys (DHS) Program. Please cite and follow the terms of each source. Each tool's own documentation says which sources it uses and what has and has not been validated.

## Contributing, security and citation

- [CONTRIBUTING.md](CONTRIBUTING.md) explains how to propose changes.
- [SECURITY.md](SECURITY.md) explains how to report a vulnerability privately.
- [CITATION.cff](CITATION.cff) gives the preferred citation.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) sets community expectations.

## Licence

[MIT](LICENSE). The licence covers the code in this repository only. Data and third-party material keep their own licences and terms of use.
