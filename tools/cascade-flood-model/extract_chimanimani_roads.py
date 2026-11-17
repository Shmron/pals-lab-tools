"""
Extract roads (with bridge/ford/layer tags) and waterways for the Chimanimani
test area from the Zimbabwe OSM PBF, using pyrosm.

Run this after zimbabwe-latest.osm.pbf has finished downloading into ./PBFs/.
"""
import os
import pyrosm
import geopandas as gpd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PBF_PATH = os.path.join(BASE_DIR, "PBFs", "zimbabwe-latest.osm.pbf")
OUT_DIR = os.path.join(BASE_DIR, "Chimanimani_extract")

# Bounding box around Chimanimani town + surrounding wards (west, south, east, north)
# Covers the area worst affected by Cyclone Idai (2019) along the Mozambique border
CHIMANIMANI_BBOX = [32.75, -19.95, 32.98, -19.65]

os.makedirs(OUT_DIR, exist_ok=True)


def main():
    if not os.path.exists(PBF_PATH):
        print(f"[X] PBF not found yet: {PBF_PATH}")
        print("    Wait for the aria2c download to finish, then re-run this script.")
        return

    print(f"[*] Loading Zimbabwe PBF, clipped to Chimanimani bbox {CHIMANIMANI_BBOX} ...")
    osm = pyrosm.OSM(PBF_PATH, bounding_box=CHIMANIMANI_BBOX)

    # --- Roads, including bridge/ford/layer tags (not in default HOT OSM exports) ---
    print("[*] Extracting roads (with bridge/ford/layer attributes) ...")
    roads = osm.get_network(network_type="all", extra_attributes=["bridge", "ford", "layer"])

    if roads is None or len(roads) == 0:
        print("[X] No roads found in this bbox.")
    else:
        roads_path = os.path.join(OUT_DIR, "chimanimani_roads.gpkg")
        roads.to_file(roads_path, driver="GPKG")
        print(f"[OK] {len(roads):,} road segments -> {roads_path}")

        if "highway" in roads.columns:
            print("\nRoad type breakdown:")
            print(roads["highway"].value_counts().to_string())

        if "bridge" in roads.columns:
            n_bridge = roads["bridge"].notna().sum()
            print(f"\nSegments tagged bridge=*: {n_bridge}")
            if n_bridge:
                print(roads["bridge"].value_counts(dropna=True).to_string())
        else:
            print("\n[!] 'bridge' column not present in this extract")

        if "ford" in roads.columns:
            n_ford = roads["ford"].notna().sum()
            print(f"\nSegments tagged ford=*: {n_ford}")
            if n_ford:
                print(roads["ford"].value_counts(dropna=True).to_string())
        else:
            print("\n[!] 'ford' column not present in this extract")

    # --- Waterways (rivers/streams), for geometric road x river crossing detection ---
    print("\n[*] Extracting waterways (river/stream) ...")
    waterways = osm.get_data_by_custom_criteria(
        custom_filter={"waterway": ["river", "stream"]},
        filter_type="keep",
        keep_nodes=False,
        keep_ways=True,
        keep_relations=False,
    )

    if waterways is None or len(waterways) == 0:
        print("[X] No waterways found in this bbox.")
    else:
        waterways_path = os.path.join(OUT_DIR, "chimanimani_waterways.gpkg")
        waterways.to_file(waterways_path, driver="GPKG")
        print(f"[OK] {len(waterways):,} waterway segments -> {waterways_path}")
        if "waterway" in waterways.columns:
            print(waterways["waterway"].value_counts().to_string())

    # --- Find road x waterway crossings (the full set of potential flood chokepoints) ---
    if roads is not None and len(roads) > 0 and waterways is not None and len(waterways) > 0:
        print("\n[*] Finding road x waterway crossing points ...")
        roads_m = roads.to_crs("EPSG:32736")   # UTM zone 36S, appropriate for eastern Zimbabwe
        waterways_m = waterways.to_crs("EPSG:32736")

        crossings = gpd.sjoin(
            roads_m[["highway", "bridge", "ford", "geometry"]] if "bridge" in roads_m.columns and "ford" in roads_m.columns else roads_m[["highway", "geometry"]],
            waterways_m[["waterway", "geometry"]],
            how="inner",
            predicate="intersects",
        )
        print(f"[OK] {len(crossings)} road/waterway intersecting pairs found")

        n_tagged_bridge = crossings["bridge"].notna().sum() if "bridge" in crossings.columns else 0
        n_tagged_ford = crossings["ford"].notna().sum() if "ford" in crossings.columns else 0
        n_untagged = len(crossings) - n_tagged_bridge - n_tagged_ford
        print(f"    tagged bridge: {n_tagged_bridge}")
        print(f"    tagged ford:   {n_tagged_ford}")
        print(f"    untagged (treat as vulnerable): {n_untagged}")

        crossings_path = os.path.join(OUT_DIR, "chimanimani_crossings.gpkg")
        # Save back in WGS84 (EPSG:4326) so it lines up with roads/waterways for anyone
        # loading these three files together later.
        crossings_wgs84 = crossings.drop(columns=["index_right"], errors="ignore").to_crs("EPSG:4326")
        crossings_wgs84.to_file(crossings_path, driver="GPKG")
        print(f"[OK] Crossings saved -> {crossings_path}")

    print("\n[DONE] Chimanimani extract complete.")


if __name__ == "__main__":
    main()
