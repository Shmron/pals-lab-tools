"""
Facility catchment shift: baseline vs flooded nearest-facility assignment,
side by side, for Chimanimani or Tokwe-Mukosi. Answers directly: does the
facility catchment change under flooding, and by how much per facility?
"""
import os
import sys
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

SITE = sys.argv[1] if len(sys.argv) > 1 else "chimanimani"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PBF_PATH = os.path.join(BASE_DIR, "PBFs", "zimbabwe-latest.osm.pbf")
POP_RASTER_PATH = os.path.join(BASE_DIR, "Population", "zwe_pop_2026_CN_100m_R2025A_v1.tif")

if SITE == "chimanimani":
    BBOX = [32.75, -19.95, 32.98, -19.65]
    EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
    FACILITIES_PATH = os.path.join(EXTRACT_DIR, "chimanimani_health_facilities.gpkg")
    ROADS_PATH = os.path.join(EXTRACT_DIR, "chimanimani_roads.gpkg")
    CROSSINGS_PATH = os.path.join(EXTRACT_DIR, "chimanimani_crossings_distinct.gpkg")
    CROSSINGS_FLAG_COL = "selfcomputed_floodplain"
    CROSSINGS_CSV = os.path.join(EXTRACT_DIR, "chimanimani_crossings_with_selfcomputed_floodplain.csv")
    EVENT_LABEL = "Idai scenario (13-19 Mar 2019)"
    OUT_PREFIX = "chimanimani"
    FLOODPLAIN_TIF = os.path.join(BASE_DIR, "DEM", "chimanimani_selfcomputed_floodplain.tif")
else:
    BBOX = [30.25, -21.45, 30.65, -20.85]
    EXTRACT_DIR = os.path.join(BASE_DIR, "TokweMukosi_extract")
    FACILITIES_PATH = os.path.join(EXTRACT_DIR, "tokwe_facilities_coded.gpkg")
    ROADS_PATH = os.path.join(EXTRACT_DIR, "tokwe_roads.gpkg")
    CROSSINGS_PATH = os.path.join(EXTRACT_DIR, "tokwe_crossings_with_selfcomputed_floodplain.gpkg")
    CROSSINGS_FLAG_COL = "selfcomputed_floodplain"
    CROSSINGS_CSV = None
    EVENT_LABEL = "2025/26 dam-spillage scenario (March 2026)"
    OUT_PREFIX = "tokwe"
    FLOODPLAIN_TIF = os.path.join(BASE_DIR, "DEM", "tokwe_selfcomputed_floodplain.tif")

H3_RESOLUTION = 9
NON_DRIVEABLE_TYPES = {"footway", "steps", "pedestrian", "bridleway", "cycleway"}

SPEED_LIMITS_BY_TYPE = {
    'motorway': 120, 'motorway_link': 80, 'trunk': 100, 'trunk_link': 60,
    'primary': 80, 'primary_link': 50, 'secondary': 60, 'secondary_link': 40,
    'tertiary': 50, 'tertiary_link': 30, 'residential': 30, 'unclassified': 30,
    'service': 20, 'track': 20, 'path': 8, 'living_street': 20, 'road': 30,
}
SURFACE_SPEED_FACTORS = {
    'paved': 1.0, 'asphalt': 1.0, 'concrete': 1.0, 'unpaved': 0.5, 'gravel': 0.5,
    'fine_gravel': 0.6, 'compacted': 0.6, 'dirt': 0.4, 'earth': 0.4, 'mud': 0.2,
    'sand': 0.3, 'grass': 0.3, 'ground': 0.4,
}


def get_driving_speed(highway_type, surface):
    base = SPEED_LIMITS_BY_TYPE.get(highway_type, 15)
    if surface and str(surface) != "nan":
        factor = SURFACE_SPEED_FACTORS.get(str(surface).lower(), 0.7)
    else:
        factor = 0.5 if highway_type in ['track', 'path', 'unclassified', 'residential'] else (
            1.0 if highway_type in ['motorway', 'trunk', 'primary'] else 0.7)
    return max(base * factor, 3)


