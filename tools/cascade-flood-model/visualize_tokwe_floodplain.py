"""Visualize the self-computed floodplain at Tokwe-Mukosi, vs JRC-confirmed crossings."""
import os
import geopandas as gpd
import rasterio
from rasterio.plot import show
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from matplotlib.colors import ListedColormap

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "TokweMukosi_extract")

roads = gpd.read_file(os.path.join(EXTRACT_DIR, "tokwe_roads.gpkg"))
crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "tokwe_crossings_with_selfcomputed_floodplain.gpkg"))

fig, ax = plt.subplots(figsize=(12, 14), dpi=150)

with rasterio.open(os.path.join(BASE_DIR, "DEM", "tokwe_selfcomputed_floodplain.tif")) as src:
    data = src.read(1)
    cmap = ListedColormap(["#f2f2ee", "#4a90d9"])
    masked = np.ma.masked_invalid(data)
    show(masked, transform=src.transform, ax=ax, cmap=cmap, vmin=0, vmax=1)

roads.plot(ax=ax, color="#999999", linewidth=0.25, zorder=2)

jrc_pts = crossings[crossings["jrc_valid"]].copy()
jrc_pts["geometry"] = jrc_pts.geometry.centroid
jrc_pts.plot(ax=ax, color="#2ca02c", markersize=70, edgecolor="black", linewidth=0.8, zorder=5,
             marker="^")

trib_fp = crossings[(~crossings["jrc_valid"]) & (crossings["selfcomputed_floodplain"] == 1)].copy()
trib_fp["geometry"] = trib_fp.geometry.centroid
trib_fp.plot(ax=ax, color="#d62728", markersize=18, edgecolor="black", linewidth=0.3, zorder=4)

ax.set_title(
    "Tokwe-Mukosi: Self-Computed Floodplain Extended to Tributaries\n"
    "Green triangles = JRC-confirmed main-river crossings (all inside floodplain) | "
    "Red dots = tributary crossings inside self-computed floodplain (52 of 111)",
    fontsize=10.5, fontweight="bold"
)
legend = [
    mpatches.Patch(color="#4a90d9", label="Self-computed floodplain (24.8% of resolved area)"),
    plt.Line2D([], [], color="w", marker="^", markerfacecolor="#2ca02c", markeredgecolor="black",
               markersize=9, label="JRC-confirmed crossing (3/3 agree)"),
    plt.Line2D([], [], color="w", marker="o", markerfacecolor="#d62728", markeredgecolor="black",
               markersize=7, label="Tributary crossing in floodplain (52/111)"),
]
ax.legend(handles=legend, loc="upper left", fontsize=8, framealpha=0.9)
ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude"); ax.set_aspect("equal")

plt.tight_layout()
out_path = os.path.join(BASE_DIR, "tokwe_selfcomputed_floodplain_map.png")
plt.savefig(out_path, bbox_inches="tight")
print(f"[OK] Saved -> {out_path}")
