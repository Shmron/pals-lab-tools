"""
Single combined Harare map: access-to-care travel time (baseline AND flooded
scenario) + flood hazard, so the point of the exercise - how flooding changes
access to care - is visible directly, not split across pages or scenarios.

Built from already-cached Phase A/B outputs (baseline + flooded routing both
done in earlier background runs) - this is a presentation-layer build, no
re-routing here. Uses one GeoJson call per layer (not one per hex) for
speed and a smaller page.
"""
import os
import json
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
import rasterio.features
import h3
import folium
import folium.plugins
from shapely.geometry import Polygon
from PIL import Image

BASE_DIR = r"PATH/TO/LOCAL/DATA"
EXTRACT = os.path.join(BASE_DIR, "Harare_extract")
DEM_DIR = os.path.join(BASE_DIR, "DEM")
ADMIN_PATH = os.path.join(BASE_DIR, "Population", "zimbabwe_districts.gpkg")

OSM_TILE = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

TYPE_COLORS = {"hospital": "#d62728", "clinic": "#1f78b4", "doctors": "#33a02c"}
TYPE_LABELS = {"hospital": "Hospital", "clinic": "Clinic", "doctors": "Doctors"}
# Icon markers (not circle markers): Leaflet keeps marker icons in a pane
# above all polygon/GeoJson overlays, so facility hover/popups stay
# reachable no matter how many choropleth layers are stacked underneath -
# circle markers shared the polygons' pane and got hover-blocked by them.
# Colors avoid red/orange entirely for facility TYPE - red is reserved
# everywhere else on this map for "slow/at-risk", so a red hospital marker
# would otherwise misread as "something's wrong with this hospital".
TYPE_ICON_COLOR = {"hospital": "darkblue", "clinic": "cadetblue", "doctors": "purple"}
TYPE_ICON_COLOR_HEX = {"hospital": "#0a3d7a", "clinic": "#5f9ea0", "doctors": "#6a3d9a"}  # legend swatches (Leaflet's own named-icon CSS colors)
TYPE_ICON_GLYPH = {"hospital": "plus", "clinic": "medkit", "doctors": "user-md"}
AT_RISK_ICON_COLOR = "red"
AT_RISK_ICON_GLYPH = "exclamation-triangle"

REGIONAL_FLOOD_THRESHOLD_MM = 50
ANTECEDENT_TRIGGER_MM = 25
ANTECEDENT_SATURATION_MM = 40

BBOX = dict(west=30.66, south=-18.22, east=31.70, north=-17.17)
CENTER = [(BBOX["south"] + BBOX["north"]) / 2, (BBOX["west"] + BBOX["east"]) / 2]

TIME_BINS = [0, 15, 30, 45, 60, 90, 99999]
TIME_COLORS = ["#ffffb2", "#fed976", "#feb24c", "#fd8d3c", "#f03b20", "#bd0026"]
TIME_BIN_LABELS = ["0-15", "15-30", "30-45", "45-60", "60-90", "90+"]

DELTA_BINS = [-99999, 1, 10, 20, 30, 99999]
DELTA_COLORS = ["#f0f0f0", "#fee8c8", "#fdbb84", "#e34a33", "#b30000"]
DELTA_LABELS = ["No real change (<1 min)", "+1-10 min slower", "+10-20 min slower", "+20-30 min slower", "+30 min slower"]
NEWLY_CUTOFF_COLOR = "#000000"

VARIANTS = [("All facilities", "All facilities"), ("Hospital", "Hospitals only"),
            ("Clinic", "Clinics only"), ("Doctors", "Doctors only"),
            ("Public", "Public facilities"), ("Private", "Private facilities")]


def time_to_color(v):
    if pd.isna(v):
        return "#cccccc"
    for i in range(len(TIME_BINS) - 1):
        if TIME_BINS[i] <= v < TIME_BINS[i + 1]:
            return TIME_COLORS[i]
    return TIME_COLORS[-1]


def delta_to_color(baseline, flooded):
    if pd.isna(baseline) and pd.isna(flooded):
        return "#eeeeee"
    if pd.notna(baseline) and pd.isna(flooded):
        return NEWLY_CUTOFF_COLOR  # newly unreachable under flooding
    if pd.isna(baseline) and pd.notna(flooded):
        return "#3182bd"  # newly reached (edge case, shouldn't really happen)
    d = flooded - baseline
    if d < DELTA_BINS[1]:
        return DELTA_COLORS[0]
    for i in range(1, len(DELTA_BINS) - 1):
        if DELTA_BINS[i] <= d < DELTA_BINS[i + 1]:
            return DELTA_COLORS[i]
    return DELTA_COLORS[-1]