def highway_of(d):
    h = d.get("highway", "road")
    return h[0] if isinstance(h, list) else h


def surface_of(d):
    s = d.get("surface")
    return s[0] if isinstance(s, list) else s


print(f"[*] Site: {SITE} — building driving graph ...")
osm = pyrosm.OSM(PBF_PATH, bounding_box=BBOX)
nodes, edges = osm.get_network(nodes=True, network_type="all", extra_attributes=["surface"])
G_all = osm.to_graph(nodes, edges, graph_type="networkx")

G_drive = G_all.copy()
remove = []
for u, v, k, data in G_drive.edges(keys=True, data=True):
    hw = highway_of(data)
    if hw in NON_DRIVEABLE_TYPES:
        remove.append((u, v, k)); continue
    length_km = max(data.get("length", 10), 10) / 1000.0
    data["length_km"] = length_km
    data["travel_time_min"] = (length_km / get_driving_speed(hw, surface_of(data))) * 60
G_drive.remove_edges_from(remove)
print(f"[OK] {G_drive.number_of_nodes():,} nodes, {G_drive.number_of_edges():,} edges")

# Flooded crossings
crossings = gpd.read_file(CROSSINGS_PATH)
if CROSSINGS_CSV:
    flag_df = pd.read_csv(CROSSINGS_CSV)
    crossings[CROSSINGS_FLAG_COL] = flag_df[CROSSINGS_FLAG_COL].values
flooded_crossings = crossings[crossings[CROSSINGS_FLAG_COL] == 1].copy()
print(f"[i] {len(flooded_crossings)} flooded crossings")

edge_list = list(G_drive.edges(keys=True, data=True))
mids, refs = [], []
for u, v, k, data in edge_list:
    geom = data.get("geometry")
    if geom is not None:
        m = geom.interpolate(0.5, normalized=True)
        mids.append([m.x, m.y])
    else:
        ux, uy = G_drive.nodes[u]["x"], G_drive.nodes[u]["y"]
        vx, vy = G_drive.nodes[v]["x"], G_drive.nodes[v]["y"]
        mids.append([(ux + vx) / 2, (uy + vy) / 2])
    refs.append((u, v, k))
tree = cKDTree(np.array(mids))
G_drive_flooded = G_drive.copy()
flooded_set = set()
for geom in flooded_crossings.geometry.centroid:
    _, idx = tree.query([geom.x, geom.y])
    flooded_set.add(refs[idx])
for u, v, k in flooded_set:
    if G_drive_flooded.has_edge(u, v, k): G_drive_flooded.remove_edge(u, v, k)
    if G_drive_flooded.has_edge(v, u, k): G_drive_flooded.remove_edge(v, u, k)
print(f"[OK] {len(flooded_set)} edges removed for flooded scenario")

# Facilities
facilities = gpd.read_file(FACILITIES_PATH).reset_index(drop=True)
if "fac_code" not in facilities.columns:
    facilities["fac_code"] = [f"F{i+1}" for i in range(len(facilities))]

# Facility closure: a facility whose own location falls inside the self-computed
# floodplain is itself at risk, not just its access roads - dropped from the
# candidate set in the flooded scenario (the original plan's "facility closure" barrier).
with rasterio.open(FLOODPLAIN_TIF) as fp_src:
    at_risk = []
    for geom in facilities.geometry:
        row, col = fp_src.index(geom.x, geom.y)
        try:
            v = fp_src.read(1, window=((row, row + 1), (col, col + 1)))[0, 0]
        except Exception:
            v = 0
        at_risk.append(bool(v == 1))
facilities["at_risk"] = at_risk
closed = facilities.loc[facilities["at_risk"], "fac_code"].tolist()
print(f"[i] Facility closure: {closed if closed else 'none'} at-risk in the flooded scenario")

