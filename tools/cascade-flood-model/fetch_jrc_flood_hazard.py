"""
Fetch JRC Global River Flood Hazard Maps v2.1 for the Chimanimani bbox.

Pulls each band SEPARATELY (not combined into one multi-band export) because
mixing float depth bands with integer category bands in a single GEO_TIFF
export appears to trigger GEE auto-normalization (first attempt returned a
suspicious [0,1] range on every band and a 6.7KB file - clearly wrong given
JRC's real depths run up to hundreds of meters).
"""
import os
import ee
import requests
import rasterio
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "JRC_FloodHazard")
os.makedirs(OUT_DIR, exist_ok=True)

CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]

ee.Initialize(project="your-earth-engine-project")
region = ee.Geometry.Rectangle(CHIMANIMANI_BBOX)

jrc = ee.ImageCollection("JRC/CEMS_GLOFAS/FloodHazard/v2_1").mosaic()

bands = [
    "RP10_depth", "RP50_depth", "RP100_depth", "RP500_depth",
    "spurious_depth_category", "permanent_water_class",
]

for band_name in bands:
    band_img = jrc.select(band_name).clip(region)
    # Force explicit float64 output, no default visualization stretching
    band_img = band_img.toFloat()

    url = band_img.getDownloadURL({
        "region": region,
        "scale": 90,
        "crs": "EPSG:4326",
        "format": "GEO_TIFF",
    })
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()

    out_path = os.path.join(OUT_DIR, f"chimanimani_jrc_{band_name}.tif")
    with open(out_path, "wb") as f:
        f.write(resp.content)

    with rasterio.open(out_path) as src:
        data = src.read(1)
        valid = data[~np.isnan(data)]
        print(f"{band_name}: {len(resp.content):,} bytes, shape={data.shape}, "
              f"valid={len(valid)}/{data.size}, "
              f"min={np.nanmin(data) if len(valid) else 'nan'}, "
              f"max={np.nanmax(data) if len(valid) else 'nan'}, "
              f"mean={np.nanmean(data) if len(valid) else 'nan'}")
