"""
Enhanced Two-Step Floating Catchment Area (E2SFCA, Luo & Qi 2009) accessibility,
baseline vs. flooded, for Chimanimani or Tokwe-Mukosi, driving mode.

This is the piece the original inherited plan (GIS analysis.docx) specified
and Watambwa et al. 2025 used, that our earlier accessibility scripts didn't
have: a facility CAPACITY / competition term, not just travel time.

Implementation note, stated honestly: this is the continuous-decay member of
the E2SFCA family (a single negative-exponential decay weight, per the
original plan's own example: Ai = e^(-beta*Ti), beta=0.05/min) rather than
Luo & Qi's original multi-zone step-function version (which needs several
arbitrary travel-time-zone cutoffs and a separate beta per zone - a further
refinement, not implemented here). Both steps of E2SFCA are present:

  Step 1 (competition): R_j = S_j / sum_i (P_i * W(T_ij))
      - how much capacity each facility has per unit of distance-weighted demand
  Step 2 (accessibility): A_i = sum_j (R_j * W(T_ij))
      - how much of that capacity each population location can actually reach

Facility capacity S_j comes from facility_capacity_estimate.py's national,
real-data-grounded tier imputation (not invented per-site). Facility closure
(a facility itself inside the floodplain) is respected in the flooded run,
same as facility_catchment_shift.py.
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

SITE = sys.argv[1] if len(sys.argv) > 1 else "chimanimani"
BETA = 0.05  # per minute, negative-exponential decay - the original plan's own example value

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PBF_PATH = os.path.join(BASE_DIR, "PBFs", "zimbabwe-latest.osm.pbf")
POP_RASTER_PATH = os.path.join(BASE_DIR, "Population", "zwe_pop_2026_CN_100m_R2025A_v1.tif")
NATIONAL_CAPACITY_CSV = os.path.join(BASE_DIR, "national_facility_capacity_estimates.csv")

if SITE == "chimanimani":
    BBOX = [32.75, -19.95, 32.98, -19.65]
    EXTRACT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")
    FACILITIES_PATH = os.path.join(EXTRACT_DIR, "chimanimani_health_facilities.gpkg")
    ROADS_PATH = os.path.join(EXTRACT_DIR, "chimanimani_roads.gpkg")
    CROSSINGS_PATH = os.path.join(EXTRACT_DIR, "chimanimani_crossings_distinct.gpkg")
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

crossings = gpd.read_file(CROSSINGS_PATH)
if CROSSINGS_CSV:
    flag_df = pd.read_csv(CROSSINGS_CSV)
    crossings["selfcomputed_floodplain"] = flag_df["selfcomputed_floodplain"].values
flooded_crossings = crossings[crossings["selfcomputed_floodplain"] == 1].copy()
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

# Facilities + closure (facility itself inside the floodplain)
facilities = gpd.read_file(FACILITIES_PATH).reset_index(drop=True)
if "fac_code" not in facilities.columns:
    facilities["fac_code"] = [f"F{i+1}" for i in range(len(facilities))]

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

# Facility capacity S_j, from the national tier-imputed table (nearest spatial match)
national = pd.read_csv(NATIONAL_CAPACITY_CSV)
national_pts = np.column_stack([national["LONGITUDE"], national["LATITUDE"]])
national_tree = cKDTree(national_pts)
cap_rows = []
for _, fac in facilities.iterrows():
    dist, idx = national_tree.query([fac.geometry.x, fac.geometry.y])
    match = national.iloc[idx]
    cap_rows.append({
        "fac_code": fac["fac_code"], "matched_name": match["NAMEOFFACI"],
        "match_dist_km": dist * 111, "workforce_est": match["workforce_est"],
        "workforce_imputed": match["workforce_imputed"],
    })
cap_df = pd.DataFrame(cap_rows)
facilities = facilities.merge(cap_df, on="fac_code")
# Floor at 1 - a facility with zero staff on paper still provides some access
facilities["S_j"] = facilities["workforce_est"].fillna(1).clip(lower=1)
print("\nFacility capacity (S_j = workforce estimate, tier-imputed unless flagged real):")
print(facilities[["fac_code", "matched_name", "match_dist_km", "S_j", "workforce_imputed"]].to_string(index=False))

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
total_pop = populated_hex["population"].sum()
print(f"\n[OK] {len(populated_hex):,} populated hexagons, {total_pop:,.0f} total population")


def nearest_node_lookup(G):
    ids = list(G.nodes())
    coords = np.array([[G.nodes[n]["x"], G.nodes[n]["y"]] for n in ids])
    tree_ = cKDTree(coords)
    def f(lon, lat):
        _, idx = tree_.query([lon, lat])
        return ids[idx]
    return f


def decay(t_min):
    return np.exp(-BETA * t_min)


def run_e2sfca(G, label):
    nn = nearest_node_lookup(G)
    candidates = facilities[~facilities["at_risk"]] if label == "flooded" else facilities
    hex_nodes = [nn(pt.x, pt.y) for pt in populated_hex["centroid"]]
    pops = populated_hex["population"].values

    # Step 1: R_j = S_j / sum_i (P_i * W(T_ij))
    R = {}
    per_fac_times = {}
    for _, fac in candidates.iterrows():
        fac_node = nn(fac.geometry.x, fac.geometry.y)
        times = nx.single_source_dijkstra_path_length(G, fac_node, weight="travel_time_min")
        per_fac_times[fac["fac_code"]] = times
        t_arr = np.array([times.get(n, np.nan) for n in hex_nodes])
        w = np.where(np.isnan(t_arr), 0.0, decay(t_arr))
        demand = np.sum(pops * w)
        R[fac["fac_code"]] = fac["S_j"] / demand if demand > 0 else 0.0

    # Step 2: A_i = sum_j (R_j * W(T_ij))
    A = np.zeros(len(hex_nodes))
    for code, times in per_fac_times.items():
        t_arr = np.array([times.get(n, np.nan) for n in hex_nodes])
        w = np.where(np.isnan(t_arr), 0.0, decay(t_arr))
        A += R[code] * w

    out = populated_hex[["h3_id", "population", "geometry"]].copy()
    out[f"e2sfca_{label}"] = A
    r_df = pd.DataFrame([{"fac_code": c, f"R_{label}": r} for c, r in R.items()])
    return out, r_df


print("\n[*] Running E2SFCA - baseline ...")
base_out, base_R = run_e2sfca(G_drive, "baseline")
print("[*] Running E2SFCA - flooded ...")
flood_out, flood_R = run_e2sfca(G_drive_flooded, "flooded")

merged = base_out.merge(flood_out[["h3_id", "e2sfca_flooded"]], on="h3_id")
merged["delta_e2sfca"] = merged["e2sfca_flooded"] - merged["e2sfca_baseline"]

A_baseline = (merged["e2sfca_baseline"] * merged["population"]).sum() / total_pop
A_flooded = (merged["e2sfca_flooded"] * merged["population"]).sum() / total_pop
pct_change = (A_flooded - A_baseline) / A_baseline * 100

print(f"\n{'='*70}\nE2SFCA RESULTS — {SITE} driving, beta={BETA}/min\n{'='*70}")
print(f"Population-weighted E2SFCA accessibility, baseline: {A_baseline:.4f}")
print(f"Population-weighted E2SFCA accessibility, flooded:  {A_flooded:.4f} ({pct_change:+.1f}%)")

r_compare = base_R.merge(flood_R, on="fac_code", how="outer")
r_compare = r_compare.merge(facilities[["fac_code", "S_j"]], on="fac_code")
print("\nPer-facility competition ratio R_j (capacity per unit distance-weighted demand):")
print(r_compare.to_string(index=False))

merged.drop(columns=[], errors="ignore").to_file(
    os.path.join(EXTRACT_DIR, f"{OUT_PREFIX}_e2sfca.gpkg"), driver="GPKG")
r_compare.to_csv(os.path.join(EXTRACT_DIR, f"{OUT_PREFIX}_e2sfca_facility_ratios.csv"), index=False)
with open(os.path.join(EXTRACT_DIR, f"{OUT_PREFIX}_e2sfca_summary.csv"), "w") as f:
    f.write("scenario,pop_weighted_e2sfca\n")
    f.write(f"baseline,{A_baseline}\n")
    f.write(f"flooded,{A_flooded}\n")
print(f"\n[OK] Saved gpkg + facility ratios + summary in {EXTRACT_DIR}")

# ---------------------------------------------------------------------------
# Map: baseline E2SFCA, flooded E2SFCA, change
# ---------------------------------------------------------------------------
roads = gpd.read_file(ROADS_PATH)
fig, axes = plt.subplots(1, 3, figsize=(22, 8), dpi=150)
vmax = merged["e2sfca_baseline"].quantile(0.98)

for ax, col, title in [(axes[0], "e2sfca_baseline", "E2SFCA — Baseline"),
                         (axes[1], "e2sfca_flooded", f"E2SFCA — Flooded ({EVENT_LABEL})")]:
    roads.plot(ax=ax, color="#cccccc", linewidth=0.3, zorder=1)
    merged.plot(ax=ax, column=col, cmap="viridis", vmin=0, vmax=vmax, zorder=2,
                legend=True, legend_kwds={"label": "E2SFCA accessibility", "shrink": 0.7})
    for _, fac in facilities.iterrows():
        closed_now = (col == "e2sfca_flooded") and fac["at_risk"]
        ax.scatter(fac.geometry.x, fac.geometry.y, color="#888888" if closed_now else "red",
                   s=50, marker="^", edgecolor="white", linewidth=0.8, zorder=5)
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_aspect("equal")

ax = axes[2]
roads.plot(ax=ax, color="#dddddd", linewidth=0.3, zorder=1)
vmax_d = max(abs(merged["delta_e2sfca"].min()), abs(merged["delta_e2sfca"].max()), 1e-9)
merged.plot(ax=ax, column="delta_e2sfca", cmap="RdBu", vmin=-vmax_d, vmax=vmax_d, zorder=2,
            legend=True, legend_kwds={"label": "Change in E2SFCA (flooded - baseline)", "shrink": 0.7})
for _, fac in facilities.iterrows():
    ax.scatter(fac.geometry.x, fac.geometry.y, color="#888888" if fac["at_risk"] else "red",
               s=50, marker="^", edgecolor="white", linewidth=0.8, zorder=5)
ax.set_title("Change due to flooding", fontsize=11, fontweight="bold")
ax.set_aspect("equal")

fig.suptitle(
    f"{SITE.title()}: E2SFCA Accessibility (capacity-weighted), Baseline vs. {EVENT_LABEL}\n"
    f"Population-weighted regional score: {A_baseline:.4f} -> {A_flooded:.4f} ({pct_change:+.1f}%) | beta={BETA}/min, negative-exponential decay",
    fontsize=12.5, fontweight="bold"
)
plt.tight_layout(rect=[0, 0.02, 1, 0.92])
out_path = os.path.join(BASE_DIR, f"{OUT_PREFIX}_e2sfca_map.png")
plt.savefig(out_path, bbox_inches="tight")
print(f"[OK] Saved -> {out_path}")
