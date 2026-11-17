"""
Worked examples of run_intervention_scenario(), one per real site (Beitbridge
excluded - pipeline-mechanics test only). Intervention locations aren't
picked arbitrarily: each is the population-weighted centroid of the
underserved hexes nearest to the facility the site's own findings already
flagged as the relevant pressure point (Chimanimani: F1, the facility that
actually closes under flooding; Tokwe-Mukosi: F1/Neshuro, the zone the
E2SFCA analysis already identified as concentrated accessibility loss - see
METHODS_LOG.md), then checked against the site's own floodplain raster to
confirm the chosen point isn't itself inside the floodplain (a temp health
point sited inside the floodplain would defeat its own purpose).
"""
import os
import numpy as np
import geopandas as gpd
import rasterio
from flood_accessibility_pipeline import FloodAccessibilityPipeline, SITE_CONFIGS


def pick_intervention_site(pipeline, near_fac_code, n_nearest=15):
    """Population-weighted centroid of the n_nearest underserved hexes to
    the named facility, nudged to the nearest non-flooded candidate hex if
    the raw centroid itself falls inside the floodplain."""
    cfg = pipeline.cfg
    underserved = gpd.read_file(os.path.join(cfg["extract_dir"], f"{cfg['prefix']}_underserved_areas.gpkg"))
    fac = pipeline.facilities.set_index("fac_code").loc[near_fac_code]
    underserved["dist_to_fac"] = underserved.geometry.distance(fac.geometry)
    nearest = underserved.sort_values("dist_to_fac").head(n_nearest).copy()

    with rasterio.open(cfg["floodplain_tif"]) as src:
        def is_flooded(geom):
            c = geom.centroid
            row, col = src.index(c.x, c.y)
            try:
                v = src.read(1, window=((row, row + 1), (col, col + 1)))[0, 0]
            except Exception:
                return True
            return bool(v == 1)
        nearest["flooded"] = nearest.geometry.apply(is_flooded)

    safe = nearest[~nearest["flooded"]]
    if len(safe) == 0:
        raise RuntimeError(f"All {n_nearest} nearest underserved hexes to {near_fac_code} are in the "
                            f"floodplain - widen n_nearest or pick a different reference facility.")
    cx = (safe.geometry.centroid.x * safe["population"]).sum() / safe["population"].sum()
    cy = (safe.geometry.centroid.y * safe["population"]).sum() / safe["population"].sum()
    print(f"[i] {near_fac_code} reference: {len(nearest)} nearest underserved hexes, "
          f"{len(safe)} outside the floodplain, chosen centroid ({cx:.5f}, {cy:.5f})")
    return cx, cy


print("=" * 70)
print("CHIMANIMANI: temp health point near F1 (Chimanimani Village Health")
print("Center, the facility that closes under flooding)")
print("=" * 70)
p1 = FloodAccessibilityPipeline("chimanimani")
p1.run_phase_a().build_graphs().load_facilities().build_population_grid().join_vulnerability_index()
p1.run_mean_travel_time()
p1.run_catchment_shift()
p1.run_e2sfca()
p1.run_distance_bands()
p1.run_priority_ranking()
p1.run_underserved_areas()

lon, lat = pick_intervention_site(p1, "F1")
p1.run_intervention_scenario(
    [{"name": "Temp health point near F1's former catchment (public-infrastructure proxy)",
      "lon": lon, "lat": lat, "capacity": 3}],
    scenario_name="temp_health_point_f1",
)

print()
print("=" * 70)
print("TOKWE-MUKOSI: mobile clinic near F1/Neshuro's concentrated")
print("accessibility-loss zone")
print("=" * 70)
p2 = FloodAccessibilityPipeline("tokwe")
p2.run_phase_a().build_graphs().load_facilities().build_population_grid().join_vulnerability_index()
p2.run_mean_travel_time()
p2.run_catchment_shift()
p2.run_e2sfca()
p2.run_distance_bands()
p2.run_priority_ranking()
p2.run_underserved_areas()

lon, lat = pick_intervention_site(p2, "F1", n_nearest=25)
p2.run_intervention_scenario(
    [{"name": "Mobile clinic near F1/Neshuro's underserved zone", "lon": lon, "lat": lat, "capacity": 2}],
    scenario_name="mobile_clinic_f1",
)
