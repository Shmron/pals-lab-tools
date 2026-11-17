"""
Validates Shmron AI's proposed Earth Engine dataset definitions against the
real Earth Engine catalog before they can be used in an extraction job.

Nothing here executes arbitrary code: a "custom dataset" is a plain dict in
the exact same shape as core.datasets.DATASETS, and it only becomes usable
once we've confirmed (via a real, read-only Earth Engine call) that the
collection exists and the requested band(s) are actually present.
"""
import os

SA_EMAIL = 'ee-service-account@your-project.iam.gserviceaccount.com'
SA_KEY = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'palsearth-sa-key.json')
EE_PROJECT = 'your-earth-engine-project'

_VALID_TYPES = {'timeseries', 'static', 'static_fc', 'static_mode', 'static_points_fc'}
_VALID_STATS = {'mean', 'min', 'max', 'sum', 'p50', 'p75', 'p95', 'p99', 'mode'}

_ee_ready = False


def _ensure_ee():
    global _ee_ready
    import ee
    if not _ee_ready:
        creds = ee.ServiceAccountCredentials(SA_EMAIL, SA_KEY)
        ee.Initialize(credentials=creds, project=EE_PROJECT)
        _ee_ready = True
    return ee


def validate_dataset_candidate(collection, band, scale, ds_type, stats):
    """
    Real, read-only check against Earth Engine. Returns:
        (ok: bool, message: str, available_bands: list[str] | None)
    """
    if ds_type not in _VALID_TYPES:
        return False, f"type must be one of {sorted(_VALID_TYPES)}", None

    bad_stats = [s for s in stats if s not in _VALID_STATS]
    if bad_stats:
        return False, f"unsupported stat(s) {bad_stats}, allowed: {sorted(_VALID_STATS)}", None

    bands = band if isinstance(band, list) else [band]
    if not collection or not bands:
        return False, "collection and band are required", None

    try:
        ee = _ensure_ee()
    except Exception as e:
        return False, f"Earth Engine could not initialise: {e}", None

    is_points_fc = ds_type == 'static_points_fc'
    try:
        if is_points_fc:
            # Some community (sat-io) EE assets described as rasters are
            # actually a FeatureCollection of points with a numeric property
            # per point (e.g. Meta's Relative Wealth Index) — loading one as
            # ee.Image/ImageCollection throws "Expected ... to be an
            # ImageCollection, found 'EECollection'" every single time, which
            # a caller catching broad exceptions can silently swallow. Check
            # property names, not band names, for this type.
            feat = ee.FeatureCollection(collection).first()
            available = feat.propertyNames().getInfo()
        else:
            if ds_type in ('timeseries', 'static_fc'):
                img = ee.ImageCollection(collection).first()
            else:
                img = ee.Image(collection)
            available = img.bandNames().getInfo()
    except Exception as e:
        return False, f"Collection '{collection}' could not be read from Earth Engine: {e}", None

    missing = [b for b in bands if b not in available]
    if missing:
        noun = "propert(y/ies)" if is_points_fc else "band(s)"
        return (
            False,
            f"{noun.capitalize()} {missing} not found in '{collection}'. Available: {available}",
            available,
        )

    if scale is None or not isinstance(scale, (int, float)) or scale <= 0:
        return False, "scale must be a positive number of metres", available

    return True, f"Verified — '{collection}' has band(s) {bands} at your requested scale.", available
