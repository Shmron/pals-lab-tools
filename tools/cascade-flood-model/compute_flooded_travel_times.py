import time
import numpy as np
import pandas as pd
import networkx as nx
import flood_accessibility_pipeline as pipe_mod
from flood_accessibility_pipeline import FloodAccessibilityPipeline

t0 = time.time()
pipe_mod.H3_RESOLUTION = 7  # match the cached baseline file's hex grid

p = FloodAccessibilityPipeline("harare")
print("[*] build_graphs() ...", flush=True)
t1 = time.time()
p.build_graphs()
print(f"[OK] build_graphs() took {time.time()-t1:.1f}s - baseline {p.G_baseline.number_of_nodes():,} nodes/"
      f"{p.G_baseline.number_of_edges():,} edges, flooded {p.G_flooded.number_of_nodes():,} nodes/"
      f"{p.G_flooded.number_of_edges():,} edges", flush=True)

print("[*] load_facilities() ...", flush=True)
p.load_facilities()
n_at_risk = int(p.facilities["at_risk"].sum())
print(f"[OK] {n_at_risk}/{len(p.facilities)} facilities at_risk", flush=True)

print("[*] build_population_grid() at H3 res 7 ...", flush=True)
t2 = time.time()
p.build_population_grid()
print(f"[OK] build_population_grid() took {time.time()-t2:.1f}s - {len(p.populated_hex):,} populated hexes", flush=True)

nn_flooded = p._nearest_node_lookup(p.G_flooded)
hex_nodes = [nn_flooded(pt.x, pt.y) for pt in p.populated_hex["centroid"]]

variants = [
    ("All facilities", None, None),
    ("Hospital", "amenity", "hospital"),
    ("Clinic", "amenity", "clinic"),
    ("Doctors", "amenity", "doctors"),
    ("Public", "classification", "Public"),
    ("Private", "classification", "Private"),
]

out = pd.DataFrame({"h3_id": p.populated_hex["h3_id"].values, "population": p.populated_hex["population"].values})

for col_name, field, val in variants:
    cand = p.facilities[~p.facilities["at_risk"]]
    if field is not None:
        cand = cand[cand[field] == val]
    print(f"[*] {col_name}: {len(cand)} open facilities (excluding at_risk) ...", flush=True)
    if len(cand) == 0:
        out[col_name] = np.nan
        print(f"[!] {col_name}: no open facilities of this type - all NaN", flush=True)
        continue
    src_nodes = [nn_flooded(g.x, g.y) for g in cand.geometry]
    times = nx.multi_source_dijkstra_path_length(p.G_flooded, sources=src_nodes, weight="travel_time_min")
    out[col_name] = [times.get(n, np.nan) for n in hex_nodes]
    reach = out[col_name].notna().sum()
    print(f"[OK] {col_name}: {reach}/{len(out)} hexes reach a facility in the flooded scenario", flush=True)

out_path = r"PATH/TO/LOCAL/DATA"
out.to_csv(out_path, index=False)
print(f"[OK] saved -> {out_path}", flush=True)

# --- Verification against baseline ---
baseline_path = r"PATH/TO/LOCAL/DATA"
base = pd.read_csv(baseline_path)

same_ids = set(base["h3_id"]) == set(out["h3_id"])
print(f"[VERIFY] h3_id sets identical: {same_ids} (baseline {len(base)} rows, flooded {len(out)} rows)", flush=True)

merged = base.merge(out, on="h3_id", suffixes=("_base", "_flood"))
print(f"[VERIFY] merged rows: {len(merged)}", flush=True)

for col_name, _, _ in variants:
    b = merged[f"{col_name}_base"]
    f = merged[f"{col_name}_flood"]
    diff = f - b
    n_increase = int((diff > 0.01).sum())
    n_newly_unreachable = int((b.notna() & f.isna()).sum())
    max_increase = diff[diff.notna()].max() if diff.notna().any() else float("nan")
    print(f"[VERIFY] {col_name}: {n_increase} hexes slower under flooding, "
          f"{n_newly_unreachable} newly unreachable, max increase {max_increase:.1f} min", flush=True)

print(f"[DONE] total runtime {time.time()-t0:.1f}s", flush=True)
