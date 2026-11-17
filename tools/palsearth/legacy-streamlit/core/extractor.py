import os
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# Earth Engine .getInfo() calls are network round trips, not CPU work, so
# running several calendar days concurrently (one HTTP request in flight per
# worker) cuts wall-clock time roughly proportionally — this is the main
# lever for "why is a 12-participant case-crossover job slow" (a handful of
# control periods × a 15-day lag window per participant can mean 50-100+
# unique calendar days, each previously a fully sequential round trip).
MAX_DATE_WORKERS = 8

JOBS_DIR = '/home/USER/PRECISE/palsearth/jobs'

PERCENTILE_MAP = {'p50': 50, 'p75': 75, 'p95': 95, 'p99': 99}
BATCH = 50


def _friendly_filename(datasets_list, job_id, ext):
    """e.g. urban_settlement_ghsl_340fc31c.csv — descriptive, still unique."""
    import re
    slugs = [re.sub(r'[^a-z0-9]+', '_', d.lower()).strip('_') for d in datasets_list[:2]]
    slug = '_and_'.join(slugs) if slugs else 'extraction'
    if len(datasets_list) > 2:
        slug += f'_plus{len(datasets_list) - 2}'
    return f'{slug}_{job_id[:8]}.{ext}'


def _apply_value_transform(wide, ds_info, col_names):
    """
    Applies a documented raw-value back-transform (e.g. iSDAsoil's log-scale
    encoding, or a MODIS raw-integer scale factor) so the CSV holds the real
    physical value, not the source dataset's internal storage encoding.
    Verified against each dataset's Earth Engine catalog page — see
    core/datasets.py for the specific values and sources.
    """
    vt = ds_info.get('value_transform')
    if not vt:
        return
    for col in col_names:
        if col not in wide.columns:
            continue
        if vt['type'] == 'linear_scale':
            wide[col] = wide[col] * vt['factor']
        elif vt['type'] == 'exp_scale_minus1':
            scale = vt['scale']
            wide[col] = wide[col].apply(lambda v: (np.exp(v / scale) - 1) if pd.notna(v) else v)


def _dataset_stats(ds_info, stats_requested):
    """Same stat-filtering logic used at extraction time — needed again here
    to reconstruct the exact output column names for the metadata file."""
    if ds_info['type'] == 'static_mode':
        return ['mode']
    return [s for s in stats_requested if s in (ds_info.get('stats', []) + list(PERCENTILE_MAP.keys()) + ['mean', 'min', 'max', 'sum'])] or ['mean']


def _resolve_unit(ds_info, col_name):
    """Matches a generated output column (which may carry a _<stat> and/or
    _<window> suffix) back to its documented unit."""
    units = ds_info.get('units')
    if units is None:
        return None
    if isinstance(units, str):
        return units
    for base, unit in units.items():
        if col_name == base or col_name.startswith(base + '_'):
            return unit
    return None


def _apply_temp_conversion(df, requested_ds, stats_requested, window_keys=None):
    """
    Converts every output column documented as Kelvin (per core.datasets'
    own `units` metadata — the single source of truth, so this needs no
    dataset-specific code and stays correct if a new Kelvin dataset is added
    later) to Celsius in place. Returns the set of column names actually
    converted, so the metadata file can describe them correctly instead of
    still claiming "Kelvin".
    """
    converted = set()
    for ds_name, ds_info in requested_ds.items():
        if ds_info['type'] != 'timeseries':
            continue
        ds_stats = _dataset_stats(ds_info, stats_requested)
        window_list = window_keys if window_keys else [None]
        for wk in window_list:
            col_names = _col_names_windowed(ds_info, ds_stats, wk) if wk else _col_names(ds_info, ds_stats)
            for col in col_names:
                if col in df.columns and _resolve_unit(ds_info, col) == 'Kelvin':
                    df[col] = df[col] - 273.15
                    converted.add(col)
    return converted


def _metadata_lines(job, requested_ds, stats_requested, window_keys=None, converted_to_celsius=None):
    """
    Builds the metadata content as a flat list of lines — shared by both the
    .txt writer and the .pdf writer below, so the two formats can never
    drift out of sync with each other.
    """
    converted_to_celsius = converted_to_celsius or set()
    lines = [
        "PALs Earth: extraction metadata",
        f"Job ID: {job.get('id', '')}",
        f"Created: {job.get('created_at', '')}",
        "",
        "One entry per output column below: which dataset/band it came from, its",
        "unit, and any conversion already applied before the value was saved.",
        "",
    ]
    for ds_name, ds_info in requested_ds.items():
        ds_stats = _dataset_stats(ds_info, stats_requested)
        band_disp = ', '.join(ds_info['band']) if isinstance(ds_info['band'], list) else ds_info['band']
        lines.append(f"=== {ds_name} ===")
        lines.append(f"Earth Engine collection: {ds_info['collection']}  (band: {band_disp})")
        if ds_info.get('description'):
            lines.append(ds_info['description'])
        if ds_info.get('value_transform'):
            lines.append("Note: the source stores this as a scaled/encoded raw value, already "
                          "converted to the real unit below before saving; no further conversion needed.")
        if ds_info.get('fallback'):
            lines.append(f"Note: {ds_info['fallback']['note']}")
        if (ds_info['type'] == 'timeseries' and not window_keys
                and len(_dataset_stats(ds_info, stats_requested)) > 1
                and not ds_info.get('native_cadence')):
            lines.append(f"Note: this dataset's native revisit cadence is {ds_info.get('temporal', 'daily')}, "
                          "i.e. one real image per row here, so mean/min/max/p95 etc. are therefore all the "
                          "same number by definition (there is only one observation to summarise), not a "
                          "bug. To see min/max/percentile spread for this variable, request a multi-day "
                          "window instead of single-day rows.")
        lines.append("")

        window_list = window_keys if (window_keys and ds_info['type'] == 'timeseries') else [None]
        for wk in window_list:
            col_names = _col_names_windowed(ds_info, ds_stats, wk) if wk else _col_names(ds_info, ds_stats)
            for col in col_names:
                if col in converted_to_celsius:
                    unit = "°C (converted from Kelvin at export)"
                else:
                    unit = _resolve_unit(ds_info, col) or "unit not documented, check the source collection"
                lines.append(f"  {col}: {unit}")

        clf = ds_info.get('classify')
        if clf:
            labels = ', '.join(sorted({r[2] for r in clf['ranges']}))
            lines.append(f"  {clf['output_col']}: human-readable label ({labels}) derived from {clf['source_col']}")
        lines.append("")

    return lines


