"""
Simulate the Idai flood scenario: knock out the top-N highest flood-risk
crossings from the routing graph, then compare facility-to-facility travel
time before vs. after disruption.
"""
import os
import pickle
import geopandas as gpd
import networkx as nx
import numpy as np
from scipy.spatial import cKDTree

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")

TOP_N_FLOODED = 20  # adjustable

with open(os.path.join(EXTRACT_DIR, "chimanimani_road_graph.gpickle"), "rb") as f:
    G_baseline = pickle.load(f)

crossings = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_crossings_flood_risk.gpkg"))
crossings_sorted = crossings.sort_values("flood_risk_index", ascending=False)
flooded_crossings = crossings_sorted.head(TOP_N_FLOODED).copy()

print(f"[*] Simulating failure of the top {TOP_N_FLOODED} crossings by flood risk index")
print(flooded_crossings[["status", "highway", "waterway", "flood_risk_index"]].to_string(index=False))

# --- Build spatial index of graph edges (as line segments) to snap each flooded
# crossing point to the nearest edge, since crossings sit mid-segment, not at nodes ---
G_flooded = G_baseline.copy()

edge_list = list(G_baseline.edges(keys=True, data=True))
edge_midpoints = []
edge_refs = []
for u, v, k, data in edge_list:
    geom = data.get("geometry")
    if geom is not None:
        mid = geom.interpolate(0.5, normalized=True)
        edge_midpoints.append([mid.x, mid.y])
    else:
        # Fallback: straight line between node coords
        ux, uy = G_baseline.nodes[u]["x"], G_baseline.nodes[u]["y"]
        vx, vy = G_baseline.nodes[v]["x"], G_baseline.nodes[v]["y"]
        edge_midpoints.append([(ux + vx) / 2, (uy + vy) / 2])
    edge_refs.append((u, v, k))

edge_tree = cKDTree(np.array(edge_midpoints))

n_flooded_edges = 0
flooded_edge_set = set()
for _, row in flooded_crossings.iterrows():
    pt = row.geometry.centroid
    _, idx = edge_tree.query([pt.x, pt.y])
    u, v, k = edge_refs[idx]
    flooded_edge_set.add((u, v, k))
    n_flooded_edges += 1

# Knock out matched edges in both directions (undirected road = 2 directed edges in graph)
n_removed = 0
for u, v, k in flooded_edge_set:
    if G_flooded.has_edge(u, v, k):
        G_flooded.remove_edge(u, v, k)
        n_removed += 1
    if G_flooded.has_edge(v, u, k):
        G_flooded.remove_edge(v, u, k)
        n_removed += 1

print(f"\n[OK] Matched {n_flooded_edges} flooded crossings to {len(flooded_edge_set)} unique edges, "
      f"removed {n_removed} directed edges from graph")

n_components_after = nx.number_weakly_connected_components(G_flooded)
largest_cc_after = len(max(nx.weakly_connected_components(G_flooded), key=len))
print(f"[i] After flooding: {n_components_after} weakly connected components "
      f"(largest: {largest_cc_after:,}/{G_flooded.number_of_nodes():,} nodes)")

# --- Compare facility-to-facility travel time: baseline vs flooded ---
facilities = gpd.read_file(os.path.join(EXTRACT_DIR, "chimanimani_health_facilities.gpkg"))

node_ids = list(G_baseline.nodes())
node_coords = np.array([[G_baseline.nodes[n]["x"], G_baseline.nodes[n]["y"]] for n in node_ids])
tree = cKDTree(node_coords)

def nearest_node(lon, lat):
    _, idx = tree.query([lon, lat])
    return node_ids[idx]

facilities["nearest_node"] = facilities.geometry.apply(lambda geom: nearest_node(geom.x, geom.y))
names = facilities["name"].fillna("(unnamed)").tolist()
fac_nodes = facilities["nearest_node"].tolist()

print(f"\n[*] Facility-to-facility travel time: BASELINE vs FLOODED (minutes)\n")
results = []
for i, (name_i, node_i) in enumerate(zip(names, fac_nodes)):
    for j, (name_j, node_j) in enumerate(zip(names, fac_nodes)):
        if i >= j:
            continue
        try:
            t_base = nx.shortest_path_length(G_baseline, node_i, node_j, weight="travel_time_min")
        except nx.NetworkXNoPath:
            t_base = None
        try:
            t_flood = nx.shortest_path_length(G_flooded, node_i, node_j, weight="travel_time_min")
        except nx.NetworkXNoPath:
            t_flood = None

        if t_base is not None and t_flood is not None:
            delta = t_flood - t_base
            pct = (delta / t_base * 100) if t_base > 0 else float("nan")
            status = f"+{delta:.1f} min ({pct:+.0f}%)"
        elif t_base is not None and t_flood is None:
            status = "CUT OFF (no path)"
        else:
            status = "n/a"

        results.append((name_i, name_j, t_base, t_flood, status))
        print(f"  {name_i[:26]:26s} <-> {name_j[:26]:26s}  "
              f"baseline={t_base if t_base is None else round(t_base,1):>6}  "
              f"flooded={t_flood if t_flood is None else round(t_flood,1):>6}   {status}")

import pandas as pd
results_df = pd.DataFrame(results, columns=["facility_a", "facility_b", "baseline_min", "flooded_min", "change"])
out_path = os.path.join(EXTRACT_DIR, "idai_scenario_facility_impact.csv")
results_df.to_csv(out_path, index=False)
print(f"\n[OK] Results saved -> {out_path}")
