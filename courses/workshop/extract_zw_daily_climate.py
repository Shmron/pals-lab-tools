"""
extract_zw_daily_climate.py — hybrid approach
==============================================
From local NetCDF (/PALS/Data and Dictionaries/CAMS_DustAod_temp_PM2.5.nc):
  t2m       → ERA5 2-m temperature (K → °C)
  duaod550  → CAMS dust aerosol optical depth at 550nm
  pm2p5     → CAMS PM2.5 surface concentration (µg/m³)

From EE (demonstrated in NB02; background extraction for completeness):
  CHIRPS    → daily precipitation via getRegion() batches
  NDVI      → MODIS daily/16-day composite via getRegion() batches

NC extraction completes in minutes. EE extraction: several hours.
"""

import pathlib, sys, time, logging
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

ROOT       = pathlib.Path(__file__).parent
DATA_DIR   = ROOT / 'data'
LOG_FILE   = ROOT / 'extract_daily_climate.log'
OUT_FILE   = DATA_DIR / 'zw_daily_climate.csv'
CKPT_DIR   = DATA_DIR
NC_FILE    = pathlib.Path('/PALS/Data and Dictionaries/CAMS_DustAod_temp_PM2.5.nc')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, mode='a'), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Load cluster locations ────────────────────────────────────────────────
clusters = pd.read_csv(DATA_DIR / 'zw_clusters.csv')[
    ['cluster_id', 'cluster_lat', 'cluster_lon']
].copy()
cluster_arr = clusters[['cluster_lat', 'cluster_lon']].values
cluster_tree = cKDTree(cluster_arr)
log.info(f'Clusters loaded: {len(clusters)}')

# ── Unique target dates from extraction targets ───────────────────────────
targets = pd.read_csv(DATA_DIR / 'zw_extraction_targets.csv', parse_dates=['date'])
all_dates = pd.to_datetime(targets['date'].unique())
all_dates = pd.DatetimeIndex(sorted(all_dates))
log.info(f'Unique target dates: {len(all_dates)} ({all_dates[0].date()} → {all_dates[-1].date()})')

# ─────────────────────────────────────────────────────────────────────────
# PART 1 — ERA5 temperature from local NetCDF
# ─────────────────────────────────────────────────────────────────────────
log.info('\n=== ERA5 t2m from local NetCDF ===')

NC_CKPT = CKPT_DIR / '_ckpt2_nc.csv'

# NC variables: (nc_variable, output_column, transform_fn)
NC_VARS = [
    ('t2m',      'temp_c',      lambda v: v - 273.15),   # K → °C
    ('duaod550', 'dust_aod550', lambda v: v),             # dimensionless
    ('pm2p5',    'pm25',        lambda v: v),             # µg/m³
]

def extract_nc_local():
    import xarray as xr
    ds = xr.open_dataset(str(NC_FILE))
    log.info(f'NC file: {len(ds.valid_time)} time steps, vars: {list(ds.data_vars)}')

    nc_lat = ds.latitude.values
    nc_lon = ds.longitude.values

    # For each cluster, find the nearest NC grid point
    # NC grid is a full lat×lon grid; build 2-D lookup
    nc_coords = np.array([[la, lo] for la in nc_lat for lo in nc_lon])
    nc_tree = cKDTree(nc_coords)
    _, nc_flat_idx = nc_tree.query(cluster_arr)
    nc_lat_pos = nc_flat_idx // len(nc_lon)
    nc_lon_pos = nc_flat_idx  % len(nc_lon)

    nc_dates = pd.to_datetime(ds.valid_time.values).normalize()
    date_to_tidx = {d: i for i, d in enumerate(nc_dates)}

    # Load all arrays into memory at once (faster than repeated xarray indexing)
    arrays = {nc_var: ds[nc_var].values for nc_var, _, _ in NC_VARS}

    records = []
    for dt in all_dates:
        tidx = date_to_tidx.get(pd.Timestamp(dt.date()))
        if tidx is None:
            continue
        for ci, row in clusters.iterrows():
            li, loi = int(nc_lat_pos[ci]), int(nc_lon_pos[ci])
            rec = {
                'cluster_id': int(row.cluster_id),
                'Latitude':   float(row.cluster_lat),
                'Longitude':  float(row.cluster_lon),
                'date':       dt.date(),
            }
            for nc_var, out_col, fn in NC_VARS:
                raw_val = float(arrays[nc_var][tidx, li, loi])
                rec[out_col] = round(fn(raw_val), 4)
            records.append(rec)

    df = pd.DataFrame(records)
    df.to_csv(NC_CKPT, index=False)
    df.to_csv(DATA_DIR / 'zw_daily_nc.csv', index=False)
    log.info(f'NC extraction done: {len(df):,} rows → zw_daily_nc.csv')
    return df

