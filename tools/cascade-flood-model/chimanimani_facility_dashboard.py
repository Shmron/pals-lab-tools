"""
Chimanimani Health Facilities Accessibility Dashboard (Driving Only) -
matches the format of District_Analysis_<X>_Health_Facilities.png outputs:
  1. Hex grid map, colored by nearest-facility assignment, alpha=population
  2. Facility catchment analysis (population served + grid count)
  3. Population-weighted distances per facility
  4. Population-weighted travel times per facility
  5. Efficiency scatter (time vs population served)
  6. Summary table

Uses our own routing graph (built from OSM, driving-mode-only, road-type/
surface speed table) rather than an external OSRM server - same underlying
physics, self-contained.
"""
import os
import pyrosm
import h3
import geopandas as gpd
import numpy as np
import pandas as pd
import networkx as nx
import rasterio
from rasterio.mask import mask
from shapely.geometry import Polygon
from scipy.spatial import cKDTree
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.table import Table

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PBF_PATH = os.path.join(BASE_DIR, "PBFs", "zimbabwe-latest.osm.pbf")
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
POP_RASTER_PATH = os.path.join(BASE_DIR, "Population", "zwe_pop_2026_CN_100m_R2025A_v1.tif")

CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]
H3_RESOLUTION = 9
NON_DRIVEABLE_TYPES = {"path", "footway", "steps", "pedestrian", "bridleway", "cycleway"}

SPEED_LIMITS_BY_TYPE = {
    'motorway': 120, 'motorway_link': 80, 'trunk': 100, 'trunk_link': 60,
    'primary': 80, 'primary_link': 50, 'secondary': 60, 'secondary_link': 40,
    'tertiary': 50, 'tertiary_link': 30, 'residential': 30, 'unclassified': 30,
    'service': 20, 'track': 20, 'path': 10, 'footway': 5, 'living_street': 20,
    'road': 30,
}
SURFACE_SPEED_FACTORS = {
    'paved': 1.0, 'asphalt': 1.0, 'concrete': 1.0, 'concrete:plates': 0.9,
    'concrete:lanes': 0.9, 'cobblestone': 0.7, 'sett': 0.7, 'unpaved': 0.5,
    'gravel': 0.5, 'fine_gravel': 0.6, 'compacted': 0.6, 'dirt': 0.4,
    'earth': 0.4, 'mud': 0.2, 'sand': 0.3, 'grass': 0.3, 'ground': 0.4,
}


def get_driving_speed(highway_type, surface):
    base_speed = SPEED_LIMITS_BY_TYPE.get(highway_type, 30)
    if surface and str(surface) != "nan":
        factor = SURFACE_SPEED_FACTORS.get(str(surface).lower(), 0.7)
    else:
        if highway_type in ['track', 'unclassified', 'residential']:
            factor = 0.5
        elif highway_type in ['motorway', 'trunk', 'primary']:
            factor = 1.0
        else:
            factor = 0.7
    return max(base_speed * factor, 5)


def highway_of(data):
    h = data.get("highway", "road")
    return h[0] if isinstance(h, list) else h


def surface_of(data):
    s = data.get("surface")
    return s[0] if isinstance(s, list) else s


# ---------------------------------------------------------------------------
# 1. Build driving graph
# ---------------------------------------------------------------------------
print("[*] Building driving graph ...")
osm = pyrosm.OSM(PBF_PATH, bounding_box=CHIMANIMANI_BBOX)
nodes, edges = osm.get_network(nodes=True, network_type="all", extra_attributes=["surface"])
G_all = osm.to_graph(nodes, edges, graph_type="networkx")

G_drive = G_all.copy()
remove_edges = []
for u, v, k, data in G_drive.edges(keys=True, data=True):
    highway = highway_of(data)
    if highway in NON_DRIVEABLE_TYPES:
        remove_edges.append((u, v, k))
        continue
    surface = surface_of(data)
    length_m = max(data.get("length", 10), 10)
    data["length_km"] = length_m / 1000.0
    speed = get_driving_speed(highway, surface)
    data["travel_time_min"] = (data["length_km"] / speed) * 60
