"""
Fetch CHIRPS daily rainfall for the Chimanimani bbox around Cyclone Idai's
landfall (mid-March 2019), to combine with MERIT Hydro upstream drainage area
for a rainfall-weighted crossing risk index (Rational Method: Q ~ i * A).
"""
import os
import ee
import requests
import rasterio
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "Rainfall")
os.makedirs(OUT_DIR, exist_ok=True)

CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]

# Idai made landfall in Mozambique ~14-15 March 2019, with the worst Chimanimani
# rainfall/flooding on 15-16 March 2019. Pull a window either side to capture
# the full rain event.
START_DATE = "2019-03-13"
END_DATE = "2019-03-19"

ee.Initialize(project="your-earth-engine-project")
region = ee.Geometry.Rectangle(CHIMANIMANI_BBOX)

chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterDate(START_DATE, END_DATE)
n_images = chirps.size().getInfo()
print(f"[i] {n_images} CHIRPS daily images found for {START_DATE} to {END_DATE}")

# Event total rainfall (sum across the window) - what matters for saturated-soil
# flash flooding is cumulative rainfall over the event, not any single day
event_total = chirps.sum().clip(region)

url = event_total.getDownloadURL({
    "region": region,
    "scale": 5566,  # CHIRPS native resolution (~0.05deg / ~5.5km)
    "crs": "EPSG:4326",
    "format": "GEO_TIFF",
})

print("[*] Downloading CHIRPS event-total rainfall ...")
resp = requests.get(url, timeout=120)
resp.raise_for_status()

out_path = os.path.join(OUT_DIR, "chimanimani_chirps_idai_total.tif")
with open(out_path, "wb") as f:
    f.write(resp.content)
print(f"[OK] Saved -> {out_path} ({len(resp.content):,} bytes)")

with rasterio.open(out_path) as src:
    data = src.read(1)
    print(f"\nShape: {src.shape}, CRS: {src.crs}")
    print(f"Event total rainfall ({START_DATE} to {END_DATE}): "
          f"min={np.nanmin(data):.1f}mm, max={np.nanmax(data):.1f}mm, mean={np.nanmean(data):.1f}mm")

# Also pull daily series for the bbox average, to see the day-by-day profile
print("\n[*] Daily rainfall profile (bbox mean, mm/day):")
daily_list = chirps.toList(n_images)
for i in range(n_images):
    img = ee.Image(daily_list.get(i))
    date = img.date().format("YYYY-MM-dd").getInfo()
    mean_val = img.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=region, scale=5566
    ).getInfo()
    precip = mean_val.get("precipitation", None)
    print(f"  {date}: {precip:.1f} mm" if precip is not None else f"  {date}: no data")
