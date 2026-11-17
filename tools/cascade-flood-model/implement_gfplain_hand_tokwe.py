"""
Self-computed floodplain (HAND + Leopold power law) for the Tokwe-Mukosi
bbox - same method as Chimanimani (implement_gfplain_hand.py), applied to
the 111 tributary crossings JRC doesn't resolve.
"""
import os
import numpy as np
import rasterio
import geopandas as gpd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MERIT_PATH = os.path.join(BASE_DIR, "DEM", "tokwe_merit_hydro.tif")
EXTRACT_DIR = os.path.join(BASE_DIR, "TokweMukosi_extract")

A_PARAM = 0.01
B_PARAM = 0.30
BL_TRESH_KM2 = 0.05

D8_OFFSETS = {
    1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
    16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1),
}


def main():
    with rasterio.open(MERIT_PATH) as src:
        elv = src.read(1).astype(np.float64)
        flowdir = src.read(2).astype(np.int32)
        upa = src.read(3).astype(np.float64)
        transform = src.transform
        crs = src.crs
        nrows, ncols = elv.shape

    print(f"[i] Grid: {nrows}x{ncols} ({nrows*ncols:,} cells), "
          f"elevation {np.nanmin(elv):.0f}-{np.nanmax(elv):.0f}m, "
          f"upa {np.nanmin(upa):.4f}-{np.nanmax(upa):.1f} km2")

    is_stream = upa >= BL_TRESH_KM2
    n_stream = is_stream.sum()
    print(f"[i] Stream network (upa >= {BL_TRESH_KM2} km2): {n_stream:,} cells "
          f"({n_stream / elv.size * 100:.1f}% of grid)")

    area_m2 = upa * 1e6
    water_level = A_PARAM * np.power(np.maximum(area_m2, 1.0), B_PARAM)

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
                continue

            path = []
            r, c = r0, c0
            steps = 0
            while steps < 3000:
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
                tr, tc = -1, -1

            for pr, pc in path:
                target_row[pr, pc] = tr
                target_col[pr, pc] = tc
        if r0 % 100 == 0:
            print(f"    row {r0}/{nrows} ...")

    resolved = target_row != -1
    print(f"[OK] {resolved.sum():,}/{elv.size:,} cells resolved to a stream cell")

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

    out_path = os.path.join(BASE_DIR, "DEM", "tokwe_selfcomputed_floodplain.tif")
    with rasterio.open(
        out_path, "w", driver="GTiff", height=nrows, width=ncols, count=1,
        dtype=np.float32, crs=crs, transform=transform, nodata=np.nan,
    ) as dst:
        dst.write(floodplain.astype(np.float32), 1)
    print(f"[OK] Saved -> {out_path}")

    # ------------------------------------------------------------------
    # Sample at the 114 crossings, split by JRC-valid (main river) vs
    # tributary (JRC nodata)
    # ------------------------------------------------------------------
    crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "tokwe_crossings_jrc.gpkg"))

    # upa_km2 / tributary_risk_index weren't saved back to this file earlier
    # (only the flooded subset was) - recompute inline
    upa_vals = []
    for pt in crossings.geometry:
        r, c = rasterio.transform.rowcol(transform, pt.x, pt.y)
        v = upa[r, c] if (0 <= r < upa.shape[0] and 0 <= c < upa.shape[1]) else np.nan
        upa_vals.append(v)
    crossings["upa_km2"] = upa_vals

    rain_path = os.path.join(BASE_DIR, "Rainfall", "tokwe_chirps_mar2026.tif")
    if os.path.exists(rain_path):
        with rasterio.open(rain_path) as rsrc:
            rain_band = rsrc.read(1)
            rain_vals = []
            for pt in crossings.geometry:
                r, c = rasterio.transform.rowcol(rsrc.transform, pt.x, pt.y)
                v = rain_band[r, c] if (0 <= r < rain_band.shape[0] and 0 <= c < rain_band.shape[1]) else np.nan
                rain_vals.append(v)
        crossings["mar2026_rainfall_mm"] = rain_vals
        crossings["tributary_risk_index"] = crossings["mar2026_rainfall_mm"] * crossings["upa_km2"]

    with rasterio.open(out_path) as src:
        data = src.read(1)
        vals = []
        for pt in crossings.geometry:
            r, c = rasterio.transform.rowcol(src.transform, pt.x, pt.y)
            v = data[r, c] if (0 <= r < data.shape[0] and 0 <= c < data.shape[1]) else np.nan
            vals.append(v)
    crossings["selfcomputed_floodplain"] = vals

    tributary = crossings[~crossings["jrc_valid"]]
    main_river = crossings[crossings["jrc_valid"]]

    print(f"\n[COMPARISON] Tributary crossings (JRC blind to these): "
          f"{tributary['selfcomputed_floodplain'].notna().sum()}/{len(tributary)} classified, "
          f"{(tributary['selfcomputed_floodplain'] == 1).sum()} as floodplain")
    print(f"[COMPARISON] Main-river crossings (JRC already covers these): "
          f"{main_river['selfcomputed_floodplain'].notna().sum()}/{len(main_river)} classified, "
          f"{(main_river['selfcomputed_floodplain'] == 1).sum()} as floodplain "
          f"({'all agree with JRC' if (main_river['selfcomputed_floodplain'] == 1).all() else 'partial agreement'})")

    print("\nTributary crossings ranked by our rainfall x drainage-area index - does self-computed floodplain agree?")
    top_tributary = tributary.sort_values("tributary_risk_index", ascending=False).head(15) \
        if "tributary_risk_index" in tributary.columns else tributary.sort_values("upa_km2", ascending=False).head(15)
    cols = [c for c in ["highway", "waterway", "upa_km2", "tributary_risk_index", "selfcomputed_floodplain"] if c in top_tributary.columns]
    print(top_tributary[cols].to_string(index=False))

    crossings.to_file(os.path.join(EXTRACT_DIR, "tokwe_crossings_with_selfcomputed_floodplain.gpkg"), driver="GPKG")
    print(f"\n[OK] Saved full comparison")


if __name__ == "__main__":
    main()