G_drive.remove_edges_from(remove_edges)
print(f"[OK] Driving graph: {G_drive.number_of_nodes():,} nodes, {G_drive.number_of_edges():,} edges")

# ---------------------------------------------------------------------------
# 2. Facilities: snap to nearest node, run per-facility Dijkstra with paths
# ---------------------------------------------------------------------------
facilities = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_health_facilities.gpkg")).reset_index(drop=True)
facilities["fac_code"] = [f"F{i+1}" for i in range(len(facilities))]

node_ids = list(G_drive.nodes())
node_coords = np.array([[G_drive.nodes[n]["x"], G_drive.nodes[n]["y"]] for n in node_ids])
node_tree = cKDTree(node_coords)

def nearest_node(lon, lat):
    _, idx = node_tree.query([lon, lat])
    return node_ids[idx]

facilities["nearest_node"] = facilities.geometry.apply(lambda g: nearest_node(g.x, g.y))

print("[*] Running per-facility Dijkstra (time + path) for nearest-facility assignment ...")
per_facility_time = {}
per_facility_dist = {}
for _, fac in facilities.iterrows():
    times, paths = nx.single_source_dijkstra(G_drive, fac["nearest_node"], weight="travel_time_min")
    dists = {}
    for target, path in paths.items():
        d = sum(G_drive[path[i]][path[i+1]][0].get("length_km", 0) for i in range(len(path) - 1))
        dists[target] = d
    per_facility_time[fac["fac_code"]] = times
    per_facility_dist[fac["fac_code"]] = dists

# Assign each node to its nearest facility (min travel time)
all_nodes = set(node_ids)
node_assignment = {}
for n in all_nodes:
    best_fac, best_time, best_dist = None, np.inf, np.nan
    for fac_code in facilities["fac_code"]:
        t = per_facility_time[fac_code].get(n)
        if t is not None and t < best_time:
            best_fac, best_time = fac_code, t
            best_dist = per_facility_dist[fac_code].get(n, np.nan)
    node_assignment[n] = (best_fac, best_time, best_dist)

# ---------------------------------------------------------------------------
# 3. H3 population grid + assignment
# ---------------------------------------------------------------------------
print("[*] Building H3 population grid ...")
west, south, east, north = CHIMANIMANI_BBOX
bbox_poly = Polygon([(west, south), (east, south), (east, north), (west, north)])
hexagons = h3.geo_to_cells(bbox_poly, res=H3_RESOLUTION)
hex_geoms, hex_ids = [], []
for hex_id in hexagons:
    boundary = h3.cell_to_boundary(hex_id)
    hex_geoms.append(Polygon([(lon, lat) for lat, lon in boundary]))
    hex_ids.append(hex_id)
hex_gdf = gpd.GeoDataFrame({"h3_id": hex_ids, "geometry": hex_geoms}, crs="EPSG:4326")
hex_gdf = hex_gdf[hex_gdf.intersects(bbox_poly)].reset_index(drop=True)

pop_raster = rasterio.open(POP_RASTER_PATH)
populations = []
for geom in hex_gdf.geometry:
    try:
        masked, _ = mask(pop_raster, [geom], crop=True)
        data = masked[0]
        if pop_raster.nodata is not None:
            data = np.where(data == pop_raster.nodata, np.nan, data)
        pop_sum = np.nansum(data)
        populations.append(pop_sum if pop_sum > 0.1 else 0)
    except Exception:
        populations.append(0)
hex_gdf["population"] = populations
populated_hex = hex_gdf[hex_gdf["population"] > 0].copy()
populated_hex["centroid"] = populated_hex.geometry.to_crs("EPSG:32736").centroid.to_crs("EPSG:4326")

hex_nodes = [nearest_node(pt.x, pt.y) for pt in populated_hex["centroid"]]
assigned = [node_assignment.get(n, (None, np.nan, np.nan)) for n in hex_nodes]
populated_hex["assigned_facility"] = [a[0] for a in assigned]
populated_hex["driving_time_min"] = [a[1] for a in assigned]
populated_hex["driving_dist_km"] = [a[2] for a in assigned]

