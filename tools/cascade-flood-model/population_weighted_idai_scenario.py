"""
Population-weighted accessibility, baseline vs Idai flood scenario:
1. Build H3 (res 9) hexagon grid over the Chimanimani bbox - same resolution as
   the PRECISE pipeline (Popullation Weighted Metrics 2026.py).
2. Extract WorldPop 2026 population into each hexagon.
3. For every populated hexagon, find travel time to NEAREST facility, both on
   the baseline road graph and the Idai-flooded graph (top-20 crossings removed).
4. Population-weight the results: average travel time, population with
   increased travel time, population cut off entirely.
"""
import os
import pickle
import h3
import geopandas as gpd
import numpy as np
import pandas as pd
import networkx as nx
import rasterio
from rasterio.mask import mask
from shapely.geometry import Polygon
from scipy.spatial import cKDTree

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
POP_RASTER_PATH = os.path.join(BASE_DIR, "Population", "zwe_pop_2026_CN_100m_R2025A_v1.tif")

CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]  # west, south, east, north
H3_RESOLUTION = 9
TOP_N_FLOODED = 20

# ---------------------------------------------------------------------------
# 1. Build H3 grid over the bbox
# ---------------------------------------------------------------------------
print("[*] Building H3 (res 9) hexagon grid over Chimanimani bbox ...")
west, south, east, north = CHIMANIMANI_BBOX
bbox_poly = Polygon([(west, south), (east, south), (east, north), (west, north)])

h3_version = int(h3.__version__.split(".")[0])
hexagons = h3.geo_to_cells(bbox_poly, res=H3_RESOLUTION) if h3_version >= 4 else None

hex_geoms, hex_ids = [], []
for hex_id in hexagons:
    boundary = h3.cell_to_boundary(hex_id)  # [(lat, lon), ...]
    coords = [(lon, lat) for lat, lon in boundary]
    hex_geoms.append(Polygon(coords))
    hex_ids.append(hex_id)

hex_gdf = gpd.GeoDataFrame({"h3_id": hex_ids, "geometry": hex_geoms}, crs="EPSG:4326")
hex_gdf = hex_gdf[hex_gdf.intersects(bbox_poly)].reset_index(drop=True)
print(f"[OK] {len(hex_gdf):,} H3 hexagons covering the bbox")

# ---------------------------------------------------------------------------
# 2. Extract population per hexagon
# ---------------------------------------------------------------------------
print("[*] Extracting population per hexagon (WorldPop 2026) ...")
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
total_pop = populated_hex["population"].sum()
print(f"[OK] {len(populated_hex):,}/{len(hex_gdf):,} hexagons populated, "
      f"total population in bbox: {total_pop:,.0f}")

# ---------------------------------------------------------------------------
# 3. Load routing graphs (baseline + rebuild flooded version)
# ---------------------------------------------------------------------------
print("[*] Loading baseline routing graph and rebuilding flood scenario ...")
with open(os.path.join(EXTRACT_DIR, "chimanimani_road_graph.gpickle"), "rb") as f:
    G_baseline = pickle.load(f)

crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings_flood_risk.gpkg"))
flooded_crossings = crossings.sort_values("flood_risk_index", ascending=False).head(TOP_N_FLOODED)

edge_list = list(G_baseline.edges(keys=True, data=True))
edge_midpoints, edge_refs = [], []
for u, v, k, data in edge_list:
    geom = data.get("geometry")
    if geom is not None:
        mid = geom.interpolate(0.5, normalized=True)
        edge_midpoints.append([mid.x, mid.y])
    else:
        ux, uy = G_baseline.nodes[u]["x"], G_baseline.nodes[u]["y"]
        vx, vy = G_baseline.nodes[v]["x"], G_baseline.nodes[v]["y"]
        edge_midpoints.append([(ux + vx) / 2, (uy + vy) / 2])
    edge_refs.append((u, v, k))
edge_tree = cKDTree(np.array(edge_midpoints))

G_flooded = G_baseline.copy()
flooded_edge_set = set()
for _, row in flooded_crossings.iterrows():
    pt = row.geometry.centroid
    _, idx = edge_tree.query([pt.x, pt.y])
    flooded_edge_set.add(edge_refs[idx])

for u, v, k in flooded_edge_set:
    if G_flooded.has_edge(u, v, k):
        G_flooded.remove_edge(u, v, k)
    if G_flooded.has_edge(v, u, k):
        G_flooded.remove_edge(v, u, k)
print(f"[OK] Flood scenario graph built ({len(flooded_edge_set)} edges removed)")

