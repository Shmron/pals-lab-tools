"""
Parameter sensitivity: how the self-computed floodplain changes with the
stream-initiation threshold (BL_TRESH_KM2 - the minimum upstream drainage
area a cell needs before it counts as a "stream" at all). Reuses the exact
HAND/Leopold algorithm from flood_accessibility_pipeline.compute_floodplain,
parameterized, run at two thresholds against the same cached MERIT Hydro
raster (Chimanimani pilot) - not a new dataset, same input, different knob.
"""
import numpy as np
import rasterio
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

A_PARAM = 0.01
B_PARAM = 0.30
D8_OFFSETS = {
    1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
    16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1),
}


def compute_floodplain(merit_tif, bl_tresh_km2):
    with rasterio.open(merit_tif) as src:
        elv = src.read(1).astype(np.float64)
        flowdir = src.read(2).astype(np.int32)
        upa = src.read(3).astype(np.float64)
        transform, crs = src.transform, src.crs
        nrows, ncols = elv.shape

    is_stream = upa >= bl_tresh_km2
    area_m2 = upa * 1e6
    water_level = A_PARAM * np.power(np.maximum(area_m2, 1.0), B_PARAM)

    target_row = np.full((nrows, ncols), -1, dtype=np.int32)
    target_col = np.full((nrows, ncols), -1, dtype=np.int32)
    for r0 in range(nrows):
        for c0 in range(ncols):
            if is_stream[r0, c0]:
                target_row[r0, c0] = r0; target_col[r0, c0] = c0
                continue
            if target_row[r0, c0] != -1:
                continue
            path = []
            r, c = r0, c0
            steps = 0
            while steps < 2000:
                if target_row[r, c] != -1:
                    tr, tc = target_row[r, c], target_col[r, c]; break
                if is_stream[r, c]:
                    tr, tc = r, c; break
                path.append((r, c))
                d = flowdir[r, c]
                if d not in D8_OFFSETS or d <= 0:
                    tr, tc = -1, -1; break
                dr, dc = D8_OFFSETS[d]
                nr, nc = r + dr, c + dc
                if not (0 <= nr < nrows and 0 <= nc < ncols):
                    tr, tc = -1, -1; break
                r, c = nr, nc; steps += 1
            else:
                tr, tc = -1, -1
            for pr, pc in path:
                target_row[pr, pc] = tr; target_col[pr, pc] = tc

    resolved = target_row != -1
    hand = np.full((nrows, ncols), np.nan)
    threshold = np.full((nrows, ncols), np.nan)
    rr, cc = np.where(resolved)
    tr, tc = target_row[rr, cc], target_col[rr, cc]
    hand[rr, cc] = elv[rr, cc] - elv[tr, tc]
    threshold[rr, cc] = water_level[tr, tc]
    floodplain = np.where(resolved & (hand <= threshold), 1, np.where(resolved, 0, np.nan))
    n_stream = int(is_stream.sum())
    pct = float(np.nansum(floodplain == 1) / resolved.sum() * 100)
    bounds = rasterio.transform.array_bounds(nrows, ncols, transform)
    return floodplain, pct, n_stream, bounds


MERIT_TIF = "DEM/chimanimani_merit_hydro.tif"

print("[*] Computing floodplain at BL_TRESH_KM2 = 0.01 (loose - almost every small gully counts as a stream) ...")
fp_loose, pct_loose, n_loose, bounds = compute_floodplain(MERIT_TIF, 0.01)
print(f"    {n_loose} stream cells, floodplain = {pct_loose:.1f}%")

print("[*] Computing floodplain at BL_TRESH_KM2 = 1.0 (strict - only large streams count, 20x the default) ...")
fp_strict, pct_strict, n_strict, _ = compute_floodplain(MERIT_TIF, 1.0)
print(f"    {n_strict} stream cells, floodplain = {pct_strict:.1f}%")

fig, axes = plt.subplots(1, 2, figsize=(13, 7.5))
for ax, fp, pct, n, label in zip(
        axes, [fp_loose, fp_strict], [pct_loose, pct_strict], [n_loose, n_strict],
        ["LOOSE: 0.01 km² threshold\n(catches nearly every gully)", "STRICT: 1.0 km² threshold\n(only substantial streams, 20× stricter)"]):
    masked = np.ma.masked_where(fp != 1, fp)
    ax.imshow(masked, extent=[bounds[0], bounds[2], bounds[1], bounds[3]], cmap="Blues", vmin=0, vmax=1)
    ax.set_title(f"{label}\n{n:,} stream cells → floodplain = {pct:.1f}% of area", fontsize=11, fontweight="bold")
    ax.set_xlabel("Longitude")
axes[0].set_ylabel("Latitude")
fig.suptitle("Chimanimani — same elevation data, same rain, different stream-initiation threshold",
             fontsize=13, fontweight="bold")
fig.tight_layout()
fig.savefig("floodplain_sensitivity_comparison.png", dpi=130)
print(f"\n[RESULT] Floodplain area: {pct_loose:.1f}% (loose) vs {pct_strict:.1f}% (strict) "
      f"-- a {pct_loose/pct_strict:.1f}x difference from one parameter")
print(f"[i] Current pipeline default (BL_TRESH_KM2=0.05) sits between these two, already reported at 21.4%")
