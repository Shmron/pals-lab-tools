"""
Phase B (routing graph, population grid, per-facility-type nearest-access
travel time) + two folium interactive maps for the Harare access-to-care /
flood-hazard deliverable. Reuses flood_accessibility_pipeline's own graph-
building, facility-loading and population-grid machinery rather than
reimplementing it - only the per-type/classification travel-time split and
the folium rendering are new.
"""
import os
import sys
import json
import numpy as np
import pandas as pd
import geopandas as gpd
import networkx as nx
import rasterio
from rasterio.features import shapes as rio_shapes
from shapely.geometry import shape as shapely_shape
import folium
import branca.colormap as bcm

import flood_accessibility_pipeline as fap
from flood_accessibility_pipeline import FloodAccessibilityPipeline

print("[*] Loading pipeline state for 'harare' ...", flush=True)
p = FloodAccessibilityPipeline("harare")
p.build_graphs()
print(f"[OK] baseline graph: {p.G_baseline.number_of_nodes():,} nodes, {p.G_baseline.number_of_edges():,} edges", flush=True)
p.load_facilities()
print(f"[OK] {len(p.facilities)} facilities loaded, {int(p.facilities['at_risk'].sum())} at-risk in floodplain", flush=True)

fap.H3_RESOLUTION = 7  # coarser than the pipeline default (9) - keeps hex count browser-friendly over this wide bbox
p.build_population_grid()
print(f"[OK] {len(p.populated_hex):,} populated hexagons (H3 res 7), total pop {p.total_pop:,.0f}", flush=True)

# --- per-type / per-classification nearest-facility travel time ------------
nn = p._nearest_node_lookup(p.G_baseline)
hex_nodes = [nn(pt.x, pt.y) for pt in p.populated_hex["centroid"]]


def travel_time_for_subset(subset):
    if len(subset) == 0:
        return np.full(len(hex_nodes), np.nan)
    fac_nodes = [nn(g.x, g.y) for g in subset.geometry]
    t = nx.multi_source_dijkstra_path_length(p.G_baseline, sources=fac_nodes, weight="travel_time_min")
    return np.array([t.get(n, np.nan) for n in hex_nodes], dtype=float)


variants = {}
variants["All facilities"] = travel_time_for_subset(p.facilities)
for amtype, label in [("hospital", "Hospital"), ("clinic", "Clinic"), ("doctors", "Doctors")]:
    variants[label] = travel_time_for_subset(p.facilities[p.facilities["amenity"] == amtype])
for cls in ["Public", "Private"]:
    variants[cls] = travel_time_for_subset(p.facilities[p.facilities["classification"] == cls])

for label, arr in variants.items():
    reach = np.isfinite(arr).sum()
    print(f"[travel-time] {label}: {reach}/{len(arr)} hexes reach a facility, "
          f"median {np.nanmedian(arr):.1f} min" if reach else f"[travel-time] {label}: no reachable hexes", flush=True)

hex_gdf = p.populated_hex[["h3_id", "geometry", "population"]].copy().reset_index(drop=True)
for label, arr in variants.items():
    hex_gdf[label] = arr

out_dir = p.cfg["extract_dir"]
hex_gdf.drop(columns="geometry").to_csv(os.path.join(out_dir, "harare_travel_time_by_type.csv"), index=False)

# Shared color scale across all layers so switching layers stays comparable.
all_vals = np.concatenate([v[np.isfinite(v)] for v in variants.values() if np.isfinite(v).any()])
vmax = float(np.nanpercentile(all_vals, 95)) if len(all_vals) else 60.0
colormap = bcm.LinearColormap(colors=["#ffffb2", "#fecc5c", "#fd8d3c", "#f03b20", "#bd0026"],
                               vmin=0, vmax=vmax, caption="Driving time to nearest facility (minutes)")

AMENITY_COLOR = {"hospital": "#d62728", "clinic": "#1f77b4", "doctors": "#2ca02c"}
AMENITY_LABEL = {"hospital": "Hospital", "clinic": "Clinic", "doctors": "Doctors"}

center = [float(p.populated_hex["centroid"].y.mean()), float(p.populated_hex["centroid"].x.mean())]

# =============================================================================
# MAP 1: access to care
# =============================================================================
m1 = folium.Map(location=center, zoom_start=10, tiles="cartodbpositron")