total_pop = populated_hex["population"].sum()
pop_unreachable = populated_hex.loc[populated_hex["assigned_facility"].isna(), "population"].sum()
print(f"[OK] {len(populated_hex):,} populated hexagons, {total_pop:,.0f} total population, "
      f"{pop_unreachable:,.0f} unreachable by driving")

# ---------------------------------------------------------------------------
# 4. Per-facility summary
# ---------------------------------------------------------------------------
summary = []
for _, fac in facilities.iterrows():
    code = fac["fac_code"]
    served = populated_hex[populated_hex["assigned_facility"] == code]
    pop_served = served["population"].sum()
    n_grids = len(served)
    if pop_served > 0:
        w_dist = (served["driving_dist_km"] * served["population"]).sum() / pop_served
        w_time = (served["driving_time_min"] * served["population"]).sum() / pop_served
    else:
        w_dist, w_time = np.nan, np.nan
    summary.append({
        "fac_code": code, "name": fac["name"], "grids": n_grids,
        "population": pop_served, "dist_km": w_dist, "time_min": w_time,
        "lon": fac.geometry.x, "lat": fac.geometry.y,
    })
summary_df = pd.DataFrame(summary)
print("\n" + summary_df.to_string(index=False))

summary_df.to_csv(os.path.join(EXTRACT_DIR, "chimanimani_facility_dashboard_summary.csv"), index=False)
populated_hex.drop(columns=["centroid"]).to_file(
    os.path.join(EXTRACT_DIR, "chimanimani_facility_assignment.gpkg"), driver="GPKG"
)

# ---------------------------------------------------------------------------
# 5. Dashboard
# ---------------------------------------------------------------------------
print("[*] Building dashboard ...")
roads = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_roads.gpkg"))
served_df = summary_df.dropna(subset=["time_min"]).reset_index(drop=True)
colors = plt.cm.tab10(np.linspace(0, 1, len(served_df)))
fac_color_map = dict(zip(served_df["fac_code"], colors))

fig = plt.figure(figsize=(20, 16), dpi=150)
gs = fig.add_gridspec(3, 3, height_ratios=[1.3, 1, 0.5], hspace=0.35, wspace=0.3)

# --- Panel 1: hex assignment map ---
ax1 = fig.add_subplot(gs[0, :2])
roads.plot(ax=ax1, color="#bbbbbb", linewidth=0.3, zorder=1)
max_pop = populated_hex["population"].max()
for code, color in fac_color_map.items():
    subset = populated_hex[populated_hex["assigned_facility"] == code]
    if len(subset):
        alphas = 0.25 + 0.65 * (subset["population"] / max_pop)
        for geom, a in zip(subset.geometry, alphas):
            gpd.GeoSeries([geom], crs="EPSG:4326").plot(ax=ax1, color=[color], alpha=min(a, 1.0), zorder=2)

for _, fac in summary_df.iterrows():
    size = 200 + (fac["grids"] * 15 if pd.notna(fac["grids"]) else 0)
    ax1.scatter(fac["lon"], fac["lat"], s=min(size, 900), color="white",
                edgecolor=fac_color_map.get(fac["fac_code"], "black"), linewidth=2, zorder=4)
    label = f"{fac['fac_code']}\n({int(fac['grids'])})" if pd.notna(fac["grids"]) else f"{fac['fac_code']}\n(0)"
    ax1.annotate(label, (fac["lon"], fac["lat"]), fontsize=7, ha="center", va="center", zorder=5)

legend_handles = [mpatches.Patch(color=fac_color_map[row["fac_code"]],
                                  label=f"{row['fac_code']}: {row['name'][:18]}")
                  for _, row in served_df.iterrows()]
ax1.legend(handles=legend_handles, loc="upper left", fontsize=7, title="Health Facilities", framealpha=0.9)
ax1.set_title("Chimanimani: Health Facility Grid Assignments (Driving Only)\n"
              "(Grid color = assigned facility, transparency = population density)",
              fontsize=11, fontweight="bold")
