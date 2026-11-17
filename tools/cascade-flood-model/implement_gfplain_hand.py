"""
Self-computed floodplain layer for Chimanimani, replicating the decoded
GFPLAIN algorithm (Nardi et al.) directly on MERIT Hydro (90m), instead of
the pre-computed 250m global product that returned 100% nodata here.

Method (see METHODS_LOG.md for the full derivation):
  1. Stream network = cells where upstream drainage area (upa) >= bl_tresh
  2. Water level at each stream cell = a * (area_m2)^b, Leopold power law
     (a=0.01, b=0.30 - the exact GFPLAIN v1.0 parameters)
  3. HAND = each cell's elevation minus its nearest DOWNSTREAM stream cell's
     elevation, traced via MERIT Hydro's D8 flow direction codes
  4. Floodplain = HAND <= water level of that same nearest stream cell

No ArcGIS/WhiteboxTools needed - traced directly in numpy using MERIT
Hydro's D8 encoding (standard ESRI power-of-two: 1=E,2=SE,4=S,8=SW,16=W,
32=NW,64=N,128=NE), with memoization so each cell's downstream trace reuses
any already-resolved path it merges into.
"""
import os
import numpy as np
import rasterio
import geopandas as gpd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MERIT_PATH = os.path.join(BASE_DIR, "DEM", "chimanimani_merit_hydro.tif")
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")

A_PARAM = 0.01
B_PARAM = 0.30
BL_TRESH_KM2 = 0.05  # channel initiation threshold - humid mountainous terrain, ~50,000 m2

# D8 direction codes (ESRI/MERIT Hydro standard) -> (d_row, d_col)
D8_OFFSETS = {
    1: (0, 1),    # E
    2: (1, 1),    # SE
    4: (1, 0),    # S
    8: (1, -1),   # SW
    16: (0, -1),  # W
    32: (-1, -1), # NW
    64: (-1, 0),  # N
    128: (-1, 1), # NE
}


