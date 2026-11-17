"""
Validates Meta's Relative Wealth Index (dense, 2.4km, countrywide) against
Zimbabwe's 2015 DHS household wealth data (sparse, 400 clusters, ground
truth) by nearest-point correlation, and writes
Population/vulnerability_source_decision.json recording which layer the
accessibility pipeline should trust as its per-hex vulnerability source.

Reimplemented per flood_accessibility_pipeline.py's own docstring spec (the
original script is gone, but the method and expected result -- "r=0.837,
n=400 clusters" -- are documented there).
"""
import os
import json
import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.spatial import cKDTree

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RWI_PATH = os.path.join(BASE_DIR, "Population", "zimbabwe_rwi.csv")
DHS_WEALTH_PATH = os.path.join(BASE_DIR, "Population", "zimbabwe_dhs2015_wealth_clusters.gpkg")
OUT_PATH = os.path.join(BASE_DIR, "Population", "vulnerability_source_decision.json")

# RWI wins by default when correlation holds: dense (2.4km, countrywide) vs.
# DHS's 400 sparse point clusters frozen at 2015 -- same tradeoff the
# pipeline's own docstring describes (RWI validated against DHS, not
# replaced by it).
CORRELATION_THRESHOLD = 0.3


def main():
    rwi = pd.read_csv(RWI_PATH)
    dhs = gpd.read_file(DHS_WEALTH_PATH)

    rwi_tree = cKDTree(rwi[["lon", "lat"]].values)
    dist, idx = rwi_tree.query(np.column_stack([dhs.geometry.x, dhs.geometry.y]))

    matched_rwi = rwi.iloc[idx]["rwi"].values
    dhs_wealth = dhs["mean_wealth"].values

    r = np.corrcoef(matched_rwi, dhs_wealth)[0, 1]
    n = len(dhs)
    source = "rwi" if r >= CORRELATION_THRESHOLD else "dhs"

    decision = {
        "source": source,
        "pearson_r": round(float(r), 3),
        "n": int(n),
        "threshold": CORRELATION_THRESHOLD,
        "note": "RWI (dense, 2.4km, countrywide) validated against Zimbabwe 2015 DHS "
                "household wealth (sparse, 400 clusters, ground truth) by nearest-point "
                "Pearson correlation. RWI used as the per-hex vulnerability layer when "
                "correlation clears the threshold; DHS used directly otherwise.",
    }
    with open(OUT_PATH, "w") as f:
        json.dump(decision, f, indent=2)

    print(f"[OK] Pearson r={r:.3f} (n={n}) -> source='{source}'")
    print(f"[OK] -> {OUT_PATH}")


if __name__ == "__main__":
    main()
