"""
Observed flood extent for Cyclone Idai (Chimanimani, March 2019) from Sentinel-1
change detection, then compared with the self-computed HAND floodplain.

Adapted from the UN-SPIDER Recommended Practice "Flood Mapping and Damage
Assessment Using Sentinel-1 SAR Data in Google Earth Engine", with these changes:
  * change metric is a DIFFERENCE IN dB (after - before), not a ratio of dB values
  * same relative orbit + pass direction for before and after (no orbit mixing)
  * terrain mask is MERIT Hydro HAND (hnd) < 15 m, not slope < 5 deg, which would
    remove most of steep Chimanimani. hnd is independent of our own HAND floodplain.
  * permanent water (JRC seasonality >= 10 months) masked, as in the original
  * speckle filter + connected-pixel noise removal, as in the original

IMPORTANT interpretation limits: SAR "after" scenes are 2-4 days after the peak,
so this is a lower bound on peak extent; C-band misses flooding under canopy and
in built-up areas; steep valleys have layover/shadow. Threshold is not calibrated
against ground truth.
"""
import json
import os

import ee
import numpy as np
import rasterio
import requests
from rasterio.warp import Resampling, reproject

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "SAR")
os.makedirs(OUT_DIR, exist_ok=True)

BBOX = [32.75, -19.95, 32.98, -19.65]
HAND_FLOODPLAIN = os.path.join(BASE_DIR, "DEM", "chimanimani_selfcomputed_floodplain.tif")

# (name, pass, relative orbit, before date, after date) - all Sentinel-1 IW, 1SDV
PAIRS = [
    ("orbit72_asc", "ASCENDING", 72, "2019-03-12", "2019-03-18"),
    ("orbit174_asc", "ASCENDING", 174, "2019-03-13", "2019-03-19"),
]
POLARISATIONS = ["VH", "VV"]
THRESHOLDS_DB = [-2.0, -3.0]  # (after - before) minus scene median shift, below this => new open water
HAND_MAX_M = 15.0          # exclude terrain high above drainage (radar shadow, hillslopes)
SCALE = 20                 # metres; 10 m exceeds the direct-download size limit for VV+VH

ee.Initialize(project="your-earth-engine-project")
region = ee.Geometry.Rectangle(BBOX)


def scene(pass_dir, orbit, date, pol):
    d = ee.Date(date)
    col = (ee.ImageCollection("COPERNICUS/S1_GRD")
           .filter(ee.Filter.eq("instrumentMode", "IW"))
           .filter(ee.Filter.eq("orbitProperties_pass", pass_dir))
           .filter(ee.Filter.eq("relativeOrbitNumber_start", orbit))
           .filter(ee.Filter.listContains("transmitterReceiverPolarisation", pol))
           .filterBounds(region)
           .filterDate(d, d.advance(1, "day"))
           .select(pol))
    return col, col.mosaic().clip(region).focal_mean(50, "circle", "meters")


swater = ee.Image("JRC/GSW1_0/GlobalSurfaceWater").select("seasonality")
permanent_water = swater.gte(10)
hand = ee.Image("MERIT/Hydro/v1_0_1").select("hnd")

results = {}
for name, pass_dir, orbit, d_before, d_after in PAIRS:
    for pol, thr in [(p, t) for p in POLARISATIONS for t in THRESHOLDS_DB]:
        key = f"{name}_{pol}_{int(abs(thr))}dB"
        col_b, before = scene(pass_dir, orbit, d_before, pol)
        col_a, after = scene(pass_dir, orbit, d_after, pol)
        nb, na = col_b.size().getInfo(), col_a.size().getInfo()
        print(f"[i] {key}: before={d_before} ({nb} tiles), after={d_after} ({na} tiles)")
        if nb == 0 or na == 0:
            results[key] = {"error": "empty collection", "n_before": nb, "n_after": na}
            continue

        diff_db = after.subtract(before)
        # Remove the scene-wide radiometric shift (wet soil/vegetation raised
        # backscatter by ~+1.8 dB everywhere after Idai; a fixed threshold on the
        # raw difference then hides real darkening from open water).
        shift = ee.Number(diff_db.reduceRegion(
            ee.Reducer.median(), region, 100, maxPixels=1e9).get(pol))
        shift_db = shift.getInfo()
        diff_db = diff_db.subtract(shift)
        print(f"    scene median shift removed: {shift_db:+.2f} dB")
        flood = diff_db.lt(thr)
        flood = flood.updateMask(permanent_water.Not().unmask(1))
        flood = flood.updateMask(hand.lt(HAND_MAX_M))
        flood = flood.selfMask()
        flood = flood.updateMask(flood.connectedPixelCount(100, True).gte(8))
        flood_u8 = flood.unmask(0).toUint8().rename("flood")

        url = flood_u8.getDownloadURL({
            "region": region, "scale": SCALE, "crs": "EPSG:4326", "format": "GEO_TIFF"})
        r = requests.get(url, timeout=300)
        r.raise_for_status()
        path = os.path.join(OUT_DIR, f"chimanimani_idai_sar_flood_{key}.tif")
        with open(path, "wb") as f:
            f.write(r.content)
        print(f"[OK] {path} ({len(r.content):,} bytes)")
        results[key] = {"path": path, "n_before": nb, "n_after": na,
                        "before": d_before, "after": d_after,
                        "median_shift_removed_db": round(shift_db, 2), "threshold_db": thr}

# ---- Compare with the self-computed HAND floodplain (90 m grid) ----
with rasterio.open(HAND_FLOODPLAIN) as ref:
    ref_arr = ref.read(1)
    ref_valid = np.isfinite(ref_arr)
    ref_fp = (ref_arr == 1) & ref_valid
    ref_transform = ref.transform

for key, info in results.items():
    if "path" not in info:
        continue
    with rasterio.open(info["path"]) as src:
        sar = src.read(1).astype("float32")
        # fraction of each 90 m cell that SAR marks flooded (average-resample)
        frac = np.zeros(ref_arr.shape, dtype="float32")
        reproject(sar, frac, src_transform=src.transform, src_crs=src.crs,
                  dst_transform=ref_transform,
                  dst_crs="EPSG:4326", resampling=Resampling.average)
    sar_cell = (frac >= 0.25) & ref_valid          # >=25% of the 90 m cell is SAR water
    inter = (sar_cell & ref_fp).sum()
    info.update({
        "sar_flooded_cells_90m": int(sar_cell.sum()),
        "hand_floodplain_cells_90m": int(ref_fp.sum()),
        "share_of_sar_inside_hand": round(float(inter / max(sar_cell.sum(), 1)), 3),
        "share_of_hand_with_sar": round(float(inter / max(ref_fp.sum(), 1)), 3),
        "sar_share_of_domain": round(float(sar_cell.sum() / ref_valid.sum()), 4),
        "hand_share_of_domain": round(float(ref_fp.sum() / ref_valid.sum()), 4),
    })
    print(f"[cmp] {key}: SAR cells={info['sar_flooded_cells_90m']}, "
          f"HAND cells={info['hand_floodplain_cells_90m']}, "
          f"SAR inside HAND={info['share_of_sar_inside_hand']}, "
          f"HAND with SAR={info['share_of_hand_with_sar']}")

out_json = os.path.join(OUT_DIR, "chimanimani_idai_sar_vs_hand.json")
with open(out_json, "w") as f:
    json.dump(results, f, indent=2)
print(f"[OK] {out_json}")