def harare_boundary():
    districts = gpd.read_file(ADMIN_PATH)
    return districts[districts["ADM2_NAME"] == "Harare"]


def add_boundary_layer(m):
    boundary = harare_boundary()
    fg = folium.FeatureGroup(name="Harare district boundary (ADM2)", show=True)
    folium.GeoJson(
        boundary,
        style_function=lambda f: {"fillOpacity": 0, "color": "#000000", "weight": 3, "dashArray": "8, 5"},
        tooltip="Harare district (ADM2) - the analysis area extends beyond this boundary",
    ).add_to(fg)
    fg.add_to(m)


def legend_box(title, rows, caption=None, top="10px", width="290px"):
    rows_html = "".join(
        f'<div style="display:flex; align-items:center; margin:3px 0;">'
        f'<div style="flex:0 0 22px;">{sw}</div>'
        f'<div style="font-size:12.5px; color:#222;">{label}</div></div>'
        for sw, label in rows
    )
    cap = f'<div style="font-size:11px; color:#666; margin-top:6px; line-height:1.4;">{caption}</div>' if caption else ""
    return f"""
    <div style="position: fixed; top: {top}; right: 10px; z-index: 9999; background: white;
                padding: 10px 12px; border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.35);
                font-family: Arial, sans-serif; width: {width}; max-height: 85vh; overflow-y: auto;">
    <div style="font-weight:bold; font-size:13.5px; margin-bottom:6px; border-bottom:1px solid #ddd; padding-bottom:4px;">{title}</div>
    {rows_html}
    {cap}
    </div>
    """


def circle_swatch(color, border="#333"):
    return f'<div style="width:14px;height:14px;border-radius:50%;background:{color};border:1.5px solid {border};margin:auto;"></div>'


def ring_swatch():
    return '<div style="width:16px;height:16px;border-radius:50%;background:#eee;border:3px solid #000;margin:auto;"></div>'


def line_swatch(color, dash=False, weight=3):
    style = f"border-top: {weight}px {'dashed' if dash else 'solid'} {color};"
    return f'<div style="{style} width:20px; margin: 8px auto;"></div>'


def square_swatch(color, opacity=0.5):
    return f'<div style="width:16px;height:16px;background:{color};opacity:{opacity};border:1px solid #333;margin:auto;"></div>'


def triangle_swatch(color):
    return (f'<div style="width:0;height:0;margin:2px auto;'
            f'border-left:7px solid transparent;border-right:7px solid transparent;'
            f'border-bottom:12px solid {color};"></div>')


def title_banner(text, sub):
    return f"""
    <div style="position: fixed; top: 10px; left: 50px; z-index: 9999; background: white;
                padding: 8px 14px; border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.35);
                font-family: Arial, sans-serif; max-width: 480px;">
    <div style="font-weight:bold; font-size:14px;">{text}</div>
    <div style="font-size:11.5px; color:#555; margin-top:3px; line-height:1.4;">{sub}</div>
    </div>
    """


def add_choropleth_layer(m, hex_gdf, color_col, name, show, tooltip_col="tooltip"):
    """One GeoJson call for the whole layer (fast), style driven by a
    precomputed per-row color column rather than a per-feature lambda loop."""
    fg = folium.FeatureGroup(name=name, show=show)
    gj = folium.GeoJson(
        hex_gdf.__geo_interface__,
        style_function=lambda f: {
            "fillColor": f["properties"][color_col], "color": "#999999", "weight": 0.3,
            "fillOpacity": 0.1 if f["properties"][color_col] in ("#cccccc", "#eeeeee") else 0.7,
        },
        tooltip=folium.GeoJsonTooltip(fields=[tooltip_col], aliases=[""], labels=False),
    )
    gj.add_to(fg)
    fg.add_to(m)
    return fg


