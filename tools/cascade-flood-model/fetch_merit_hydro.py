"""
Fetch MERIT Hydro (elevation, flow direction, upstream drainage area) for the
Chimanimani test area from Google Earth Engine.

Bands:
  elv - bias-corrected elevation (m)
  dir - flow direction (D8)
  upa - upstream drainage area / flow accumulation (km^2)
"""
import os
import ee
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "DEM")
os.makedirs(OUT_DIR, exist_ok=True)

# Same bbox as the roads/waterways/facilities extracts (west, south, east, north)
CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]

ee.Initialize(project="your-earth-engine-project")

region = ee.Geometry.Rectangle(CHIMANIMANI_BBOX)

merit_hydro = ee.Image("MERIT/Hydro/v1_0_1").select(["elv", "dir", "upa"])

url = merit_hydro.getDownloadURL({
    "region": region,
    "scale": 90,  # MERIT Hydro native resolution (~90m / 3 arc-sec)
    "crs": "EPSG:4326",
    "format": "GEO_TIFF",
})

print(f"[*] Downloading MERIT Hydro (elv, dir, upa) for Chimanimani bbox ...")
resp = requests.get(url, timeout=120)
resp.raise_for_status()

out_path = os.path.join(OUT_DIR, "chimanimani_merit_hydro.tif")
with open(out_path, "wb") as f:
    f.write(resp.content)

print(f"[OK] Saved -> {out_path} ({len(resp.content):,} bytes)")

# Quick sanity check
import rasterio
import numpy as np

with rasterio.open(out_path) as src:
    print(f"\nShape: {src.shape}, bands: {src.count}, CRS: {src.crs}")
    print(f"Bounds: {src.bounds}")
    elv = src.read(1)
    upa = src.read(3)
    print(f"Elevation range: {np.nanmin(elv):.0f} - {np.nanmax(elv):.0f} m")
    print(f"Upstream drainage area range: {np.nanmin(upa):.3f} - {np.nanmax(upa):.1f} km2")