if NC_CKPT.exists():
    nc_df = pd.read_csv(NC_CKPT, parse_dates=['date'])
    log.info(f'NC checkpoint loaded: {len(nc_df):,} rows (skipping NC extraction)')
else:
    nc_df = extract_nc_local()

# ─────────────────────────────────────────────────────────────────────────
# PART 2 — EE setup (CHIRPS + NDVI)
# ─────────────────────────────────────────────────────────────────────────
SA_KEY   = ROOT.parent / 'palsearth' / 'palsearth-sa-key.json'
SA_EMAIL = 'ee-service-account@your-project.iam.gserviceaccount.com'
import ee
credentials = ee.ServiceAccountCredentials(SA_EMAIL, str(SA_KEY))
ee.Initialize(credentials)
log.info('Earth Engine initialised.')

# Split 399 clusters into batches of 50 for getRegion
POINT_BATCH_SIZE = 50
cluster_batches = [
    clusters.iloc[i:i+POINT_BATCH_SIZE].reset_index(drop=True)
    for i in range(0, len(clusters), POINT_BATCH_SIZE)
]
log.info(f'Point batches: {len(cluster_batches)} × up to {POINT_BATCH_SIZE} points')

def make_fc(batch_df):
    """Build EE FeatureCollection for a cluster batch."""
    return ee.FeatureCollection([
        ee.Feature(
            ee.Geometry.Point([float(r.cluster_lon), float(r.cluster_lat)]),
            {'cid': int(r.cluster_id)}
        )
        for _, r in batch_df.iterrows()
    ])

# Weekly time windows spanning target dates
week_starts = pd.date_range(
    all_dates[0].floor('D') - pd.Timedelta(days=all_dates[0].dayofweek),
    all_dates[-1],
    freq='W-MON'
)
log.info(f'Weekly windows: {len(week_starts)}')


def fetch_week_batch(collection_id, band, week_start, batch_df, scale,
                     value_scale=1.0, offset=0.0, tag=''):
    """getRegion for one week × one point batch. Returns DataFrame with cluster_id."""
    w_end = (week_start + pd.Timedelta(days=7)).strftime('%Y-%m-%d')
    w_str = week_start.strftime('%Y-%m-%d')
    fc = make_fc(batch_df)

    coll = (ee.ImageCollection(collection_id)
              .select(band)
              .filterDate(w_str, w_end))

    for attempt in range(4):
        try:
            raw = coll.getRegion(fc, scale).getInfo()
            break
        except Exception as exc:
            if attempt == 3:
                return pd.DataFrame()
            time.sleep(2 ** attempt * 3)
    else:
        return pd.DataFrame()

    if not raw or len(raw) < 2:
        return pd.DataFrame()

    header = raw[0]
    try:
        band_idx = header.index(band)
        time_idx = header.index('time')
        lon_idx  = header.index('longitude')
        lat_idx  = header.index('latitude')
    except ValueError:
        return pd.DataFrame()

    # Build local KDTree for this batch to match returned pixel coords → cluster_id
    batch_arr = batch_df[['cluster_lat', 'cluster_lon']].values
    batch_tree = cKDTree(batch_arr)

    records = []
    for r in raw[1:]:
        try:
            v   = r[band_idx]
            ts  = r[time_idx]
            lon = r[lon_idx]
            lat = r[lat_idx]
            if v is None or ts is None:
                continue
            date = pd.Timestamp(ts, unit='ms').date()
            val  = float(v) * value_scale + offset
            # Nearest cluster in this batch
            _, ci = batch_tree.query([lat, lon])
            cid = int(batch_df.iloc[ci]['cluster_id'])
            clat = float(batch_df.iloc[ci]['cluster_lat'])
            clon = float(batch_df.iloc[ci]['cluster_lon'])
            records.append({'cluster_id': cid, 'Latitude': clat, 'Longitude': clon,
                            'date': date, 'value': val})
        except Exception:
            continue

    return pd.DataFrame(records) if records else pd.DataFrame()


