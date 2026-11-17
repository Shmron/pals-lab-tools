"""
Builds national_facility_capacity_estimates.csv from the HDX "2025 Health
Africa" facility list already vendored in the Harmonaize toolkit, filtered
to Zimbabwe.

The original national_facility_capacity_estimates.csv (1,674 MoH-listed
facilities) that flood_accessibility_pipeline.py's extract_facilities()
reads is gone from this server, and per the user, exhaustive real
bed/workforce data was never available even on their other machine -- the
original file's own beds_imputed/workforce_imputed columns confirm it was
already estimated-by-type, not a real MoH census. So this substitute uses
the same approach the original already relied on: facility location is real
(from HDX), capacity is imputed from facility type using published Zimbabwe
MoHCC tier norms, flagged imputed=1 throughout.

CATHMENTPO (catchment population) is left blank -- grep of
flood_accessibility_pipeline.py shows it's carried as a column but never
read by the routing/E2SFCA logic (only workforce_est is, via S_j, and that
already has a fillna(1) fallback for missing values).

Source: /home/USER/Harmonaize/core/HarmonAIze/harmonaize/geolocation/
data_geocoding/2025_Health Africa.csv (pan-African HDX health facility
list used by HarmonAIze's load_hdx_data.py importer).
"""
import os
import pandas as pd
import geopandas as gpd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HDX_CSV = "/home/USER/Harmonaize/core/HarmonAIze/harmonaize/geolocation/data_geocoding/2025_Health Africa.csv"
DISTRICTS_GPKG = os.path.join(BASE_DIR, "Population", "zimbabwe_districts.gpkg")
OUT_CSV = os.path.join(BASE_DIR, "national_facility_capacity_estimates.csv")

# Typical Zimbabwe MoHCC facility-tier bed/workforce norms (approximate,
# published ranges) -- same role the original list's beds_imputed/
# workforce_imputed columns already served: a documented estimate, not a
# per-facility census.
TIER_NORMS = {
    "Central Hospital":              {"tier": "central_hospital",    "beds_est": 800, "workforce_est": 1200},
    "Provincial Hospital":           {"tier": "provincial_hospital", "beds_est": 350, "workforce_est": 500},
    "District/Provincial Hospital":  {"tier": "provincial_hospital", "beds_est": 350, "workforce_est": 500},
    "District Hospital":             {"tier": "district_hospital",   "beds_est": 150, "workforce_est": 200},
    "Rural Hospital":                {"tier": "rural_hospital",      "beds_est": 60,  "workforce_est": 80},
    "Clinic":                        {"tier": "clinic",              "beds_est": 15,  "workforce_est": 20},
    "Rural Health Clinic":           {"tier": "rural_health_clinic", "beds_est": 10,  "workforce_est": 12},
}
DEFAULT_NORM = {"tier": "clinic", "beds_est": 15, "workforce_est": 20}


def main():
    df = pd.read_csv(HDX_CSV, encoding="latin1")
    zw = df[df["Country"].str.strip() == "Zimbabwe"].copy()

    zw["HDX Longitude"] = pd.to_numeric(zw["HDX Longitude"], errors="coerce")
    zw["HDX Latitude"] = pd.to_numeric(zw["HDX Latitude"], errors="coerce")
    zw = zw[(zw["HDX Longitude"].abs() > 0.001) & (zw["HDX Latitude"].abs() > 0.001)]
    zw = zw.dropna(subset=["HDX Longitude", "HDX Latitude"])
    print(f"[*] {len(zw)} Zimbabwe facilities with valid coordinates")

    gdf = gpd.GeoDataFrame(
        zw, geometry=gpd.points_from_xy(zw["HDX Longitude"], zw["HDX Latitude"]), crs="EPSG:4326"
    )

    districts = gpd.read_file(DISTRICTS_GPKG)[["ADM2_NAME", "geometry"]]
    joined = gpd.sjoin(gdf, districts, predicate="within", how="left")
    n_unmatched = joined["ADM2_NAME"].isna().sum()
    if n_unmatched:
        print(f"[!] {n_unmatched} facilities fell outside all district polygons (kept, DISTRICT left blank)")

    norms = joined["Facility type"].str.strip().map(TIER_NORMS)
    norms = norms.where(norms.notna(), [DEFAULT_NORM] * len(norms))

    out = pd.DataFrame({
        "ID": range(1, len(joined) + 1),
        "NAMEOFFACI": joined["Facility name"].fillna("").str.strip(),
        "DISTRICT": joined["ADM2_NAME"].fillna(""),
        "TYPEOFFACI": joined["Facility type"].fillna("").str.strip(),
        "tier": [n["tier"] for n in norms],
        "beds_est": [n["beds_est"] for n in norms],
        "beds_imputed": 1,
        "workforce_est": [n["workforce_est"] for n in norms],
        "workforce_imputed": 1,
        "CATHMENTPO": "",
        "LATITUDE": joined["HDX Latitude"],
        "LONGITUDE": joined["HDX Longitude"],
    })

    out.to_csv(OUT_CSV, index=False)
    print(f"[OK] {len(out)} facilities -> {OUT_CSV}")
    print(out["TYPEOFFACI"].value_counts())


if __name__ == "__main__":
    main()