for label in ["All facilities", "Hospital", "Clinic", "Doctors", "Public", "Private"]:
    fg = folium.FeatureGroup(name=f"Travel time - {label}", show=(label == "All facilities"))
    sub = hex_gdf[["h3_id", "geometry", "population", label]].copy()
    sub["geometry"] = sub["geometry"].simplify(0.0005)
    geojson_data = json.loads(sub.set_geometry("geometry").to_json())
    for feat, val, pop in zip(geojson_data["features"], sub[label], sub["population"]):
        feat["properties"]["minutes"] = None if pd.isna(val) else round(float(val), 1)
        feat["properties"]["population"] = round(float(pop))

    def style_fn(feature, _cm=colormap):
        mins = feature["properties"]["minutes"]
        if mins is None:
            return {"fillColor": "#cccccc", "color": "#999999", "weight": 0.2, "fillOpacity": 0.25}
        return {"fillColor": _cm(mins), "color": "#555555", "weight": 0.2, "fillOpacity": 0.75}

    folium.GeoJson(
        geojson_data, style_function=style_fn,
        tooltip=folium.GeoJsonTooltip(fields=["minutes", "population"],
                                       aliases=["Drive time (min)", "Population"], localize=True),
    ).add_to(fg)
    fg.add_to(m1)

colormap.add_to(m1)

for amtype, grp_label in AMENITY_LABEL.items():
    fg = folium.FeatureGroup(name=f"Facilities - {grp_label}", show=True)
    sub = p.facilities[p.facilities["amenity"] == amtype]
    for _, fac in sub.iterrows():
        popup = (f"<b>{fac.get('name', fac['fac_code'])}</b><br>"
                 f"Type: {grp_label}<br>Classification: {fac.get('classification', 'n/a')}<br>"
                 f"CEmONC: {fac.get('CEmONC', 'n/a')}")
        folium.CircleMarker(
            location=[fac.geometry.y, fac.geometry.x], radius=4,
            color="black", weight=0.6, fill=True, fill_color=AMENITY_COLOR[amtype], fill_opacity=0.9,
            popup=folium.Popup(popup, max_width=250),
        ).add_to(fg)
    fg.add_to(m1)

folium.LayerControl(collapsed=False).add_to(m1)
title_html = ('<div style="position: fixed; top: 10px; left: 50px; z-index: 9999; '
              'background: white; padding: 8px 14px; border-radius: 4px; '
              'box-shadow: 0 1px 4px rgba(0,0,0,0.3); font-size: 15px;">'
              '<b>Harare: Access to Care (driving time to nearest facility)</b><br>'
              '<span style="font-size:12px;">Toggle layers to filter by facility type or Public/Private. '
              f'{len(p.facilities)} facilities, real road-network routing (not straight-line).</span></div>')
m1.get_root().html.add_child(folium.Element(title_html))

out1 = os.path.join(out_dir, "harare_access_to_care_map.html")
m1.save(out1)
print(f"[OK] -> {out1}", flush=True)

# =============================================================================
# MAP 2: flood hazard context
# =============================================================================
with rasterio.open(p.cfg["floodplain_tif"]) as src:
    data = src.read(1)
    transform = src.transform
mask_arr = (data == 1).astype(np.uint8)
polys = []
for geom, val in rio_shapes(mask_arr, mask=mask_arr.astype(bool), transform=transform):
    polys.append(shapely_shape(geom))
flood_gdf = gpd.GeoDataFrame(geometry=polys, crs="EPSG:4326")
# Drop sliver polygons and simplify - at 90m/1300x1300 this raster can
# otherwise polygonize into tens of thousands of tiny blobs that would make
# the browser map unusably heavy; a ~1-pixel area floor (90m^2 -> ~1e-7 deg^2
# but using a generous ~0.00003 deg^2 floor, roughly a few pixels) keeps the
# visually meaningful floodplain shapes while cutting noise.
flood_gdf = flood_gdf[flood_gdf.area > 0.00002].copy()
flood_gdf["geometry"] = flood_gdf.geometry.simplify(0.0004)
print(f"[OK] floodplain polygonized: {len(flood_gdf):,} polygons after sliver/simplify filter", flush=True)

m2 = folium.Map(location=center, zoom_start=10, tiles="cartodbpositron")