# ---------------------------------------------------------------------------
# 4. Multi-source shortest path: time to NEAREST facility, for every node,
#    on both graphs (single Dijkstra pass each, much faster than per-facility)
# ---------------------------------------------------------------------------
facilities = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_health_facilities.gpkg"))

node_ids = list(G_baseline.nodes())
node_coords = np.array([[G_baseline.nodes[n]["x"], G_baseline.nodes[n]["y"]] for n in node_ids])
node_tree = cKDTree(node_coords)

def nearest_node(lon, lat):
    _, idx = node_tree.query([lon, lat])
    return node_ids[idx]

facilities["nearest_node"] = facilities.geometry.apply(lambda g: nearest_node(g.x, g.y))
facility_nodes = facilities["nearest_node"].tolist()

print("[*] Computing nearest-facility travel time for every node (baseline + flooded) ...")
baseline_time = nx.multi_source_dijkstra_path_length(
    G_baseline, sources=facility_nodes, weight="travel_time_min"
)
flooded_time = nx.multi_source_dijkstra_path_length(
    G_flooded, sources=facility_nodes, weight="travel_time_min"
)

# ---------------------------------------------------------------------------
# 5. Assign each populated hexagon its nearest-facility time, both scenarios
# ---------------------------------------------------------------------------
populated_hex["centroid"] = populated_hex.geometry.centroid
hex_nodes = [nearest_node(pt.x, pt.y) for pt in populated_hex["centroid"]]
populated_hex["nearest_node"] = hex_nodes

populated_hex["baseline_min"] = [baseline_time.get(n, np.nan) for n in hex_nodes]
populated_hex["flooded_min"] = [flooded_time.get(n, np.nan) for n in hex_nodes]
populated_hex["cut_off"] = populated_hex["baseline_min"].notna() & populated_hex["flooded_min"].isna()

# ---------------------------------------------------------------------------
# 6. Population-weighted summary
# ---------------------------------------------------------------------------
reachable_baseline = populated_hex.dropna(subset=["baseline_min"])
pop_weighted_baseline = (
    (reachable_baseline["baseline_min"] * reachable_baseline["population"]).sum()
    / reachable_baseline["population"].sum()
)

reachable_both = populated_hex.dropna(subset=["baseline_min", "flooded_min"])
pop_weighted_flooded = (
    (reachable_both["flooded_min"] * reachable_both["population"]).sum()
    / reachable_both["population"].sum()
) if len(reachable_both) else float("nan")

pop_cut_off = populated_hex.loc[populated_hex["cut_off"], "population"].sum()
pop_increased = reachable_both.loc[
    reachable_both["flooded_min"] > reachable_both["baseline_min"], "population"
].sum()
pop_total_reachable_baseline = reachable_baseline["population"].sum()

print("\n" + "=" * 70)
print("POPULATION-WEIGHTED ACCESSIBILITY: BASELINE vs IDAI FLOOD SCENARIO")
print("=" * 70)
print(f"Total population in bbox:                    {total_pop:>12,.0f}")
print(f"Population reachable to a facility (baseline): {pop_total_reachable_baseline:>12,.0f}")
print(f"Pop-weighted avg travel time - BASELINE:       {pop_weighted_baseline:>9.1f} min")
print(f"Pop-weighted avg travel time - FLOODED:        {pop_weighted_flooded:>9.1f} min")
print(f"Pop-weighted change:                           {pop_weighted_flooded - pop_weighted_baseline:>+9.1f} min "
      f"({(pop_weighted_flooded - pop_weighted_baseline) / pop_weighted_baseline * 100:+.1f}%)")
print(f"\nPopulation with INCREASED travel time:         {pop_increased:>12,.0f} "
      f"({pop_increased / pop_total_reachable_baseline * 100:.1f}% of reachable population)")
print(f"Population CUT OFF entirely (no path to any facility): {pop_cut_off:>12,.0f} "
      f"({pop_cut_off / pop_total_reachable_baseline * 100:.1f}% of reachable population)")
print("=" * 70)

# Save results
out_cols = ["h3_id", "population", "baseline_min", "flooded_min", "cut_off"]
out_path = os.path.join(EXTRACT_DIR, "idai_scenario_population_impact.csv")
populated_hex[out_cols].to_csv(out_path, index=False)
print(f"\n[OK] Per-hexagon results saved -> {out_path}")

out_gpkg = os.path.join(EXTRACT_DIR, "idai_scenario_population_impact.gpkg")
populated_hex.drop(columns=["centroid"]).to_file(out_gpkg, driver="GPKG")
print(f"[OK] Per-hexagon results (with geometry) -> {out_gpkg}")
