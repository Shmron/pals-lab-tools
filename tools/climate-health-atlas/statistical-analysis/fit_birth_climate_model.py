"""
First-pass model: does climate exposure in the 90 days before a birth
predict whether that birth was a facility delivery, or whether the
mother had 4+ ANC visits -- controlling for mother's age, education,
household wealth, urban/rural residence, region, and survey round, with
standard errors clustered on the DHS primary sampling unit (PSU).

PRELIMINARY / EXPLORATORY. Not a finished analysis:
- Heat exposure uses heat_days_90d (each district's own TX90p threshold,
  same convention as the dashboard) -- interpret as "unusually hot days
  relative to that place's own climate", not an absolute temperature.
- No DHS sample weights applied yet (v005 is in the data, unused here) --
  coefficients below are unweighted; weighting is a natural next step,
  not a cosmetic one, since it changes which part of the population the
  estimate represents.
- Region (v024, DHS's own 8-10 region coding) used as the geographic fixed
  effect, not province, to stay inside DHS's native stratification rather
  than introduce a second geography. Survey round absorbs the national
  secular trend (health-system access improved markedly 1999->2015).
- Logistic regression, cluster-robust (sandwich) SEs by PSU -- the
  standard correction for DHS's clustered sampling design. Not yet
  incorporating finite-population or stratum-level corrections a full
  survey-design estimator (e.g. R's svyglm) would add.
"""
import duckdb
import pandas as pd
import statsmodels.formula.api as smf

DB = "/home/USER/PRECISE/climate_hiv_statistical_analysis/birth_climate.duckdb"
con = duckdb.connect(DB, read_only=True)
df = con.execute("SELECT * FROM analysis_births").fetchdf()
con.close()

df["mother_age_sq"] = df["mother_age"] ** 2
df["facility_delivery"] = pd.to_numeric(df["facility_delivery"], errors="coerce")
df["anc_4plus"] = pd.to_numeric(df["anc_4plus"], errors="coerce")

FORMULA_BASE = (
    "{outcome} ~ heat_days_90d + heavy_rain_days_90d + mother_age + mother_age_sq "
    "+ wealth_index + C(mother_education) + urban_rural + C(region_code) + C(survey_code)"
)

for outcome in ["facility_delivery", "anc_4plus"]:
    sub = df.dropna(subset=[outcome, "heat_days_90d", "heavy_rain_days_90d", "mother_age",
                             "wealth_index", "mother_education", "urban_rural", "region_code", "psu"]).copy()
    print(f"\n{'='*70}\nOUTCOME: {outcome}  (n={len(sub)}, {sub[outcome].mean():.1%} positive)\n{'='*70}")
    model = smf.logit(FORMULA_BASE.format(outcome=outcome), data=sub)
    result = model.fit(disp=0, cov_type="cluster", cov_kwds={"groups": sub["psu"]})
    coefs = result.summary2().tables[1]
    climate_rows = coefs.loc[["heat_days_90d", "heavy_rain_days_90d"]]
    print("\nClimate exposure coefficients (log-odds scale):")
    print(climate_rows[["Coef.", "Std.Err.", "P>|z|"]].to_string())
    or_heat = (result.params["heat_days_90d"] * 10)
    import numpy as np
    print(f"\nOdds ratio per +10 heat_days_90d: {np.exp(or_heat):.3f} "
          f"(p={coefs.loc['heat_days_90d','P>|z|']:.4f})")
    or_rain = (result.params["heavy_rain_days_90d"] * 5)
    print(f"Odds ratio per +5 heavy_rain_days_90d: {np.exp(or_rain):.3f} "
          f"(p={coefs.loc['heavy_rain_days_90d','P>|z|']:.4f})")
    print(f"\nPseudo R-sq: {result.prsquared:.4f}  |  n clusters (PSU): {sub['psu'].nunique()}")