def main():
    with rasterio.open(MERIT_PATH) as src:
        elv = src.read(1).astype(np.float64)
        flowdir = src.read(2).astype(np.int32)
        upa = src.read(3).astype(np.float64)
        transform = src.transform
        crs = src.crs
        nrows, ncols = elv.shape

    print(f"[i] Grid: {nrows}x{ncols}, elevation {elv.min():.0f}-{elv.max():.0f}m, "
          f"upa {np.nanmin(upa):.4f}-{np.nanmax(upa):.1f} km2")

    is_stream = upa >= BL_TRESH_KM2
    n_stream = is_stream.sum()
    print(f"[i] Stream network (upa >= {BL_TRESH_KM2} km2): {n_stream:,} cells "
          f"({n_stream / elv.size * 100:.1f}% of grid)")

    # Water level (meters) at each stream cell - Leopold power law on drainage area
    area_m2 = upa * 1e6
    water_level = A_PARAM * np.power(np.maximum(area_m2, 1.0), B_PARAM)

    # For every cell, trace downstream along D8 flow direction until hitting a
    # stream cell. Memoize: once we know cell X's target stream cell, any cell
    # that flows through X reuses that result instead of re-tracing.
    target_row = np.full((nrows, ncols), -1, dtype=np.int32)
    target_col = np.full((nrows, ncols), -1, dtype=np.int32)

    print("[*] Tracing every cell downstream to its nearest stream cell (D8, memoized) ...")
    for r0 in range(nrows):
        for c0 in range(ncols):
            if is_stream[r0, c0]:
                target_row[r0, c0] = r0
                target_col[r0, c0] = c0
                continue
            if target_row[r0, c0] != -1:
                continue  # already resolved via a previous trace merging in

            path = []
            r, c = r0, c0
            steps = 0
            while steps < 2000:
                if target_row[r, c] != -1:
                    tr, tc = target_row[r, c], target_col[r, c]
                    break
                if is_stream[r, c]:
                    tr, tc = r, c
                    break
                path.append((r, c))
                d = flowdir[r, c]
                if d not in D8_OFFSETS or d <= 0:
                    tr, tc = -1, -1
                    break
                dr, dc = D8_OFFSETS[d]
                nr, nc = r + dr, c + dc
                if not (0 <= nr < nrows and 0 <= nc < ncols):
                    tr, tc = -1, -1
                    break
                r, c = nr, nc
                steps += 1
            else:
                tr, tc = -1, -1  # exceeded max steps (likely a flow-direction loop)

            for pr, pc in path:
                target_row[pr, pc] = tr
                target_col[pr, pc] = tc
        if r0 % 50 == 0:
            print(f"    row {r0}/{nrows} ...")

    resolved = target_row != -1
    print(f"[OK] {resolved.sum():,}/{elv.size:,} cells resolved to a stream cell "
          f"({(~resolved).sum():,} never reached one within the raster - edge effects)")

    # HAND and water-level-of-target, for every resolved cell
    hand = np.full((nrows, ncols), np.nan)
    threshold = np.full((nrows, ncols), np.nan)
    rr, cc = np.where(resolved)
    tr, tc = target_row[rr, cc], target_col[rr, cc]
    hand[rr, cc] = elv[rr, cc] - elv[tr, tc]
    threshold[rr, cc] = water_level[tr, tc]

    floodplain = np.where(resolved & (hand <= threshold), 1, np.where(resolved, 0, np.nan))

    n_fp = np.nansum(floodplain == 1)
    n_not_fp = np.nansum(floodplain == 0)
    print(f"\n[RESULT] Floodplain cells: {n_fp:,.0f}  |  Not floodplain: {n_not_fp:,.0f}  "
          f"|  Floodplain share of resolved area: {n_fp / (n_fp + n_not_fp) * 100:.1f}%")

    # Save floodplain raster
    out_path = os.path.join(BASE_DIR, "DEM", "chimanimani_selfcomputed_floodplain.tif")
    with rasterio.open(
        out_path, "w", driver="GTiff", height=nrows, width=ncols, count=1,
        dtype=np.float32, crs=crs, transform=transform, nodata=np.nan,
    ) as dst:
        dst.write(floodplain.astype(np.float32), 1)
    print(f"[OK] Saved -> {out_path}")

    # Flood-depth proxy for damage estimation: depth below the plausible flood
    # surface = water level of the nearest stream cell - HAND. This is a
    # geomorphic proxy (the Leopold water level is a bankfull-scale stage), NOT a
    # simulated depth for any specific event.
    depth = np.where(floodplain == 1, np.clip(threshold - hand, 0, None), np.nan)
    depth_path = os.path.join(BASE_DIR, "DEM", "chimanimani_selfcomputed_flood_depth.tif")
    with rasterio.open(
        depth_path, "w", driver="GTiff", height=nrows, width=ncols, count=1,
        dtype=np.float32, crs=crs, transform=transform, nodata=np.nan,
    ) as dst:
        dst.write(depth.astype(np.float32), 1)
    print(f"[OK] Saved depth proxy -> {depth_path} "
          f"(floodplain depth: median {np.nanmedian(depth):.2f} m, max {np.nanmax(depth):.2f} m)")

    # ------------------------------------------------------------------
    # Sample at our 343 crossings - the actual test that matters
    # ------------------------------------------------------------------
    crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings_distinct.gpkg"))
    with rasterio.open(out_path) as src:
        data = src.read(1)
        vals = []
        for pt in crossings.geometry:
            r, c = rasterio.transform.rowcol(src.transform, pt.x, pt.y)
            v = data[r, c] if (0 <= r < data.shape[0] and 0 <= c < data.shape[1]) else np.nan
            vals.append(v)
    crossings["selfcomputed_floodplain"] = vals
    n_valid_crossings = crossings["selfcomputed_floodplain"].notna().sum()
    n_fp_crossings = (crossings["selfcomputed_floodplain"] == 1).sum()
    print(f"\n[COMPARISON vs GFPLAIN250m] Our version: {n_valid_crossings}/{len(crossings)} crossings "
          f"classified at all ({n_fp_crossings} as floodplain) - GFPLAIN250m gave 0/462 (100% nodata)")

    print("\nTop 15 crossings by flood risk index - does self-computed floodplain agree?")
    top15 = crossings.sort_values("flood_risk_index", ascending=False).head(15)
    print(top15[["status", "highway", "waterway", "upa_km2", "flood_risk_index", "selfcomputed_floodplain"]]
          .to_string(index=False))

    crossings.to_csv(os.path.join(EXTRACT_DIR, "chimanimani_crossings_with_selfcomputed_floodplain.csv"), index=False)
    print(f"\n[OK] Comparison saved")


if __name__ == "__main__":
    main()
