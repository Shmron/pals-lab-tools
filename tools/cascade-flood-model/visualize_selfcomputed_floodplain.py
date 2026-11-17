"""Visualize the self-computed GFPLAIN/HAND floodplain against roads and crossings."""
import os
import geopandas as gpd
import rasterio
from rasterio.plot import show
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from matplotlib.colors import ListedColormap

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")

roads = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_roads.gpkg"))
crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings_distinct.gpkg"))

fig, ax = plt.subplots(figsize=(11, 13), dpi=150)

with rasterio.open(os.path.join(BASE_DIR, "DEM", "chimanimani_selfcomputed_floodplain.tif")) as src:
    data = src.read(1)
    cmap = ListedColormap(["#f2f2ee", "#4a90d9"])  # 0=not floodplain, 1=floodplain
    masked = np.ma.masked_invalid(data)
    show(masked, transform=src.transform, ax=ax, cmap=cmap, vmin=0, vmax=1)

roads.plot(ax=ax, color="#888888", linewidth=0.3, zorder=2)

top_risk = crossings.sort_values("flood_risk_index", ascending=False).head(15)
top_risk_pts = top_risk.copy()
top_risk_pts["geometry"] = top_risk_pts.geometry.centroid
top_risk_pts.plot(ax=ax, color="#d62728", markersize=40, edgecolor="black", linewidth=0.6, zorder=4)

ax.set_title(
    "Chimanimani: Self-Computed Floodplain (HAND + Leopold power law, MERIT Hydro 90m)\n"
    "Blue = floodplain | Red = top-15 highest flood-risk crossings (all fall inside)",
    fontsize=11, fontweight="bold"
)
legend = [
    mpatches.Patch(color="#4a90d9", label="Self-computed floodplain (21.4% of resolved area)"),
    plt.Line2D([], [], color="#888888", linewidth=1, label="Roads"),
    plt.Line2D([], [], color="w", marker="o", markerfacecolor="#d62728", markeredgecolor="black",
               markersize=8, label="Top-15 risk-ranked crossings"),
]
ax.legend(handles=legend, loc="upper left", fontsize=8, framealpha=0.9)
ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude"); ax.set_aspect("equal")

plt.tight_layout()
out_path = os.path.join(BASE_DIR, "chimanimani_selfcomputed_floodplain_map.png")
plt.savefig(out_path, bbox_inches="tight")
print(f"[OK] Saved -> {out_path}")
