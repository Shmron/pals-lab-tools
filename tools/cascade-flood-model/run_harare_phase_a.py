"""Phase A for 'harare', skipping extract_facilities() - the gpkg was already
seeded by seed_harare_facilities.py from the user's own curated facility list."""
from flood_accessibility_pipeline import FloodAccessibilityPipeline

p = FloodAccessibilityPipeline("harare")
(p.extract_roads_waterways()
   .extract_raw_crossings()
   .fetch_merit_hydro()
   .fetch_chirps()
   .fetch_chirps_thresholds()
   .fetch_runoff_coefficient()
   .rank_and_dedupe_crossings()
   .apply_runoff_coefficient()
   .compute_floodplain())
print("\n[DONE] Phase A complete for harare")
