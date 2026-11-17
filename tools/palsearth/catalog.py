"""
Presentation layer over core.datasets — turns the technical catalog into the
shape the templates render: short code, colour, tint, group, kind, resolution
and a plain-language description of what each dataset measures.

core/ stays the single source of truth for what a dataset *is*; this module
only decides how it *looks* and how it's *explained*.
"""
from core.datasets import DATASETS
from core.design import DATASET_CODES

# ── Category → (colour, tint). Values taken from the redesign mockup; the
#    three categories the mockup had no card for reuse core.design's hues.
CATEGORY_STYLE = {
    "Vegetation":               ("#2f9e5b", "#e3f5ea"),
    "Climate":                  ("#2b7fd6", "#e2eefb"),
    "Land Surface Temperature": ("#c2410c", "#fbe7dc"),
    "Air Quality":              ("#7c4ddb", "#efe9fe"),
    "Terrain":                  ("#8a6a4a", "#f1ebe4"),
    "Soil":                     ("#b07c0c", "#f7eed7"),
    "Urban":                    ("#475569", "#e8ecf1"),
    "Water":                    ("#0f9fb0", "#dff4f6"),
    "Population":               ("#c2568a", "#f8e5ee"),
    "Socioeconomic":            ("#c9a227", "#f8f1d8"),
}
FALLBACK_STYLE = ("#5d6b62", "#ecefe9")

# ── Per-dataset colour overrides, where a distinct hue carries real meaning.
#    Wet bulb vs air temp vs land surface temp is the exact confusion this
#    tool exists to remove, so the three never share a colour.
DATASET_STYLE = {
    "Temperature - ERA5-Land":          ("#e8663d", "#fdeae3"),
    "Temperature + Wet Bulb - MERRA-2": ("#0f9fb0", "#dff4f6"),
    "Humidity - ERA5-Land":             ("#3aa0c9", "#e4f2f9"),
    "Wind - ERA5-Land":                 ("#6b93b8", "#e9eff6"),
    "Night Lights - VIIRS":             ("#d99a16", "#fbf1d9"),
}

# ── Which filter tab a category sits under (mockup: All / Climate / Land / People)
CATEGORY_GROUP = {
    "Vegetation": "Land", "Terrain": "Land", "Soil": "Land", "Water": "Land",
    "Climate": "Climate", "Land Surface Temperature": "Climate", "Air Quality": "Climate",
    "Urban": "People", "Population": "People", "Socioeconomic": "People",
}
GROUPS = ["All", "Climate", "Land", "People"]

# ── Plain-language descriptions. core's own `description` is written for
#    people who already know the collections; these are written for an
#    environmental epidemiologist who has never opened Earth Engine.
PLAIN = {
    "EVI (MODIS)":
        "How green and dense the vegetation is. Handles thick canopy better than "
        "NDVI, which flattens out once leaves get dense.",
    "NDVI (MODIS)":
        "The standard greenness index. High where leaves are abundant, low over "
        "bare soil, built-up land and water.",
    "Land Surface Temp - MODIS":
        "How hot the ground surface itself gets, measured separately for day and "
        "night. Runs hotter than air temperature, especially over bare ground.",
    "Precipitation - CHIRPS":
        "Daily rainfall, blending satellite estimates with rain-gauge records. "
        "Arrives with roughly a two-month lag, so don't expect last week's rain.",
    "Temperature - ERA5-Land":
        "Air temperature 2 m above the ground, the “shade temperature” a weather "
        "report quotes. This is the usual default for heat exposure.",
    "Temperature + Wet Bulb - MERRA-2":
        "Wet bulb temperature combines heat and humidity. It reflects how well a "
        "body can still cool itself by sweating, so it tracks heat stress on "
        "people better than air temperature alone.",
    "Humidity - ERA5-Land":
        "How much water vapour the air holds. Pair it with temperature when you "
        "care about whether heat is survivable rather than just high.",
    "Wind - ERA5-Land":
        "Wind speed near the surface. Moving air helps the body cool and disperses "
        "pollutants away from a location.",
    "Air Quality - CAMS NRT":
        "Modelled near-surface pollutants including PM2.5, the fine particles "
        "most consistently linked to respiratory and cardiac outcomes.",
    "Aerosol Optical Depth - MAIAC":
        "How much haze the whole air column holds. A satellite stand-in for "
        "particulate pollution where no ground monitors exist.",
    "NO₂ - Sentinel-5P":
        "Nitrogen dioxide, largely from traffic and combustion. A marker of fresh "
        "vehicle and industrial emissions.",
    "CO - Sentinel-5P":
        "Carbon monoxide from incomplete burning: biomass fires, cooking fuel "
        "and engines.",
    "SO₂ - Sentinel-5P":
        "Sulphur dioxide from coal burning, smelting and volcanic activity.",
    "Elevation - MERIT DEM":
        "Height above sea level, with satellite errors removed. Drives local "
        "temperature, rainfall and where water drains to.",
    "Soil Nitrogen (iSDAsoil)":
        "Total soil nitrogen across Africa at field scale. The nutrient that most "
        "often limits how well crops grow.",
    "Soil Phosphorus (iSDAsoil)":
        "Plant-available phosphorus in the soil, needed for root and seed "
        "development.",
    "Soil Potassium (iSDAsoil)":
        "Extractable potassium, which governs how well a crop regulates water.",
    "Soil Calcium (iSDAsoil)":
        "Extractable calcium, tied to soil acidity and soil structure.",
    "Urban Settlement - GHSL":
        "Classifies each place as rural, town or city from built-up density and "
        "population: the degree of urbanisation, in one code.",
    "Night Lights - VIIRS":
        "How brightly a place is lit at night. Widely used as a proxy for "
        "electrification and economic activity.",
    "Land Cover - MODIS":
        "What physically covers the ground: cropland, forest, grassland, built-up "
        "or water.",
    "Surface Water - JRC":
        "How often a location is under water, and in how many months of the year. "
        "Separates permanent water from seasonal flooding.",
    "Population - WorldPop":
        "Estimated number of people living in each 100 m cell.",
    "Relative Wealth Index (Facebook)":
        "Relative household wealth estimated from satellite imagery and "
        "connectivity data. Higher means wealthier than other places in the "
        "same country; it is not an income figure.",
}


