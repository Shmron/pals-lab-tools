"""Tokwe-Mukosi v2 accessibility maps - v3 fixes, same as Chimanimani:
black cutoff (white edge), event window in title, flooded crossings shown."""
import os
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from matplotlib.colors import TwoSlopeNorm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "TokweMukosi_extract")
EVENT_LABEL = "2025/26 dam-spillage scenario - crossings inside the self-computed floodplain, flagged peak month March 2026"

roads = gpd.read_file(os.path.join(EXTRACT_DIR, "tokwe_roads.gpkg"))
facilities = gpd.read_file(os.path.join(EXTRACT_DIR, "tokwe_facilities_coded.gpkg"))
walk = gpd.read_file(os.path.join(EXTRACT_DIR, "tokwe_v2_walking.gpkg"))
drive = gpd.read_file(os.path.join(EXTRACT_DIR, "tokwe_v2_driving.gpkg"))

crossings_gdf = gpd.read_file(os.path.join(EXTRACT_DIR, "tokwe_crossings_with_selfcomputed_floodplain.gpkg"))
flooded_pts = crossings_gdf[crossings_gdf["selfcomputed_floodplain"] == 1].copy()
flooded_pts["geometry"] = flooded_pts.geometry.centroid
jrc_confirmed = flooded_pts[flooded_pts["jrc_valid"]]
print(f"[i] {len(flooded_pts)} flooded crossing points to overlay ({len(jrc_confirmed)} JRC-confirmed)")

for gdf in (walk, drive):
    gdf["delta_min"] = gdf["flooded_min"] - gdf["baseline_min"]

fig, axes = plt.subplots(2, 3, figsize=(22, 17), dpi=150)

mode_specs = [
    ("WALKING", walk, 650, axes[0]),
    ("DRIVING", drive, 300, axes[1]),
]


def add_facility_labels(ax):
    for _, fac in facilities.iterrows():
        ax.scatter(fac.geometry.x, fac.geometry.y, color="blue", s=55, marker="^",
                   edgecolor="white", linewidth=0.8, zorder=6)
        ax.annotate(fac["fac_code"], (fac.geometry.x, fac.geometry.y), fontsize=8,
                    fontweight="bold", color="blue", xytext=(4, 4), textcoords="offset points", zorder=6)


def add_flooded_crossings(ax):
    # Tributary crossings: cyan x. JRC-confirmed main-river crossings: larger red x (higher confidence).
    non_jrc = flooded_pts[~flooded_pts["jrc_valid"]]
    ax.scatter(non_jrc.geometry.x, non_jrc.geometry.y, marker="x", color="#00b8d9",
               s=40, linewidth=1.5, zorder=5)
    ax.scatter(jrc_confirmed.geometry.x, jrc_confirmed.geometry.y, marker="x", color="#d62728",
               s=90, linewidth=2.4, zorder=5)


for mode_name, gdf, vmax_time, (ax_base, ax_flood, ax_delta) in mode_specs:
    roads.plot(ax=ax_base, color="#cccccc", linewidth=0.3, zorder=1)
    gdf.dropna(subset=["baseline_min"]).plot(
        ax=ax_base, column="baseline_min", cmap="YlOrRd", vmin=0, vmax=vmax_time, zorder=2,
        legend=True, legend_kwds={"label": "Travel time (min)", "shrink": 0.7})
    add_facility_labels(ax_base)
    ax_base.set_title(f"{mode_name} — Baseline (before any flood)", fontsize=11, fontweight="bold")

    roads.plot(ax=ax_flood, color="#cccccc", linewidth=0.3, zorder=1)
    gdf.dropna(subset=["flooded_min"]).plot(
        ax=ax_flood, column="flooded_min", cmap="YlOrRd", vmin=0, vmax=vmax_time, zorder=2,
        legend=True, legend_kwds={"label": "Travel time (min)", "shrink": 0.7})
    cutoff = gdf[gdf["cut_off"]]
    if len(cutoff):
        cutoff.plot(ax=ax_flood, color="black", edgecolor="white", linewidth=0.6, zorder=4)
    add_flooded_crossings(ax_flood)
    add_facility_labels(ax_flood)
    n_cutoff = int(gdf["cut_off"].sum())
    pop_cutoff = gdf.loc[gdf["cut_off"], "population"].sum()
    ax_flood.set_title(f"{mode_name} — After flooding\n"
                        f"(black = {n_cutoff} hexes cut off, {pop_cutoff:,.0f} people | "
                        f"x = {len(flooded_pts)} failed crossings, {len(jrc_confirmed)} JRC-confirmed)",
                        fontsize=10.5, fontweight="bold")

    roads.plot(ax=ax_delta, color="#dddddd", linewidth=0.3, zorder=1)
    reach_both = gdf.dropna(subset=["baseline_min", "flooded_min"])
    vmax_delta = max(reach_both["delta_min"].max(), 1) if len(reach_both) else 1
    norm = TwoSlopeNorm(vmin=0, vcenter=0.01, vmax=vmax_delta)
    reach_both.plot(ax=ax_delta, column="delta_min", cmap="Reds", norm=norm, zorder=2,
                     legend=True, legend_kwds={"label": "Added travel time (min)", "shrink": 0.7})
    if len(cutoff):
        cutoff.plot(ax=ax_delta, color="black", edgecolor="white", linewidth=0.6, zorder=4)
    add_flooded_crossings(ax_delta)
    add_facility_labels(ax_delta)
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
    f"Tokwe-Mukosi Population-Weighted Accessibility: Baseline vs. {EVENT_LABEL}\n"
    "Driving includes informal tracks at reduced speed (not excluded)",
    fontsize=12.5, fontweight="bold", y=0.998
)

legend_handles = [
    plt.Line2D([], [], color="w", marker="^", markerfacecolor="blue", markeredgecolor="white",
               markersize=8, label="Health facility (F1-F4)"),
    plt.Line2D([], [], color="w", marker="x", markeredgecolor="#d62728", markerfacecolor="#d62728",
               markersize=10, markeredgewidth=2.4, label="JRC-confirmed break (main river)"),
    plt.Line2D([], [], color="w", marker="x", markeredgecolor="#00b8d9", markerfacecolor="#00b8d9",
               markersize=8, markeredgewidth=1.5, label="Tributary break (self-computed only)"),
    mpatches.Patch(facecolor="black", edgecolor="white", label="Cut off entirely"),
]
fig.legend(handles=legend_handles, loc="lower center", ncol=4, fontsize=9, bbox_to_anchor=(0.5, -0.01))

plt.tight_layout(rect=[0, 0.02, 1, 0.965])
out_path = os.path.join(BASE_DIR, "tokwe_v2_accessibility_map.png")
plt.savefig(out_path, bbox_inches="tight")
print(f"[OK] Saved -> {out_path}")
