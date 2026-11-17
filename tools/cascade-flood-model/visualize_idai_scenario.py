"""
Visualize the mode-separated Idai flood scenario results:
  Panel 1: baseline mode accessibility (driving-reachable vs walking-only)
  Panel 2: walking travel-time change (flooded - baseline), with cut-off hexes
"""
import os
import geopandas as gpd
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import TwoSlopeNorm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")

hex_gdf = gpd.read_file(os.path.join(EXTRACT_DIR, "idai_scenario_population_impact.gpkg"))
walk_df = pd.read_csv(os.path.join(EXTRACT_DIR, "idai_scenario_population_impact_walking.csv"))
drive_df = pd.read_csv(os.path.join(EXTRACT_DIR, "idai_scenario_population_impact_driving.csv"))

hex_gdf = hex_gdf.merge(walk_df[["h3_id", "baseline_min", "flooded_min", "cut_off"]],
                         on="h3_id", suffixes=("", "_walk"))
hex_gdf = hex_gdf.merge(drive_df[["h3_id", "baseline_min"]].rename(columns={"baseline_min": "drive_baseline_min"}),
                         on="h3_id", how="left")
hex_gdf["driving_reachable"] = hex_gdf["drive_baseline_min"].notna()
hex_gdf["walk_time_change"] = hex_gdf["flooded_min"] - hex_gdf["baseline_min"]

roads = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_roads.gpkg"))
facilities = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_health_facilities.gpkg"))
crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings_flood_risk.gpkg"))
flooded = crossings.sort_values("flood_risk_index", ascending=False).head(20).copy()
flooded["geometry"] = flooded.geometry.centroid

fig, axes = plt.subplots(1, 2, figsize=(20, 11), dpi=150)

# --- Panel 1: baseline mode accessibility ---
ax = axes[0]
roads.plot(ax=ax, color="#cccccc", linewidth=0.4, zorder=1)
hex_gdf[hex_gdf["driving_reachable"]].plot(ax=ax, color="#2ca02c", alpha=0.5, zorder=2)
hex_gdf[~hex_gdf["driving_reachable"]].plot(ax=ax, color="#d62728", alpha=0.5, zorder=2)
facilities.plot(ax=ax, color="black", markersize=60, marker="^", zorder=4)
for _, row in facilities.iterrows():
    ax.annotate(row["name"].replace(" Health Center", "").replace(" Rural Hospital", " Hosp"),
                (row.geometry.x, row.geometry.y), fontsize=7, xytext=(3, 3), textcoords="offset points")

n_driving = hex_gdf["driving_reachable"].sum()
n_walking_only = (~hex_gdf["driving_reachable"]).sum()
pop_driving = hex_gdf.loc[hex_gdf["driving_reachable"], "population"].sum()
pop_walking_only = hex_gdf.loc[~hex_gdf["driving_reachable"], "population"].sum()

ax.set_title(
    "Baseline Mode Accessibility\n"
    f"Driving-reachable: {pop_driving:,.0f} people ({n_driving} hexes)  |  "
    f"Walking-only: {pop_walking_only:,.0f} people ({n_walking_only} hexes)",
    fontsize=11, fontweight="bold"
)
legend1 = [
    mpatches.Patch(color="#2ca02c", alpha=0.5, label="Driving-reachable"),
    mpatches.Patch(color="#d62728", alpha=0.5, label="Walking-only (no vehicle road)"),
    plt.Line2D([], [], color="black", marker="^", linestyle="", markersize=8, label="Health facility"),
]
ax.legend(handles=legend1, loc="upper left", fontsize=8)
ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude"); ax.set_aspect("equal")

# --- Panel 2: walking travel-time change under Idai scenario ---
ax = axes[1]
roads.plot(ax=ax, color="#dddddd", linewidth=0.4, zorder=1)

not_cutoff = hex_gdf[~hex_gdf["cut_off"]]
vmax = max(abs(not_cutoff["walk_time_change"].min()), abs(not_cutoff["walk_time_change"].max()), 1)
norm = TwoSlopeNorm(vmin=0, vcenter=0.01, vmax=vmax)
not_cutoff.plot(ax=ax, column="walk_time_change", cmap="Reds", norm=norm, zorder=2,
                 legend=True, legend_kwds={"label": "Walking time increase (min)", "shrink": 0.6})

cutoff = hex_gdf[hex_gdf["cut_off"]]
if len(cutoff):
    cutoff.plot(ax=ax, color="black", zorder=3, label="CUT OFF (no path)")

flooded.plot(ax=ax, color="blue", markersize=25, marker="x", zorder=5)
facilities.plot(ax=ax, color="black", markersize=60, marker="^", zorder=4)

pop_increased = not_cutoff.loc[not_cutoff["walk_time_change"] > 0, "population"].sum()
pop_cutoff = cutoff["population"].sum()
ax.set_title(
    "Idai Flood Scenario: Walking Time Impact\n"
    f"+time: {pop_increased:,.0f} people  |  cut off entirely: {pop_cutoff:,.0f} people  |  "
    f"x = flooded crossing (top 20 by risk)",
    fontsize=11, fontweight="bold"
)
legend2 = [
    mpatches.Patch(color="black", label=f"Cut off entirely (n={len(cutoff)} hexes)"),
    plt.Line2D([], [], color="blue", marker="x", linestyle="", markersize=8, label="Flooded crossing"),
    plt.Line2D([], [], color="black", marker="^", linestyle="", markersize=8, label="Health facility"),
]
ax.legend(handles=legend2, loc="upper left", fontsize=8)
ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude"); ax.set_aspect("equal")

plt.tight_layout()
out_path = os.path.join(BASE_DIR, "chimanimani_idai_scenario_map.png")
plt.savefig(out_path, bbox_inches="tight")
print(f"[OK] Saved -> {out_path}")
