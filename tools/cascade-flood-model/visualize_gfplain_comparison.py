"""
Side-by-side proof: GFPLAIN250m (the pre-computed global product) vs. our
self-computed floodplain, same bbox, same scale - showing GFPLAIN returns
nothing here while our reimplementation gives full coverage.
"""
import os
import geopandas as gpd
import rasterio
from rasterio.windows import from_bounds
from rasterio.plot import show
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from matplotlib.colors import ListedColormap

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]

roads = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_roads.gpkg"))

fig, axes = plt.subplots(1, 2, figsize=(18, 11), dpi=150)

# --- Panel 1: GFPLAIN250m, same bbox ---
ax = axes[0]
with rasterio.open(os.path.join(BASE_DIR, "GFPLAIN", "GFPLAIN TIFF", "AF.TIF")) as src:
    west, south, east, north = CHIMANIMANI_BBOX
    window = from_bounds(west, south, east, north, src.transform)
    data = src.read(1, window=window)
    win_transform = src.window_transform(window)
    # 255 = nodata in GFPLAIN's encoding
    masked = np.ma.masked_equal(data, 255)
    cmap = ListedColormap(["#4a90d9"])  # would be floodplain if any existed
    show(masked, transform=win_transform, ax=ax, cmap=cmap, vmin=0, vmax=1)
    n_valid = (data != 255).sum()
roads.plot(ax=ax, color="#999999", linewidth=0.3, zorder=2)
ax.set_facecolor("#f2f2ee")
ax.set_title(f"GFPLAIN250m (pre-computed global product)\n{n_valid} of {data.size:,} pixels classified — effectively none",
             fontsize=12, fontweight="bold", color="#a83e3e")
ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude"); ax.set_aspect("equal")

# --- Panel 2: our self-computed floodplain ---
ax2 = axes[1]
with rasterio.open(os.path.join(BASE_DIR, "DEM", "chimanimani_selfcomputed_floodplain.tif")) as src:
    data2 = src.read(1)
    cmap2 = ListedColormap(["#f2f2ee", "#4a90d9"])
    masked2 = np.ma.masked_invalid(data2)
    show(masked2, transform=src.transform, ax=ax2, cmap=cmap2, vmin=0, vmax=1)
    n_fp = np.nansum(data2 == 1)
    n_classified = np.nansum(~np.isnan(data2))
roads.plot(ax=ax2, color="#999999", linewidth=0.3, zorder=2)
ax2.set_title(f"Our self-computed floodplain (HAND + Leopold power law, MERIT Hydro 90m)\n"
              f"{n_classified:,.0f} of {data2.size:,} pixels classified ({n_fp:,.0f} as floodplain)",
              fontsize=12, fontweight="bold", color="#2f7d5e")
ax2.set_xlabel("Longitude"); ax2.set_ylabel("Latitude"); ax2.set_aspect("equal")

for a in (ax, ax2):
    a.set_xlim(CHIMANIMANI_BBOX[0], CHIMANIMANI_BBOX[2])
    a.set_ylim(CHIMANIMANI_BBOX[1], CHIMANIMANI_BBOX[3])

legend = [mpatches.Patch(color="#4a90d9", label="Floodplain (where classified)")]
fig.legend(handles=legend, loc="lower center", ncol=1, fontsize=10, bbox_to_anchor=(0.5, -0.02))

fig.suptitle("Same bounding box, same resolution class, same method family — different result",
             fontsize=13, fontweight="bold")

plt.tight_layout(rect=[0, 0.03, 1, 0.95])
out_path = os.path.join(BASE_DIR, "gfplain_vs_selfcomputed_comparison.png")
plt.savefig(out_path, bbox_inches="tight")
print(f"[OK] Saved -> {out_path}")
print(f"GFPLAIN250m valid pixels: {n_valid}")
print(f"Self-computed valid pixels: {n_classified:,.0f} ({n_fp:,.0f} floodplain)")
