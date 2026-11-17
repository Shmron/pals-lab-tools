"""
Extract health facility points/polygons for the Chimanimani test area from the
already-downloaded Zimbabwe OSM PBF (same bbox as extract_chimanimani_roads.py).
"""
import os
import pyrosm
import geopandas as gpd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PBF_PATH = os.path.join(BASE_DIR, "PBFs", "zimbabwe-latest.osm.pbf")
OUT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")

# Same bbox as the roads/waterways extract
CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]

os.makedirs(OUT_DIR, exist_ok=True)


def main():
    if not os.path.exists(PBF_PATH):
        print(f"[X] PBF not found: {PBF_PATH}")
        return

    print(f"[*] Loading Zimbabwe PBF, clipped to Chimanimani bbox {CHIMANIMANI_BBOX} ...")
    osm = pyrosm.OSM(PBF_PATH, bounding_box=CHIMANIMANI_BBOX)

    print("[*] Extracting health facilities (amenity=hospital/clinic/doctors/pharmacy, healthcare=*) ...")
    facilities = osm.get_pois(
        custom_filter={
            "amenity": ["hospital", "clinic", "doctors", "pharmacy"],
            "healthcare": True,
        }
    )

    if facilities is None or len(facilities) == 0:
        print("[X] No health facilities found via OSM tags in this bbox.")
        print("    This is common in rural areas with sparse OSM mapping coverage -")
        print("    doesn't mean no facilities exist, only that none are mapped/tagged.")
        return

    # Collapse to point geometries (polygons -> centroid) for consistent downstream use
    facilities = facilities.copy()
    facilities["geometry"] = facilities.geometry.apply(
        lambda g: g.centroid if g.geom_type != "Point" else g
    )

    name_col = "name" if "name" in facilities.columns else None
    keep_cols = [c for c in ["id", "name", "amenity", "healthcare", "operator", "geometry"] if c in facilities.columns]
    facilities = facilities[keep_cols]

    out_path = os.path.join(OUT_DIR, "chimanimani_health_facilities.gpkg")
    facilities.to_file(out_path, driver="GPKG")
    print(f"[OK] {len(facilities)} health facilities -> {out_path}")

    print("\nFacilities found:")
    for _, row in facilities.iterrows():
        name = row.get("name", "(unnamed)") or "(unnamed)"
        amenity = row.get("amenity", "")
        healthcare = row.get("healthcare", "")
        print(f"  - {name}  [amenity={amenity}, healthcare={healthcare}]  at ({row.geometry.y:.5f}, {row.geometry.x:.5f})")


if __name__ == "__main__":
    main()
