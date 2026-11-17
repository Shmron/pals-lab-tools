"""
Visualize the Chimanimani roads/waterways/crossings extract.
"""
import os
import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.lines as mlines

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")

roads = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_roads.gpkg"))
waterways = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_waterways.gpkg"))
crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings.gpkg"))

# Classify each crossing pair as bridge / ford / untagged
def classify(row):
    if row.get("bridge") not in (None, ""):
        return "bridge"
    if row.get("ford") not in (None, ""):
        return "ford"
    return "untagged"

crossings["status"] = crossings.apply(classify, axis=1)

# crossings.gpkg was built in UTM 36S (EPSG:32736) for the sjoin; reproject back to
# WGS84 so it lines up with roads/waterways, which are in EPSG:4326.
crossings = crossings.to_crs(roads.crs)

# Crossing pairs are line-intersections (roads x waterways) -> take representative points
crossing_points = crossings.copy()
crossing_points["geometry"] = crossing_points.geometry.centroid

fig, ax = plt.subplots(figsize=(12, 12), dpi=150)

roads.plot(ax=ax, color="#999999", linewidth=0.5, zorder=1, label="Roads")
waterways.plot(ax=ax, color="#4a90d9", linewidth=0.8, zorder=2, label="Waterways")

colors = {"bridge": "#2ca02c", "ford": "#ff7f0e", "untagged": "#d62728"}
sizes = {"bridge": 30, "ford": 40, "untagged": 18}
for status, color in colors.items():
    subset = crossing_points[crossing_points["status"] == status]
    if len(subset):
        subset.plot(ax=ax, color=color, markersize=sizes[status], zorder=3,
                    edgecolor="black", linewidth=0.3)

ax.set_title(
    "Chimanimani Test Area: Roads, Waterways & Road×Waterway Crossings\n"
    f"({len(roads):,} road segments, {len(waterways):,} waterway segments, {len(crossings):,} crossings)",
    fontsize=13, fontweight="bold"
)
ax.set_xlabel("Longitude")
ax.set_ylabel("Latitude")

legend_handles = [
    mlines.Line2D([], [], color="#999999", linewidth=1.5, label="Road"),
    mlines.Line2D([], [], color="#4a90d9", linewidth=1.5, label="Waterway (river/stream)"),
    mlines.Line2D([], [], color="w", marker="o", markerfacecolor="#2ca02c", markeredgecolor="black",
                  markersize=8, label=f"Crossing: bridge tagged (n={(crossings['status']=='bridge').sum()})"),
    mlines.Line2D([], [], color="w", marker="o", markerfacecolor="#ff7f0e", markeredgecolor="black",
                  markersize=9, label=f"Crossing: ford tagged (n={(crossings['status']=='ford').sum()})"),
    mlines.Line2D([], [], color="w", marker="o", markerfacecolor="#d62728", markeredgecolor="black",
                  markersize=6, label=f"Crossing: UNTAGGED (n={(crossings['status']=='untagged').sum()})"),
]
ax.legend(handles=legend_handles, loc="upper left", fontsize=9, framealpha=0.9)

ax.set_aspect("equal")
plt.tight_layout()

out_path = os.path.join(BASE_DIR, "chimanimani_roads_crossings_map.png")
plt.savefig(out_path, bbox_inches="tight")
print(f"[OK] Saved map -> {out_path}")
