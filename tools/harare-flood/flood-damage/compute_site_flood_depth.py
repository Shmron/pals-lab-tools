"""
HAND flood-depth proxy for any CASCADE site (same method as
flood_accessibility_pipeline.compute_floodplain, plus a depth output).

depth = Leopold stage at the nearest downstream stream cell - HAND, where the
cell is in the floodplain (HAND <= stage). Geomorphic scenario depth, NOT an
event-simulated depth. Verifies that the recomputed floodplain matches the
stored floodplain raster cell-for-cell before writing depth.

Usage: python compute_site_flood_depth.py <site>   (tokwe | beitbridge | chimanimani_district)
"""
import os
import sys

import numpy as np
import rasterio

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
A_PARAM, B_PARAM = 0.01, 0.30
D8 = {1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1), 16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1)}
SITES = {
    # Harare uses the Hu & Demir (2021) / Nobre et al. (2016) 4.05 km2 drainage threshold, as in the live tool
    # (merit file, stored floodplain to verify against or None, threshold km2, output suffix)
    "harare": ("harare_merit_hydro.tif", None, 4.05, "_4p05km2"),
    # the locally stored harare floodplain raster is the DENSE (0.05 km2) preset - verify that
    "harare_dense": ("harare_merit_hydro.tif", "harare_selfcomputed_floodplain.tif", 0.05, "_0p05km2"),
    "tokwe": ("tokwe_merit_hydro.tif", "tokwe_selfcomputed_floodplain.tif", 0.05, ""),
    "beitbridge": ("beitbridge_merit_hydro.tif", "beitbridge_selfcomputed_floodplain.tif", 0.05, ""),
    "chimanimani_district": ("chimanimani_district_merit_hydro.tif",
                             "chimanimani_district_selfcomputed_floodplain.tif", 0.05, ""),
}


def main(site):
    merit, fp_name, thr_km2, suffix = SITES[site]
    with rasterio.open(os.path.join(BASE_DIR, "DEM", merit)) as src:
        elv = src.read(1).astype(np.float64)
        flowdir = src.read(2).astype(np.int32)
        upa = src.read(3).astype(np.float64)
        transform, crs = src.transform, src.crs
    nrows, ncols = elv.shape
    is_stream = upa >= thr_km2
    water_level = A_PARAM * np.power(np.maximum(upa * 1e6, 1.0), B_PARAM)
    trow = np.full((nrows, ncols), -1, dtype=np.int32)
    tcol = np.full((nrows, ncols), -1, dtype=np.int32)
    for r0 in range(nrows):
        for c0 in range(ncols):
            if is_stream[r0, c0]:
                trow[r0, c0], tcol[r0, c0] = r0, c0
                continue
            if trow[r0, c0] != -1:
                continue
            path, r, c, steps = [], r0, c0, 0
            while steps < 2000:
                if trow[r, c] != -1:
                    tr, tc = trow[r, c], tcol[r, c]; break
                if is_stream[r, c]:
                    tr, tc = r, c; break
                path.append((r, c))
                d = flowdir[r, c]
                if d not in D8 or d <= 0:
                    tr, tc = -1, -1; break
                nr, nc = r + D8[d][0], c + D8[d][1]
                if not (0 <= nr < nrows and 0 <= nc < ncols):
                    tr, tc = -1, -1; break
                r, c = nr, nc; steps += 1
            else:
                tr, tc = -1, -1
            for pr, pc in path:
                trow[pr, pc], tcol[pr, pc] = tr, tc
    resolved = trow != -1
    hand = np.full((nrows, ncols), np.nan)
    stage = np.full((nrows, ncols), np.nan)
    rr, cc = np.where(resolved)
    hand[rr, cc] = elv[rr, cc] - elv[trow[rr, cc], tcol[rr, cc]]
    stage[rr, cc] = water_level[trow[rr, cc], tcol[rr, cc]]
    fp = np.where(resolved & (hand <= stage), 1, np.where(resolved, 0, np.nan))
    share = np.nansum(fp == 1) / max(resolved.sum(), 1)
    print(f"[i] floodplain at {thr_km2} km2 threshold: {np.nansum(fp == 1):,.0f} cells ({share:.1%} of resolved)")
    if fp_name:
        with rasterio.open(os.path.join(BASE_DIR, "DEM", fp_name)) as ref:
            stored = ref.read(1)
        same = np.array_equal(np.nan_to_num(fp, nan=-1), np.nan_to_num(stored, nan=-1))
        print(f"[check] recomputed floodplain identical to stored raster: {same}")
        if not same:
            raise SystemExit("Recomputed floodplain differs from stored raster - not writing depth.")
    stem = {"harare": "harare", "harare_dense": "harare"}.get(site, os.path.basename(merit).replace("_merit_hydro.tif", ""))
    depth = np.where(fp == 1, np.clip(stage - hand, 0, None), np.nan).astype(np.float32)
    out = os.path.join(BASE_DIR, "DEM", f"{stem}_selfcomputed_flood_depth{suffix}.tif")
    fp_out = os.path.join(BASE_DIR, "DEM", f"{stem}_selfcomputed_floodplain{suffix}.tif") if suffix else None
    prof = dict(driver="GTiff", height=nrows, width=ncols, count=1, dtype=np.float32,
                crs=crs, transform=transform, nodata=np.nan)
    with rasterio.open(out, "w", **prof) as dst:
        dst.write(depth, 1)
    if fp_out and not os.path.exists(fp_out):
        with rasterio.open(fp_out, "w", **prof) as dst:
            dst.write(fp.astype(np.float32), 1)
    print(f"[OK] {out}: median {np.nanmedian(depth):.2f} m, max {np.nanmax(depth):.2f} m")


if __name__ == "__main__":
    main(sys.argv[1])