def load_checkpoint2(var):
    p = CKPT_DIR / f'_ckpt2_{var}.csv'
    if p.exists():
        df = pd.read_csv(p, parse_dates=['date'])
        df['date'] = pd.to_datetime(df['date']).dt.date
        return df
    return pd.DataFrame()


def save_checkpoint2(df, var):
    df.to_csv(CKPT_DIR / f'_ckpt2_{var}.csv', index=False)


def done_week_keys(df):
    """Set of 'YYYY-WW' strings already extracted."""
    if df.empty:
        return set()
    return set(pd.to_datetime(df['date']).dt.to_period('W').astype(str))


# ─────────────────────────────────────────────────────────────────────────
# PART 2a — CHIRPS
# ─────────────────────────────────────────────────────────────────────────
log.info('\n=== CHIRPS daily precipitation ===')
chirps_df  = load_checkpoint2('chirps')
done_weeks = done_week_keys(chirps_df)
frames     = [chirps_df] if not chirps_df.empty else []
n_done = 0

for wi, ws in enumerate(week_starts):
    w_key = str(ws.to_period('W'))
    if w_key in done_weeks:
        n_done += 1
        continue

    week_frames = []
    for bi, batch in enumerate(cluster_batches):
        df = fetch_week_batch('UCSB-CHG/CHIRPS/DAILY', 'precipitation',
                              ws, batch, scale=5000, tag='[CHIRPS]')
        if not df.empty:
            week_frames.append(df)

    if week_frames:
        week_df = pd.concat(week_frames, ignore_index=True)
        week_df = week_df.rename(columns={'value': 'precipitation'})
        frames.append(week_df)
        all_chirps = pd.concat(frames, ignore_index=True)
        save_checkpoint2(all_chirps, 'chirps')
        done_weeks.add(w_key)
        n_done += 1
        rows_this_week = len(week_df)
        log.info(f'[CHIRPS] {ws.date()} ({n_done}/{len(week_starts)}) '
                 f'{rows_this_week:,} rows | total {len(all_chirps):,}')
    else:
        log.warning(f'[CHIRPS] {ws.date()}: 0 rows')
        n_done += 1

chirps_final = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
if not chirps_final.empty:
    chirps_final.to_csv(DATA_DIR / 'zw_daily_chirps.csv', index=False)
    log.info(f'CHIRPS saved: {len(chirps_final):,} rows')

# ─────────────────────────────────────────────────────────────────────────
# PART 2b — MODIS NDVI
# ─────────────────────────────────────────────────────────────────────────
log.info('\n=== MODIS NDVI ===')
ndvi_df    = load_checkpoint2('ndvi')
done_weeks = done_week_keys(ndvi_df)
frames     = [ndvi_df] if not ndvi_df.empty else []
n_done = 0

