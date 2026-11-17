DATASETS = {
    # ── Vegetation ────────────────────────────────────────────────────────────
    "EVI (MODIS)": {
        "collection": "MODIS/061/MOD13Q1",
        "band": "EVI",
        "scale": 500,
        "temporal": "16-day",
        "type": "timeseries",
        "description": "Enhanced Vegetation Index from MODIS 16-day composite (500m)",
        "stats": ["mean", "min", "max", "p50"],
        "output_col": "evi",
        "category": "Vegetation",
        "units": "dimensionless (index, 0 to 1)",
        # Verified against the Earth Engine catalog page: raw pixels are stored
        # ×10000 (range -2000..10000) and MUST be multiplied by 0.0001 to get
        # the true index — this was previously undocumented AND unapplied, so
        # every EVI value extracted before this fix is off by a factor of 10000.
        "value_transform": {"type": "linear_scale", "factor": 0.0001},
    },
    "NDVI (MODIS)": {
        "collection": "MODIS/MOD09GA_006_NDVI",
        "band": "NDVI",
        "scale": 500,
        "temporal": "daily",
        "type": "timeseries",
        "description": "Normalized Difference Vegetation Index from MODIS (500m)",
        "stats": ["mean", "min", "max", "p50", "p95"],
        "output_col": "ndvi",
        "category": "Vegetation",
        "units": "dimensionless (index, -1 to 1)",
        # Verified: this specific community-curated asset is pre-scaled to the
        # real -1..1 range already (unlike MOD13Q1's own NDVI band) — no
        # transform needed here.
        #
        # Discovered 2026-10-14: this community-curated asset has had no new
        # images since 2023-02-17 — NASA retired the underlying MOD09GA
        # collection 006 in favour of 061, and nobody updated this derived
        # NDVI asset to track the replacement. Rather than switch every past
        # and future request to a different source (which would make old and
        # new exports silently incomparable — exactly what we don't want),
        # the primary source above stays untouched for any date it can still
        # cover. core/extractor.py's _resolve_source_collection automatically
        # swaps in the "fallback" below, computed from the actively
        # maintained MOD09GA.061 surface reflectance bands with its own
        # cloud mask applied (_ndvi_from_mod09ga061), only for requests that
        # reach past this cutover date — and the metadata file says so.
        "fallback": {
            "after": "2023-02-17",
            "collection": "MODIS/061/MOD09GA",
            "note": "Primary source (community-curated MOD09GA_006_NDVI) has no data after "
                    "2023-02-17. For dates after that, NDVI is instead computed from the actively "
                    "maintained MODIS/061/MOD09GA surface reflectance bands (red/NIR), with cloud "
                    "masking applied via its state_1km QA band. Same physical quantity, same sensor "
                    "family, but a different processing pipeline. Do not assume values on either "
                    "side of the cutover are numerically identical to each other.",
        },
    },
    "NDVI 16-day Composite (MODIS)": {
        # Same collection already used for EVI (MODIS) above — MOD13Q1
        # carries both VI bands. Added 2026-10-14 as a smoother alternative
        # to "NDVI (MODIS)" (daily, noisier) for users who want less day to
        # day jitter without switching to a different index (EVI) entirely.
        "collection": "MODIS/061/MOD13Q1",
        "band": "NDVI",
        "scale": 500,
        "temporal": "16-day",
        "type": "timeseries",
        "description": "Normalized Difference Vegetation Index from MODIS 16-day composite (500m). "
                        "Smoother than NDVI (MODIS)'s daily values, at the cost of temporal detail",
        "stats": ["mean", "min", "max", "p50"],
        "output_col": "ndvi_16day",
        "category": "Vegetation",
        "units": "dimensionless (index, -1 to 1)",
        # Verified live (2026-10-14), same scale factor as this collection's
        # EVI band: raw pixels stored ×10000, multiply by 0.0001 for the
        # true index.
        "value_transform": {"type": "linear_scale", "factor": 0.0001},
    },

    # ── Land Surface Temperature ──────────────────────────────────────────────
    "Land Surface Temp - MODIS": {
        "collection": "MODIS/061/MOD11A1",
        "band": ["LST_Day_1km", "LST_Night_1km"],
        "band_rename": ["lst_day", "lst_night"],
        "scale": 1000,
        "temporal": "daily",
        "type": "timeseries",
        "description": "MODIS Day/Night land surface temperature (1km)",
        "stats": ["mean", "min", "max"],
        "output_col": ["lst_day", "lst_night"],
        "category": "Land Surface Temperature",
        "units": "Kelvin",
        # Verified against the Earth Engine catalog page: raw pixels (range
        # 7500..65535) must be multiplied by 0.02 to get Kelvin — previously
        # undocumented AND unapplied, same bug class as EVI above.
        "value_transform": {"type": "linear_scale", "factor": 0.02},
    },

    # ── Climate ───────────────────────────────────────────────────────────────
    "Precipitation - CHIRPS": {
        "collection": "UCSB-CHG/CHIRPS/DAILY",
        "band": "precipitation",
        "scale": 5000,
        "temporal": "daily",
        # What the single per-period value actually IS, for correctly
        # worded stat-feasibility warnings (core.ai_agent.check_stat_
        # feasibility) — CHIRPS gives one ACCUMULATED TOTAL per day, not an
        # average of sub-daily readings, so calling it "the mean" would be
        # describing the wrong operation even though the number itself is
        # right. Flagged after the user correctly objected to that framing
        # in an explanation, 2026-10-14.
        "value_kind": "total",
        "type": "timeseries",
        "description": "Daily rainfall from CHIRPS (5km) · ~2-month data lag",
        "stats": ["mean", "min", "max"],
        "output_col": "precipitation_mm",
        "category": "Climate",
        "units": "mm/day",
    },
    "Temperature - ERA5-Land": {
        "collection": "ECMWF/ERA5_LAND/HOURLY",
        "band": "temperature_2m",
        "scale": 11132,
        "temporal": "daily",
        # Native image cadence of the SOURCE collection — distinct from
        # "temporal" above, which describes the output row cadence. This is
        # what tells the extractor (and the metadata file) that mean/min/
        # max/p95/p99 for a single calendar day are real, distinct numbers
        # (computed across that day's ~24 hourly images), not the same
        # value repeated. See core/extractor.py's _temporal_reduce.
        "native_cadence": "hourly",
        # Real sub-daily readings exist, so distinct mean/min/max/percentile
        # values are genuinely possible even for single-day rows — see
        # core.ai_agent.check_stat_feasibility.
        "value_kind": "sampled",
        "type": "timeseries",
        "description": "2m air temperature from ERA5-Land (hourly → daily stats, ~11km)",
        "stats": ["mean", "max", "min", "p50", "p75", "p95", "p99"],
        "output_col": "temp_2m_k",
        "category": "Climate",
        "units": "Kelvin",
    },
    "Temperature + Wet Bulb - MERRA-2": {
        "collection": "NASA/GSFC/MERRA/slv/2",
        "band": ["T2M", "T2MWET"],
        "scale": 1000,
        "temporal": "daily",
        "native_cadence": "hourly",
        # Real sub-daily readings exist, so distinct mean/min/max/percentile
        # values are genuinely possible even for single-day rows — see
        # core.ai_agent.check_stat_feasibility.
        "value_kind": "sampled",
        "type": "timeseries",
        "description": "2m dry and wet bulb temperature from MERRA-2",
        "stats": ["mean", "max", "p50", "p75", "p95", "p99"],
        "output_col": ["T2M", "T2MWET"],
        "category": "Climate",
        "units": {"T2M": "Kelvin", "T2MWET": "Kelvin"},
    },

    "Humidity - ERA5-Land": {
        "collection": "ECMWF/ERA5_LAND/HOURLY",
        "band": "dewpoint_temperature_2m",
        "scale": 11132,
        "temporal": "daily",
        "native_cadence": "hourly",
        # Real sub-daily readings exist, so distinct mean/min/max/percentile
        # values are genuinely possible even for single-day rows — see
        # core.ai_agent.check_stat_feasibility.
        "value_kind": "sampled",
        "type": "timeseries",
        "description": "2m dewpoint temperature from ERA5-Land (hourly → daily stats, ~11km)",
        "stats": ["mean", "min", "max"],
        "output_col": "dewpoint_2m_k",
        "category": "Climate",
        "units": "Kelvin",
    },
    "Wind - ERA5-Land": {
        "collection": "ECMWF/ERA5_LAND/HOURLY",
        "band": ["u_component_of_wind_10m", "v_component_of_wind_10m"],
        "band_rename": ["wind_u", "wind_v"],
        "scale": 11132,
        "temporal": "daily",
        "native_cadence": "hourly",
        # Real sub-daily readings exist, so distinct mean/min/max/percentile
        # values are genuinely possible even for single-day rows — see
        # core.ai_agent.check_stat_feasibility.
        "value_kind": "sampled",
        "type": "timeseries",
        "description": "10m U/V wind components from ERA5-Land (~11km) · combine for speed: √(u²+v²)",
        "stats": ["mean"],
        "output_col": ["wind_u", "wind_v"],
        "category": "Climate",
        "units": {"wind_u": "m/s", "wind_v": "m/s"},
    },

    # ── Air Quality ───────────────────────────────────────────────────────────
    "Air Quality - CAMS NRT": {
        "collection": "ECMWF/CAMS/NRT",
        "band": [
            "particulate_matter_d_less_than_25_um_surface",
            "total_column_nitrogen_dioxide_surface",
            "gems_total_column_ozone_surface",
            "total_column_carbon_monoxide_surface",
            "total_column_sulphur_dioxide_surface",
            "black_carbon_aerosol_optical_depth_at_550nm_surface",
            "total_aerosol_optical_depth_at_550nm_surface",
        ],
        "band_rename": ["pm25", "no2", "o3", "co", "so2", "black_carbon", "aod_550"],
        "scale": 50000,
        "temporal": "daily",
        # Verified live (2026-10-14): genuinely 3-hourly valid times (00, 03,
        # 06 ... 21 UTC) — but each valid time carries ~10-11 images (CAMS
        # NRT forecast lead-times/ensemble members, not independent repeat
        # observations). Flagged here for documentation accuracy only; NOT
        # yet wired into a percentile stat option in this catalog entry,
        # because reducing across all of a day's ~80-90 images the same way
        # ERA5-Land's true-hourly data is reduced would conflate forecast-lead
        # spread with real diurnal spread. Needs an extra per-timestamp
        # collapse step (e.g. mean across lead-times at each exact valid
        # time) before any percentile reducer would be meaningful here.
        "native_cadence": "3-hourly",
        "value_kind": "sampled",
        "type": "timeseries",
        "description": "CAMS NRT: PM2.5, NO₂, O₃, CO, SO₂, Black Carbon, AOD 550nm (~44km)",
        "stats": ["mean"],
        "output_col": ["pm25", "no2", "o3", "co", "so2", "black_carbon", "aod_550"],
        "category": "Air Quality",
        "units": {
            "pm25": "kg/m3", "no2": "kg/m2", "o3": "kg/m2",
            "co": "kg/m2", "so2": "kg/m2",
            "black_carbon": "dimensionless (optical depth)",
            "aod_550": "dimensionless (optical depth)",
        },
    },

    "Aerosol Optical Depth - MAIAC": {
        "collection": "MODIS/006/MCD19A2",
        "band": "Optical_Depth_055",
        "scale": 1000,
        "temporal": "daily",
        "type": "timeseries",
        "description": "MAIAC aerosol optical depth at 550nm, a PM2.5 proxy (1km)",
        "stats": ["mean"],
        "output_col": "aod_maiac",
        "category": "Air Quality",
        "units": "dimensionless (optical depth)",
    },
    "PM2.5 - GHAP": {
        "collection": "projects/sat-io/open-datasets/GHAP/GHAP_D1K_PM25",
        "band": "b1",
        "scale": 1000,
        "temporal": "daily",
        "type": "timeseries",
        "description": "GHAP modelled ground-level PM2.5 concentration (~1km, daily, from 2017), a real "
                        "concentration estimate rather than an optical-depth proxy, and far finer resolution "
                        "than CAMS NRT's ~44km regional PM2.5",
        "stats": ["mean", "min", "max"],
        "output_col": "pm25_ghap",
        "category": "Air Quality",
        "units": "ug/m3",
    },
    "NO\u2082 - Sentinel-5P": {
        "collection": "COPERNICUS/S5P/OFFL/L3_NO2",
        "band": "tropospheric_NO2_column_number_density",
        "scale": 1113,
        "temporal": "daily",
        "type": "timeseries",
        "description": "Tropospheric NO\u2082 column density from Sentinel-5P OFFL (~1.1km) · mol/m\u00b2 · available from 2018",
        "stats": ["mean"],
        "output_col": "s5p_no2",
        "category": "Air Quality",
        "units": "mol/m2",
    },
    "CO - Sentinel-5P": {
        "collection": "COPERNICUS/S5P/OFFL/L3_CO",
        "band": "CO_column_number_density",
        "scale": 1113,
        "temporal": "daily",
        "type": "timeseries",
        "description": "CO column number density from Sentinel-5P OFFL (~1.1km) · mol/m\u00b2 · available from 2018",
        "stats": ["mean"],
        "output_col": "s5p_co",
        "category": "Air Quality",
        "units": "mol/m2",
    },
    "SO\u2082 - Sentinel-5P": {
        "collection": "COPERNICUS/S5P/OFFL/L3_SO2",
        "band": "SO2_column_number_density",
        "scale": 1113,
        "temporal": "daily",
        "type": "timeseries",
        "description": "SO\u2082 column number density from Sentinel-5P OFFL (~1.1km) · mol/m\u00b2 · available from 2018",
        "stats": ["mean"],
        "output_col": "s5p_so2",
        "units": "mol/m2",
        "category": "Air Quality",
    },

    # ── Terrain ───────────────────────────────────────────────────────────────
    "Elevation - MERIT DEM": {
        "collection": "MERIT/DEM/v1_0_3",
        "band": "dem",
        "scale": 90,
        "temporal": "static",
        "type": "static",
        "description": "Digital elevation model (90m)",
        "stats": ["mean", "min", "max"],
        "output_col": "elevation_m",
        "category": "Terrain",
        "units": "meters",
    },

    # ── Soil (iSDAsoil — Africa only) ─────────────────────────────────────────
    # Units and back-transform formulas verified against each dataset's Earth
    # Engine catalog page (2026-09-24). Raw pixels are log-scaled integers,
    # NOT usable directly — previously this transform was undocumented AND
    # unapplied, so every soil value extracted before this fix was the raw
    # scaled integer, not real g/kg or ppm.
    "Soil Nitrogen (iSDAsoil)": {
        "collection": "ISDASOIL/Africa/v1/nitrogen_total",
        "band": "mean_0_20",
        "scale": 250,
        "temporal": "static",
        "type": "static",
        "description": "Total nitrogen 0-20cm depth (250m, Africa only)",
        "stats": ["mean"],
        "output_col": "soil_N",
        "category": "Soil",
        "units": "g/kg",
        "value_transform": {"type": "exp_scale_minus1", "scale": 100},
    },
    "Soil Phosphorus (iSDAsoil)": {
        "collection": "ISDASOIL/Africa/v1/phosphorus_extractable",
        "band": "mean_0_20",
        "scale": 250,
        "temporal": "static",
        "type": "static",
        "description": "Extractable phosphorus 0-20cm (250m, Africa only)",
        "stats": ["mean"],
        "output_col": "soil_P",
        "category": "Soil",
        "units": "ppm",
        "value_transform": {"type": "exp_scale_minus1", "scale": 10},
    },
    "Soil Potassium (iSDAsoil)": {
        "collection": "ISDASOIL/Africa/v1/potassium_extractable",
        "band": "mean_0_20",
        "scale": 250,
        "temporal": "static",
        "type": "static",
        "description": "Extractable potassium 0-20cm (250m, Africa only)",
        "stats": ["mean"],
        "output_col": "soil_K",
        "category": "Soil",
        "units": "ppm",
        "value_transform": {"type": "exp_scale_minus1", "scale": 10},
    },
    "Soil Calcium (iSDAsoil)": {
        "collection": "ISDASOIL/Africa/v1/calcium_extractable",
        "band": "mean_0_20",
        "scale": 30,
        "temporal": "static",
        "type": "static",
        "description": "Extractable calcium 0-20cm (30m, Africa only)",
        "stats": ["mean"],
        "output_col": "soil_Ca",
        "category": "Soil",
        "units": "ppm",
        "value_transform": {"type": "exp_scale_minus1", "scale": 10},
    },

    # ── Urban ─────────────────────────────────────────────────────────────────
    "Urban Settlement - GHSL": {
        "collection": "JRC/GHSL/P2023A/GHS_SMOD/2025",
        "band": "smod_code",
        "scale": 1000,
        "temporal": "static",
        "type": "static_mode",
        "description": "GHSL Settlement Model: Urban / Peri-urban / Rural classification (1km). "
                        "Published in 5-year epochs from 1975 to 2030 (all verified available in Earth "
                        "Engine) — defaults to the 2025 epoch, but the epoch is selectable: the Extract "
                        "page's Manual tab shows a year dropdown when this is checked, and Shmron AI can "
                        "build any other year via propose_custom_dataset (collection "
                        "'JRC/GHSL/P2023A/GHS_SMOD/<year>', same band/scale/type as this entry). The "
                        "output column name always carries the epoch, e.g. urban_class_2010, so it's "
                        "traceable from the CSV alone.",
        "stats": ["mode"],
        "output_col": "urban_smod_2025",
        "category": "Urban",
        "units": "categorical code (11-30), see the urban_class_<year> label column",
        "years": [1975, 1980, 1985, 1990, 1995, 2000, 2005, 2010, 2015, 2020, 2025, 2030],
        # Post-extraction: add a human-readable label column next to the numeric code
        "classify": {
            "source_col": "urban_smod_2025",
            "output_col": "urban_class_2025",
            "ranges": [
                (22, 30, "Urban"),
                (14, 21, "Peri-urban"),
                (11, 13, "Rural"),
            ],
            "default": "Unknown",
        },
    },

    "Night Lights - VIIRS": {
        "collection": "NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG",
        "band": "avg_rad",
        "scale": 500,
        "temporal": "static",
        "type": "static_fc",
        "description": "VIIRS monthly nighttime radiance composite, an urbanisation/SES proxy (500m)",
        "stats": ["mean"],
        "output_col": "ntl_rad",
        "category": "Urban",
        "units": "nW/cm2/sr (radiance)",
    },
    "Land Cover - MODIS": {
        "collection": "MODIS/061/MCD12Q1/2022_01_01",
        "band": "LC_Type1",
        "scale": 500,
        "temporal": "static",
        "type": "static_mode",
        "description": "MODIS IGBP land cover classification 2022 (500m) · 1=Evergreen forest … 17=Water",
        "stats": ["mode"],
        "output_col": "lc_igbp",
        "category": "Urban",
        "units": "categorical code (IGBP classes 1-17)",
    },

    # ── Water ─────────────────────────────────────────────────────────────────
    "Surface Water - JRC": {
        "collection": "JRC/GSW1_4/GlobalSurfaceWater",
        "band": ["occurrence", "seasonality"],
        "scale": 30,
        "temporal": "static",
        "type": "static",
        "description": "JRC Global Surface Water: occurrence (% of time) and seasonality (months/yr) at 30m",
        "stats": ["mean"],
        "output_col": ["occurrence", "seasonality"],
        "category": "Water",
        "units": {"occurrence": "% of time (0-100)", "seasonality": "months/year (0-12)"},
    },

    # ── Population ────────────────────────────────────────────────────────────
    "Population - WorldPop": {
        "collection": "WorldPop/GP/100m/pop",
        "band": "population",
        "scale": 100,
        "temporal": "static",
        "type": "static_fc",
        "description": "WorldPop estimated population count per 100m pixel",
        "stats": ["mean", "max"],
        "output_col": "pop_count",
        "category": "Population",
        "units": "people per pixel",
    },

    # ── Socioeconomic ─────────────────────────────────────────────────────────
    "Relative Wealth Index (Facebook)": {
        "collection": "projects/sat-io/open-datasets/facebook/relative_wealth_index",
        "band": "rwi",
        "scale": 2400,
        "temporal": "static",
        "type": "static_points_fc",
        # This EE asset is a FeatureCollection of points (one per ~2.4km
        # tile, each carrying an `rwi` property), not a raster — verified by
        # loading it directly and inspecting a feature. It cannot be loaded
        # as ee.Image/ImageCollection at all (that previously errored on
        # every single request and silently left this column blank for
        # every job that ever requested it); static_points_fc rasterizes it
        # via reduceToImage at its native ~2.4km tile scale before the usual
        # reduceRegion extraction.
        "description": "Facebook / Meta Relative Wealth Index (~2.4km)",
        "stats": ["mean"],
        "output_col": "rwi",
        "category": "Socioeconomic",
        "units": "dimensionless (standardized relative index; higher = wealthier)",
    },
}


def ghsl_for_year(year: int) -> dict:
    """
    Builds a GHSL dataset dict for a specific epoch, for use as a
    custom_datasets override keyed "Urban Settlement - GHSL" — the extractor
    merges custom_datasets over DATASETS by name, so this swaps in the chosen
    year for that one job without touching the catalog default.
    """
    base = DATASETS["Urban Settlement - GHSL"]
    if year not in base["years"]:
        raise ValueError(f"GHSL epoch {year} not available. Choose from {base['years']}")
    d = dict(base)
    d["collection"] = f"JRC/GHSL/P2023A/GHS_SMOD/{year}"
    d["output_col"] = f"urban_smod_{year}"
    d["classify"] = {
        **base["classify"],
        "source_col": f"urban_smod_{year}",
        "output_col": f"urban_class_{year}",
    }
    return d
