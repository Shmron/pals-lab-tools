"""
Cross-validate RWI against the local DHS 2015 wealth-cluster ground truth,
one time, nationally - decides which layer flood_accessibility_pipeline.py's
join_vulnerability_index() actually uses, rather than trusting RWI's own
global DHS-validation claim or DHS's sparse coverage on faith.

For each of the ~400 DHS clusters, samples the nearest RWI point and
correlates DHS's hv271 wealth-factor-score against RWI. If the correlation
is meaningfully positive, RWI (dense, 2.4km) becomes the primary per-hex
vulnerability layer; if not, this is itself a finding (RWI ruled out for
Zimbabwe with evidence, joining the project's other "tried and ruled out"
entries) and the pipeline falls back to DHS nearest-cluster-match despite
its sparsity.

Output:
  - rwi_vs_dhs_validation.png (scatter, following the gfplain_vs_selfcomputed
    convention: show the evidence, don't just assert a result)
  - Population/vulnerability_source_decision.json (the decision + correlation,
    read by flood_accessibility_pipeline.py's join_vulnerability_index())
"""
import os
import json
import geopandas as gpd
import pandas as pd
import numpy as np
from scipy.spatial import cKDTree
from scipy.stats import pearsonr, spearmanr
import matplotlib.pyplot as plt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DHS_PATH = os.path.join(BASE_DIR, "Population", "zimbabwe_dhs2015_wealth_clusters.gpkg")
RWI_PATH = os.path.join(BASE_DIR, "Population", "zimbabwe_rwi.csv")
DECISION_PATH = os.path.join(BASE_DIR, "Population", "vulnerability_source_decision.json")
PLOT_PATH = os.path.join(BASE_DIR, "rwi_vs_dhs_validation.png")

# No threshold pre-committed here - report whatever correlation the data
# gives, then use it as evidence, not as a pass/fail gate rigged in advance.
CORRELATION_MEANINGFUL_THRESHOLD = 0.3  # documented rationale below, applied AFTER seeing the result

print("[*] Loading DHS wealth clusters + RWI points ...")
dhs = gpd.read_file(DHS_PATH)
rwi = pd.read_csv(RWI_PATH)
print(f"[OK] {len(dhs)} DHS clusters, {len(rwi):,} RWI points")

rwi_tree = cKDTree(rwi[["lon", "lat"]].values)
dhs_coords = np.column_stack([dhs.geometry.x, dhs.geometry.y])
dist_deg, idx = rwi_tree.query(dhs_coords)
dhs["rwi"] = rwi.iloc[idx]["rwi"].values
dhs["rwi_match_dist_km"] = dist_deg * 111.0  # rough deg->km, fine at this precision

r_pearson, p_pearson = pearsonr(dhs["mean_wealth"], dhs["rwi"])
r_spearman, p_spearman = spearmanr(dhs["mean_wealth"], dhs["rwi"])
print(f"\n[RESULT] Pearson r = {r_pearson:.3f} (p={p_pearson:.2e})")
print(f"[RESULT] Spearman rho = {r_spearman:.3f} (p={p_spearman:.2e})")
print(f"[i] Median RWI-match distance: {dhs['rwi_match_dist_km'].median():.2f} km "
      f"(RWI grid is 2.4km, so this should be well under that)")

fig, ax = plt.subplots(figsize=(7, 6))
ax.scatter(dhs["rwi"], dhs["mean_wealth"], s=18, alpha=0.6, c="#2b6cb0", edgecolors="none")
ax.set_xlabel("RWI (Relative Wealth Index, satellite/connectivity-derived)")
ax.set_ylabel("DHS 2015 wealth index factor score (hv271, household-survey ground truth)")
ax.set_title(f"RWI vs. DHS 2015 wealth, n={len(dhs)} Zimbabwe clusters\n"
             f"Pearson r={r_pearson:.3f}, Spearman rho={r_spearman:.3f}")
z = np.polyfit(dhs["rwi"], dhs["mean_wealth"], 1)
xs = np.linspace(dhs["rwi"].min(), dhs["rwi"].max(), 50)
ax.plot(xs, np.poly1d(z)(xs), color="#c53030", linewidth=1.5, linestyle="--", label="linear fit")
ax.legend()
fig.tight_layout()
fig.savefig(PLOT_PATH, dpi=150)
print(f"[OK] Scatter plot -> {PLOT_PATH}")

# Decision: correlation direction (positive = RWI's "wealthier" agrees with
# DHS's) and a documented, if inevitably somewhat arbitrary, magnitude bar.
# 0.3 is chosen post-hoc as "detectable, not noise" for n~400 social-survey
# data (typical DHS/RWI validation papers report r in the 0.5-0.7 range at
# national scale) - stated as a judgment call, not a pre-registered test.
use_rwi = bool(r_pearson > CORRELATION_MEANINGFUL_THRESHOLD)
decision = {
    "source": "rwi" if use_rwi else "dhs",
    "pearson_r": r_pearson, "pearson_p": p_pearson,
    "spearman_rho": r_spearman, "spearman_p": p_spearman,
    "n_dhs_clusters": len(dhs),
    "n_rwi_points": len(rwi),
    "median_match_dist_km": float(dhs["rwi_match_dist_km"].median()),
    "threshold_used": CORRELATION_MEANINGFUL_THRESHOLD,
    "rationale": ("RWI correlates meaningfully with local DHS ground truth - using RWI as the "
                  "dense per-hex vulnerability layer." if use_rwi else
                  "RWI does NOT correlate meaningfully with local DHS ground truth in Zimbabwe - "
                  "falling back to DHS nearest-cluster-match despite its sparsity."),
}
with open(DECISION_PATH, "w") as f:
    json.dump(decision, f, indent=2)
print(f"\n[DECISION] vulnerability source = '{decision['source']}' -> {DECISION_PATH}")
print(f"[DECISION] {decision['rationale']}")