def _write_metadata_file(output_path, job, requested_ds, stats_requested, window_keys=None, converted_to_celsius=None):
    """
    Writes a companion <output>_metadata.txt (and, when fpdf2 is available,
    a matching <output>_metadata.pdf) next to the CSV/parquet — without it,
    a column like pm25_mean is just a number with no unit attached anywhere.
    Lists every output column's source dataset, Earth Engine collection,
    unit, and whether a raw-value transform was already applied before
    saving.
    """
    ext_idx = output_path.rfind('.')
    stem = output_path[:ext_idx] if ext_idx != -1 else output_path
    lines = _metadata_lines(job, requested_ds, stats_requested, window_keys, converted_to_celsius)

    try:
        with open(stem + '_metadata.txt', 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    except Exception as e:
        print(f"[extractor] Could not write metadata file: {e}")

    _write_metadata_pdf(stem + '_metadata.pdf', lines)


def _write_metadata_pdf(pdf_path, lines):
    """
    Same content as the .txt file, rendered as a PDF — easier to attach to
    an email or a report than a plain-text file. Soft dependency: if fpdf2
    isn't installed, this just logs and skips (the .txt file always exists
    regardless, so no job ever fails because a PDF couldn't be made).
    """
    try:
        from fpdf import FPDF
    except ImportError:
        print("[extractor] fpdf2 not installed — skipping metadata PDF (metadata.txt still written)")
        return
    try:
        pdf = FPDF(format='A4')
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font('Courier', size=9)
        for raw_line in lines:
            # The built-in Courier PDF font only covers a latin-1-ish
            # subset — an em dash or a unicode subscript (₂, °C's companion
            # glyphs, etc.) would otherwise raise and silently skip the
            # *entire* PDF. Sanitize every line uniformly, headers included,
            # rather than crashing the export over a label.
            line = raw_line.encode('latin-1', errors='replace').decode('latin-1')
            # fpdf2's multi_cell defaults to leaving the cursor at the
            # RIGHT margin afterward, not back at the left one — without
            # explicit new_x/new_y, the very next call sees ~0 width
            # available and raises. Pin it back to the left margin every time.
            if raw_line.startswith('=== '):
                pdf.set_font('Courier', 'B', 10)
                pdf.multi_cell(0, 5, line, new_x='LMARGIN', new_y='NEXT')
                pdf.set_font('Courier', size=9)
            elif raw_line.startswith('PALs Earth'):
                pdf.set_font('Courier', 'B', 13)
                pdf.multi_cell(0, 7, line, new_x='LMARGIN', new_y='NEXT')
                pdf.set_font('Courier', size=9)
            else:
                pdf.multi_cell(0, 5, line, new_x='LMARGIN', new_y='NEXT')
        pdf.output(pdf_path)
    except Exception as e:
        print(f"[extractor] Could not write metadata PDF: {e}")


def _detect_lat_lon(df):
    lat_candidates = ['latitude', 'lat', 'LAT', 'Latitude', 'LATITUDE', 'y', 'Y']
    lon_candidates = ['longitude', 'lon', 'LON', 'Longitude', 'LONGITUDE', 'x', 'X', 'lng', 'LNG']
    lat_col = next((c for c in lat_candidates if c in df.columns), None)
    lon_col = next((c for c in lon_candidates if c in df.columns), None)
    return lat_col, lon_col


def _build_reducer(stats):
    import ee
    reducer = None
    for stat in stats:
        if stat == 'mean':
            r = ee.Reducer.mean()
        elif stat == 'min':
            r = ee.Reducer.min()
        elif stat == 'max':
            r = ee.Reducer.max()
        elif stat == 'sum':
            r = ee.Reducer.sum()
        elif stat in PERCENTILE_MAP:
            r = ee.Reducer.percentile([PERCENTILE_MAP[stat]], outputNames=[stat])
        else:
            continue
        reducer = r if reducer is None else reducer.combine(r, sharedInputs=True)
    return reducer


def _dedup_by_location(locs_df):
    """
    Collapses rows sharing the exact same (_lat, _lon) coordinate into one
    query row per unique site — so Earth Engine gets asked once per site
    instead of once per participant/row AT that site. This is the real
    cost multiplier for a cohort with many participants sharing a
    location (e.g. a village-based cohort, where hundreds of participants
    can share the same site coordinate): without this, every one of them
    triggers its own redundant reduceRegion call for an identical point.
    Coordinates are compared to 6 decimal places (~0.1m) — exact-match
    only, never merges two genuinely different nearby points.

    Returns (unique_rows, loc_idx_groups): unique_rows has one row per
    distinct coordinate (whichever row's _loc_idx was seen first for it);
    loc_idx_groups maps that representative _loc_idx to the full list of
    every original _loc_idx sharing that coordinate, for
    _expand_dedup_results to broadcast the single fetched value back out
    to all of them afterward.
    """
    if locs_df.empty:
        return locs_df, {}
    keys = list(zip(locs_df['_lat'].round(6), locs_df['_lon'].round(6)))
    groups = {}
    first_seen = {}
    for key, idx in zip(keys, locs_df['_loc_idx']):
        groups.setdefault(key, []).append(idx)
        first_seen.setdefault(key, idx)
    loc_idx_groups = {first_seen[key]: idxs for key, idxs in groups.items()}
    unique_rows = locs_df[locs_df['_loc_idx'].isin(set(first_seen.values()))]
    return unique_rows, loc_idx_groups


def _expand_dedup_results(results, loc_idx_groups):
    """Broadcasts each unique-site result (keyed by the representative
    _loc_idx _dedup_by_location chose) back to every original _loc_idx
    that shared that coordinate. A no-op when loc_idx_groups is empty
    (the dedup found nothing to collapse, or wasn't applicable)."""
    if not loc_idx_groups:
        return results
    expanded = {}
    for rep_idx, props in results.items():
        for idx in loc_idx_groups.get(rep_idx, [rep_idx]):
            expanded[idx] = props
    return expanded


def _extract_points(ee_obj, locs_df, reducer, scale, point_buffer_m=0):
    """
    Extract values for points. locs_df must have _lat, _lon, _loc_idx columns.

    point_buffer_m: normally 0 (a bare point). Non-zero only for datasets
    rasterized from sparse point data (static_points_fc, via reduceToImage —
    see _build_static_image) — a bare point can land in a masked gap between
    rasterized pixels even in a well-covered area, since the sparse source
    points aren't necessarily aligned to reduceToImage's output pixel grid.
    Buffering the query by half the native tile spacing reliably catches the
    nearest rasterized pixel without being wide enough to blend in a second,
    more distant tile.
    """
    import ee
    # Blank / non-numeric / out-of-range coordinates cannot become an Earth
    # Engine point and would fail the whole batch. Leave those rows out of the
    # request; they keep empty values in the output (results are written back
    # by _loc_idx, and rows that never get a result are simply never filled).
    _lat = pd.to_numeric(locs_df['_lat'], errors='coerce')
    _lon = pd.to_numeric(locs_df['_lon'], errors='coerce')
    valid_coords = _lat.between(-90, 90) & _lon.between(-180, 180)   # NaN -> False
    locs_df = locs_df[valid_coords]
    if locs_df.empty:
        return {}
    def _geom(lon, lat):
        pt = ee.Geometry.Point([lon, lat])
        return pt.buffer(point_buffer_m) if point_buffer_m else pt
    features = [
        ee.Feature(_geom(float(r['_lon']), float(r['_lat'])), {'_idx': int(r['_loc_idx'])})
        for _, r in locs_df.iterrows()
    ]
    result = ee.FeatureCollection(features).map(
        lambda f: f.set(ee_obj.reduceRegion(reducer=reducer, geometry=f.geometry(), scale=scale, bestEffort=True))
    )
    info = result.getInfo()
    out = {}
    for feat in info.get('features', []):
        props = feat.get('properties', {})
        idx = props.pop('_idx', None)
        if idx is not None:
            out[idx] = props
    return out


def _ndvi_from_mod09ga061(start_str, end_str):
    """
    Cloud-masked NDVI computed from MODIS/061/MOD09GA — the actively
    maintained successor collection — used as the automatic fallback for
    "NDVI (MODIS)" once its primary source (the community-curated
    MOD09GA_006_NDVI asset) runs out of data (frozen since 2023-02-17,
    discovered 2026-10-14: NASA retired the underlying 006 collection and
    nobody updated the derived asset to track 061). See that dataset's
    "fallback" entry in core/datasets.py.

    MOD09GA is raw surface reflectance with NO cloud masking applied —
    computing (NIR-Red)/(NIR+Red) directly from sur_refl_b02/b01 without
    masking first picks up cloud-contaminated pixels (verified live: some
    raw samples had both bands near-saturated and nearly equal, the
    classic cloud/haze signature, giving a spuriously flat near-zero
    NDVI). The scale factor (0.0001, confirmed against the official
    MOD09GA.061 catalog page) cancels out of the ratio so it's not applied
    separately; masking is still required.

    state_1km QA bit layout (confirmed against the official MOD09GA.061
    catalog page, 2026-10-14 — do not re-derive from memory, GEE's own
    catalog page is the source of truth):
      bits 0-1: cloud state (0=clear, 1=cloudy, 2=mixed, 3=not set/assumed
                clear) — only 0 and 3 are kept
      bit 2:    cloud shadow (1=yes) — must be 0
      bit 10:   internal cloud algorithm flag (1=cloud) — must be 0
    """
    import ee

    def _one(img):
        qa = img.select('state_1km')
        cloud_state = qa.bitwiseAnd(3)
        clear = cloud_state.eq(0).Or(cloud_state.eq(3))
        no_shadow = qa.bitwiseAnd(1 << 2).eq(0)
        no_internal_cloud = qa.bitwiseAnd(1 << 10).eq(0)
        mask = clear.And(no_shadow).And(no_internal_cloud)
        red = img.select('sur_refl_b01')
        nir = img.select('sur_refl_b02')
        ndvi = nir.subtract(red).divide(nir.add(red)).rename('NDVI')
        return ndvi.updateMask(mask)

    return ee.ImageCollection('MODIS/061/MOD09GA').filterDate(start_str, end_str).map(_one)


def _resolve_source_collection(ds_info, coll_id, bands, start_str, end_str):
    """
    Returns the ImageCollection _temporal_reduce should reduce over: the
    dataset's normal collection, unless it declares a "fallback" (a
    frozen/discontinued primary source — see core/datasets.py) AND the
    requested range reaches past the fallback's cutover date, in which
    case the fallback's own collection is used for the WHOLE call instead.

    A window straddling the cutover uses the fallback for its entire span
    rather than splitting the two sources within one window — slightly
    conservative right at the boundary (a few fallback-sourced days could
    have been served by the primary source too), but guarantees no silent
    empty result for the part that the primary source can no longer cover.
    """
    import ee
    fb = ds_info.get('fallback') if ds_info else None
    if fb and end_str > fb['after']:
        if fb['collection'] == 'MODIS/061/MOD09GA':
            return _ndvi_from_mod09ga061(start_str, end_str)
        raise ValueError(f"No fallback implementation registered for {fb['collection']}")
    return ee.ImageCollection(coll_id).filterDate(start_str, end_str).select(bands)


def _temporal_reduce(coll_id, bands, band_rename, start_str, end_str, reducer, multi_stat, ds_info=None):
    """
    Collapses an ImageCollection across the time axis into the single image
    that gets spatially sampled at each point/polygon.

    When more than one stat was requested (mean + min + max + p95, etc.),
    those stats describe the spread across the native images in
    [start_str, end_str) — e.g. the ~24 hourly ERA5-Land/MERRA-2 images in
    one calendar day, or the several daily/16-day images inside a
    multi-day window — so the combined reducer must run on the TIME axis
    first (ImageCollection.reduce(), producing one band per stat), leaving
    only a trivial single-value spatial pull for the point/polygon query.

    Previously the combined reducer was applied at the spatial step, on an
    image already collapsed to a plain temporal mean — for a bare point
    (one pixel) every stat (mean/min/max/p95/p99) necessarily returned that
    same number, silently discarding all the real within-period
    variability the extra stats were supposed to show. This is why e.g.
    ERA5-Land temperature/humidity stats for a single day came back
    identical: see Malou's report, 2026-10-13.

    For a single-stat request ("mean" only — e.g. Wind, most Air Quality
    datasets), behaviour is unchanged.

    ds_info: optional — passed through to _resolve_source_collection so a
    dataset declaring a "fallback" source gets it transparently swapped in
    for date ranges its primary source can no longer cover.

    Returns (ee_img, point_reducer) — point_reducer is what the caller
    should pass to _extract_points/_extract_polygons.
    """
    import ee
    coll = _resolve_source_collection(ds_info, coll_id, bands, start_str, end_str)
    if band_rename:
        coll = coll.map(lambda img: img.rename(band_rename))
    if multi_stat:
        return coll.reduce(reducer), ee.Reducer.first()
    return coll.mean(), reducer


def _fetch_timeseries_day(coll_id, bands, band_rename, date_str, day_rows, reducer, scale, ds_stats=None,
                           ds_info=None):
    """
    One Earth Engine round trip for one calendar day, batched across every
    row that needs that day. Pure function of its arguments — touches no
    shared state — so it's safe to run concurrently across dates in a
    thread pool (see MAX_DATE_WORKERS); callers apply the returned values
    to their dataframe sequentially afterwards.
    """
    import ee
    next_str = (pd.Timestamp(date_str) + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    try:
        unique_rows, loc_idx_groups = _dedup_by_location(day_rows)
        multi_stat = bool(ds_stats) and len(ds_stats) > 1
        ee_img, point_reducer = _temporal_reduce(coll_id, bands, band_rename, date_str, next_str,
                                                   reducer, multi_stat, ds_info=ds_info)
        if ee_img.bandNames().size().getInfo() == 0:
            return {}
        results = {}
        for batch_start in range(0, len(unique_rows), BATCH):
            chunk = unique_rows.iloc[batch_start:batch_start + BATCH]
            try:
                results.update(_extract_points(ee_img, chunk, point_reducer, scale))
            except Exception as e:
                print(f"[extractor] Day batch error {coll_id} {date_str}: {e}")
        return _expand_dedup_results(results, loc_idx_groups)
    except Exception as e:
        print(f"[extractor] Day error {coll_id} {date_str}: {e}")
        return {}


def _fetch_timeseries_day_mixed(coll_id, bands, band_rename, date_str, locs_or_gdf, is_points, reducer, scale,
                                 ds_stats=None, ds_info=None):
    """
    Like _fetch_timeseries_day, but for run_extraction's shared-date-range
    path, which (unlike the per-row long/windowed paths) can also extract
    polygons, not just points. Pure function of its arguments — safe to run
    concurrently across dates in a thread pool.
    """
    import ee
    next_str = (pd.Timestamp(date_str) + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    try:
        if is_points:
            unique_rows, loc_idx_groups = _dedup_by_location(locs_or_gdf)
        else:
            unique_rows, loc_idx_groups = locs_or_gdf, {}  # polygons: no coordinate to dedup on
        multi_stat = bool(ds_stats) and len(ds_stats) > 1
        ee_img, point_reducer = _temporal_reduce(coll_id, bands, band_rename, date_str, next_str,
                                                   reducer, multi_stat, ds_info=ds_info)
        if ee_img.bandNames().size().getInfo() == 0:
            return {}
        results = {}
        n = len(unique_rows)
        for batch_start in range(0, n, BATCH):
            chunk = unique_rows.iloc[batch_start:batch_start + BATCH]
            try:
                if is_points:
                    results.update(_extract_points(ee_img, chunk, point_reducer, scale))
                else:
                    results.update(_extract_polygons(ee_img, chunk, point_reducer, scale))
            except Exception as e:
                print(f"[extractor] Day batch error {coll_id} {date_str}: {e}")
        return _expand_dedup_results(results, loc_idx_groups)
    except Exception as e:
        print(f"[extractor] Day error {coll_id} {date_str}: {e}")
        return {}


def _extract_polygons(ee_obj, gdf_batch, reducer, scale):
    """Extract values for polygons."""
    import ee
    features = [
        ee.Feature(ee.Geometry(row.geometry.__geo_interface__), {'_idx': int(i)})
        for i, row in gdf_batch.iterrows()
    ]
    result = ee.FeatureCollection(features).map(
        lambda f: f.set(ee_obj.reduceRegion(reducer=reducer, geometry=f.geometry(), scale=scale, bestEffort=True))
    )
    info = result.getInfo()
    out = {}
    for feat in info.get('features', []):
        props = feat.get('properties', {})
        idx = props.pop('_idx', None)
        if idx is not None:
            out[idx] = props
    return out


def _build_static_image(ds_info):
    """
    Returns the ee.Image to reduceRegion over for a static dataset, given its
    catalog `type`. `static_points_fc` covers an Earth Engine asset that's
    actually a FeatureCollection of points carrying a numeric property per
    point (e.g. Facebook/Meta's Relative Wealth Index) rather than any kind
    of raster — loading it as ee.Image/ImageCollection throws every time
    ("Expected asset ... to be an ImageCollection, found 'EECollection'"),
    which was being silently swallowed by the per-batch try/except and just
    left the column blank. Rasterizing via reduceToImage at the dataset's
    documented native scale fixes this without changing anything else in
    the extraction path below (it's still just an ee.Image afterward).
    """
    import ee
    coll_id = ds_info['collection']
    band = ds_info['band']
    if ds_info['type'] == 'static_points_fc':
        props = band if isinstance(band, list) else [band]
        # reduceToImage names its output band after the reducer ("mean"),
        # not the source property ("rwi") — rename back so the rest of the
        # pipeline (which expects the band to match ds_info['band']) works
        # unmodified, same as every other dataset type.
        return ee.FeatureCollection(coll_id).reduceToImage(props, ee.Reducer.mean()).rename(props)
    if ds_info['type'] == 'static_fc':
        return ee.ImageCollection(coll_id).mosaic().select(band)
    return ee.Image(coll_id).select(band)


def _effective_bands(ds_info):
    """Return the effective (possibly renamed) band list for a dataset."""
    return ds_info.get('band_rename', ds_info['band'])


def _col_names(ds_info, ds_stats):
    """Return list of output column names for a dataset."""
    band = _effective_bands(ds_info)
    out_col = ds_info['output_col']
    cols = []
    if isinstance(band, list):
        for b in band:
            for st in ds_stats:
                cols.append(f"{b}_{st}")
    else:
        for st in ds_stats:
            cols.append(f"{out_col}_{st}" if len(ds_stats) > 1 else out_col)
    return cols


def _parse_vals(props, ds_info, ds_stats):
    """Parse EE result properties into a dict of {col_name: value}.

    EE reduceRegion key format:
      - Single reducer:   {band: value}          e.g. {'dem': 42.5}
      - Combined reducer: {band_stat: value}      e.g. {'dem_mean': 42.5, 'dem_min': 40.0}
      - Percentile:       {band_pNN: value}       e.g. {'dem_p50': 41.0}
    """
    band = _effective_bands(ds_info)
    out_col = ds_info['output_col']
    multi = len(ds_stats) > 1
    result = {}
    if isinstance(band, list):
        for b in band:
            for st in ds_stats:
                ee_key = f"{b}_{st}" if multi else b
                result[f"{b}_{st}"] = props.get(ee_key) if props else None
    else:
        for st in ds_stats:
            col = f"{out_col}_{st}" if multi else out_col
            ee_key = f"{band}_{st}" if multi else band
            result[col] = props.get(ee_key) if props else None
    return result


def _col_names_windowed(ds_info, ds_stats, window_key):
    band = _effective_bands(ds_info)
    out_col = ds_info['output_col']
    cols = []
    if isinstance(band, list):
        for b in band:
            for st in ds_stats:
                cols.append(f"{b}_{st}_{window_key}")
    else:
        for st in ds_stats:
            col = f"{out_col}_{st}" if len(ds_stats) > 1 else out_col
            cols.append(f"{col}_{window_key}")
    return cols


def _parse_vals_windowed(props, ds_info, ds_stats, window_key):
    band = _effective_bands(ds_info)
    out_col = ds_info['output_col']
    multi = len(ds_stats) > 1
    result = {}
    if isinstance(band, list):
        for b in band:
            for st in ds_stats:
                ee_key = f"{b}_{st}" if multi else b
                result[f"{b}_{st}_{window_key}"] = props.get(ee_key) if props else None
    else:
        for st in ds_stats:
            ee_key = f"{band}_{st}" if multi else band
            col = f"{out_col}_{st}" if multi else out_col
            result[f"{col}_{window_key}"] = props.get(ee_key) if props else None
    return result


def _resolve_windowed_anchors(job, df):
    """
    Shared by run_windowed_extraction (aggregated/wide) — figures out, from
    job['date_mode'], which per-row anchor date(s) and window list apply, and
    returns (window_keys, win_dates_fn) where win_dates_fn(row, window_key)
    -> (start, end) or None. Raises ValueError with a user-facing message on
    missing/invalid configuration. Mutates df in place to add _anchor /
    _delivery helper columns.
    """
    import pandas as pd
    from core.windows import compute_window_dates, compute_custom_window_dates

    date_mode = job.get('date_mode', 'pregnancy_windows')
    ref_date_cols = job.get('ref_date_cols')
    ref_date_cols = json.loads(ref_date_cols) if isinstance(ref_date_cols, str) else (ref_date_cols or {})
    custom_window = job.get('custom_window')
    custom_window = json.loads(custom_window) if isinstance(custom_window, str) else (custom_window or {})

    if date_mode == 'pregnancy_windows':
        window_keys = job.get('window_keys')
        window_keys = json.loads(window_keys) if isinstance(window_keys, str) else (window_keys or [])
        conception_col = ref_date_cols.get('conception')
        delivery_col = ref_date_cols.get('delivery')
        if not window_keys:
            raise ValueError("No windows selected.")
        if not conception_col:
            raise ValueError("A conception-date column is required for pregnancy-window extraction.")
        if conception_col not in df.columns:
            raise ValueError(f"Reference date column '{conception_col}' not found in the uploaded file.")
        df['_anchor'] = pd.to_datetime(df[conception_col], errors='coerce')
        df['_delivery'] = pd.to_datetime(df[delivery_col], errors='coerce') if (delivery_col and delivery_col in df.columns) else pd.NaT

        def _win_dates(row, win_key):
            return compute_window_dates(row['_anchor'], row['_delivery'], win_key)

        return window_keys, _win_dates

    if date_mode == 'index_window':
        index_col = ref_date_cols.get('index')
        if not index_col:
            raise ValueError("ref_date_cols.index is required for index-window extraction.")
        if index_col not in df.columns:
            raise ValueError(f"Reference date column '{index_col}' not found in the uploaded file.")
        if 'day_from' not in custom_window or 'day_to' not in custom_window:
            raise ValueError("A day_from/day_to lag span is required for index-window extraction.")
        df['_anchor'] = pd.to_datetime(df[index_col], errors='coerce')
        df['_delivery'] = pd.NaT

        def _win_dates(row, win_key):
            return compute_custom_window_dates(row['_anchor'], custom_window['day_from'], custom_window['day_to'])

        return ['custom'], _win_dates

    raise ValueError(
        f"date_mode={date_mode!r} needs the daily/long output shape "
        "(run_long_extraction), not the aggregated window mean."
    )


def run_windowed_extraction(job, update_progress_fn=None):
    """
    Per-participant window extraction, aggregated to one summary value per
    window (see core.windows). Each row gets its own [start, end] per
    selected window, derived from its own anchor date(s) — not one shared
    calendar range for everyone. Output is one row per input location, with
    dataset columns suffixed by window key (e.g. temp_2m_k_mean_T1).

    Supports date_mode 'pregnancy_windows' (named trimester-relative windows)
    and 'index_window' (a single custom day_from/day_to lag span around any
    per-row reference date). For the unaggregated daily/"long" panel a DLNM
    or case-crossover analysis needs instead, see run_long_extraction.
    """
    import ee
    import pandas as pd

    job_id = job['id']
    datasets_list = json.loads(job['datasets']) if isinstance(job['datasets'], str) else job['datasets']
    stats_requested = json.loads(job['stats_requested']) if isinstance(job['stats_requested'], str) else job['stats_requested']
    input_filename = job['input_filename']
    output_format = job.get('output_format', 'csv')

    output_ext = 'csv' if output_format == 'csv' else 'parquet'
    output_path = os.path.join(JOBS_DIR, _friendly_filename(datasets_list, job_id, output_ext))

    SA_KEY = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'palsearth-sa-key.json')
    try:
        creds = ee.ServiceAccountCredentials('ee-service-account@your-project.iam.gserviceaccount.com', SA_KEY)
        ee.Initialize(credentials=creds, project='your-earth-engine-project')
    except Exception as e:
        raise RuntimeError(f"Earth Engine initialisation failed: {e}")

    if update_progress_fn:
        update_progress_fn(2, status='running')

    if not input_filename.endswith('.csv'):
        raise ValueError("Windowed extraction currently supports CSV point uploads only.")

    df = pd.read_csv(input_filename)
    lat_col, lon_col = _detect_lat_lon(df)
    if not lat_col or not lon_col:
        raise ValueError("Could not detect latitude/longitude columns.")

    locs = df.copy()
    locs['_loc_idx'] = range(len(locs))
    locs['_lon'] = df[lon_col]
    locs['_lat'] = df[lat_col]

    window_keys, _win_dates = _resolve_windowed_anchors(job, locs)

    n_locs = len(locs)

    from core.datasets import DATASETS
    custom_ds = job.get('custom_datasets')
    custom_ds = json.loads(custom_ds) if isinstance(custom_ds, str) and custom_ds else (custom_ds or {})
    all_datasets = {**DATASETS, **custom_ds}
    requested_ds = {name: all_datasets[name] for name in datasets_list if name in all_datasets}
    timeseries_ds = {n: d for n, d in requested_ds.items() if d['type'] == 'timeseries'}
    static_ds = {n: d for n, d in requested_ds.items() if d['type'] in ('static', 'static_fc', 'static_mode', 'static_points_fc')}

    wide = locs[[c for c in locs.columns if not c.startswith('_')]].copy()
    wide['_loc_idx'] = locs['_loc_idx']

    total_steps = max(1, len(timeseries_ds) * len(window_keys) + len(static_ds))
    step = 0

    # ── Timeseries datasets, aggregated per window per participant ─────────
    for win_key in window_keys:
        win_dates = {}
        for _, r in locs.iterrows():
            wd = _win_dates(r, win_key)
            if wd:
                win_dates[r['_loc_idx']] = wd

        for ds_name, ds_info in timeseries_ds.items():
            coll_id = ds_info['collection']
            band = ds_info['band']
            scale = ds_info['scale']
            ds_stats = [s for s in stats_requested if s in (ds_info.get('stats', []) + list(PERCENTILE_MAP.keys()) + ['mean', 'min', 'max', 'sum'])] or ['mean']
            reducer = _build_reducer(ds_stats)
            col_names = _col_names_windowed(ds_info, ds_stats, win_key)
            for col in col_names:
                wide[col] = np.nan

            # Group locations sharing an identical [start, end] so they batch together.
            groups = {}
            for loc_idx, (start, end) in win_dates.items():
                key = (start.strftime('%Y-%m-%d'), (end + pd.Timedelta(days=1)).strftime('%Y-%m-%d'))
                groups.setdefault(key, []).append(loc_idx)

            for (start_str, end_str), loc_idxs in groups.items():
                try:
                    bands = band if isinstance(band, list) else [band]
                    # Always a multi-day span here (a window), so even a
                    # daily/16-day-cadence dataset has several native images
                    # to reduce across — unlike the single-day long-format
                    # path, this always has real variability for multi-stat
                    # requests to capture. See _temporal_reduce's docstring.
                    ee_img, point_reducer = _temporal_reduce(coll_id, bands, ds_info.get('band_rename'),
                                                              start_str, end_str, reducer, len(ds_stats) > 1,
                                                              ds_info=ds_info)
                    n_bands = ee_img.bandNames().size().getInfo()
                    if n_bands == 0:
                        continue
                    batch_locs = locs[locs['_loc_idx'].isin(loc_idxs)]
                    unique_locs, loc_idx_groups = _dedup_by_location(batch_locs)
                    for batch_start in range(0, len(unique_locs), BATCH):
                        chunk = unique_locs.iloc[batch_start:batch_start + BATCH]
                        try:
                            vals = _expand_dedup_results(
                                _extract_points(ee_img, chunk, point_reducer, scale), loc_idx_groups)
                            for loc_idx, props in vals.items():
                                parsed = _parse_vals_windowed(props, ds_info, ds_stats, win_key)
                                row_mask = wide['_loc_idx'] == loc_idx
                                for col, val in parsed.items():
                                    wide.loc[row_mask, col] = val
                        except Exception as e:
                            print(f"[extractor] Windowed batch error {ds_name} {win_key} {start_str}: {e}")
                except Exception as e:
                    print(f"[extractor] Windowed error {ds_name} {win_key} {start_str}: {e}")

            _apply_value_transform(wide, ds_info, col_names)
            step += 1
            if update_progress_fn:
                update_progress_fn(min(90, int((step / total_steps) * 85) + 2))

    # ── Static datasets — one value per location, not window-dependent ─────
    for ds_name, ds_info in static_ds.items():
        coll_id = ds_info['collection']
        band = ds_info['band']
        scale = ds_info['scale']
        if ds_info['type'] == 'static_mode':
            ds_stats = ['mode']
            reducer = ee.Reducer.mode()
        else:
            ds_stats = [s for s in stats_requested if s in (ds_info.get('stats', []) + list(PERCENTILE_MAP.keys()) + ['mean', 'min', 'max', 'sum'])] or ['mean']
            reducer = _build_reducer(ds_stats)
        col_names = _col_names(ds_info, ds_stats)
        for col in col_names:
            wide[col] = np.nan
        try:
            ee_img = _build_static_image(ds_info)
            point_buffer_m = scale / 2 if ds_info['type'] == 'static_points_fc' else 0
            unique_locs, loc_idx_groups = _dedup_by_location(locs)
            for batch_start in range(0, len(unique_locs), BATCH):
                chunk = unique_locs.iloc[batch_start:batch_start + BATCH]
                try:
                    vals = _expand_dedup_results(
                        _extract_points(ee_img, chunk, reducer, scale, point_buffer_m=point_buffer_m),
                        loc_idx_groups)
                    for loc_idx, props in vals.items():
                        parsed = _parse_vals(props, ds_info, ds_stats)
                        row_mask = wide['_loc_idx'] == loc_idx
                        for col, val in parsed.items():
                            wide.loc[row_mask, col] = val
                except Exception as e:
                    print(f"[extractor] Static batch error {ds_name}: {e}")
        except Exception as e:
            print(f"[extractor] Static error {ds_name}: {e}")
        _apply_value_transform(wide, ds_info, col_names)
        step += 1
        if update_progress_fn:
            update_progress_fn(min(95, int((step / total_steps) * 85) + 2))

    for ds_name, ds_info in static_ds.items():
        clf = ds_info.get('classify')
        if not clf:
            continue
        src = clf['source_col']
        out = clf['output_col']
        if src not in wide.columns:
            continue
        def _apply_clf(val, ranges=clf['ranges'], default=clf.get('default')):
            if pd.isna(val):
                return None
            code = int(val)
            for lo, hi, label in ranges:
                if lo <= code <= hi:
                    return label
            return default
        wide[out] = wide[src].apply(_apply_clf)

    wide = wide.drop(columns=['_loc_idx'], errors='ignore')

    if wide.empty:
        raise ValueError("No data was extracted.")

    converted_cols = (_apply_temp_conversion(wide, requested_ds, stats_requested, window_keys=window_keys)
                      if job.get('temp_unit') == 'celsius' else set())

    if output_format == 'parquet':
        wide.to_parquet(output_path, index=False)
    else:
        wide.to_csv(output_path, index=False)

    _write_metadata_file(output_path, job, requested_ds, stats_requested, window_keys=window_keys,
                         converted_to_celsius=converted_cols)

    if update_progress_fn:
        update_progress_fn(100, status='complete')

    return output_path


def run_long_extraction(job, update_progress_fn=None):
    """
    Unaggregated daily "long" panel: one row per (input row, day within its
    window) rather than one row per window mean. Each row carries a
    `Day_Offset` column — signed days relative to that row's own anchor date
    — so the output drops straight into a DLNM cross-basis or a case-crossover
    conditional-logistic-regression dataset without further reshaping. This
    mirrors the day_offset design in Phase3_Exposure_Linkage.ipynb.

    Supports date_mode:
      - 'pregnancy_windows' : daily series per selected named window
      - 'index_window'      : daily series for one custom day_from/day_to
                               lag span around a single per-row index date
      - 'case_crossover'    : daily series for the case period AND a set of
                               auto-generated time-stratified referent
                               periods per row (same weekday, same calendar
                               month) — `Role` distinguishes case/control
    """
    import ee
    import pandas as pd
    import numpy as np
    from core.windows import (
        compute_window_dates, compute_custom_window_dates,
        build_case_crossover_referents,
    )

    job_id = job['id']
    datasets_list = json.loads(job['datasets']) if isinstance(job['datasets'], str) else job['datasets']
    stats_requested = json.loads(job['stats_requested']) if isinstance(job['stats_requested'], str) else job['stats_requested']
    input_filename = job['input_filename']
    output_format = job.get('output_format', 'csv')
    date_mode = job.get('date_mode')
    ref_date_cols = job.get('ref_date_cols')
    ref_date_cols = json.loads(ref_date_cols) if isinstance(ref_date_cols, str) else (ref_date_cols or {})
    custom_window = job.get('custom_window')
    custom_window = json.loads(custom_window) if isinstance(custom_window, str) else (custom_window or {})
    window_keys = job.get('window_keys')
    window_keys = json.loads(window_keys) if isinstance(window_keys, str) else (window_keys or [])

    output_ext = 'csv' if output_format == 'csv' else 'parquet'
    output_path = os.path.join(JOBS_DIR, _friendly_filename(datasets_list, job_id, output_ext))

    SA_KEY = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'palsearth-sa-key.json')
    try:
        creds = ee.ServiceAccountCredentials('ee-service-account@your-project.iam.gserviceaccount.com', SA_KEY)
        ee.Initialize(credentials=creds, project='your-earth-engine-project')
    except Exception as e:
        raise RuntimeError(f"Earth Engine initialisation failed: {e}")

    if update_progress_fn:
        update_progress_fn(2, status='running')

    if not input_filename.endswith('.csv'):
        raise ValueError("Long/daily extraction currently supports CSV point uploads only.")

    df = pd.read_csv(input_filename)
    lat_col, lon_col = _detect_lat_lon(df)
    if not lat_col or not lon_col:
        raise ValueError("Could not detect latitude/longitude columns.")

    df = df.copy()
    df['_loc_idx'] = range(len(df))
    df['_lon'] = df[lon_col]
    df['_lat'] = df[lat_col]

    # ── Build one (loc, period-label, start, end, anchor) row per period ───
    periods = []
    if date_mode == 'pregnancy_windows':
        conception_col = ref_date_cols.get('conception')
        delivery_col = ref_date_cols.get('delivery')
        if not window_keys:
            raise ValueError("No windows selected.")
        if not conception_col or conception_col not in df.columns:
            raise ValueError("A valid conception-date column is required for pregnancy-window extraction.")
        conception = pd.to_datetime(df[conception_col], errors='coerce')
        delivery = (pd.to_datetime(df[delivery_col], errors='coerce')
                    if (delivery_col and delivery_col in df.columns) else pd.Series(pd.NaT, index=df.index))
        for idx in df.index:
            for wk in window_keys:
                wd = compute_window_dates(conception[idx], delivery[idx], wk)
                if wd:
                    periods.append({'_loc_idx': df.at[idx, '_loc_idx'], '_period': wk,
                                     '_anchor': conception[idx], '_start': wd[0], '_end': wd[1]})

    elif date_mode == 'index_window':
        index_col = ref_date_cols.get('index')
        if not index_col or index_col not in df.columns:
            raise ValueError("A valid ref_date_cols.index column is required.")
        if 'day_from' not in custom_window or 'day_to' not in custom_window:
            raise ValueError("A day_from/day_to lag span is required.")
        index_dates = pd.to_datetime(df[index_col], errors='coerce')
        for idx in df.index:
            wd = compute_custom_window_dates(index_dates[idx], custom_window['day_from'], custom_window['day_to'])
            if wd:
                periods.append({'_loc_idx': df.at[idx, '_loc_idx'], '_period': 'custom',
                                 '_anchor': index_dates[idx], '_start': wd[0], '_end': wd[1]})

    elif date_mode == 'case_crossover':
        index_col = ref_date_cols.get('index')
        if not index_col or index_col not in df.columns:
            raise ValueError("A valid ref_date_cols.index column (the case/event date) is required for case-crossover.")
        if 'day_from' not in custom_window or 'day_to' not in custom_window:
            raise ValueError("A day_from/day_to lag span (applied around each case and referent day) is required.")
        index_dates = pd.to_datetime(df[index_col], errors='coerce')
        for idx in df.index:
            case_date = index_dates[idx]
            wd = compute_custom_window_dates(case_date, custom_window['day_from'], custom_window['day_to'])
            if wd:
                periods.append({'_loc_idx': df.at[idx, '_loc_idx'], '_period': 'case',
                                 '_anchor': case_date, '_start': wd[0], '_end': wd[1]})
            for ri, ref_date in enumerate(build_case_crossover_referents(case_date), start=1):
                wdr = compute_custom_window_dates(ref_date, custom_window['day_from'], custom_window['day_to'])
                if wdr:
                    periods.append({'_loc_idx': df.at[idx, '_loc_idx'], '_period': f'control_{ri}',
                                     '_anchor': ref_date, '_start': wdr[0], '_end': wdr[1]})
    else:
        raise ValueError(f"run_long_extraction does not support date_mode={date_mode!r}.")

    if not periods:
        raise ValueError("No valid date windows could be computed from the uploaded reference date column(s). "
                          "Check they parse as dates and aren't all blank.")

    periods_df = pd.DataFrame(periods)
    periods_df['_start'] = pd.to_datetime(periods_df['_start'])
    periods_df['_end'] = pd.to_datetime(periods_df['_end'])
    periods_df['_anchor'] = pd.to_datetime(periods_df['_anchor'])

    # ── Vectorised expansion to one row per day (HE2AT Phase2-style) ────────
    lengths = (periods_df['_end'] - periods_df['_start']).dt.days.values + 1
    offsets = np.concatenate([np.arange(n) for n in lengths]) if len(lengths) else np.array([], dtype=int)
    rep_idx = np.repeat(periods_df.index.values, lengths)
    exp_dates = np.repeat(periods_df['_start'].values, lengths) + pd.to_timedelta(offsets, unit='D')

    long_rows = periods_df.loc[rep_idx].reset_index(drop=True)
    long_rows['_date'] = pd.to_datetime(exp_dates)
    long_rows['_day_offset'] = (long_rows['_date'] - long_rows['_anchor']).dt.days
    long_rows['_row_idx'] = range(len(long_rows))

    # Bring back original attribute columns (incl. lat/lon) keyed by _loc_idx
    attrs = df[[c for c in df.columns if not c.startswith('_')] + ['_loc_idx', '_lat', '_lon']]
    long_rows = long_rows.merge(attrs, on='_loc_idx', how='left')

    from core.datasets import DATASETS
    custom_ds = job.get('custom_datasets')
    custom_ds = json.loads(custom_ds) if isinstance(custom_ds, str) and custom_ds else (custom_ds or {})
    all_datasets = {**DATASETS, **custom_ds}
    requested_ds = {name: all_datasets[name] for name in datasets_list if name in all_datasets}
    timeseries_ds = {n: d for n, d in requested_ds.items() if d['type'] == 'timeseries'}
    static_ds = {n: d for n, d in requested_ds.items() if d['type'] in ('static', 'static_fc', 'static_mode', 'static_points_fc')}

    unique_dates = sorted(long_rows['_date'].dt.strftime('%Y-%m-%d').unique())
    total_steps = max(1, len(timeseries_ds) * len(unique_dates) + len(static_ds))
    step = 0

    # ── Timeseries datasets: one Earth Engine call per unique calendar day,
    # batched across every row (any participant/window/role) that needs it ──
    for ds_name, ds_info in timeseries_ds.items():
        coll_id = ds_info['collection']
        band = ds_info['band']
        scale = ds_info['scale']
        ds_stats = [s for s in stats_requested if s in (ds_info.get('stats', []) + list(PERCENTILE_MAP.keys()) + ['mean', 'min', 'max', 'sum'])] or ['mean']
        reducer = _build_reducer(ds_stats)
        col_names = _col_names(ds_info, ds_stats)
        for col in col_names:
            long_rows[col] = np.nan

        bands = band if isinstance(band, list) else [band]
        band_rename = ds_info.get('band_rename')

        # Slice each date's rows up front (plain pandas reads, main thread
        # only) so the worker threads touch nothing shared/mutable.
        day_rows_by_date = {}
        for date_str in unique_dates:
            day_mask = long_rows['_date'].dt.strftime('%Y-%m-%d') == date_str
            day_rows_by_date[date_str] = long_rows.loc[day_mask, ['_row_idx', '_lat', '_lon']].rename(
                columns={'_row_idx': '_loc_idx'})

        with ThreadPoolExecutor(max_workers=MAX_DATE_WORKERS) as executor:
            futures = {
                executor.submit(_fetch_timeseries_day, coll_id, bands, band_rename, date_str,
                                day_rows_by_date[date_str], reducer, scale, ds_stats, ds_info): date_str
                for date_str in unique_dates
            }
            for future in as_completed(futures):
                vals = future.result()
                for row_idx, props in vals.items():
                    parsed = _parse_vals(props, ds_info, ds_stats)
                    row_mask = long_rows['_row_idx'] == row_idx
                    for col, val in parsed.items():
                        long_rows.loc[row_mask, col] = val
                step += 1
                if update_progress_fn:
                    update_progress_fn(min(95, int((step / total_steps) * 90) + 2))

        _apply_value_transform(long_rows, ds_info, col_names)

    # ── Static datasets: one value per location, computed once and broadcast
    # across every day-row at that location ─────────────────────────────────
    if static_ds:
        static_vals = df[['_loc_idx', '_lat', '_lon']].copy()
        n_locs = len(static_vals)
        for ds_name, ds_info in static_ds.items():
            coll_id = ds_info['collection']
            band = ds_info['band']
            scale = ds_info['scale']
            if ds_info['type'] == 'static_mode':
                ds_stats = ['mode']
                reducer = ee.Reducer.mode()
            else:
                ds_stats = [s for s in stats_requested if s in (ds_info.get('stats', []) + list(PERCENTILE_MAP.keys()) + ['mean', 'min', 'max', 'sum'])] or ['mean']
                reducer = _build_reducer(ds_stats)
            col_names = _col_names(ds_info, ds_stats)
            for col in col_names:
                static_vals[col] = np.nan
            try:
                ee_img = _build_static_image(ds_info)
                point_buffer_m = scale / 2 if ds_info['type'] == 'static_points_fc' else 0
                unique_vals, loc_idx_groups = _dedup_by_location(static_vals)
                for batch_start in range(0, len(unique_vals), BATCH):
                    chunk = unique_vals.iloc[batch_start:batch_start + BATCH]
                    try:
                        vals = _expand_dedup_results(
                            _extract_points(ee_img, chunk, reducer, scale, point_buffer_m=point_buffer_m),
                            loc_idx_groups)
                        for loc_idx, props in vals.items():
                            parsed = _parse_vals(props, ds_info, ds_stats)
                            row_mask = static_vals['_loc_idx'] == loc_idx
                            for col, val in parsed.items():
                                static_vals.loc[row_mask, col] = val
                    except Exception as e:
                        print(f"[extractor] Long static batch error {ds_name}: {e}")
            except Exception as e:
                print(f"[extractor] Long static error {ds_name}: {e}")
            _apply_value_transform(static_vals, ds_info, col_names)
            step += 1
            if update_progress_fn:
                update_progress_fn(min(97, int((step / total_steps) * 90) + 2))

        for ds_name, ds_info in static_ds.items():
            clf = ds_info.get('classify')
            if not clf:
                continue
            src, out = clf['source_col'], clf['output_col']
            if src not in static_vals.columns:
                continue
            def _apply_clf(val, ranges=clf['ranges'], default=clf.get('default')):
                if pd.isna(val):
                    return None
                code = int(val)
                for lo, hi, label in ranges:
                    if lo <= code <= hi:
                        return label
                return default
            static_vals[out] = static_vals[src].apply(_apply_clf)

        long_rows = long_rows.merge(static_vals.drop(columns=['_lat', '_lon']), on='_loc_idx', how='left')

    # ── Finalise columns ─────────────────────────────────────────────────
    long_rows = long_rows.sort_values(['_loc_idx', '_period', '_date']).reset_index(drop=True)
    long_rows = long_rows.rename(columns={'_date': 'Exposure_Date', '_period': 'Window', '_day_offset': 'Day_Offset'})
    if date_mode == 'case_crossover':
        long_rows['Role'] = np.where(long_rows['Window'] == 'case', 'case', 'control')

    if job.get('output_shape') == 'lag_matrix':
        # One row per location after all — but unlike run_windowed_extraction's
        # single aggregated mean per window, every individual day in the lag
        # span becomes its own column (e.g. ndvi_lag-7 ... ndvi_lag0). Pivots
        # the daily values already computed above rather than re-extracting
        # anything. index_window-only: enforced in core.ai_agent._validate_plan
        # and in the Flask routes, but checked again here since this function
        # can be called directly.
        if date_mode != 'index_window':
            raise ValueError("output_shape='lag_matrix' is only supported for date_mode='index_window'.")
        converted_cols = (_apply_temp_conversion(long_rows, requested_ds, stats_requested)
                          if job.get('temp_unit') == 'celsius' else set())
        attr_cols = [c for c in df.columns if not c.startswith('_')]
        value_cols = [c for c in long_rows.columns
                      if not c.startswith('_') and c not in ('Exposure_Date', 'Window', 'Day_Offset')
                      and c not in attr_cols]
        pivoted = long_rows.pivot_table(index='_loc_idx', columns='Day_Offset', values=value_cols, aggfunc='first')
        pivoted.columns = [f"{col}_lag{int(offset)}" for col, offset in pivoted.columns]
        pivoted = pivoted.reset_index()
        attrs_df = df[['_loc_idx'] + attr_cols].drop_duplicates('_loc_idx')
        wide_rows = attrs_df.merge(pivoted, on='_loc_idx', how='left').drop(columns=['_loc_idx'])

        if wide_rows.empty:
            raise ValueError("No data was extracted.")
        if output_format == 'parquet':
            wide_rows.to_parquet(output_path, index=False)
        else:
            wide_rows.to_csv(output_path, index=False)
        _write_metadata_file(output_path, job, requested_ds, stats_requested, converted_to_celsius=converted_cols)
        if update_progress_fn:
            update_progress_fn(100, status='complete')
        return output_path

    drop_cols = [c for c in long_rows.columns if c.startswith('_')]
    long_rows = long_rows.drop(columns=drop_cols, errors='ignore')

    front = ['Exposure_Date', 'Window', 'Day_Offset'] + (['Role'] if date_mode == 'case_crossover' else [])
    long_rows = long_rows[front + [c for c in long_rows.columns if c not in front]]

    if long_rows.empty:
        raise ValueError("No data was extracted.")

    converted_cols = (_apply_temp_conversion(long_rows, requested_ds, stats_requested)
                      if job.get('temp_unit') == 'celsius' else set())

    if output_format == 'parquet':
        long_rows.to_parquet(output_path, index=False)
    else:
        long_rows.to_csv(output_path, index=False)

    _write_metadata_file(output_path, job, requested_ds, stats_requested, converted_to_celsius=converted_cols)

    if update_progress_fn:
        update_progress_fn(100, status='complete')

    return output_path


def run_extraction(job, update_progress_fn=None):
    import ee
    import geopandas as gpd

    if job.get('date_mode') in ('pregnancy_windows', 'index_window', 'case_crossover'):
        if job.get('output_shape') in ('long', 'lag_matrix') or job.get('date_mode') == 'case_crossover':
            return run_long_extraction(job, update_progress_fn=update_progress_fn)
        return run_windowed_extraction(job, update_progress_fn=update_progress_fn)

    job_id = job['id']
    datasets_list = json.loads(job['datasets']) if isinstance(job['datasets'], str) else job['datasets']
    stats_requested = json.loads(job['stats_requested']) if isinstance(job['stats_requested'], str) else job['stats_requested']
    input_filename = job['input_filename']
    output_format = job.get('output_format', 'csv')
    start_date = job.get('start_date')
    end_date = job.get('end_date')

    output_ext = 'csv' if output_format == 'csv' else 'parquet'
    output_path = os.path.join(JOBS_DIR, _friendly_filename(datasets_list, job_id, output_ext))
    checkpoint_path = os.path.join(JOBS_DIR, f'{job_id}_checkpoint.parquet')

    # Init EE via service account
    SA_KEY = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'palsearth-sa-key.json')
    try:
        creds = ee.ServiceAccountCredentials('ee-service-account@your-project.iam.gserviceaccount.com', SA_KEY)
        ee.Initialize(credentials=creds, project='your-earth-engine-project')
    except Exception as e:
        raise RuntimeError(f"Earth Engine initialisation failed: {e}")

    if update_progress_fn:
        update_progress_fn(2, status='running')

    # Load input — auto-detect geometry type
    is_shapefile = input_filename.endswith('.shp')
    if is_shapefile:
        gdf = gpd.read_file(input_filename)
        geom_type = gdf.geometry.geom_type.iloc[0]
        is_points = geom_type in ('Point', 'MultiPoint')
        attr_cols = [c for c in gdf.columns if c != 'geometry']
        locs = gdf[attr_cols].copy()
        locs['_loc_idx'] = range(len(locs))
        if is_points:
            locs['_lon'] = gdf.geometry.x
            locs['_lat'] = gdf.geometry.y
        else:
            locs['_geometry'] = gdf.geometry
    else:
        df = pd.read_csv(input_filename)
        lat_col, lon_col = _detect_lat_lon(df)
        if not lat_col or not lon_col:
            raise ValueError("Could not detect latitude/longitude columns.")
        is_points = True
        attr_cols = [c for c in df.columns]
        locs = df.copy()
        locs['_loc_idx'] = range(len(locs))
        locs['_lon'] = df[lon_col]
        locs['_lat'] = df[lat_col]

    n_locs = len(locs)

    from core.datasets import DATASETS
    custom_ds = job.get('custom_datasets')
    custom_ds = json.loads(custom_ds) if isinstance(custom_ds, str) and custom_ds else (custom_ds or {})
    all_datasets = {**DATASETS, **custom_ds}
    requested_ds = {name: all_datasets[name] for name in datasets_list if name in all_datasets}
    timeseries_ds = {n: d for n, d in requested_ds.items() if d['type'] == 'timeseries'}
    static_ds = {n: d for n, d in requested_ds.items() if d['type'] in ('static', 'static_fc', 'static_mode', 'static_points_fc')}

    BATCH = 50
    total_steps = max(1, len(timeseries_ds) * (
        len(pd.date_range(start_date, end_date)) if start_date and end_date else 1
    ) + len(static_ds))
    step = 0

    # ── Build base wide dataframe ──────────────────────────────────────────
    # If timeseries: cross-join locations × dates
    if timeseries_ds and start_date and end_date:
        date_range = pd.date_range(start=start_date, end=end_date, freq='D')
        dates_df = pd.DataFrame({'date': [d.strftime('%Y-%m-%d') for d in date_range]})
        locs_base = locs[[c for c in locs.columns if not c.startswith('_geometry')]].copy()
        locs_base['_xkey'] = 1
        dates_df['_xkey'] = 1
        wide = locs_base.merge(dates_df, on='_xkey').drop(columns='_xkey')
    else:
        wide = locs[[c for c in locs.columns if not c.startswith('_geometry')]].copy()
        date_range = []

    # ── Timeseries extraction ──────────────────────────────────────────────
    for ds_name, ds_info in timeseries_ds.items():
        coll_id = ds_info['collection']
        band = ds_info['band']
        scale = ds_info['scale']
        ds_stats = [s for s in stats_requested if s in (ds_info.get('stats', []) + list(PERCENTILE_MAP.keys()) + ['mean', 'min', 'max', 'sum'])] or ['mean']
        reducer = _build_reducer(ds_stats)
        col_names = _col_names(ds_info, ds_stats)

        # Pre-fill columns with NaN
        for col in col_names:
            wide[col] = np.nan

        bands = band if isinstance(band, list) else [band]
        band_rename = ds_info.get('band_rename')
        locs_or_gdf = locs if is_points else gdf

        with ThreadPoolExecutor(max_workers=MAX_DATE_WORKERS) as executor:
            futures = {
                executor.submit(_fetch_timeseries_day_mixed, coll_id, bands, band_rename,
                                date.strftime('%Y-%m-%d'), locs_or_gdf, is_points, reducer, scale,
                                ds_stats, ds_info): date
                for date in date_range
            }
            for future in as_completed(futures):
                date_str = futures[future].strftime('%Y-%m-%d')
                vals = future.result()
                date_mask = wide['date'] == date_str
                for loc_idx, props in vals.items():
                    parsed = _parse_vals(props, ds_info, ds_stats)
                    loc_mask = locs['_loc_idx'] == loc_idx
                    if not loc_mask.any():
                        continue
                    row_mask = date_mask & (wide['_loc_idx'] == loc_idx)
                    for col, val in parsed.items():
                        wide.loc[row_mask, col] = val

                step += 1
                if update_progress_fn:
                    update_progress_fn(min(95, int((step / total_steps) * 90) + 2))

                # Checkpoint (write-only safety snapshot, never read back for
                # resume — order doesn't matter, just periodicity).
                if step % 10 == 0:
                    try:
                        wide.to_parquet(checkpoint_path, index=False)
                    except Exception:
                        pass

        _apply_value_transform(wide, ds_info, col_names)

    # ── Static extraction ──────────────────────────────────────────────────
    for ds_name, ds_info in static_ds.items():
        coll_id = ds_info['collection']
        band = ds_info['band']
        scale = ds_info['scale']

        # static_mode always uses mode reducer regardless of user stat selection
        if ds_info['type'] == 'static_mode':
            ds_stats = ['mode']
            reducer = ee.Reducer.mode()
        else:
            ds_stats = [s for s in stats_requested if s in (ds_info.get('stats', []) + list(PERCENTILE_MAP.keys()) + ['mean', 'min', 'max', 'sum'])] or ['mean']
            reducer = _build_reducer(ds_stats)

        col_names = _col_names(ds_info, ds_stats)

        for col in col_names:
            wide[col] = np.nan

        try:
            ee_img = _build_static_image(ds_info)
            point_buffer_m = scale / 2 if ds_info['type'] == 'static_points_fc' else 0

            if is_points:
                unique_locs, loc_idx_groups = _dedup_by_location(locs)
                n_batches = len(unique_locs)
            else:
                unique_locs, loc_idx_groups = locs, {}  # polygons: no coordinate to dedup on
                n_batches = n_locs

            for batch_start in range(0, n_batches, BATCH):
                batch_locs = unique_locs.iloc[batch_start:batch_start + BATCH]
                try:
                    if is_points:
                        vals = _expand_dedup_results(
                            _extract_points(ee_img, batch_locs, reducer, scale, point_buffer_m=point_buffer_m),
                            loc_idx_groups)
                    else:
                        batch_gdf = gdf.iloc[batch_start:batch_start + BATCH]
                        vals = _extract_polygons(ee_img, batch_gdf, reducer, scale)

                    for loc_idx, props in vals.items():
                        parsed = _parse_vals(props, ds_info, ds_stats)
                        loc_mask = wide['_loc_idx'] == loc_idx
                        for col, val in parsed.items():
                            wide.loc[loc_mask, col] = val
                except Exception as e:
                    print(f"[extractor] Static batch error {ds_name}: {e}")

        except Exception as e:
            print(f"[extractor] Static error {ds_name}: {e}")

        _apply_value_transform(wide, ds_info, col_names)
        step += 1
        if update_progress_fn:
            update_progress_fn(min(97, int((step / total_steps) * 90) + 2))

    # ── Post-processing: apply classification labels ───────────────────────
    for ds_name, ds_info in static_ds.items():
        clf = ds_info.get('classify')
        if not clf:
            continue
        src = clf['source_col']
        out = clf['output_col']
        if src not in wide.columns:
            continue
        def _apply_clf(val, ranges=clf['ranges'], default=clf.get('default')):
            if pd.isna(val):
                return None
            code = int(val)
            for lo, hi, label in ranges:
                if lo <= code <= hi:
                    return label
            return default
        wide[out] = wide[src].apply(_apply_clf)

    # ── Clean up internal columns and save ────────────────────────────────
    drop_cols = [c for c in wide.columns if c.startswith('_')]
    wide = wide.drop(columns=drop_cols, errors='ignore')

    # Put date first if present
    if 'date' in wide.columns:
        cols = ['date'] + [c for c in wide.columns if c != 'date']
        wide = wide[cols]

    if wide.empty:
        raise ValueError("No data was extracted.")

    converted_cols = (_apply_temp_conversion(wide, requested_ds, stats_requested)
                      if job.get('temp_unit') == 'celsius' else set())

    if output_format == 'parquet':
        wide.to_parquet(output_path, index=False)
    else:
        wide.to_csv(output_path, index=False)

    _write_metadata_file(output_path, job, requested_ds, stats_requested, converted_to_celsius=converted_cols)

    if os.path.exists(checkpoint_path):
        try:
            os.remove(checkpoint_path)
        except Exception:
            pass

    if update_progress_fn:
        update_progress_fn(100, status='complete')

    return output_path
