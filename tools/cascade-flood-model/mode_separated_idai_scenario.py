"""
Mode-separated population-weighted accessibility, baseline vs Idai flood
scenario, for the Chimanimani test area.

Fixes the mode-conflation issue in the earlier single-graph version: that
version applied a vehicle-style road-type speed table to EVERY road including
'path' (81% of segments here are path+track), implicitly assuming vehicles
travel footpaths at 10 km/h. This version builds two separate graphs:

  WALKING graph: every road segment is walkable, flat 5 km/h (Bohannon 1997 /
                 WHO standard - same assumption as the PRECISE pipeline).
  DRIVING graph: excludes highway=path (the only non-vehicle-passable type
                 present in this extract), speed from the road-type/surface
                 table as before.

Both graphs get the same Idai flood scenario (top-20 crossings by flood risk
index removed), then population-weighted nearest-facility time is computed
for each mode separately.
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PBF_PATH = os.path.join(BASE_DIR, "PBFs", "zimbabwe-latest.osm.pbf")
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
POP_RASTER_PATH = os.path.join(BASE_DIR, "Population", "zwe_pop_2026_CN_100m_R2025A_v1.tif")

CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]
H3_RESOLUTION = 9
TOP_N_FLOODED = 17  # top 5% of 343 distinct crossings (mapclassify.NaturalBreaks confirms
                     # a real gap in the risk distribution near this point, not a round number)
WALKING_SPEED_KMH = 5.0

# highway types that are NOT vehicle-passable (excluded from the driving graph)
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


# ---------------------------------------------------------------------------
# 1. Load road network + build both mode graphs
# ---------------------------------------------------------------------------
print("[*] Loading roads + node topology for Chimanimani bbox ...")
osm = pyrosm.OSM(PBF_PATH, bounding_box=CHIMANIMANI_BBOX)
nodes, edges = osm.get_network(
    nodes=True, network_type="all",
    extra_attributes=["surface", "bridge", "ford", "layer"]
)
G_all = osm.to_graph(nodes, edges, graph_type="networkx")
print(f"[OK] {G_all.number_of_nodes():,} nodes, {G_all.number_of_edges():,} edges loaded")


def highway_of(data):
    h = data.get("highway", "road")
    return h[0] if isinstance(h, list) else h


def surface_of(data):
    s = data.get("surface")
    return s[0] if isinstance(s, list) else s


print("[*] Building WALKING graph (flat 5 km/h, all road types) ...")
G_walk = G_all.copy()
for u, v, k, data in G_walk.edges(keys=True, data=True):
    length_km = max(data.get("length", 10) / 1000.0, 0.01)
    data["travel_time_min"] = (length_km / WALKING_SPEED_KMH) * 60

print("[*] Building DRIVING graph (excludes highway=path/footway/etc, road-type speeds) ...")
G_drive = G_all.copy()
drive_edges_to_remove = []
for u, v, k, data in G_drive.edges(keys=True, data=True):
    highway = highway_of(data)
    if highway in NON_DRIVEABLE_TYPES:
        drive_edges_to_remove.append((u, v, k))
        continue
    surface = surface_of(data)
    length_km = max(data.get("length", 10) / 1000.0, 0.01)
    speed = get_driving_speed(highway, surface)
    data["travel_time_min"] = (length_km / speed) * 60
G_drive.remove_edges_from(drive_edges_to_remove)
print(f"[OK] Driving graph: removed {len(drive_edges_to_remove)} non-driveable edges "
      f"({G_drive.number_of_edges():,} edges remain)")

for name, G in [("walking", G_walk), ("driving", G_drive)]:
    n_comp = nx.number_weakly_connected_components(G)
    largest = len(max(nx.weakly_connected_components(G), key=len)) if G.number_of_nodes() else 0
    print(f"    {name}: {n_comp} components, largest={largest:,}/{G.number_of_nodes():,} nodes")

# ---------------------------------------------------------------------------
# 2. Apply Idai flood scenario (same 20 crossings) to both graphs
# ---------------------------------------------------------------------------
print(f"\n[*] Applying Idai flood scenario (top {TOP_N_FLOODED} crossings by risk) to both graphs ...")
crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings_distinct.gpkg"))
flooded_crossings = crossings.sort_values("flood_risk_index", ascending=False).head(TOP_N_FLOODED)


def flood_graph(G):
    edge_list = list(G.edges(keys=True, data=True))
    edge_midpoints, edge_refs = [], []
    for u, v, k, data in edge_list:
        geom = data.get("geometry")
        if geom is not None:
            mid = geom.interpolate(0.5, normalized=True)
            edge_midpoints.append([mid.x, mid.y])
        else:
            ux, uy = G.nodes[u]["x"], G.nodes[u]["y"]
            vx, vy = G.nodes[v]["x"], G.nodes[v]["y"]
            edge_midpoints.append([(ux + vx) / 2, (uy + vy) / 2])
        edge_refs.append((u, v, k))
    tree = cKDTree(np.array(edge_midpoints))

    G_flooded = G.copy()
    flooded_set = set()
    for _, row in flooded_crossings.iterrows():
        pt = row.geometry.centroid
        _, idx = tree.query([pt.x, pt.y])
        flooded_set.add(edge_refs[idx])
    for u, v, k in flooded_set:
        if G_flooded.has_edge(u, v, k):
            G_flooded.remove_edge(u, v, k)
        if G_flooded.has_edge(v, u, k):
            G_flooded.remove_edge(v, u, k)
    return G_flooded, len(flooded_set)


G_walk_flooded, n_walk = flood_graph(G_walk)
G_drive_flooded, n_drive = flood_graph(G_drive)
print(f"[OK] Walking graph: {n_walk} edges knocked out. Driving graph: {n_drive} edges knocked out "
      f"(some flooded crossings may already be absent from the driving graph if they were path-only)")

# ---------------------------------------------------------------------------
# 3. H3 population grid (reused for both modes)
# ---------------------------------------------------------------------------
print("\n[*] Building H3 population grid ...")
west, south, east, north = CHIMANIMANI_BBOX
bbox_poly = Polygon([(west, south), (east, south), (east, north), (west, north)])
hexagons = h3.geo_to_cells(bbox_poly, res=H3_RESOLUTION)

hex_geoms, hex_ids = [], []
for hex_id in hexagons:
    boundary = h3.cell_to_boundary(hex_id)
    coords = [(lon, lat) for lat, lon in boundary]
    hex_geoms.append(Polygon(coords))
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
total_pop = populated_hex["population"].sum()
print(f"[OK] {len(populated_hex):,} populated hexagons, total population {total_pop:,.0f}")

# ---------------------------------------------------------------------------
# 4. Facilities: snap to nearest node in EACH graph separately
# ---------------------------------------------------------------------------
facilities = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_health_facilities.gpkg"))


def nearest_node_lookup(G):
    node_ids = list(G.nodes())
    coords = np.array([[G.nodes[n]["x"], G.nodes[n]["y"]] for n in node_ids])
    tree = cKDTree(coords)
    def lookup(lon, lat):
        _, idx = tree.query([lon, lat])
        return node_ids[idx]
    return lookup


def evaluate_mode(mode_name, G_base, G_flood):
    nn = nearest_node_lookup(G_base)
    fac_nodes = [nn(g.x, g.y) for g in facilities.geometry]

    base_time = nx.multi_source_dijkstra_path_length(G_base, sources=fac_nodes, weight="travel_time_min")
    flood_time = nx.multi_source_dijkstra_path_length(G_flood, sources=fac_nodes, weight="travel_time_min")

    hex_nodes = [nn(pt.x, pt.y) for pt in populated_hex["centroid"]]
    baseline_min = [base_time.get(n, np.nan) for n in hex_nodes]
    flooded_min = [flood_time.get(n, np.nan) for n in hex_nodes]

    df = populated_hex[["h3_id", "population"]].copy()
    df["baseline_min"] = baseline_min
    df["flooded_min"] = flooded_min
    df["cut_off"] = pd.notna(df["baseline_min"]) & pd.isna(df["flooded_min"])

    reachable_base = df.dropna(subset=["baseline_min"])
    pop_reachable = reachable_base["population"].sum()
    w_base = (reachable_base["baseline_min"] * reachable_base["population"]).sum() / pop_reachable

    reachable_both = df.dropna(subset=["baseline_min", "flooded_min"])
    w_flood = ((reachable_both["flooded_min"] * reachable_both["population"]).sum()
               / reachable_both["population"].sum()) if len(reachable_both) else np.nan

    pop_cutoff = df.loc[df["cut_off"], "population"].sum()
    pop_increased = reachable_both.loc[
        reachable_both["flooded_min"] > reachable_both["baseline_min"], "population"
    ].sum()

    print(f"\n--- {mode_name.upper()} ---")
    print(f"  Population reachable (baseline): {pop_reachable:,.0f} / {total_pop:,.0f}")
    print(f"  Pop-weighted avg time - baseline: {w_base:.1f} min")
    print(f"  Pop-weighted avg time - flooded:  {w_flood:.1f} min  "
          f"({(w_flood - w_base) / w_base * 100:+.1f}%)")
    print(f"  Population with increased time:   {pop_increased:,.0f} ({pop_increased / pop_reachable * 100:.1f}%)")
    print(f"  Population cut off entirely:      {pop_cutoff:,.0f} ({pop_cutoff / pop_reachable * 100:.1f}%)")

    df.to_csv(os.path.join(EXTRACT_DIR, f"idai_scenario_population_impact_{mode_name}.csv"), index=False)
    return {
        "mode": mode_name, "pop_reachable": pop_reachable,
        "baseline_min": w_base, "flooded_min": w_flood,
        "pct_change": (w_flood - w_base) / w_base * 100,
        "pop_increased": pop_increased, "pop_cutoff": pop_cutoff,
    }


print("\n" + "=" * 70)
print("MODE-SEPARATED POPULATION-WEIGHTED ACCESSIBILITY: BASELINE vs IDAI")
print("=" * 70)
results = []
results.append(evaluate_mode("walking", G_walk, G_walk_flooded))
results.append(evaluate_mode("driving", G_drive, G_drive_flooded))

summary_df = pd.DataFrame(results)
summary_path = os.path.join(EXTRACT_DIR, "idai_scenario_mode_summary.csv")
summary_df.to_csv(summary_path, index=False)
print(f"\n[OK] Summary saved -> {summary_path}")
