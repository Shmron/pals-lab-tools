"""
build_tidy_exposure.py
======================
Builds the tidy daily exposure dataset for the GeoAI workshop.

Output: zw_births_daily_exposure.csv
Format: one row per (birth × pregnancy day)
Columns: caseid, cluster_id, Latitude, Longitude, date,
         temp_c, pm25, dust_aod550, precipitation, ndvi

~1.5 million rows (5,794 births × ~270 days each).

Run after zw_daily_climate.csv is complete.
"""

import pathlib, logging
import numpy as np
import pandas as pd

ROOT     = pathlib.Path(__file__).parent
DATA_DIR = ROOT / 'data'
OUT      = DATA_DIR / 'zw_births_daily_exposure.csv'
LOG      = ROOT / 'build_tidy_exposure.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[logging.FileHandler(LOG, mode='w'), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Load inputs ───────────────────────────────────────────────────────────
log.info('Loading births...')
births = pd.read_csv(DATA_DIR / 'zw_births_raw.csv')
log.info(f'  {len(births):,} birth records, {births["cluster_id"].nunique()} clusters')

log.info('Loading daily climate...')
climate = pd.read_csv(DATA_DIR / 'zw_daily_climate.csv', parse_dates=['date'])
log.info(f'  {len(climate):,} daily climate rows')
log.info(f'  Variables: {[c for c in climate.columns if c not in ["cluster_id","Latitude","Longitude","date"]]}')

# Index climate by (cluster_id, date) for fast lookup
climate['date'] = pd.to_datetime(climate['date'])
clim_idx = climate.set_index(['cluster_id', 'date'])
env_cols = [c for c in climate.columns if c not in ('cluster_id', 'Latitude', 'Longitude', 'date')]
log.info(f'  Env columns: {env_cols}')

# ── Expand each birth into daily rows ─────────────────────────────────────
log.info('Expanding pregnancy windows...')

records = []
skipped = 0

for _, row in births.iterrows():
    try:
        conception_date = pd.Timestamp(int(row.conception_year), int(row.conception_month), 1)
        birth_date      = pd.Timestamp(int(row.birth_year),      int(row.birth_month),      1)
    except Exception:
        skipped += 1
        continue

    if birth_date <= conception_date:
        skipped += 1
        continue

    date_range = pd.date_range(conception_date, birth_date, freq='D')
    cid = int(row.cluster_id)

    for d in date_range:
        key = (cid, d)
        if key in clim_idx.index:
            clim_row = clim_idx.loc[key]
            rec = {
                'birth_id':   f"{row.caseid}__{int(row.bidx)}",
                'caseid':     row.caseid,
                'bidx':       int(row.bidx),
                'cluster_id': cid,
                'Latitude':   row.cluster_lat,
                'Longitude':  row.cluster_lon,
                'date':       d.date(),
            }
            for col in env_cols:
                rec[col] = clim_row[col] if col in clim_row else np.nan
            records.append(rec)

log.info(f'  Skipped {skipped} births with missing/invalid dates')
log.info(f'  Building DataFrame from {len(records):,} rows...')

out = pd.DataFrame(records)
out.to_csv(OUT, index=False)
log.info(f'Saved: {OUT.name}  ({len(out):,} rows × {len(out.columns)} columns)')

# ── Quick coverage check ───────────────────────────────────────────────────
log.info('\nCoverage summary:')
for col in env_cols:
    if col in out.columns:
        n = out[col].notna().sum()
        pct = n / len(out) * 100
        log.info(f'  {col:20s}: {n:>10,} non-null ({pct:.1f}%)')

log.info('\nDone. Deploy with:')
log.info(f'  cp {OUT} /opt/geoai_workshop/data/')
