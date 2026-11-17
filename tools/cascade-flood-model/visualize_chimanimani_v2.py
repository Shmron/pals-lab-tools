"""
Chimanimani v2 accessibility maps - v3 fixes:
  - Cutoff markers: black (per request) with a white edge so they stay
    distinct from YlOrRd's own near-black dark-red high end.
  - Event window stated explicitly in the title (no more unlabeled "flood
    scenario" with no date).
  - The actual flooded crossings (the break points) are now plotted on the
    flooded/change panels - cyan X markers - so cause (a specific crossing
    failing) and effect (the cutoff zone / longer routes around it) are
    both visible on the same map, not just implied.
"""
import os
import geopandas as gpd
import pandas as pd
import rasterio
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from matplotlib.colors import TwoSlopeNorm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
EVENT_LABEL = "Idai flood scenario - crossings inside the self-computed floodplain, event window 13-19 Mar 2019"

roads = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_roads.gpkg"))
facilities = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_health_facilities.gpkg")).reset_index(drop=True)
facilities["fac_code"] = [f"F{i+1}" for i in range(len(facilities))]
walk = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_v2_walking.gpkg"))
drive = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_v2_driving.gpkg"))

# Facility closure: a facility whose own location falls inside the self-computed
# floodplain is dropped from the flooded scenario, not just its access roads.
with rasterio.open(os.path.join(BASE_DIR, "DEM", "chimanimani_selfcomputed_floodplain.tif")) as fp_src:
    at_risk = []
    for geom in facilities.geometry:
        row, col = fp_src.index(geom.x, geom.y)
        try:
            v = fp_src.read(1, window=((row, row + 1), (col, col + 1)))[0, 0]
        except Exception:
            v = 0
        at_risk.append(bool(v == 1))
facilities["at_risk"] = at_risk
closed_codes = facilities.loc[facilities["at_risk"], "fac_code"].tolist()
print(f"[i] Facility closure: {closed_codes if closed_codes else 'none'}")

crossings_gdf = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings_distinct.gpkg"))
floodplain_flags = pd.read_csv(os.path.join(EXTRACT_DIR, "chimanimani_crossings_with_selfcomputed_floodplain.csv"))[
    ["cluster_id", "selfcomputed_floodplain"]]
crossings_gdf = crossings_gdf.merge(floodplain_flags, on="cluster_id", how="left")
flooded_pts = crossings_gdf[crossings_gdf["selfcomputed_floodplain"] == 1].copy()
flooded_pts["geometry"] = flooded_pts.geometry.centroid
print(f"[i] {len(flooded_pts)} flooded crossing points to overlay")

for gdf in (walk, drive):
    gdf["delta_min"] = gdf["flooded_min"] - gdf["baseline_min"]

fig, axes = plt.subplots(2, 3, figsize=(22, 15), dpi=150)

mode_specs = [
    ("WALKING", walk, 90, axes[0]),
    ("DRIVING", drive, 40, axes[1]),
]


def add_facility_labels(ax, show_closure=False):
    for _, fac in facilities.iterrows():
        closed = show_closure and fac["at_risk"]
        color = "#888888" if closed else "blue"
        ax.scatter(fac.geometry.x, fac.geometry.y, color=color, s=55, marker="^",
                   edgecolor="white", linewidth=0.8, zorder=6)
        if closed:
            ax.scatter(fac.geometry.x, fac.geometry.y, color="red", s=90, marker="x",
                       linewidth=2.2, zorder=7)
        ax.annotate(fac["fac_code"], (fac.geometry.x, fac.geometry.y), fontsize=8,
                    fontweight="bold", color=color, xytext=(4, 4), textcoords="offset points", zorder=6)


def add_flooded_crossings(ax):
    ax.scatter(flooded_pts.geometry.x, flooded_pts.geometry.y, marker="x", color="#00b8d9",
               s=45, linewidth=1.6, zorder=5)


