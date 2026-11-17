"""
Building exposure and monetary flood damage, Chimanimani (self-computed HAND
floodplain and Sentinel-1 observed Idai extent).

Inputs
  - Google Open Buildings v3 footprints (via Earth Engine), confidence >= 0.7
  - DEM/chimanimani_selfcomputed_flood_depth.tif  (HAND depth proxy, 90 m)
  - SAR/chimanimani_idai_sar_flood_*.tif          (observed Idai extent, lower bound)

Damage model (JRC global flood depth-damage functions, Huizinga et al. 2017,
JRC105688, spreadsheet "copy_of_global_flood_depth-damage_functions__30102017.xlsx"):
  damage_EUR2010 = footprint_area_m2 * max_damage_EUR_per_m2 * f(depth)
  f(depth): Africa, residential buildings, linearly interpolated between the
            published depths 0,0.5,1,1.5,2,3,4,5,6 m; +/-1 SD curve for a range.
  max damage: Zimbabwe residential, structure 109.47 EUR/m2, content 54.74 EUR/m2
            (price level 2010 EUR - NOT inflation or exchange-rate adjusted).

Caveats are written to the summary JSON and must travel with any number quoted:
  * depth is a geomorphic proxy (bankfull-scale stage minus HAND), not an event depth
  * all buildings treated as residential; footprint area used as floor area
  * country-average replacement cost; rural/informal building stock is likely
    cheaper than the JRC average, so damage is more likely overestimated than not
"""
import json
import os
import time

import ee
import numpy as np
import pandas as pd
import rasterio
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
import sys

BLD_DIR = os.path.join(BASE_DIR, "Buildings")
os.makedirs(BLD_DIR, exist_ok=True)

# site -> (bbox, depth raster, optional observed-SAR raster)
SITES = {
    # Harare presets as in the live tool: recommended 4.05 km2 drainage threshold, and the dense 0.05 km2 one
    "harare": ([30.66, -18.22, 31.70, -17.17], "harare_selfcomputed_flood_depth_4p05km2.tif", None),
    "harare_dense": ([30.66, -18.22, 31.70, -17.17], "harare_selfcomputed_flood_depth_0p05km2.tif", None),
    "chimanimani": ([32.75, -19.95, 32.98, -19.65], "chimanimani_selfcomputed_flood_depth.tif",
                    "chimanimani_idai_sar_flood_orbit72_asc_VH_2dB.tif"),
    "chimanimani_district": ([32.348, -20.120, 33.056, -19.374],
                             "chimanimani_district_selfcomputed_flood_depth.tif", None),
    "tokwe": ([30.25, -21.45, 30.65, -20.85], "tokwe_selfcomputed_flood_depth.tif", None),
    "beitbridge": ([29.85, -22.35, 30.15, -22.05], "beitbridge_selfcomputed_flood_depth.tif", None),
}
SITE = sys.argv[1] if len(sys.argv) > 1 else "chimanimani"
BBOX, _depth, _sar = SITES[SITE]
BLD_KEY = "harare" if SITE.startswith("harare") else SITE  # presets share one building download
BLD_CSV = os.path.join(BLD_DIR, f"{BLD_KEY}_open_buildings_conf70.csv")
OUT_CSV = os.path.join(BLD_DIR, f"{SITE}_building_damage.csv")
OUT_JSON = os.path.join(BLD_DIR, f"{SITE}_building_damage_summary.json")
DEPTH_TIF = os.path.join(BASE_DIR, "DEM", _depth)
SAR_TIF = os.path.join(BASE_DIR, "SAR", _sar) if _sar else None
MIN_CONFIDENCE = 0.7
TILE_DEG = 0.04 if BLD_KEY == "harare" else 0.08  # dense urban tiles must stay under EE download limits

# JRC Africa, residential buildings (spreadsheet column G / N), copied verbatim
DEPTHS = np.array([0, 0.5, 1, 1.5, 2, 3, 4, 5, 6], dtype=float)
FRAC = np.array([0, 0.2199254, 0.3782200, 0.5305890, 0.6356367, 0.8169398, 0.9034347, 0.9571522, 1.0])
SD = np.array([0, 0.0420037, 0.1142963, 0.1983962, 0.2078216, 0.2052469, 0.1418564, 0.0762088, 0.0])
MAXD_STRUCTURE = 109.4715016916206   # EUR(2010)/m2, Zimbabwe residential structure
MAXD_CONTENT = 54.7357508458103      # EUR(2010)/m2, Zimbabwe residential content


