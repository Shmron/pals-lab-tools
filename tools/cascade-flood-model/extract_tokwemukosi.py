"""
Extract roads/bridges/fords/waterways/crossings/facilities for the Tokwe-Mukosi
dam study area - same method as Chimanimani, different bbox and hazard source
(JRC simulated depth instead of CHIRPS x drainage-area, since this is a
large-catchment site where JRC actually works).
"""
import os
import pyrosm
import geopandas as gpd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PBF_PATH = os.path.join(BASE_DIR, "PBFs", "zimbabwe-latest.osm.pbf")
OUT_DIR = os.path.join(BASE_DIR, "TokweMukosi_extract")
os.makedirs(OUT_DIR, exist_ok=True)

# Spans the reservoir + downstream river reach where the 2025/26 spillage
# reportedly affected communities (marooned households, destroyed bridges)
TOKWE_BBOX = [30.25, -21.45, 30.65, -20.85]


def main():
    print(f"[*] Loading Zimbabwe PBF, clipped to Tokwe-Mukosi bbox {TOKWE_BBOX} ...")
    osm = pyrosm.OSM(PBF_PATH, bounding_box=TOKWE_BBOX)

    print("[*] Extracting roads (with bridge/ford/layer/surface attributes) ...")
    roads = osm.get_network(network_type="all", extra_attributes=["bridge", "ford", "layer", "surface"])
    roads_path = os.path.join(OUT_DIR, "tokwe_roads.gpkg")
    roads.to_file(roads_path, driver="GPKG")
    print(f"[OK] {len(roads):,} road segments -> {roads_path}")
    if "highway" in roads.columns:
        print(roads["highway"].value_counts().to_string())

    print("\n[*] Extracting waterways ...")
    waterways = osm.get_data_by_custom_criteria(
        custom_filter={"waterway": ["river", "stream"]},
        filter_type="keep", keep_nodes=False, keep_ways=True, keep_relations=False,
    )
    waterways_path = os.path.join(OUT_DIR, "tokwe_waterways.gpkg")
    waterways.to_file(waterways_path, driver="GPKG")
    print(f"[OK] {len(waterways):,} waterway segments -> {waterways_path}")

    print("\n[*] Finding road x waterway crossings ...")
    roads_m = roads.to_crs("EPSG:32736")
    waterways_m = waterways.to_crs("EPSG:32736")
    keep_cols = [c for c in ["highway", "bridge", "ford", "geometry"] if c in roads_m.columns]
    crossings = gpd.sjoin(roads_m[keep_cols], waterways_m[["waterway", "geometry"]],
                           how="inner", predicate="intersects")
    crossings = crossings.drop(columns=["index_right"], errors="ignore").to_crs("EPSG:4326")
    crossings_path = os.path.join(OUT_DIR, "tokwe_crossings.gpkg")
    crossings.to_file(crossings_path, driver="GPKG")
    print(f"[OK] {len(crossings)} road/waterway intersecting pairs -> {crossings_path}")

    print("\n[*] Extracting health facilities ...")
    facilities = osm.get_pois(custom_filter={
        "amenity": ["hospital", "clinic", "doctors", "pharmacy"], "healthcare": True,
    })
    if facilities is not None and len(facilities):
        facilities = facilities.copy()
        facilities["geometry"] = facilities.geometry.apply(lambda g: g.centroid if g.geom_type != "Point" else g)
        keep = [c for c in ["id", "name", "amenity", "healthcare", "geometry"] if c in facilities.columns]
        facilities = facilities[keep]
        facilities_path = os.path.join(OUT_DIR, "tokwe_health_facilities.gpkg")
        facilities.to_file(facilities_path, driver="GPKG")
        print(f"[OK] {len(facilities)} health facilities -> {facilities_path}")
        for _, row in facilities.iterrows():
            print(f"  - {row.get('name', '(unnamed)')}  [{row.get('amenity')}]  at ({row.geometry.y:.4f}, {row.geometry.x:.4f})")
    else:
        print("[X] No health facilities found via OSM tags in this bbox.")


if __name__ == "__main__":
    main()