for wi, ws in enumerate(week_starts):
    w_key = str(ws.to_period('W'))
    if w_key in done_weeks:
        n_done += 1
        continue

    week_frames = []
    for bi, batch in enumerate(cluster_batches):
        # Try daily MOD09GA first
        df = fetch_week_batch('MODIS/MOD09GA_006_NDVI', 'NDVI',
                              ws, batch, scale=500, tag='[NDVI]')
        # Fallback to 16-day composite with ±8 day window
        if df.empty:
            w_ext_start = ws - pd.Timedelta(days=8)
            w_ext_end   = ws + pd.Timedelta(days=15)
            fc = make_fc(batch)
            coll16 = (ee.ImageCollection('MODIS/061/MOD13A1')
                        .select('NDVI')
                        .filterDate(w_ext_start.strftime('%Y-%m-%d'),
                                    w_ext_end.strftime('%Y-%m-%d')))
            for attempt in range(3):
                try:
                    raw16 = coll16.getRegion(fc, 500).getInfo()
                    break
                except Exception:
                    if attempt == 2: raw16 = None
                    time.sleep(2 ** attempt * 3)

            if raw16 and len(raw16) > 1:
                header16 = raw16[0]
                try:
                    bi16  = header16.index('NDVI')
                    ti16  = header16.index('time')
                    lai16 = header16.index('latitude')
                    loi16 = header16.index('longitude')
                    batch_arr = batch[['cluster_lat', 'cluster_lon']].values
                    btree = cKDTree(batch_arr)
                    recs16 = []
                    for r in raw16[1:]:
                        v, ts, lat, lon = r[bi16], r[ti16], r[lai16], r[loi16]
                        if v is None or ts is None: continue
                        _, ci = btree.query([lat, lon])
                        recs16.append({
                            'cluster_id': int(batch.iloc[ci]['cluster_id']),
                            'Latitude': float(batch.iloc[ci]['cluster_lat']),
                            'Longitude': float(batch.iloc[ci]['cluster_lon']),
                            'date': pd.Timestamp(ts, unit='ms').date(),
                            'value': float(v) * 1e-4,
                        })
                    if recs16:
                        df = pd.DataFrame(recs16)
                except Exception:
                    pass

        if not df.empty:
            week_frames.append(df)

    if week_frames:
        week_df = pd.concat(week_frames, ignore_index=True)
        week_df = week_df.rename(columns={'value': 'ndvi'})
        frames.append(week_df)
        all_ndvi = pd.concat(frames, ignore_index=True)
        save_checkpoint2(all_ndvi, 'ndvi')
        done_weeks.add(w_key)
        n_done += 1
        log.info(f'[NDVI]   {ws.date()} ({n_done}/{len(week_starts)}) '
                 f'{len(week_df):,} rows | total {len(all_ndvi):,}')
    else:
        log.warning(f'[NDVI] {ws.date()}: 0 rows')
        n_done += 1

ndvi_final = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
if not ndvi_final.empty:
    ndvi_final.to_csv(DATA_DIR / 'zw_daily_ndvi.csv', index=False)
    log.info(f'NDVI saved: {len(ndvi_final):,} rows')

# ─────────────────────────────────────────────────────────────────────────
# PART 3 — Merge
# ─────────────────────────────────────────────────────────────────────────
log.info('\n=== Merging into zw_daily_climate.csv ===')
merge_parts = []

# NC-derived variables (wide table, multiple columns at once)
if (DATA_DIR / 'zw_daily_nc.csv').exists():
    nc_merge = pd.read_csv(DATA_DIR / 'zw_daily_nc.csv', parse_dates=['date'])
    nc_cols = [c for _, c, _ in NC_VARS]
    merge_parts.append(
        nc_merge.set_index(['cluster_id', 'Latitude', 'Longitude', 'date'])[nc_cols]
    )

# EE-derived variables (one column each)
for fpath, vcol in [
    (DATA_DIR / 'zw_daily_chirps.csv', 'precipitation'),
    (DATA_DIR / 'zw_daily_ndvi.csv',   'ndvi'),
]:
    if fpath.exists():
        df = pd.read_csv(fpath, parse_dates=['date'])
        merge_parts.append(
            df.set_index(['cluster_id', 'Latitude', 'Longitude', 'date'])[[vcol]]
        )

if merge_parts:
    wide = merge_parts[0].join(merge_parts[1:], how='outer').reset_index()
    wide.to_csv(OUT_FILE, index=False)
    log.info(f'Merged: {len(wide):,} rows → zw_daily_climate.csv')
    for col in ['temp_c', 'dust_aod550', 'pm25', 'precipitation', 'ndvi']:
        if col in wide.columns:
            pct = wide[col].notna().mean() * 100
            log.info(f'  {col}: {wide[col].notna().sum():,} ({pct:.1f}% coverage)')

log.info('\n=== Done ===')
