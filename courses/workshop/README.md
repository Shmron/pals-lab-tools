# GeoAI for Climate, Environment and Health
**Pre-Conference Workshop — Zimbabwe Young Surveyors Network**
25 June 2026 | Bulawayo, Zimbabwe

---

## Course overview

A 3-hour hands-on workshop introducing young surveyors and geomatics professionals to climate-health GIS workflows, culminating in a natural-language AI interface over a real spatial database.

Follows the **WorldQuant University** notebook format:
📖 Concept notes → ✅ Worked examples → 🧪 Do-it-yourself exercises → ✔ Auto-check

---

## Directory structure

```
workshop/
├── data/                        ← pre-generated synthetic data
│   ├── health_facilities.csv    ← 26 geocoded facilities (base workshop)
│   ├── climate_grid.csv         ← 810-cell grid snapshot (base workshop)
│   ├── villages.csv             ← 10 study villages
│   ├── participants.csv         ← 60 synthetic pregnant women
│   ├── visits.csv               ← 180 visit records (3 per participant)
│   ├── exposure_grid.csv        ← 567k rows — daily grid over study period
│   ├── participant_exposure_days.csv     ← 17k rows — per-day locations
│   ├── participants_with_exposures.csv   ← 60 rows — trimester summaries
│   └── exposures.duckdb         ← all tables + spatial extension
├── source/                      ← instructor notebooks (with solutions)
│   ├── 01_spatial_health_data.ipynb
│   ├── 02_environmental_datasets.ipynb
│   ├── 03_spatial_join.ipynb
│   ├── 04_exposure_timeline_generation.ipynb
│   ├── 05_earth_engine_extraction.ipynb
│   └── 06_exposure_characterisation_geoai.ipynb
├── release/                     ← student notebooks (solutions stripped by nbgrader)
├── ee_scripts/
│   └── sample_points_demo.js   ← Earth Engine Code Editor script
├── generate_base_data.py        ← generates facilities + base climate grid
├── generate_cohort.py           ← two-pass cohort + exposure grid generator
├── shared_utils.py              ← shared logic (cKDTree join, trimester, timeline)
├── setup_duckdb.py              ← builds exposures.duckdb
└── facilitator_guide.md         ← setup, timing, troubleshooting
```

---

## Quick start (facilitator)

```bash
cd /home/USER/PRECISE/workshop

# 1. Generate all data
python generate_base_data.py
python generate_cohort.py
python setup_duckdb.py

# 2. Install dependencies
pip install jupyterhub nbgrader ipywidgets duckdb scipy

# 3. Generate student notebooks
nbgrader generate_assignment --force 01_spatial_health_data
# ... repeat for 02–06

# 4. Start JupyterHub and direct participants to release/
```

See `facilitator_guide.md` for full details, GeoAI API setup, and troubleshooting.

---

## Notebooks

| # | Title | Time | Points |
|---|-------|------|--------|
| 01 | Spatial Health Data | 25 min | 5 |
| 02 | Environmental Datasets | 20 min | 5 |
| 03 | The Spatial Join | 20 min | 5 |
| 04 | Exposure Timeline Generation | 25 min | 6 |
| 05 | Earth Engine Extraction | 20 min | 3 |
| 06 | Characterisation, EDA & GeoAI | 30 min | 11 (+reflection) |

---

## Dependencies

- `pandas`, `numpy`, `matplotlib`, `scipy` (standard scientific Python)
- `duckdb` + spatial extension (auto-installed by `setup_duckdb.py`)
- `ipywidgets` (GeoAI interface in Notebook 06)
- `earthengine-api` (optional — Notebook 05 demo cells only)
- `jupyterhub`, `nbgrader` (course infrastructure)

---

## GeoAI endpoint

The `/api/workshop/geoai` endpoint is added to the PRECISE Flask server (`api.py`).
It accepts `{"question": "..."}` and returns `{"sql": "...", "error": null}`.
If the server is unreachable, notebooks fall back to a set of pre-written demo Q→SQL pairs.

---

*Convened by CeSHHAR Zimbabwe, Place Alert Labs, Midlands State University — P T Makanga, Z Chinwadzimba, R Sibanda*