# Population grid
west, south, east, north = BBOX
bbox_poly = Polygon([(west, south), (east, south), (east, north), (west, north)])
hexagons = h3.geo_to_cells(bbox_poly, res=H3_RESOLUTION)
hex_geoms, hex_ids = [], []
for hid in hexagons:
    boundary = h3.cell_to_boundary(hid)
    hex_geoms.append(Polygon([(lon, lat) for lat, lon in boundary]))
    hex_ids.append(hid)
hex_gdf = gpd.GeoDataFrame({"h3_id": hex_ids, "geometry": hex_geoms}, crs="EPSG:4326")
hex_gdf = hex_gdf[hex_gdf.intersects(bbox_poly)].reset_index(drop=True)

pop_raster = rasterio.open(POP_RASTER_PATH)
populations = []
for geom in hex_gdf.geometry:
    try:
        masked, _ = mask(pop_raster, [geom], crop=True)
        d = masked[0]
        if pop_raster.nodata is not None:
            d = np.where(d == pop_raster.nodata, np.nan, d)
        s = np.nansum(d)
        populations.append(s if s > 0.1 else 0)
    except Exception:
        populations.append(0)
hex_gdf["population"] = populations
populated_hex = hex_gdf[hex_gdf["population"] > 0].copy()
populated_hex["centroid"] = populated_hex.geometry.to_crs("EPSG:32736").centroid.to_crs("EPSG:4326")
print(f"[OK] {len(populated_hex):,} populated hexagons")


def nearest_node_lookup(G):
    ids = list(G.nodes())
    coords = np.array([[G.nodes[n]["x"], G.nodes[n]["y"]] for n in ids])
    tree_ = cKDTree(coords)
    def f(lon, lat):
        _, idx = tree_.query([lon, lat])
        return ids[idx]
    return f


def assign(G, label):
    nn = nearest_node_lookup(G)
    facilities["nearest_node"] = facilities.geometry.apply(lambda g: nn(g.x, g.y))
    candidates = facilities[~facilities["at_risk"]] if label == "flooded" else facilities
    per_fac_time = {}
    for _, fac in candidates.iterrows():
        per_fac_time[fac["fac_code"]] = nx.single_source_dijkstra_path_length(
            G, fac["nearest_node"], weight="travel_time_min")

    hex_nodes = [nn(pt.x, pt.y) for pt in populated_hex["centroid"]]
    assigned_fac, assigned_time = [], []
    for n in hex_nodes:
        best_fac, best_t = None, np.inf
        for code, times in per_fac_time.items():
            t = times.get(n)
            if t is not None and t < best_t:
                best_fac, best_t = code, t
        assigned_fac.append(best_fac)
        assigned_time.append(best_t if best_t < np.inf else np.nan)

    out = populated_hex[["h3_id", "population", "geometry"]].copy()
    out[f"assigned_{label}"] = assigned_fac
    out[f"time_{label}"] = assigned_time
    return out


print("[*] Assigning baseline catchments ...")
base_assign = assign(G_drive, "baseline")
print("[*] Assigning flooded catchments ...")
flood_assign = assign(G_drive_flooded, "flooded")

merged = base_assign.merge(
    flood_assign[["h3_id", "assigned_flooded", "time_flooded"]], on="h3_id"
)
merged["switched"] = (merged["assigned_baseline"] != merged["assigned_flooded"]) & merged["assigned_flooded"].notna()
merged["newly_unreachable"] = merged["assigned_baseline"].notna() & merged["assigned_flooded"].isna()

# Per-facility summary: population served, baseline vs flooded
rows = []
for _, fac in facilities.iterrows():
    code = fac["fac_code"]
    pop_base = merged.loc[merged["assigned_baseline"] == code, "population"].sum()
    pop_flood = merged.loc[merged["assigned_flooded"] == code, "population"].sum()
    rows.append({"fac_code": code, "name": fac.get("name", code),
                 "pop_baseline": pop_base, "pop_flooded": pop_flood,
                 "change": pop_flood - pop_base})