def fetch_buildings():
    if os.path.exists(BLD_CSV):
        return pd.read_csv(BLD_CSV)
    _key = os.environ.get("EE_SA_KEY")  # set on the PALs server (service account); locally falls back to user login
    if _key:
        ee.Initialize(credentials=ee.ServiceAccountCredentials(os.environ["EE_SA_EMAIL"], _key),
                      project="your-earth-engine-project")
    else:
        ee.Initialize(project="your-earth-engine-project")
    x0, y0, x1, y1 = BBOX
    nx = max(1, int(np.ceil((x1 - x0) / TILE_DEG)))
    ny = max(1, int(np.ceil((y1 - y0) / TILE_DEG)))
    parts = []
    for i in range(nx):
        for j in range(ny):
            tile = [x0 + (x1 - x0) * i / nx, y0 + (y1 - y0) * j / ny,
                    x0 + (x1 - x0) * (i + 1) / nx, y0 + (y1 - y0) * (j + 1) / ny]
            fc = (ee.FeatureCollection("GOOGLE/Research/open-buildings/v3/polygons")
                  .filterBounds(ee.Geometry.Rectangle(tile))
                  .filter(ee.Filter.gte("confidence", MIN_CONFIDENCE))
                  .map(lambda f: ee.Feature(f.geometry().centroid(1), {
                      "area_m2": f.get("area_in_meters"), "confidence": f.get("confidence"),
                      "plus": f.get("full_plus_code")})))
            tmp = os.path.join(BLD_DIR, f"_tile_{BLD_KEY}_{i}_{j}.csv")
            if not os.path.exists(tmp):
                for attempt in range(5):
                    try:
                        url = fc.getDownloadURL("csv", ["area_m2", "confidence", "plus", ".geo"])
                        r = requests.get(url, timeout=300)
                        r.raise_for_status()
                        open(tmp, "wb").write(r.content)
                        break
                    except (requests.exceptions.RequestException, ee.EEException) as e:
                        print(f"    tile {i},{j} attempt {attempt + 1} failed: {e.__class__.__name__}")
                        time.sleep(5 * (attempt + 1))
                else:
                    raise RuntimeError(f"tile {i},{j} failed after 5 attempts")
            parts.append(pd.read_csv(tmp))
    df = pd.concat(parts, ignore_index=True)
    coords = df[".geo"].str.extract(r"\[(-?[\d.]+),\s*(-?[\d.]+)\]").astype(float)
    df["lon"], df["lat"] = coords[0], coords[1]
    df = df.drop(columns=[".geo", "system:index"], errors="ignore").drop_duplicates("plus")
    df.to_csv(BLD_CSV, index=False)
    for f in os.listdir(BLD_DIR):
        if f.startswith(f"_tile_{BLD_KEY}_"):
            os.remove(os.path.join(BLD_DIR, f))
    return df


def sample(path, lon, lat):
    with rasterio.open(path) as src:
        vals = np.array([v[0] for v in src.sample(zip(lon, lat))], dtype=float)
        if src.nodata is not None and not np.isnan(src.nodata):
            vals[vals == src.nodata] = np.nan
    return vals


def main():
    df = fetch_buildings()
    print(f"[i] {len(df):,} buildings (confidence >= {MIN_CONFIDENCE}), "
          f"total footprint {df.area_m2.sum()/1e4:,.1f} ha, median {df.area_m2.median():.0f} m2")

    df["depth_m"] = sample(DEPTH_TIF, df.lon.values, df.lat.values)
    df["in_hand_floodplain"] = df.depth_m.notna()
    df["depth_m"] = df.depth_m.fillna(0.0)
    df["in_sar_flood"] = (np.nan_to_num(sample(SAR_TIF, df.lon.values, df.lat.values)) == 1
                          if SAR_TIF else False)

    for tag, f in (("central", FRAC), ("low", np.clip(FRAC - SD, 0, 1)), ("high", np.clip(FRAC + SD, 0, 1))):
        df[f"dmg_frac_{tag}"] = np.interp(df.depth_m, DEPTHS, f)
        df[f"dmg_struct_eur2010_{tag}"] = df.area_m2 * MAXD_STRUCTURE * df[f"dmg_frac_{tag}"]
        df[f"dmg_total_eur2010_{tag}"] = df.area_m2 * (MAXD_STRUCTURE + MAXD_CONTENT) * df[f"dmg_frac_{tag}"]
    # zero damage outside the floodplain regardless of curve
    df.loc[~df.in_hand_floodplain, [c for c in df.columns if c.startswith("dmg_")]] = 0.0
    df.to_csv(OUT_CSV, index=False)

    hand = df[df.in_hand_floodplain]
    sar = df[df.in_sar_flood]
    summary = {
        "buildings_total": int(len(df)),
        "buildings_in_hand_floodplain": int(len(hand)),
        "share_in_hand_floodplain": round(len(hand) / len(df), 3),
        "site": SITE,
        "buildings_in_sar_idai_flood_lower_bound": int(len(sar)) if SAR_TIF else None,
        "hand_floodplain_depth_m": {"median": round(float(hand.depth_m.median()), 2),
                                    "p90": round(float(hand.depth_m.quantile(0.9)), 2),
                                    "max": round(float(hand.depth_m.max()), 2)},
        "damage_eur2010_structure_only": {
            t: round(float(df[f"dmg_struct_eur2010_{t}"].sum())) for t in ("low", "central", "high")},
        "damage_eur2010_structure_plus_content": {
            t: round(float(df[f"dmg_total_eur2010_{t}"].sum())) for t in ("low", "central", "high")},
        "sar_flooded_buildings_damage_eur2010_structure_central": None if not SAR_TIF else round(
            float((sar.area_m2 * MAXD_STRUCTURE * np.interp(
                sar.depth_m.where(sar.in_hand_floodplain, 0.3), DEPTHS, FRAC)).sum())),
        "note_sar_depth": "buildings in SAR extent outside the HAND floodplain are given a nominal 0.3 m depth",
        "caveats": [
            "Depth is a geomorphic proxy (stage minus HAND), not an event depth; on very large catchments (e.g. Limpopo) the stage formula gives implausibly deep values, but the JRC curve saturates at 6 m so damage fraction is capped at 1.",
            "HAND floodplain is a potential-flood layer: the damage is a scenario value (flood reaching the whole floodplain), not an Idai loss.",
            "All buildings treated as residential; footprint area used as floor area.",
            "JRC country-average max damage, 2010 EUR, not inflation/exchange adjusted; likely overstates informal rural stock.",
            "Low/high = Africa residential curve -/+ 1 SD only; max-damage uncertainty is not included.",
            "Open Buildings 90 m depth sampling: buildings near floodplain edges are sensitive to cell alignment.",
        ],
    }
    json.dump(summary, open(OUT_JSON, "w"), indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