ax1.set_xlabel("Longitude"); ax1.set_ylabel("Latitude"); ax1.set_aspect("equal")

# --- Panel 2: catchment analysis (dual axis) ---
ax2 = fig.add_subplot(gs[0, 2])
ax2b = ax2.twinx()
bars = ax2.bar(served_df["fac_code"], served_df["population"], color="skyblue")
ax2.set_ylabel("Population Served", color="skyblue")
for bar, val in zip(bars, served_df["population"]):
    ax2.text(bar.get_x() + bar.get_width()/2, val, f"{val:,.0f}", ha="center", va="bottom", fontsize=7)
ax2b.plot(served_df["fac_code"], served_df["grids"], color="red", marker="o")
ax2b.set_ylabel("Number of Grids", color="red")
ax2.set_title("Facility Catchment Analysis", fontsize=11, fontweight="bold")
ax2.tick_params(axis="x", rotation=0, labelsize=7)

# --- Panel 3: pop-weighted distance ---
ax3 = fig.add_subplot(gs[1, 0])
bars = ax3.bar(served_df["fac_code"], served_df["dist_km"], color="#4a90a4")
for bar, val in zip(bars, served_df["dist_km"]):
    ax3.text(bar.get_x() + bar.get_width()/2, val, f"{val:.1f}", ha="center", va="bottom", fontsize=8)
ax3.set_title("Population-Weighted Distances (Driving)", fontsize=10, fontweight="bold")
ax3.set_ylabel("Pop-Weighted Distance (km)")

# --- Panel 4: pop-weighted time ---
ax4 = fig.add_subplot(gs[1, 1])
bars = ax4.bar(served_df["fac_code"], served_df["time_min"], color="#e88a8a")
for bar, val in zip(bars, served_df["time_min"]):
    ax4.text(bar.get_x() + bar.get_width()/2, val, f"{val:.1f}", ha="center", va="bottom", fontsize=8)
ax4.set_title("Population-Weighted Travel Times (Driving)", fontsize=10, fontweight="bold")
ax4.set_ylabel("Pop-Weighted Time (minutes)")

# --- Panel 5: efficiency scatter ---
ax5 = fig.add_subplot(gs[1, 2])
for _, row in served_df.iterrows():
    ax5.scatter(row["time_min"], row["population"], color=fac_color_map[row["fac_code"]], s=60, edgecolor="black")
    ax5.annotate(row["fac_code"], (row["time_min"], row["population"]), fontsize=8,
                 xytext=(4, 4), textcoords="offset points")
ax5.set_title("Driving Facility Efficiency\n(Lower-left = Better)", fontsize=10, fontweight="bold")
ax5.set_xlabel("Pop-Weighted Driving Time (min)"); ax5.set_ylabel("Population Served")

# --- Panel 6: summary table ---
ax6 = fig.add_subplot(gs[2, :])
ax6.axis("off")
table_df = served_df[["fac_code", "name", "grids", "population", "dist_km", "time_min"]].copy()
table_df.columns = ["Facility", "Name", "Grids Served", "Population Served", "Driving (km)", "Driving (min)"]
table_df["Population Served"] = table_df["Population Served"].map("{:,.0f}".format)
table_df["Driving (km)"] = table_df["Driving (km)"].map("{:.1f}".format)
table_df["Driving (min)"] = table_df["Driving (min)"].map("{:.1f}".format)
tbl = ax6.table(cellText=table_df.values, colLabels=table_df.columns, loc="center", cellLoc="center")
tbl.auto_set_font_size(False)
tbl.set_fontsize(8)
tbl.scale(1, 1.6)
for j in range(len(table_df.columns)):
    tbl[0, j].set_facecolor("#4a6fa5")
    tbl[0, j].set_text_props(color="white", fontweight="bold")
ax6.set_title("Grid-to-Facility Assignment Results: Chimanimani - Health Facilities",
               fontsize=12, fontweight="bold", pad=15)

out_path = os.path.join(BASE_DIR, "chimanimani_facility_dashboard.png")
plt.savefig(out_path, bbox_inches="tight")
print(f"[OK] Saved -> {out_path}")