summary = pd.DataFrame(rows)
print("\n" + summary.to_string(index=False))

n_switched = merged["switched"].sum()
pop_switched = merged.loc[merged["switched"], "population"].sum()
n_unreach = merged["newly_unreachable"].sum()
pop_unreach = merged.loc[merged["newly_unreachable"], "population"].sum()
print(f"\n[i] {n_switched} hexes ({pop_switched:,.0f} people) switched to a DIFFERENT facility under flooding")
print(f"[i] {n_unreach} hexes ({pop_unreach:,.0f} people) lost access to ANY facility under flooding")

summary.to_csv(os.path.join(EXTRACT_DIR, f"{OUT_PREFIX}_catchment_shift_summary.csv"), index=False)
merged.drop(columns=["centroid"], errors="ignore").to_file(
    os.path.join(EXTRACT_DIR, f"{OUT_PREFIX}_catchment_shift.gpkg"), driver="GPKG")

# ---------------------------------------------------------------------------
# Visualization: baseline vs flooded assignment, side by side
# ---------------------------------------------------------------------------
roads = gpd.read_file(ROADS_PATH)
colors = plt.cm.tab10(np.linspace(0, 1, len(facilities)))
fac_color_map = dict(zip(facilities["fac_code"], colors))

fig, axes = plt.subplots(1, 2, figsize=(20, 11), dpi=150)

for ax, col, title in [(axes[0], "assigned_baseline", "BASELINE catchments"),
                         (axes[1], "assigned_flooded", f"FLOODED catchments — {EVENT_LABEL}")]:
    roads.plot(ax=ax, color="#bbbbbb", linewidth=0.3, zorder=1)
    max_pop = merged["population"].max()
    for code, color in fac_color_map.items():
        subset = merged[merged[col] == code]
        if len(subset):
            alphas = 0.25 + 0.65 * (subset["population"] / max_pop)
            for geom, a in zip(subset.geometry, alphas):
                gpd.GeoSeries([geom], crs="EPSG:4326").plot(ax=ax, color=[color], alpha=min(a, 1.0), zorder=2)
    if col == "assigned_flooded":
        unreach = merged[merged["newly_unreachable"]]
        if len(unreach):
            unreach.plot(ax=ax, color="black", edgecolor="white", linewidth=0.4, zorder=3)
    for _, fac in facilities.iterrows():
        ax.scatter(fac.geometry.x, fac.geometry.y, s=140, color="white",
                   edgecolor=fac_color_map[fac["fac_code"]], linewidth=2.2, zorder=5)
        ax.annotate(fac["fac_code"], (fac.geometry.x, fac.geometry.y), fontsize=8, fontweight="bold",
                    ha="center", va="center", zorder=6)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude"); ax.set_aspect("equal")

legend_handles = [mpatches.Patch(color=fac_color_map[row["fac_code"]],
                                  label=f"{row['fac_code']}: {str(row.get('name',''))[:18]}")
                  for _, row in facilities.iterrows()]
legend_handles.append(mpatches.Patch(facecolor="black", edgecolor="white", label="Lost access to any facility"))
fig.legend(handles=legend_handles, loc="lower center", ncol=len(legend_handles), fontsize=8, bbox_to_anchor=(0.5, -0.02))

fig.suptitle(
    f"{SITE.title()}: Facility Catchment Shift, Baseline vs. Flooded ({n_switched} hexes / {pop_switched:,.0f} people switched facility, "
    f"{n_unreach} hexes / {pop_unreach:,.0f} people lost access entirely)",
    fontsize=13, fontweight="bold"
)

plt.tight_layout(rect=[0, 0.04, 1, 0.94])
out_path = os.path.join(BASE_DIR, f"{OUT_PREFIX}_catchment_shift_map.png")
plt.savefig(out_path, bbox_inches="tight")
print(f"\n[OK] Saved -> {out_path}")
