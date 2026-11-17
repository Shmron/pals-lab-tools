"""
Does climate exposure in the 365 days before a woman's DHS interview
predict whether she was tested for HIV in the past 12 months (v828,
conditional on ever-tested v781 -- see build script for why that
derivation matters), controlling for age, education, wealth, urban/rural,
region, and survey round. Cluster-robust (PSU) standard errors.

PRELIMINARY / EXPLORATORY -- same caveats as the births model:
unweighted (DHS sample weights v005 present but not yet applied), one
exposure-window specification (365 days, chosen to match v828's own
12-month recall -- the 90-day window is also in the data for comparison),
heat is relative to each district's own climate (TX90p), not absolute.
"""
import duckdb
import pandas as pd
import numpy as np
import statsmodels.formula.api as smf

DB = "/home/USER/PRECISE/climate_hiv_statistical_analysis/birth_climate.duckdb"
con = duckdb.connect(DB, read_only=True)
df = con.execute("SELECT * FROM analysis_hiv_testing").fetchdf()
con.close()

df["age_sq"] = df["age"] ** 2
df["tested_12mo"] = pd.to_numeric(df["tested_12mo"], errors="coerce")
df["ever_tested"] = pd.to_numeric(df["ever_tested"], errors="coerce")

FORMULA = (
    "{outcome} ~ heat_days_90d + heavy_rain_days_90d + age + age_sq "
    "+ wealth_index + C(education) + urban_rural + C(region_code) + C(survey_code)"
)

for outcome in ["tested_12mo", "ever_tested"]:
    sub = df.dropna(subset=[outcome, "heat_days_90d", "heavy_rain_days_90d", "age",
                             "wealth_index", "education", "urban_rural", "region_code", "psu"]).copy()
    print(f"\n{'='*70}\nOUTCOME: {outcome}  (n={len(sub)}, {sub[outcome].mean():.1%} positive)\n{'='*70}")
    model = smf.logit(FORMULA.format(outcome=outcome), data=sub)
    result = model.fit(disp=0, cov_type="cluster", cov_kwds={"groups": sub["psu"]})
    coefs = result.summary2().tables[1]
    print("\nClimate exposure coefficients (log-odds scale):")
    print(coefs.loc[["heat_days_90d", "heavy_rain_days_90d"], ["Coef.", "Std.Err.", "P>|z|"]].to_string())
    print(f"\nOdds ratio per +10 heat_days_90d: {np.exp(result.params['heat_days_90d']*10):.3f} "
          f"(p={coefs.loc['heat_days_90d','P>|z|']:.4f})")
    print(f"Odds ratio per +5 heavy_rain_days_90d: {np.exp(result.params['heavy_rain_days_90d']*5):.3f} "
          f"(p={coefs.loc['heavy_rain_days_90d','P>|z|']:.4f})")
    print(f"\nPseudo R-sq: {result.prsquared:.4f}  |  n clusters (PSU): {sub['psu'].nunique()}")
