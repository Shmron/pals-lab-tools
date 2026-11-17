"""
Admin-defined registry of internal cohorts the Cohort Exposure Assignment
tool can operate on. One dict entry per cohort — adding a future cohort
(e.g. a malaria dataset) means adding one entry here with its own id/date/
location/filter columns; no other code in cohort_query.py or the app routes
should ever hardcode a column name, so the tool keeps working for a dataset
shaped nothing like PRECISE.

`pregnancy_window_support` gates whether the pregnancy-specific named
windows (core.windows.PREGNANCY_WINDOWS) are even offered for a cohort — the
generic `index_window` / `case_crossover` modes (any single event-date
column + a lag span) are always available to every cohort regardless of
this flag, since they carry no pregnancy assumption.

`precomputed_exposure_columns` is a dict of {group_label: [column, ...]}
purely for UI grouping — grouping has no effect on the query logic itself.
"""

COHORTS = {
    "precise": {
        "label": "PRECISE cohort",
        "source": {
            "type": "duckdb",
            "path": "/home/USER/PRECISE/precise.duckdb",
            "table": "daily_data",
        },
        "id_column": "f2a_participant_id",
        "location_columns": {"lat": "Latitude", "lon": "Longitude"},

        # One row per participant-day; exposure_day is what every precomputed
        # exposure column below is actually indexed against.
        "daily_date_column": "exposure_day",

        # Per-participant reference dates usable as a window anchor.
        "date_columns": [
            {"name": "conception_date", "label": "Conception date", "role": "pregnancy_start"},
            {"name": "delivery_date", "label": "Delivery date", "role": "pregnancy_end"},
            {"name": "edd", "label": "Expected delivery date", "role": "pregnancy_end_fallback"},
            {"name": "Visit 1", "label": "Visit 1", "role": "event"},
            {"name": "Visit 2", "label": "Visit 2", "role": "event"},
            {"name": "Birth visit - after birth", "label": "Birth/delivery visit", "role": "event"},
            {"name": "Postpartum Visit", "label": "Postpartum visit", "role": "event"},
        ],
        "pregnancy_window_support": True,

        # The column per-account access is scoped against (core.auth's
        # cohort_countries) — PRECISE spans Kenya/Mozambique/Gambia, and an
        # account granted cohort_access must still be restricted to only the
        # country/site(s) it's actually allowed to see.
        "access_scope_column": "Country",

        "filterable_columns": [
            "Country", "Village", "health_facility", "participant_status", "cohort",
        ],
        # Human-friendly label per filterable column, for the "Who" section
        # of the Cohort page (which renders generically off
        # filterable_columns — see cohort_assign.html). Falls back to the
        # raw column name if a column has no entry here.
        "column_labels": {
            "health_facility": "Health facility (fallback for no-village participants)",
        },

        # Village is the primary site identifier; ~4% of participants have
        # no Village recorded, in which case health_facility is the fallback
        # identifier for the same site. These two are OR'd together rather
        # than AND'd like every other filter, so picking a village doesn't
        # silently exclude village-less participants at that same facility.
        "site_fallback_columns": ["Village", "health_facility"],

        # Both are 1:1 with Country (verified live, 2026-10-14: zero Village
        # or health_facility names shared across countries) — the filter UI
        # tags each option with its own country so picking a Country
        # narrows these lists instead of mixing all three countries' sites
        # together regardless of what's selected.
        "site_columns_scoped_by_country": ["Village", "health_facility"],

        # Removed as a filter 2026-10-14: this was a reclassification of
        # GHSL into 3 groups (Urban/Peri_Urban/Rural), not an independent
        # classification — the same 3-group scheme is already produced by
        # the "Urban Settlement - GHSL" catalog entry's own `classify`
        # block, so it belongs in the dataset picker (an assignable
        # exposure) rather than duplicated here as a static filter on a
        # single frozen GHSL epoch.
        # "overall_village_class" intentionally omitted from the list above.

        # Real, already-computed exposure/location columns from `daily_data`
        # (confirmed against `DESCRIBE daily_data` on 2026-10-08). Deliberately
        # excludes the large block of participant clinical/demographic/SES
        # survey covariates in the same table (e.g. hiv_status, parity,
        # f3_* survey fields, birthweight, etc.) — those are cohort outcome/
        # covariate data, not environmental exposures, and are out of scope
        # for this tool.
        "precomputed_exposure_columns": {
            "Vegetation": ["NDVI", "NDVI_village", "NDVI_facility"],
            "Temperature": [
                "ERA5_LST_mean", "ERA5_LST_mean_village", "ERA5_LST_mean_facility",
                "ERA5_T2M_Mean", "ERA5_T2M_Mean_village", "ERA5_T2M_Mean_facility",
                "ERA5_T2M_Max", "ERA5_T2M_Max_village", "ERA5_T2M_Max_facility",
                "ERA5_T2M_Min", "ERA5_T2M_Min_village", "ERA5_T2M_Min_facility",
                "ERA5_T2M_Diurnal",
                "ERA5_T2M_threshold", "ERA5_T2M_threshold_village", "ERA5_T2M_threshold_facility",
                "ERA5_T2M_extreme_hot_day", "ERA5_T2M_extreme_hot_day_village", "ERA5_T2M_extreme_hot_day_facility",
                "ERA5_T2M_heatwave_day", "ERA5_T2M_heatwave_day_village", "ERA5_T2M_heatwave_day_facility",
                "ERA5_T2M_deviation", "ERA5_T2M_deviation_village", "ERA5_T2M_deviation_facility",
                "MERRA2_T2M_mean", "MERRA2_T2M_mean_village", "MERRA2_T2M_mean_facility", "MERRA2_T2M_max",
                "MERRA2_T2MWET_mean", "MERRA2_T2MWET_mean_village", "MERRA2_T2MWET_mean_facility", "MERRA2_T2MWET_max",
                "CAMS2_t2m_C", "CAMS2_d2m_C",
            ],
            "Humidity": ["Relative_Humidity"],
            "Precipitation": ["Precipitation", "Precip_village", "Precip_facility"],
            "Air quality": [
                "duaod550", "duaod550_village", "duaod550_facility",
                "CAMS2_pm2p5_ugm3", "CAMS2_aod550", "CAMS2_bcaod550", "CAMS2_duaod550",
                "CAMS2_tcco_gm2", "CAMS2_tcno2_umolm2", "CAMS2_gtco3_DU", "CAMS2_tcso2_DU",
                "Fire_Smoke_PM25", "Non_Fire_Smoke_PM25",
                "MERRA2_dust_AOD550", "MERRA2_dust_pm25_ugm3", "MERRA2_dust_column_kgm2",
            ],
            "Thermal comfort indices": [
                "UTCI_min", "UTCI_mean", "UTCI_max", "WBGT_mean", "WBGTsimple_mean", "WBT_mean",
                "humidex_mean", "HI_mean", "tasapp_mean", "tasdp_mean", "Wind_Chill_mean",
                "MRT_mean", "NET_mean", "ws_mean",
            ],
            "Location characteristics (static, not daily-varying)": [
                "GHSL_code", "GHSL_class", "IPCC_code", "IPCC_zone", "climate_zone",
                "VIIRS", "RWI", "RQI", "meanDEM",
                "PW_RoadDens", "PW_EuclMajorRd", "PW_EuclHwy",
                "PW_WalkIso_MajorRd", "PW_WalkIso_Hwy", "PW_DriveIso_MajorRd", "PW_DriveIso_Hwy",
                "PW_WalkDist_Fac", "PW_DriveDist_Fac", "PW_PubTrans_Dist_Fac",
                "PW_WalkTime_Fac", "PW_DriveTime_Fac", "PW_PubTrans_Time_Fac",
                "N_mean", "P_mean", "K_mean", "Ca_mean",
            ],
            "Season flags": ["season_wb", "season_park_25_75", "season_park_33_67", "season_park_50_50"],
        },
    },

    # Added 2026-10-14. Source: DHS Program Birth Records (BR), joined to
    # GPS cluster coordinates, across 35 Sub-Saharan African countries —
    # see /home/USER/PRECISE/dhs_br_geo.duckdb's own `cohort_births`
    # view (built from the raw `br_geo` table, filtered to the ~4.08M of
    # 5.28M rows that actually have usable coordinates).
    #
    # Two real differences from PRECISE, worth knowing before trusting
    # exposure windows built on this cohort:
    #   1. GPS cluster coordinates are DHS's own standard — deliberately
    #      randomly displaced up to ~2km (urban) or ~5-10km (rural, 1% up
    #      to 10km) for respondent privacy. Expected/accepted in DHS-based
    #      environmental epi, but it's an approximate location, not an
    #      exact household one.
    #   2. Birth dates are MONTH-precision only (DHS's CMC encoding has no
    #      day), so `birth_date_approx` assumes the 15th of the reported
    #      month — a real source of uncertainty for any day-level lag
    #      window, unlike PRECISE's actual visit dates.
    "dhs": {
        "label": "DHS Birth Records (Sub-Saharan Africa)",
        "source": {
            "type": "duckdb",
            "path": "/home/USER/PRECISE/dhs_br_geo.duckdb",
            "table": "cohort_births",
        },
        "id_column": "birth_id",
        "location_columns": {"lat": "latnum", "lon": "longnum"},

        # No real daily panel here — one row per birth, not per
        # participant-day — so there's no "daily_date_column" the way
        # PRECISE has one; only the single approximate event date below.
        "date_columns": [
            {"name": "birth_date_approx", "label": "Birth date (month-precision; day assumed as the 15th)",
             "role": "event"},
        ],
        # No conception/delivery pair recorded — only a single approximate
        # event date — so the pregnancy-stage windows (which need both)
        # don't apply here. index_window/case_crossover (single reference
        # date + lag span) work fine and carry no pregnancy assumption.
        "pregnancy_window_support": False,

        "access_scope_column": "country_name",
        "filterable_columns": ["country_name", "urban_rura", "dhsregna"],
        "column_labels": {
            "country_name": "Country",
            "urban_rura": "Urban/rural (DHS survey classification)",
            "dhsregna": "DHS region",
        },
        # No village/facility-style fallback concept in DHS survey clusters.
        "site_fallback_columns": [],

        # No precomputed environmental exposures exist for this cohort yet
        # (unlike PRECISE, which inherited years of prior linkage work) —
        # this cohort is "assign a new exposure" (Mode B) only, which is
        # also the only mode the current UI exposes.
        "precomputed_exposure_columns": {},
    },
}


def get_cohort(key):
    cohort = COHORTS.get(key)
    if cohort is None:
        raise KeyError(f"Unknown cohort: {key!r}")
    return cohort


def list_cohorts():
    return [{"key": k, "label": v["label"]} for k, v in COHORTS.items()]


def flat_exposure_columns(cohort):
    """Every precomputed exposure column for a cohort, regardless of group."""
    out = []
    for cols in cohort["precomputed_exposure_columns"].values():
        out.extend(cols)
    return out
