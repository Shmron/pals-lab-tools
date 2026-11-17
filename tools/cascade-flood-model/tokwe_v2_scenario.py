"""
Tokwe-Mukosi accessibility, v2 - same fixes as Chimanimani v2:
  - Driving model relaxed: 'path' driveable at 8 km/h (rough/informal),
    not excluded. Only genuinely non-vehicle infrastructure stays excluded.
  - Flooded crossings = self-computed floodplain membership (already
    confirmed the 3 JRC-validated main-river crossings all fall inside it,
    so floodplain membership alone captures everything JRC confirmed, plus
    the tributary crossings JRC can't see at all).
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
EXTRACT_DIR = os.path.join(BASE_DIR, "TokweMukosi_extract")
POP_RASTER_PATH = os.path.join(BASE_DIR, "Population", "zwe_pop_2026_CN_100m_R2025A_v1.tif")

TOKWE_BBOX = [30.25, -21.45, 30.65, -20.85]
H3_RESOLUTION = 9
WALKING_SPEED_KMH = 5.0

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


# ---------------------------------------------------------------------------
# 1. Build graphs
# ---------------------------------------------------------------------------
print("[*] Loading roads + building graphs ...")
osm = pyrosm.OSM(PBF_PATH, bounding_box=TOKWE_BBOX)
nodes, edges = osm.get_network(nodes=True, network_type="all", extra_attributes=["surface"])
G_all = osm.to_graph(nodes, edges, graph_type="networkx")

G_walk = G_all.copy()
for u, v, k, data in G_walk.edges(keys=True, data=True):
    length_km = max(data.get("length", 10), 10) / 1000.0
    data["length_km"] = length_km
    data["travel_time_min"] = (length_km / WALKING_SPEED_KMH) * 60

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

for name, G in [("walking", G_walk), ("driving", G_drive)]:
    n_comp = nx.number_weakly_connected_components(G)
    largest = len(max(nx.weakly_connected_components(G), key=len))
    print(f"    {name}: {G.number_of_nodes():,}n/{G.number_of_edges():,}e, "
          f"{n_comp} components, largest={largest:,}")

# ---------------------------------------------------------------------------
# 2. Flooded crossings = self-computed floodplain membership
# ---------------------------------------------------------------------------
crossings_gdf = gpd.read_file(os.path.join(EXTRACT_DIR, "tokwe_crossings_with_selfcomputed_floodplain.gpkg"))
flooded_crossings = crossings_gdf[crossings_gdf["selfcomputed_floodplain"] == 1].copy()
n_jrc_in_flooded = flooded_crossings["jrc_valid"].sum()
print(f"\n[i] Flooded crossings (inside self-computed floodplain): "
      f"{len(flooded_crossings)}/{len(crossings_gdf)} "
      f"(includes {n_jrc_in_flooded}/{crossings_gdf['jrc_valid'].sum()} JRC-confirmed main-river crossings)")


def flood_graph(G):
    edge_list = list(G.edges(keys=True, data=True))
    mids, refs = [], []
    for u, v, k, data in edge_list:
        geom = data.get("geometry")
        if geom is not None:
            m = geom.interpolate(0.5, normalized=True)
            mids.append([m.x, m.y])
        else:
            ux, uy = G.nodes[u]["x"], G.nodes[u]["y"]
            vx, vy = G.nodes[v]["x"], G.nodes[v]["y"]
            mids.append([(ux + vx) / 2, (uy + vy) / 2])
        refs.append((u, v, k))
    tree = cKDTree(np.array(mids))
    Gf = G.copy()
    flooded_set = set()
    for geom in flooded_crossings.geometry.centroid:
        _, idx = tree.query([geom.x, geom.y])
        flooded_set.add(refs[idx])
    for u, v, k in flooded_set:
        if Gf.has_edge(u, v, k): Gf.remove_edge(u, v, k)
        if Gf.has_edge(v, u, k): Gf.remove_edge(v, u, k)
    return Gf, len(flooded_set)


G_walk_f, nw = flood_graph(G_walk)
G_drive_f, nd = flood_graph(G_drive)
print(f"[OK] Edges removed - walking: {nw}, driving: {nd}")

# ---------------------------------------------------------------------------
# 3. H3 population grid
# ---------------------------------------------------------------------------
print("\n[*] Building H3 population grid ...")
west, south, east, north = TOKWE_BBOX
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
print(f"[OK] {len(populated_hex):,} populated hexagons, {total_pop:,.0f} total population")

# ---------------------------------------------------------------------------
# 4. Accessibility: baseline + flooded, both modes
# ---------------------------------------------------------------------------
facilities = gpd.read_file(os.path.join(EXTRACT_DIR, "tokwe_health_facilities.gpkg")).reset_index(drop=True)
facilities["fac_code"] = [f"F{i+1}" for i in range(len(facilities))]

# Facility closure: a facility whose own location falls inside the self-computed
# floodplain is itself at risk, not just its access roads - dropped from the
# candidate set in the flooded scenario (the original plan's "facility closure" barrier).
with rasterio.open(os.path.join(BASE_DIR, "DEM", "tokwe_selfcomputed_floodplain.tif")) as fp_src:
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


def nn_lookup(G):
    ids = list(G.nodes())
    coords = np.array([[G.nodes[n]["x"], G.nodes[n]["y"]] for n in ids])
    tree = cKDTree(coords)
    def f(lon, lat):
        _, idx = tree.query([lon, lat])
        return ids[idx]
    return f


def evaluate(mode, G_base, G_flood):
    nn = nn_lookup(G_base)
    fac_nodes = [nn(g.x, g.y) for g in facilities.geometry]
    open_fac_nodes = [nn(g.x, g.y) for g in facilities.loc[~facilities["at_risk"]].geometry]
    base_t = nx.multi_source_dijkstra_path_length(G_base, sources=fac_nodes, weight="travel_time_min")
    flood_t = nx.multi_source_dijkstra_path_length(G_flood, sources=open_fac_nodes, weight="travel_time_min")
    hex_nodes = [nn(pt.x, pt.y) for pt in populated_hex["centroid"]]
    baseline_min = [base_t.get(n, np.nan) for n in hex_nodes]
    flooded_min = [flood_t.get(n, np.nan) for n in hex_nodes]
    df = populated_hex[["h3_id", "population", "geometry"]].copy()
    df["baseline_min"] = baseline_min
    df["flooded_min"] = flooded_min
    df["cut_off"] = pd.notna(df["baseline_min"]) & pd.isna(df["flooded_min"])

    reach_base = df.dropna(subset=["baseline_min"])
    pop_reach = reach_base["population"].sum()
    w_base = (reach_base["baseline_min"] * reach_base["population"]).sum() / pop_reach if pop_reach else np.nan
    reach_both = df.dropna(subset=["baseline_min", "flooded_min"])
    w_flood = ((reach_both["flooded_min"] * reach_both["population"]).sum() / reach_both["population"].sum()
               ) if len(reach_both) else np.nan
    pop_cutoff = df.loc[df["cut_off"], "population"].sum()
    pop_increased = reach_both.loc[reach_both["flooded_min"] > reach_both["baseline_min"], "population"].sum()

    print(f"\n--- {mode.upper()} ---")
    print(f"  Population reachable (baseline): {pop_reach:,.0f} / {total_pop:,.0f} "
          f"({pop_reach/total_pop*100:.1f}%)")
    print(f"  Pop-weighted time - baseline: {w_base:.1f} min")
    print(f"  Pop-weighted time - flooded:  {w_flood:.1f} min ({(w_flood - w_base) / w_base * 100:+.1f}%)")
    print(f"  Population with increased time: {pop_increased:,.0f}")
    print(f"  Population cut off entirely: {pop_cutoff:,.0f}")

    out = gpd.GeoDataFrame(df.drop(columns=["centroid"], errors="ignore"), geometry="geometry", crs="EPSG:4326")
    out.to_file(os.path.join(EXTRACT_DIR, f"tokwe_v2_{mode}.gpkg"), driver="GPKG")
    return {"mode": mode, "pop_reachable": pop_reach, "pct_reachable": pop_reach / total_pop * 100,
            "baseline_min": w_base, "flooded_min": w_flood,
            "pop_increased": pop_increased, "pop_cutoff": pop_cutoff}


print("\n" + "=" * 70)
print("TOKWE-MUKOSI v2: RELAXED DRIVING MODEL + FLOODPLAIN-BASED SCENARIO")
print("=" * 70)
results = [evaluate("walking", G_walk, G_walk_f), evaluate("driving", G_drive, G_drive_f)]
pd.DataFrame(results).to_csv(os.path.join(EXTRACT_DIR, "tokwe_v2_summary.csv"), index=False)
facilities.to_file(os.path.join(EXTRACT_DIR, "tokwe_facilities_coded.gpkg"), driver="GPKG")
print("\n[OK] Done")