def build_combined_map():
    facilities = gpd.read_file(os.path.join(EXTRACT, "harare_health_facilities.gpkg"))
    tt_base = pd.read_csv(os.path.join(EXTRACT, "harare_travel_time_by_type.csv"))
    tt_flood = pd.read_csv(os.path.join(EXTRACT, "harare_travel_time_by_type_flooded.csv"))
    assert set(tt_base["h3_id"]) == set(tt_flood["h3_id"]), "h3_id mismatch between baseline and flooded CSVs"
    tt_flood = tt_flood.set_index("h3_id").loc[tt_base["h3_id"]].reset_index()  # align order

    waterways = gpd.read_file(os.path.join(EXTRACT, "harare_waterways.gpkg"))
    crossings_geom = gpd.read_file(os.path.join(EXTRACT, "harare_crossings_distinct.gpkg"))[["cluster_id", "geometry"]]
    crossings_flag = pd.read_csv(os.path.join(EXTRACT, "harare_crossings_with_selfcomputed_floodplain.csv"))
    crossings = crossings_geom.merge(crossings_flag, on="cluster_id")

    daily_max = crossings["event_rainfall_daily_max_mm"].fillna(0)
    antecedent = crossings["event_antecedent_3day_mm"].fillna(0)
    had_rain = (daily_max > REGIONAL_FLOOD_THRESHOLD_MM) | (
        (daily_max > ANTECEDENT_TRIGGER_MM) & (antecedent > ANTECEDENT_SATURATION_MM))
    crossings["flooded_this_scenario"] = (crossings["selfcomputed_floodplain"] == 1) & had_rain
    n_flooded = int(crossings["flooded_this_scenario"].sum())

    with rasterio.open(os.path.join(DEM_DIR, "harare_selfcomputed_floodplain.tif")) as fp_src:
        fp_band = fp_src.read(1)
        in_fp = []
        for geom in facilities.geometry:
            row, col = fp_src.index(geom.x, geom.y)
            v = fp_band[row, col] if (0 <= row < fp_band.shape[0] and 0 <= col < fp_band.shape[1]) else 0
            in_fp.append(bool(v == 1))
    with rasterio.open(os.path.join(EXTRACT, "harare_chirps_thresholds.tif")) as ch_src:
        dm_band, an_band = ch_src.read(1), ch_src.read(2)
        fac_rain = []
        for geom in facilities.geometry:
            row, col = ch_src.index(geom.x, geom.y)
            if 0 <= row < dm_band.shape[0] and 0 <= col < dm_band.shape[1]:
                fac_rain.append((float(dm_band[row, col]), float(an_band[row, col])))
            else:
                fac_rain.append((0.0, 0.0))
    fac_had_rain = [(dm > REGIONAL_FLOOD_THRESHOLD_MM) or (dm > ANTECEDENT_TRIGGER_MM and an > ANTECEDENT_SATURATION_MM)
                     for dm, an in fac_rain]
    facilities["at_risk"] = [a and b for a, b in zip(in_fp, fac_had_rain)]
    n_at_risk = int(facilities["at_risk"].sum())
    print(f"[OK] {n_flooded}/{len(crossings)} crossings flooded this scenario, {n_at_risk}/{len(facilities)} facilities at risk")

    # Rendered as a raster PNG overlay, not vector polygons. The HAND method
    # produces a genuinely speckled pattern at 90m (median contiguous patch
    # is a SINGLE pixel - this is real signal, not noise to denoise away:
    # verified 108,639 polygons from rasterio.features.shapes, 75% of them
    # 1-3 cells). Any vector simplification (block-downsample-then-threshold,
    # area-filtering) tested either lost 35-50% of real flooded area or
    # produced too many features to render fast - a raster overlay keeps
    # every pixel's real signal at a file size that only depends on image
    # dimensions, not feature count.
    floodplain_png = os.path.join(EXTRACT, "harare_floodplain_overlay.png")
    with rasterio.open(os.path.join(DEM_DIR, "harare_selfcomputed_floodplain.tif")) as src:
        data = np.nan_to_num(src.read(1), nan=0.0)
        floodplain_bounds = src.bounds  # (left, bottom, right, top) - exact raster extent
        rgba = np.zeros((*data.shape, 4), dtype=np.uint8)
        rgba[data == 1] = [49, 130, 189, 140]  # #3182bd at ~55% alpha
        Image.fromarray(rgba, "RGBA").save(floodplain_png)
    print(f"[OK] floodplain rendered as raster overlay -> {floodplain_png}")

    hex_geoms = []
    for hid in tt_base["h3_id"]:
        boundary = h3.cell_to_boundary(hid)
        hex_geoms.append(Polygon([(lon, lat) for lat, lon in boundary]))
    hex_base = gpd.GeoDataFrame(tt_base, geometry=hex_geoms, crs="EPSG:4326")
    hex_flood = gpd.GeoDataFrame(tt_flood, geometry=hex_geoms, crs="EPSG:4326")

    m = folium.Map(location=CENTER, zoom_start=10, tiles=None, control_scale=True)
    folium.TileLayer(tiles=OSM_TILE, attr=OSM_ATTR, name="OpenStreetMap", control=False).add_to(m)

    time_layers = []
    for col, label in VARIANTS:
        sub = hex_base[["geometry", col]].copy()
        sub["color"] = sub[col].apply(time_to_color)
        sub["tooltip"] = sub[col].apply(lambda v: f"{label} (baseline): {v:.0f} min" if pd.notna(v) else f"{label} (baseline): unreachable")
        fg = add_choropleth_layer(m, sub, "color", f"BASELINE drive time - {label}", show=(col == "All facilities"))
        time_layers.append(fg)

    for col, label in VARIANTS:
        sub = hex_flood[["geometry", col]].copy()
        sub["color"] = sub[col].apply(time_to_color)
        sub["tooltip"] = sub[col].apply(lambda v: f"{label} (flooded): {v:.0f} min" if pd.notna(v) else f"{label} (flooded): unreachable")
        fg = add_choropleth_layer(m, sub, "color", f"FLOODED-SCENARIO drive time - {label}", show=False)
        time_layers.append(fg)

    for col, label in VARIANTS:
        sub = hex_base[["geometry"]].copy()
        b, f_ = hex_base[col], hex_flood[col]
        sub["color"] = [delta_to_color(bb, ff) for bb, ff in zip(b, f_)]
        sub["tooltip"] = [
            ("newly cut off by flooding" if pd.notna(bb) and pd.isna(ff) else
             (f"+{ff - bb:.0f} min under flooding" if pd.notna(bb) and pd.notna(ff) else "no data"))
            for bb, ff in zip(b, f_)
        ]
        sub["tooltip"] = label + " - change: " + sub["tooltip"]
        fg = add_choropleth_layer(m, sub, "color", f"CHANGE under flooding - {label}", show=False)
        time_layers.append(fg)

    fg_fp = folium.FeatureGroup(name="Self-computed floodplain (HAND method)", show=True)
    folium.raster_layers.ImageOverlay(
        image=floodplain_png,
        bounds=[[floodplain_bounds.bottom, floodplain_bounds.left], [floodplain_bounds.top, floodplain_bounds.right]],
        opacity=1.0,  # alpha already baked into the PNG itself
    ).add_to(fg_fp)
    fg_fp.add_to(m)

    fg_wway = folium.FeatureGroup(name="Rivers & streams (OSM)", show=False)
    folium.GeoJson(waterways, style_function=lambda f: {"color": "#2171b5", "weight": 1.5, "opacity": 0.8}).add_to(fg_wway)
    fg_wway.add_to(m)

    flooded_x = crossings[crossings["flooded_this_scenario"]]
    other_x = crossings[~crossings["flooded_this_scenario"]]

    fg_flooded_x = folium.FeatureGroup(name=f"Flooded crossings this scenario ({n_flooded})", show=True)
    for _, row in flooded_x.iterrows():
        folium.Marker(
            location=[row.geometry.y, row.geometry.x],
            icon=folium.DivIcon(html=(
                '<div style="width:0;height:0;border-left:8px solid transparent;'
                'border-right:8px solid transparent;border-bottom:14px solid #e6550d;'
                'filter:drop-shadow(0 0 1px #000);"></div>')),
            tooltip=f"Flooded crossing - {row.get('highway','road')} x {row.get('waterway','waterway')}",
        ).add_to(fg_flooded_x)
    fg_flooded_x.add_to(m)

    fg_other_x = folium.FeatureGroup(name=f"Other road/river crossings ({len(other_x)})", show=False)
    for _, row in other_x.iterrows():
        folium.CircleMarker(
            location=[row.geometry.y, row.geometry.x], radius=2.5,
            color="#888888", weight=1, fill=True, fill_color="#bbbbbb", fill_opacity=0.6,
            tooltip="Crossing (not flooded this scenario)",
        ).add_to(fg_other_x)
    fg_other_x.add_to(m)

    for t in ["hospital", "clinic", "doctors"]:
        sub = facilities[facilities["amenity"] == t]
        fg = folium.FeatureGroup(name=f"Facility markers - {TYPE_LABELS[t]} ({len(sub)})", show=True)
        for _, row in sub.iterrows():
            at_risk = bool(row["at_risk"])
            icon_color = AT_RISK_ICON_COLOR if at_risk else TYPE_ICON_COLOR[t]
            icon_glyph = AT_RISK_ICON_GLYPH if at_risk else TYPE_ICON_GLYPH[t]
            folium.Marker(
                location=[row.geometry.y, row.geometry.x],
                icon=folium.Icon(color=icon_color, icon=icon_glyph, prefix="fa"),
                popup=folium.Popup(
                    f"<b>{row['name']}</b><br>Type: {TYPE_LABELS[t]}<br>"
                    f"Classification: {row['classification']}<br>CEmONC: {row['CEmONC']}<br>"
                    f"<b>{'AT RISK this scenario' if at_risk else 'Not flagged at risk this scenario'}</b>",
                    max_width=260),
                tooltip=f"{row['name']}{' - AT RISK' if at_risk else ''}",
            ).add_to(fg)
        fg.add_to(m)

    add_boundary_layer(m)

    folium.LayerControl(collapsed=False).add_to(m)
    folium.plugins.GroupedLayerControl(
        groups={"Drive time to nearest facility (pick ONE)": time_layers}, exclusive_groups=True, collapsed=False,
    ).add_to(m)

    legend_rows = [("<b>Drive time (baseline/flooded)</b>", "")]
    legend_rows += [(f'<div style="width:16px;height:12px;background:{c};margin:auto;"></div>', f"{l} min")
                     for c, l in zip(TIME_COLORS, TIME_BIN_LABELS)]
    legend_rows.append(("<b>Change under flooding</b>", ""))
    legend_rows += [(f'<div style="width:16px;height:12px;background:{c};margin:auto;"></div>', l)
                     for c, l in zip(DELTA_COLORS, DELTA_LABELS)]
    legend_rows.append((f'<div style="width:16px;height:12px;background:{NEWLY_CUTOFF_COLOR};margin:auto;"></div>', "Newly cut off by flooding"))
    legend_rows.append(("<b>Flood hazard</b>", ""))
    legend_rows += [
        (square_swatch("#3182bd", 0.35), "Self-computed floodplain"),
        (line_swatch("#2171b5", weight=3), "Rivers &amp; streams"),
        (triangle_swatch("#e6550d"), f"Flooded crossing this scenario ({n_flooded})"),
        (circle_swatch("#bbbbbb", "#888888"), "Other crossing (not flooded)"),
    ]
    legend_rows.append(("<b>Facilities</b>", ""))
    for t in ["hospital", "clinic", "doctors"]:
        legend_rows.append((circle_swatch(TYPE_ICON_COLOR_HEX[t]), f"{TYPE_LABELS[t]} marker"))
    legend_rows.append((circle_swatch("#e81224"), f"AT RISK this scenario ({n_at_risk} currently) - red overrides type color"))
    legend_rows.append((line_swatch("#000000", dash=True, weight=3), "Harare district boundary"))

    legend_html = legend_box(
        "Access to care + flood hazard - legend",
        legend_rows,
        caption=("Use the drive-time layer picker (top right) to switch between BASELINE, FLOODED-SCENARIO, and "
                 "CHANGE views, each filterable by facility type - only one shows at a time. "
                 "<b>Flood window is illustrative</b> (Dec 2025-Feb 2026 peak wet season), not a documented event. "
                 "90m self-computed HAND method, not a calibrated crossing-capacity model."),
        width="300px",
    )
    m.get_root().html.add_child(folium.Element(legend_html))

    banner = title_banner(
        "Harare - access to care &amp; flood hazard (combined)",
        f"152 facilities. {n_flooded}/{len(crossings)} crossings flooded, {n_at_risk}/{len(facilities)} facilities "
        f"at risk this illustrative scenario. Flooding measurably slows access in a localized way (see 'Change' "
        f"layers) but cuts nobody off entirely in this scenario. District boundary shown for scale.",
    )
    m.get_root().html.add_child(folium.Element(banner))

    out = os.path.join(EXTRACT, "harare_combined_access_flood_map.html")
    m.save(out)
    print(f"[OK] combined map -> {out} ({os.path.getsize(out):,} bytes)")
    return out


if __name__ == "__main__":
    build_combined_map()
