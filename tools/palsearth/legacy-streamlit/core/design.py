"""
PALs Earth — visual design tokens (Phase 1 of the redesign).

Single source of truth for the category color system and per-dataset short
codes (replacing emoji icons), used by the Extract, Dataset Reference, and
Home pages so they stay in sync.
"""

# ── Palette (from the redesign mockup) ──────────────────────────────────────
BG = "#f6f4ee"
BG_ALT = "#f4f1e8"
SURFACE = "#ffffff"
BRAND_900 = "#0b2a1d"
BRAND_800 = "#0f3d2a"
BRAND_700 = "#123f2b"
BRAND_600 = "#1d5a3c"
BRAND_500 = "#1f7a4a"
BRAND_400 = "#2f9e5b"
LIME = "#b8f35a"
LIME_LIGHT = "#c9f77a"
TEXT_MUTED = "#5d6b62"
TEXT_FAINT = "#8a948d"
BORDER = "#e2ddd0"
BORDER_STRONG = "#cfd9c8"

# ── Category colors — one hue per category, used for dataset tiles/chips ───
CATEGORY_COLORS = {
    "Vegetation":              "#2f9e5b",  # green
    "Climate":                 "#2f6fd6",  # blue
    "Land Surface Temperature":"#e07a2f",  # orange (heat)
    "Air Quality":             "#8b5cf6",  # purple
    "Terrain":                 "#8a5a3c",  # brown
    "Soil":                    "#c08a3e",  # tan/amber
    "Urban":                   "#5b7a94",  # slate
    "Water":                   "#2aa6a0",  # teal
    "Population":              "#c2568a",  # rose
    "Socioeconomic":           "#c9a227",  # gold
}

# ── Per-dataset short codes — replace emoji with a coloured code tile ──────
DATASET_CODES = {
    "EVI (MODIS)":                       "EVI",
    "NDVI (MODIS)":                      "NDVI",
    "Land Surface Temp - MODIS":         "LST",
    "Precipitation - CHIRPS":            "RAIN",
    "Temperature - ERA5-Land":           "TEMP",
    "Temperature + Wet Bulb - MERRA-2":  "WET",
    "Humidity - ERA5-Land":              "HUM",
    "Wind - ERA5-Land":                  "WIND",
    "Air Quality - CAMS NRT":            "AQ",
    "Aerosol Optical Depth - MAIAC":     "AOD",
    "NO₂ - Sentinel-5P":            "NO2",
    "CO - Sentinel-5P":                  "CO",
    "SO₂ - Sentinel-5P":            "SO2",
    "Elevation - MERIT DEM":             "DEM",
    "Soil Nitrogen (iSDAsoil)":          "N",
    "Soil Phosphorus (iSDAsoil)":        "P",
    "Soil Potassium (iSDAsoil)":         "K",
    "Soil Calcium (iSDAsoil)":           "CA",
    "Urban Settlement - GHSL":           "GHSL",
    "Night Lights - VIIRS":              "VIIRS",
    "Land Cover - MODIS":                "LC",
    "Surface Water - JRC":               "H2O",
    "Population - WorldPop":             "POP",
    "Relative Wealth Index (Facebook)":  "RWI",
}


def dataset_tile(name: str, category: str):
    """Returns (short_code, color_hex) for a dataset, falling back sensibly."""
    code = DATASET_CODES.get(name)
    if not code:
        code = "".join(w[0] for w in name.split()[:3]).upper()[:4]
    color = CATEGORY_COLORS.get(category, TEXT_MUTED)
    return code, color
