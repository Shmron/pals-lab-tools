"""
National facility-capacity estimator, for E2SFCA's supply term.

Problem: the MoH facility CSV (Healthcare_Facilities.csv, 1,674 facilities
nationwide) has the exact right schema for SARA-style capacity (doctors,
nurses, beds, catchment population - matching WHO's Service Availability and
Readiness Assessment reference manual, WHO/HIS/HSI/2014.5) but is populated
for only ~1-13% of facilities. A District Hospital showing 0 beds is a
data-collection gap, not a real zero.

Fix: this is NOT specific to any one district or pilot site - it tiers every
facility nationally by TYPEOFFACI (normalizing ~90 messy raw strings into a
handful of canonical service levels matching Zimbabwe's actual health-system
hierarchy: clinic < rural health centre < district/rural hospital < mission
or private hospital < provincial/central), then, for facilities with no
reported capacity, imputes the median bed count and median core workforce
(doctors + nurses + primary care nurses, matching SARA's "core health
workforce" definition) from OTHER facilities of the same tier that DO have
real reported numbers. Facilities with real data keep their real data.
Every estimate is flagged is_imputed so downstream use can see which numbers
are observed vs. inferred - and this works for any facility, anywhere in
Zimbabwe, not just the two pilot sites.

Known limitation, stated honestly: the 'provincial' tier (Central/Provincial
hospitals) has zero facilities nationally with any reported bed/workforce
data at all, so it cannot be estimated this way - falls back to NaN with a
flag, not a fabricated number. Neither Chimanimani nor Tokwe-Mukosi has a
provincial-tier facility, so this doesn't block the current pilot, but any
future site that does would need a literature-based value instead.
"""
import os
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = r"PATH/TO/LOCAL/DATA"

# SARA reference manual (WHO/HIS/HSI/2014.5, table 3.1.2) benchmark densities -
# not used as per-facility values (SARA densities are population-normalized,
# area-level indicators), but kept here as the international readiness
# context for interpreting whatever real/imputed numbers a facility gets.
SARA_BEDS_PER_10K_BENCHMARK = 25
SARA_WORKFORCE_PER_10K_BENCHMARK = 23

NON_FACILITY_STRINGS = {
    "", "pmd offices", "municipality", "council", "gvt council", "zps",
    "matebeleland north", "family planning",
}


def normalize_tier(raw_type):
    """Map a raw TYPEOFFACI string (one of ~90 real-world variants) to a
    canonical service-level tier. Generalizes to any facility in the
    national list, not just the two pilot sites."""
    if pd.isna(raw_type):
        return "unknown"
    s = str(raw_type).strip().lower()
    if s in NON_FACILITY_STRINGS:
        return "non_facility"
    if "not open" in s or "construction" in s or "not operat" in s:
        return "non_operational"
    if "central hospital" in s or "provincial hospital" in s:
        return "provincial"
    if "mission hospital" in s or "private hospital" in s:
        return "mission_private_hospital"
    if "hospital" in s or "hosp" in s:
        return "district_rural_hospital"
    if "health centre" in s or s == "rhc":
        return "rural_health_centre"
    if "clinic" in s:
        return "clinic"
    return "other"


WORKFORCE_COLS = ["NUMOFDOCTO", "NUMOFNURSE", "NUMOFNUR_1", "NUMOFPCN"]


def build_national_capacity_table():
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    df["tier"] = df["TYPEOFFACI"].apply(normalize_tier)

    for c in WORKFORCE_COLS + ["NUMOFBEDS", "CATHMENTPO"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df["workforce_reported"] = df[WORKFORCE_COLS].sum(axis=1)
    df["beds_reported"] = df["NUMOFBEDS"]

    # Tier-level medians, computed ONLY from facilities with real (nonzero)
    # reports - never from imputed values, so this doesn't self-reinforce.
    tier_medians = {}
    for t in df["tier"].unique():
        sub = df[df["tier"] == t]
        beds_real = sub.loc[sub["beds_reported"] > 0, "beds_reported"]
        wf_real = sub.loc[sub["workforce_reported"] > 0, "workforce_reported"]
        tier_medians[t] = {
            "beds_median": beds_real.median() if len(beds_real) >= 5 else None,
            "beds_n": len(beds_real),
            "workforce_median": wf_real.median() if len(wf_real) >= 5 else None,
            "workforce_n": len(wf_real),
        }

    def estimate(row):
        has_beds = row["beds_reported"] > 0
        has_wf = row["workforce_reported"] > 0
        tm = tier_medians.get(row["tier"], {})
        beds_est = row["beds_reported"] if has_beds else tm.get("beds_median")
        wf_est = row["workforce_reported"] if has_wf else tm.get("workforce_median")
        return pd.Series({
            "beds_est": beds_est,
            "workforce_est": wf_est,
            "beds_imputed": not has_beds,
            "workforce_imputed": not has_wf,
        })

    est = df.apply(estimate, axis=1)
    df = pd.concat([df, est], axis=1)

    print("Tier-level medians (from real reported data only, min n=5 to estimate):")
    for t, m in tier_medians.items():
        print(f"  {t}: beds median={m['beds_median']} (n={m['beds_n']}), "
              f"workforce median={m['workforce_median']} (n={m['workforce_n']})")

    n_beds_imputed = df["beds_imputed"].sum()
    n_wf_imputed = df["workforce_imputed"].sum()
    print(f"\n[i] {len(df):,} facilities nationally. "
          f"Beds imputed for {n_beds_imputed:,} ({100*n_beds_imputed/len(df):.1f}%), "
          f"workforce imputed for {n_wf_imputed:,} ({100*n_wf_imputed/len(df):.1f}%).")

    out_cols = ["ID", "NAMEOFFACI", "DISTRICT", "LATITUDE", "LONGITUDE",
                "TYPEOFFACI", "tier", "beds_est", "beds_imputed",
                "workforce_est", "workforce_imputed", "CATHMENTPO"]
    out = df[out_cols]
    out_path = os.path.join(BASE_DIR, "national_facility_capacity_estimates.csv")
    out.to_csv(out_path, index=False)
    print(f"[OK] Saved -> {out_path}")
    return out


if __name__ == "__main__":
    build_national_capacity_table()