for mode_name, gdf, vmax_time, (ax_base, ax_flood, ax_delta) in mode_specs:
    # --- Baseline: continuous time. No cutoff markers, no flooded-crossing
    #     markers - the flood hasn't happened in this panel. ---
    roads.plot(ax=ax_base, color="#cccccc", linewidth=0.3, zorder=1)
    gdf.dropna(subset=["baseline_min"]).plot(
        ax=ax_base, column="baseline_min", cmap="YlOrRd", vmin=0, vmax=vmax_time, zorder=2,
        legend=True, legend_kwds={"label": "Travel time (min)", "shrink": 0.7})
    add_facility_labels(ax_base)
    ax_base.set_title(f"{mode_name} — Baseline (before any flood)", fontsize=11, fontweight="bold")

    # --- Flooded: continuous time + the actual break points + cutoff markers ---
    roads.plot(ax=ax_flood, color="#cccccc", linewidth=0.3, zorder=1)
    gdf.dropna(subset=["flooded_min"]).plot(
        ax=ax_flood, column="flooded_min", cmap="YlOrRd", vmin=0, vmax=vmax_time, zorder=2,
        legend=True, legend_kwds={"label": "Travel time (min)", "shrink": 0.7})
    cutoff = gdf[gdf["cut_off"]]
    if len(cutoff):
        cutoff.plot(ax=ax_flood, color="black", edgecolor="white", linewidth=0.6, zorder=4)
    add_flooded_crossings(ax_flood)
    add_facility_labels(ax_flood, show_closure=True)
    n_cutoff = int(gdf["cut_off"].sum())
    pop_cutoff = gdf.loc[gdf["cut_off"], "population"].sum()
    closure_note = f" | {', '.join(closed_codes)} closed" if closed_codes else ""
    ax_flood.set_title(f"{mode_name} — After flooding\n"
                        f"(black = {n_cutoff} hexes cut off, {pop_cutoff:,.0f} people | "
                        f"x = {len(flooded_pts)} failed crossings{closure_note})",
                        fontsize=11, fontweight="bold")

    # --- Change: flooded - baseline, diverging scale + break points ---
    roads.plot(ax=ax_delta, color="#dddddd", linewidth=0.3, zorder=1)
    reach_both = gdf.dropna(subset=["baseline_min", "flooded_min"])
    vmax_delta = max(reach_both["delta_min"].max(), 1) if len(reach_both) else 1
    norm = TwoSlopeNorm(vmin=0, vcenter=0.01, vmax=vmax_delta)
    reach_both.plot(ax=ax_delta, column="delta_min", cmap="Reds", norm=norm, zorder=2,
                     legend=True, legend_kwds={"label": "Added travel time (min)", "shrink": 0.7})
    if len(cutoff):
        cutoff.plot(ax=ax_delta, color="black", edgecolor="white", linewidth=0.6, zorder=4)
    add_flooded_crossings(ax_delta)
    add_facility_labels(ax_delta, show_closure=True)
    pop_increased = reach_both.loc[reach_both["delta_min"] > 0.01, "population"].sum()
    ax_delta.set_title(f"{mode_name} — Change due to flooding\n"
                        f"({pop_increased:,.0f} people with longer travel time)",
                        fontsize=11, fontweight="bold")

    for ax in (ax_base, ax_flood, ax_delta):
        ax.set_xlabel("Longitude", fontsize=8)
        ax.set_ylabel("Latitude", fontsize=8)
        ax.set_aspect("equal")
        ax.tick_params(labelsize=7)

fig.suptitle(
    f"Chimanimani Population-Weighted Accessibility: Baseline vs. {EVENT_LABEL}\n"
    "Driving includes informal tracks at reduced speed, not excluded",
    fontsize=12.5, fontweight="bold", y=0.998
)

legend_handles = [
    plt.Line2D([], [], color="w", marker="^", markerfacecolor="blue", markeredgecolor="white",
               markersize=8, label="Health facility (F1-F6)"),
    plt.Line2D([], [], color="w", marker="x", markeredgecolor="#00b8d9", markerfacecolor="#00b8d9",
               markersize=9, markeredgewidth=2, label="Flooded crossing (the actual break point)"),
    mpatches.Patch(facecolor="black", edgecolor="white", label="Cut off entirely (flooded scenario only - never in baseline)"),
    plt.Line2D([], [], color="w", marker="x", markeredgecolor="red", markerfacecolor="red",
               markersize=9, markeredgewidth=2.2, label="Facility itself closed (its own location is inside the floodplain)"),
]
fig.legend(handles=legend_handles, loc="lower center", ncol=2, fontsize=9.5, bbox_to_anchor=(0.5, -0.03))

plt.tight_layout(rect=[0, 0.02, 1, 0.965])
out_path = os.path.join(BASE_DIR, "chimanimani_v2_accessibility_map.png")
plt.savefig(out_path, bbox_inches="tight")
print(f"[OK] Saved -> {out_path}")
