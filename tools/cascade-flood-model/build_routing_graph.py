"""
Build a routable networkx graph from the Chimanimani road extract, weighted by
travel time (minutes), using the same road-type/surface speed tables as the
PRECISE pipeline (Popullation Weighted Metrics 2026.py). Then run a baseline
sanity-check: travel time from each health facility to every other facility.

This graph is the base we'll knock edges out of for flood-disruption scenarios.
"""
import os
import pickle
import pyrosm
import geopandas as gpd
import networkx as nx
import numpy as np
from scipy.spatial import cKDTree

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PBF_PATH = os.path.join(BASE_DIR, "PBFs", "zimbabwe-latest.osm.pbf")
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]

# --- Same speed tables as Popullation Weighted Metrics 2026.py ---
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


def get_effective_speed(highway_type, surface):
    base_speed = SPEED_LIMITS_BY_TYPE.get(highway_type, 30)
    if surface and str(surface) != "nan":
        factor = SURFACE_SPEED_FACTORS.get(str(surface).lower(), 0.7)
    else:
        if highway_type in ['track', 'path', 'unclassified', 'residential']:
            factor = 0.5
        elif highway_type in ['motorway', 'trunk', 'primary']:
            factor = 1.0
        else:
            factor = 0.7
    return max(base_speed * factor, 5)


def main():
    print("[*] Loading roads + node topology for Chimanimani bbox ...")
    osm = pyrosm.OSM(PBF_PATH, bounding_box=CHIMANIMANI_BBOX)
    nodes, edges = osm.get_network(
        nodes=True, network_type="all",
        extra_attributes=["surface", "bridge", "ford", "layer"]
    )
    print(f"[OK] {len(nodes):,} nodes, {len(edges):,} edges")

    print("[*] Building networkx graph with travel-time edge weights ...")
    G = osm.to_graph(nodes, edges, graph_type="networkx")

    # Add travel_time_min to every edge based on highway type + surface
    n_missing_length = 0
    for u, v, k, data in G.edges(keys=True, data=True):
        highway = data.get("highway", "road")
        if isinstance(highway, list):
            highway = highway[0]
        surface = data.get("surface")
        if isinstance(surface, list):
            surface = surface[0]
        length_km = data.get("length", 0) / 1000.0
        if length_km <= 0:
            n_missing_length += 1
            length_km = 0.01  # fallback ~10m, avoid zero-weight edges
        speed_kmh = get_effective_speed(highway, surface)
        travel_time_min = (length_km / speed_kmh) * 60
        data["travel_time_min"] = travel_time_min

    print(f"[OK] Graph built: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges "
          f"({n_missing_length} edges had missing length, used fallback)")

    n_components = nx.number_weakly_connected_components(G)
    largest_cc_size = len(max(nx.weakly_connected_components(G), key=len))
    print(f"[i] Weakly connected components: {n_components} "
          f"(largest has {largest_cc_size:,}/{G.number_of_nodes():,} nodes)")

    # Save graph for reuse in the flood-scenario script
    graph_path = os.path.join(EXTRACT_DIR, "chimanimani_road_graph.gpickle")
    with open(graph_path, "wb") as f:
        pickle.dump(G, f)
    print(f"[OK] Graph saved -> {graph_path}")

    # --- Baseline sanity check: travel time between every pair of facilities ---
    facilities = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_health_facilities.gpkg"))

    node_ids = list(G.nodes())
    node_coords = np.array([[G.nodes[n]["x"], G.nodes[n]["y"]] for n in node_ids])
    tree = cKDTree(node_coords)

    def nearest_node(lon, lat):
        _, idx = tree.query([lon, lat])
        return node_ids[idx]

    facilities["nearest_node"] = facilities.geometry.apply(
        lambda geom: nearest_node(geom.x, geom.y)
    )

    print("\n[*] Baseline travel time (minutes) between facilities, via road network:")
    names = facilities["name"].fillna("(unnamed)").tolist()
    fac_nodes = facilities["nearest_node"].tolist()

    header = "                              " + "".join(f"{n[:12]:>14s}" for n in names)
    print(header)
    for i, (name_i, node_i) in enumerate(zip(names, fac_nodes)):
        row = f"{name_i[:28]:28s}  "
        for j, (name_j, node_j) in enumerate(zip(names, fac_nodes)):
            if i == j:
                row += f"{'--':>14s}"
                continue
            try:
                t = nx.shortest_path_length(G, node_i, node_j, weight="travel_time_min")
                row += f"{t:>14.1f}"
            except nx.NetworkXNoPath:
                row += f"{'NO PATH':>14s}"
        print(row)


if __name__ == "__main__":
    main()
