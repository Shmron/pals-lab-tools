# Facilitator Guide
**GeoAI for Climate, Environment and Health**
25 June 2026 | Bulawayo, Zimbabwe | Zimbabwe Young Surveyors Network

---

## Workshop at a glance

| Time | Activity |
|------|----------|
| 0:00 | Welcome, context-setting (slides) — CeSHHAR / Place Alert Labs |
| 0:15 | Notebooks 01–03 (base workshop) — spatial health data, environmental grids, spatial join |
| 1:20 | Break (5 min) |
| 1:25 | Notebook 04 — exposure timeline generation |
| 1:50 | Notebook 05 — Earth Engine extraction |
| 2:10 | Notebook 06 — characterisation, EDA, GeoAI |
| 2:45 | Wrap-up, Q&A |
| 3:00 | End |

---

## Pre-event server setup

### 1. Generate all data files

```bash
cd /home/USER/PRECISE/workshop
python generate_base_data.py
python generate_cohort.py
python setup_duckdb.py
```

All files land in `workshop/data/`. Expected output:
- `health_facilities.csv` — 26 rows
- `climate_grid.csv` — 810 rows
- `villages.csv` — 10 rows
- `participants.csv` — 60 rows
- `visits.csv` — 180 rows
- `exposure_grid.csv` — ~567,000 rows
- `participant_exposure_days.csv` — ~17,000 rows
- `participants_with_exposures.csv` — 60 rows
- `exposures.duckdb` — ~5 tables + 1 view

### 2. Install dependencies (if not already present)

```bash
pip install jupyterhub nbgrader ipywidgets duckdb scipy
jupyter nbextension enable --py widgetsnbextension --sys-prefix
```

### 3. Generate student-facing notebooks (strip solutions)

```bash
cd /home/USER/PRECISE/workshop
nbgrader generate_assignment --force 01_spatial_health_data
nbgrader generate_assignment --force 02_environmental_datasets
nbgrader generate_assignment --force 03_spatial_join
nbgrader generate_assignment --force 04_exposure_timeline_generation
nbgrader generate_assignment --force 05_earth_engine_extraction
nbgrader generate_assignment --force 06_exposure_characterisation_geoai
```

Student notebooks appear in `release/`. Verify they have `...` stubs where solutions were.

### 4. GeoAI API key

The `/api/workshop/geoai` endpoint uses `ANTHROPIC_API_KEY` from the Flask server environment. Confirm the key is set:

```bash
grep ANTHROPIC_API_KEY /etc/environment   # or wherever the key is stored
```

The endpoint will still run in demo mode if the key is missing — participants see pre-written Q→SQL pairs.

Optionally set a workshop-day key to limit access:

```bash
export WORKSHOP_KEY=geoai_june2026   # set before restarting api.py
```

Notebooks will need this in the `X-Workshop-Key` header if set. If `WORKSHOP_KEY` is empty, the endpoint is open (fine for a workshop LAN).

### 5. Test the endpoint

```bash
curl -s -X POST http://localhost:5000/api/workshop/geoai \
  -H "Content-Type: application/json" \
  -d '{"question": "How many participants had a preterm birth?"}' | python -m json.tool
```

Expected response shape: `{"sql": "SELECT COUNT(*) ...", "error": null}`

### 6. Earth Engine availability

The EE demo cells (Notebook 05) use the PALs Earth service account:
- Key: `palsearth/palsearth-sa-key.json`
- Project: `your-earth-engine-project`

If the workshop venue has restricted internet (or EE quota is exhausted), the cells will print a skip message and continue — they do not block downstream cells.

Add placeholder screenshots to `workshop/images/`:
- `ee_asset_upload.png` — showing the Assets panel → New → Table upload dialog
- `ee_tasks.png` — showing the Tasks panel with a completed export

---

## nbgrader commands (quick reference)

```bash
# List assignments
nbgrader list

# Collect a submission (if using JupyterHub submit flow)
nbgrader collect 01_spatial_health_data --course-dir .

# Autograde
nbgrader autograde 01_spatial_health_data

# Generate feedback HTML
nbgrader generate_feedback 01_spatial_health_data

# View scores
nbgrader export
```

For the one-day event, participants can see their scores inline (the auto-check cells print ✅/❌ immediately). Formal collection is optional.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `ModuleNotFoundError: shared_utils` | The notebook path detection relies on `pathlib.Path().resolve().parent`. Ensure notebooks are opened from `release/` or `source/`, not a copy at another path. |
| DuckDB spatial extension fails to install | The notebook falls back to a pandas equivalent for spatial queries. No action needed. |
| `ipywidgets` not rendering | Run `jupyter nbextension enable --py widgetsnbextension` and restart the kernel. |
| EE `ee.Initialize()` fails | Skip section 2 of Notebook 05 — nothing else depends on it. |
| GeoAI returns demo mode | Check `ANTHROPIC_API_KEY` is set in the Flask process environment. |
| Notebooks take >3 hours | Notebooks 01–03 can be shortened by skipping worked examples and going directly to exercises if participants are already comfortable with pandas. |
