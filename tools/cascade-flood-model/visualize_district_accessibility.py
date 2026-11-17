"""
District-scale accessibility map (driving only, for the case-study deck) -
reuses the pipeline's own Phase A cache + run_mean_travel_time(), then plots
baseline/flooded/change as a 3-panel hex map. Not part of the pipeline
itself (visualization only).
"""
import sys
sys.setrecursionlimit(100000)
# NOTE: flood_accessibility_pipeline (pulls in pyrosm/GDAL/GEOS via geopandas)
# must be imported BEFORE matplotlib on this machine - importing matplotlib
# first causes a native DLL-conflict segfault at import time (root-caused
# 2026-08-27, reproduced and fixed by reordering these two imports).
from flood_accessibility_pipeline import FloodAccessibilityPipeline
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

print("[*] Starting (unbuffered) ...", flush=True)

p = FloodAccessibilityPipeline("chimanimani_district")
print("[*] run_phase_a ...", flush=True); p.run_phase_a()
print("[*] build_graphs ...", flush=True); p.build_graphs()
print("[*] load_facilities ...", flush=True); p.load_facilities()
print("[*] build_population_grid ...", flush=True); p.build_population_grid()
print("[*] run_mean_travel_time ...", flush=True); p.run_mean_travel_time()
print("[*] done with pipeline stages", flush=True)

df = p._travel_time_df.copy()
hexes = p.populated_hex[["h3_id", "geometry"]].merge(df, on="h3_id")
print(f"[*] {len(hexes)} hexes ready for plotting", flush=True)

fig, axes = plt.subplots(1, 3, figsize=(16, 7))
vmax = np.nanpercentile(hexes["flooded_min"].dropna(), 95)

for ax, col, title in zip(axes, ["baseline_min", "flooded_min"], ["BASELINE (before flooding)", "FLOODED (after)"]):
    pass

hexes.plot(column="baseline_min", ax=axes[0], cmap="YlOrRd", markersize=1, vmin=0, vmax=vmax, legend=True,
           legend_kwds={"label": "Travel time (min)", "shrink": 0.7})
axes[0].set_title("BASELINE\n(before flooding)", fontweight="bold")

hexes.plot(column="flooded_min", ax=axes[1], cmap="YlOrRd", markersize=1, vmin=0, vmax=vmax, legend=True,
           legend_kwds={"label": "Travel time (min)", "shrink": 0.7})
cutoff = hexes[hexes["cut_off"]]
cutoff.plot(ax=axes[1], color="black", markersize=3, marker="x")
axes[1].set_title(f"FLOODED\n(after) — {int(hexes['cut_off'].sum())} hexes cut off entirely", fontweight="bold")

hexes["change"] = hexes["flooded_min"] - hexes["baseline_min"]
hexes.plot(column="change", ax=axes[2], cmap="Reds", markersize=1, vmin=0, legend=True,
           legend_kwds={"label": "Added minutes", "shrink": 0.7})
axes[2].set_title("CHANGE\n(added travel time)", fontweight="bold")

for ax in axes:
    ax.set_xlabel("Longitude")
    ax.set_xticklabels([])
    ax.set_yticklabels([])

fig.suptitle(f"Chimanimani District — driving accessibility, {p.travel_time_result['baseline_min']:.1f} → "
             f"{p.travel_time_result['flooded_min']:.1f} min ({p.travel_time_result['pct_change']:+.1f}%), "
             f"{p.travel_time_result['pop_cutoff']:,.0f} people cut off", fontsize=13, fontweight="bold")
fig.tight_layout()
fig.savefig("chimanimani_district_accessibility_map.png", dpi=130)
print("[OK] saved")
print(p.travel_time_result)