def _res(scale):
    """Metres → the human string the mockup shows ('500 m', '11 km')."""
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        return "—"
    if scale < 1000:
        return f"{scale:g} m"
    km = scale / 1000.0
    return f"{km:.0f} km" if km >= 10 else f"{km:.1f} km".replace(".0 ", " ")


def _has_kelvin(units):
    """True if any band of this dataset is documented as Kelvin — drives the
    manual UI's conditional 'Temperature unit' toggle (Celsius conversion is
    only offered for datasets that are actually in Kelvin)."""
    if not units:
        return False
    if isinstance(units, dict):
        return any(v == "Kelvin" for v in units.values())
    return units == "Kelvin"


def _units_text(units):
    """core stores units as a str, or a dict when a dataset has several bands."""
    if not units:
        return "—"
    if isinstance(units, dict):
        return " · ".join(f"{k}: {v}" for k, v in units.items())
    return str(units)


def _bands_text(band):
    return ", ".join(band) if isinstance(band, (list, tuple)) else str(band)


def style_for(name, category):
    if name in DATASET_STYLE:
        return DATASET_STYLE[name]
    return CATEGORY_STYLE.get(category, FALLBACK_STYLE)


def short_for(name):
    code = DATASET_CODES.get(name)
    if not code:
        code = "".join(w[0] for w in name.split()[:3]).upper()[:4]
    return code


def view(name, info):
    """One dataset, in the shape the templates expect."""
    category = info.get("category") or "Other"
    color, tint = style_for(name, category)
    is_ts = info.get("type") == "timeseries"
    return {
        "id": name,
        "name": name,
        "short": short_for(name),
        "cat": category,
        "grp": CATEGORY_GROUP.get(category, "Land"),
        "kind": "Timeseries" if is_ts else "Static",
        "is_timeseries": is_ts,
        "has_kelvin": _has_kelvin(info.get("units")),
        "res": _res(info.get("scale")),
        "color": color,
        "tint": tint,
        "coll": info.get("collection", ""),
        "band": _bands_text(info.get("band", "")),
        "units": _units_text(info.get("units")),
        "temporal": info.get("temporal", ""),
        "stats": info.get("stats", []),
        "desc": PLAIN.get(name) or info.get("description", ""),
        "tech": info.get("description", ""),
        "years": info.get("years"),
        "search": f"{name} {category} {PLAIN.get(name, '')} {info.get('description', '')}".lower(),
    }


def all_views(custom=None):
    """Catalog datasets plus any this session's custom ones, in catalog order."""
    out = [view(name, info) for name, info in DATASETS.items()]
    for name, info in (custom or {}).items():
        v = view(name, {**info, "category": "Custom"})
        v["grp"] = "Custom"
        v["color"], v["tint"] = "#1d5a3c", "#e6f0e9"
        v["is_custom"] = True
        v["desc"] = info.get("description", "Verified against Earth Engine this session.")
        out.append(v)
    return out


def by_name(custom=None):
    return {v["name"]: v for v in all_views(custom)}


def chips_for(names, custom=None):
    """Small coloured name chips for a list of dataset names (jobs, proposals)."""
    idx = by_name(custom)
    out = []
    for n in names or []:
        v = idx.get(n)
        if v:
            out.append({"name": v["name"], "short": v["short"], "color": v["color"], "tint": v["tint"]})
        else:
            out.append({"name": n, "short": short_for(n), "color": "#5d6b62", "tint": "#ecefe9"})
    return out


# ── How-it-works cards on the Datasets page (mockup howSteps) ───────────────
HOW_STEPS = [
    ("01", "Upload locations",
     "A CSV with latitude/longitude columns, or a zipped shapefile of points or polygons.",
     "#2f9e5b"),
    ("02", "Pick data",
     "Choose datasets and statistics yourself, or describe your study to Shmron AI and let it choose.",
     "#2b7fd6"),
    ("03", "Submit & wait",
     "Jobs run in the background on the server. Progress updates live, so you can close the tab.",
     "#e8663d"),
    ("04", "Download",
     "Wide format: date + your own columns + one column per variable, with a units file alongside.",
     "#7c4ddb"),
]

# ── Starter prompts on the AI tab (mockup suggestions) ──────────────────────
SUGGESTIONS = [
    ("Pull temperature and rainfall for my uploaded locations, 2020 to 2024", "#2b7fd6", "#f3f8fe"),
    ("What is the difference between wet bulb, air temperature and land surface temperature?", "#0f9fb0", "#f1fafb"),
    ("I need PM2.5 for a heat-and-air-quality study", "#7c4ddb", "#f7f4ff"),
    ("What environmental data can you pull for my locations?", "#e8663d", "#fef6f2"),
]