fg_flood = folium.FeatureGroup(name="Self-computed floodplain (HAND)", show=True)
folium.GeoJson(
    flood_gdf.to_json(),
    style_function=lambda f: {"fillColor": "#2166ac", "color": "#2166ac", "weight": 0.3, "fillOpacity": 0.45},
).add_to(fg_flood)
fg_flood.add_to(m2)

waterways = gpd.read_file(p.cfg["waterways_path"])
fg_water = folium.FeatureGroup(name="Waterways", show=True)
folium.GeoJson(waterways.to_json(), style_function=lambda f: {"color": "#4292c6", "weight": 1.5}).add_to(fg_water)
fg_water.add_to(m2)

n_flooded_crossings = len(p.flooded_crossing_points)
fg_crossings = folium.FeatureGroup(name=f"Flooded road crossings this scenario (n={n_flooded_crossings})", show=True)
for _, row in p.flooded_crossing_points.iterrows():
    c = row.geometry.centroid
    folium.CircleMarker(
        location=[c.y, c.x], radius=5, color="black", weight=0.6,
        fill=True, fill_color="#e31a1c", fill_opacity=0.9,
        popup=f"Crossing - flooded this scenario<br>waterway: {row.get('waterway', 'n/a')}<br>"
              f"highway: {row.get('highway', 'n/a')}",
    ).add_to(fg_crossings)
fg_crossings.add_to(m2)

fg_fac = folium.FeatureGroup(name="Health facilities (red = at risk this scenario)", show=True)
for _, fac in p.facilities.iterrows():
    at_risk = bool(fac["at_risk"])
    color = "#e31a1c" if at_risk else AMENITY_COLOR.get(fac.get("amenity"), "#555555")
    popup = (f"<b>{fac.get('name', fac['fac_code'])}</b><br>"
             f"Type: {fac.get('amenity', 'n/a')}<br>Classification: {fac.get('classification', 'n/a')}<br>"
             f"At risk this scenario: {'YES' if at_risk else 'No'}")
    folium.CircleMarker(
        location=[fac.geometry.y, fac.geometry.x], radius=5 if at_risk else 3.5,
        color="black", weight=0.6, fill=True, fill_color=color, fill_opacity=0.95,
        popup=folium.Popup(popup, max_width=250),
    ).add_to(fg_fac)
fg_fac.add_to(m2)

folium.LayerControl(collapsed=False).add_to(m2)

pct_floodplain = float(np.nansum(data == 1) / np.sum(~np.isnan(data)) * 100)
caption_html = (
    '<div style="position: fixed; top: 10px; left: 50px; z-index: 9999; '
    'background: white; padding: 10px 14px; border-radius: 4px; max-width: 480px; '
    'box-shadow: 0 1px 4px rgba(0,0,0,0.3); font-size: 13px;">'
    '<b>Harare: Flood Hazard Context (illustrative, not a validated hazard model)</b><br>'
    f'Self-computed floodplain (HAND + Leopold power law, 90m MERIT Hydro): '
    f'{pct_floodplain:.1f}% of resolved area is geomorphically floodplain.<br>'
    f'Illustrative wet-season rainfall window ({p.cfg["event_start_date"]} to {p.cfg["event_end_date"]}), '
    'not tied to a documented flood event.<br>'
    f'{n_flooded_crossings} road crossings flagged as flooded under this scenario '
    f'({int(p.facilities["at_risk"].sum())} of {len(p.facilities)} facilities at risk).<br>'
    '<i>Not a calibrated crossing-capacity or depth/discharge model - a terrain + rainfall-intensity proxy, '
    'same method as the Beitbridge/Chimanimani/Tokwe CASCADE sites.</i></div>'
)
m2.get_root().html.add_child(folium.Element(caption_html))

out2 = os.path.join(out_dir, "harare_flood_hazard_map.html")
m2.save(out2)
print(f"[OK] -> {out2}", flush=True)

print("\n[DONE] Phase B + maps complete for harare", flush=True)
print(f"[STATS] floodplain: {pct_floodplain:.1f}% of resolved area, {n_flooded_crossings} flooded crossings "
      f"this illustrative window, {int(p.facilities['at_risk'].sum())}/{len(p.facilities)} facilities at risk", flush=True)
