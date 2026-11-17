# -*- coding: utf-8 -*-
"""
Live, continent-scale accessibility engine for PRECISE/PALs.

Wraps a self-hosted Valhalla routing service (Africa-wide tiles, built from
/home/USER/pbf/africa-latest.osm.pbf) to answer three things no
precomputed table can:

  - route():                travel time/distance between two arbitrary points
  - nearest_facility():     which of a set of candidate facilities is nearest
                             (by travel time) - one /route call per facility;
                             see the function docstring for why the matrix
                             endpoint isn't used
  - disruption_impact():    before/after comparison when specific points
                             (e.g. a flooded crossing) are excluded from routing
  - regional_accessibility(): population-weighted accessibility (and, with
                             blocked_points, flood-disruption impact) across
                             many points at once - e.g. every populated H3
                             cell in a district

Unlike OSRM, Valhalla lets you exclude roads/areas *per request*
(`exclude_polygons`) without rebuilding or reprocessing anything - this is
the direct analog of flood_accessibility_pipeline.py's edge-removal
disruption modeling (CASCADE Zimbabwe flood work), translated from
"remove graph edges" to "exclude a small polygon around the blocked point
on this one request."

Valhalla's own `auto`/`pedestrian` costing models already encode road-type
speed and surface effects from OSM tags - the hand-rolled speed-limit and
surface-factor tables in `Popullation Weighted Metrics 2026.py` are not
needed here; Valhalla supersedes them.

No dependency on this being wired into Shmron or any other agent - these
are plain functions, callable from anywhere (a Flask route, a script, a
future tool definition).
"""
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

VALHALLA_URL = "http://127.0.0.1:8002"
DEFAULT_TIMEOUT = 15  # seconds - generous for a single route/matrix call


class AccessibilityEngineError(Exception):
    """Raised when Valhalla is unreachable or returns an unexpected shape."""


def _post(path, payload, timeout=DEFAULT_TIMEOUT):
    try:
        resp = requests.post(f"{VALHALLA_URL}{path}", json=payload, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise AccessibilityEngineError(f"Could not reach Valhalla at {VALHALLA_URL}{path}: {e}")
    if resp.status_code != 200:
        raise AccessibilityEngineError(
            f"Valhalla {path} returned {resp.status_code}: {resp.text[:500]}"
        )
    return resp.json()


def _buffer_polygon(lat, lon, buffer_m=50):
    """A small square ring around (lat, lon), ~buffer_m on each side -
    good enough to block the road(s) passing through that point without
    also blocking a much wider area. Valhalla wants [lon, lat] pairs and
    closes the ring itself if left open, but we close it explicitly anyway."""
    # ~1 degree latitude = 111,320 m; longitude scales by cos(latitude)
    dlat = buffer_m / 111_320.0
    dlon = buffer_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    ring = [
        [lon - dlon, lat - dlat],
        [lon + dlon, lat - dlat],
        [lon + dlon, lat + dlat],
        [lon - dlon, lat + dlat],
        [lon - dlon, lat - dlat],
    ]
    return ring


def route(origin_lat, origin_lon, dest_lat, dest_lon, costing="auto",
          exclude_points=None, buffer_m=50):
    """
    Travel distance (km) and time (minutes) between two points.

    exclude_points: optional list of (lat, lon) to route around (e.g. a
    flooded crossing) - each gets a small buffer polygon excluded from
    the route for this call only. No graph rebuild.

    Returns dict: {distance_km, time_min, found_route}. found_route=False
    (distance_km/time_min=None) when Valhalla can't connect the two points
    at all (e.g. every path is excluded) - not an exception, since "no
    route" is itself a meaningful result for disruption modeling.
    """
    payload = {
        "locations": [
            {"lat": origin_lat, "lon": origin_lon},
            {"lat": dest_lat, "lon": dest_lon},
        ],
        "costing": costing,
        "units": "kilometers",
    }
    if exclude_points:
        payload["exclude_polygons"] = [_buffer_polygon(lat, lon, buffer_m) for lat, lon in exclude_points]

    try:
        data = _post("/route", payload)
    except AccessibilityEngineError as e:
        # Valhalla returns a 400 with an error body (not a 200) when no
        # route can be found at all - distinguish that from a real outage.
        if "No path could be found" in str(e) or "400" in str(e):
            return {"distance_km": None, "time_min": None, "found_route": False}
        raise

    summary = data.get("trip", {}).get("summary", {})
    if "length" not in summary or "time" not in summary:
        raise AccessibilityEngineError(f"Unexpected /route response shape: {data}")
    return {
        "distance_km": summary["length"],
        "time_min": summary["time"] / 60.0,
        "found_route": True,
    }


def nearest_facility(origin_lat, origin_lon, facilities, costing="auto"):
    """
    Nearest reachable facility by travel time.

    Uses one /route call per facility rather than the /sources_to_targets
    matrix endpoint - verified against this deployment that the matrix
    endpoint's auto-selected 'costmatrix' algorithm genuinely fails on some
    reachable pairs that /route's algorithm succeeds on (confirmed: e.g.
    Harare -> Mutare, 262.7km/143.7min via /route, returns null/null via
    the matrix endpoint for the identical coordinates). Facility counts here
    are always small (a handful to a few dozen), so N /route calls is a
    small, worthwhile cost for correctness over the matrix endpoint's
    single-call efficiency.

    facilities: list of dicts, each with at least 'lat', 'lon', and any
    identifying fields you want echoed back (e.g. 'fac_code', 'name').

    Returns dict: {nearest: facility_dict_or_None, time_min, distance_km,
    all_facilities: [facility_dict + time_min/distance_km, ...]} - the
    full ranked list is included so callers can see runner-up facilities,
    not just the winner.
    """
    if not facilities:
        raise ValueError("facilities must be a non-empty list")

    ranked = []
    for fac in facilities:
        r = route(origin_lat, origin_lon, fac["lat"], fac["lon"], costing=costing)
        ranked.append({**fac, "time_min": r["time_min"], "distance_km": r["distance_km"]})

    reachable = [f for f in ranked if f["time_min"] is not None]
    reachable.sort(key=lambda f: f["time_min"])

    return {
        "nearest": reachable[0] if reachable else None,
        "time_min": reachable[0]["time_min"] if reachable else None,
        "distance_km": reachable[0]["distance_km"] if reachable else None,
        "all_facilities": sorted(ranked, key=lambda f: (f["time_min"] is None, f["time_min"])),
    }


def disruption_impact(origin_lat, origin_lon, blocked_points, dest_lat=None, dest_lon=None,
                       facilities=None, costing="auto", buffer_m=50):
    """
    Before/after comparison when blocked_points (e.g. flooded crossings)
    are excluded from routing. Direct analog of
    flood_accessibility_pipeline.py's build_graphs() edge removal, done
    live per-request instead of by rebuilding a graph.

    Exactly one of (dest_lat/dest_lon) or (facilities) must be given:
      - dest_lat/dest_lon: point-to-point disruption impact
      - facilities: nearest-facility disruption impact (uses
        nearest_facility() before/after)

    Returns dict: {baseline: {...}, disrupted: {...}, time_delta_min,
    distance_delta_km, cut_off} - cut_off=True if the disrupted case has
    no route/no reachable facility at all where the baseline did.
    """
    if (dest_lat is None) == (facilities is None):
        raise ValueError("Provide exactly one of (dest_lat & dest_lon) or facilities")

    if facilities is not None:
        # No direct exclude_polygons support in the matrix endpoint's
        # multi-target call in the same way as /route - run the baseline
        # matrix normally, then re-run nearest_facility() against each
        # candidate via /route with exclusions applied, since matrix-level
        # exclusion isn't a documented Valhalla capability. Simpler and
        # still one call per facility, which is fine - facility counts
        # here are small (a handful to a few dozen), not thousands.
        baseline = nearest_facility(origin_lat, origin_lon, facilities, costing=costing)
        disrupted_ranked = []
        for fac in facilities:
            r = route(origin_lat, origin_lon, fac["lat"], fac["lon"],
                      costing=costing, exclude_points=blocked_points, buffer_m=buffer_m)
            disrupted_ranked.append({**fac, "time_min": r["time_min"], "distance_km": r["distance_km"]})
        reachable = [f for f in disrupted_ranked if f["time_min"] is not None]
        reachable.sort(key=lambda f: f["time_min"])
        disrupted = {
            "nearest": reachable[0] if reachable else None,
            "time_min": reachable[0]["time_min"] if reachable else None,
            "distance_km": reachable[0]["distance_km"] if reachable else None,
            "all_facilities": sorted(disrupted_ranked, key=lambda f: (f["time_min"] is None, f["time_min"])),
        }
    else:
        baseline = route(origin_lat, origin_lon, dest_lat, dest_lon, costing=costing)
        disrupted = route(origin_lat, origin_lon, dest_lat, dest_lon, costing=costing,
                           exclude_points=blocked_points, buffer_m=buffer_m)

    cut_off = bool(baseline.get("time_min") is not None and disrupted.get("time_min") is None)
    time_delta = (
        disrupted["time_min"] - baseline["time_min"]
        if baseline.get("time_min") is not None and disrupted.get("time_min") is not None
        else None
    )
    distance_delta = (
        disrupted["distance_km"] - baseline["distance_km"]
        if baseline.get("distance_km") is not None and disrupted.get("distance_km") is not None
        else None
    )

    return {
        "baseline": baseline,
        "disrupted": disrupted,
        "time_delta_min": time_delta,
        "distance_delta_km": distance_delta,
        "cut_off": cut_off,
    }


def regional_accessibility(cells, facilities, blocked_points=None, costing="auto",
                            buffer_m=50, max_workers=16):
    """
    Population-weighted accessibility (and, with blocked_points, flood-
    disruption impact) across many points at once - e.g. every populated H3
    cell in a district. The regional counterpart to nearest_facility() /
    disruption_impact(), which only handle one origin at a time.

    cells: list of (cell_id, population, lat, lon) - exactly what api.py's
    _select_cells() produces once its (cell_id, rec, lat, lon) tuples are
    adapted (population = float(rec.get('p', 0) or 0)).

    facilities: same shape as nearest_facility() - list of dicts with at
    least 'lat'/'lon' and any identifying fields (a 'fac_code' or 'name'
    is used to key the per-facility catchment breakdown in the summary;
    falls back to a lat/lon string if neither is present).

    blocked_points: optional list of (lat, lon) - when given, computes the
    flooded scenario for every cell too, via ONE disruption_impact() call
    per cell (not a separate nearest_facility() + disruption_impact() pair -
    that would redundantly recompute the same baseline twice).

    Valhalla is local (127.0.0.1) so a real thread pool is safe and
    effective here - this MUST run concurrently, not serially: a district-
    sized region (low hundreds to ~1000 H3 res-7 cells) times a handful of
    facilities times up to 2 scenarios is hundreds to low thousands of
    individual Valhalla calls. Per-cell failures (a transient timeout under
    load) are caught and recorded rather than aborting the whole batch -
    one bad cell in a batch of hundreds shouldn't waste the rest.

    Returns {per_cell: [...], summary: {...}}:
      - per_cell: one row per input cell - cell_id, population, error
        (None on success), baseline_time_min, baseline_facility, and (if
        blocked_points) flooded_time_min, flooded_facility, cut_off.
      - summary: population-weighted regional numbers - n_cells,
        total_population, mean_baseline_time_min, baseline_catchment_population
        (dict: facility key -> population), and, if blocked_points:
        mean_flooded_time_min, pct_change, pop_cutoff,
        flooded_catchment_population - the CASCADE catchment-shift concept,
        generalized to any region.
    """
    if not cells:
        raise ValueError("cells must be a non-empty list")
    if not facilities:
        raise ValueError("facilities must be a non-empty list")

    def _facility_key(fac):
        if not fac:
            return None
        return fac.get("fac_code") or fac.get("name") or f"{fac['lat']:.5f},{fac['lon']:.5f}"

    def _compute_one(cell):
        cell_id, population, lat, lon = cell
        try:
            if blocked_points:
                di = disruption_impact(lat, lon, blocked_points, facilities=facilities,
                                        costing=costing, buffer_m=buffer_m)
                return {
                    "cell_id": cell_id, "population": population, "error": None,
                    "baseline_time_min": di["baseline"]["time_min"],
                    "baseline_facility": di["baseline"]["nearest"],
                    "flooded_time_min": di["disrupted"]["time_min"],
                    "flooded_facility": di["disrupted"]["nearest"],
                    "cut_off": di["cut_off"],
                }
            baseline = nearest_facility(lat, lon, facilities, costing=costing)
            return {
                "cell_id": cell_id, "population": population, "error": None,
                "baseline_time_min": baseline["time_min"],
                "baseline_facility": baseline["nearest"],
            }
        except AccessibilityEngineError as e:
            return {"cell_id": cell_id, "population": population, "error": str(e),
                    "baseline_time_min": None, "baseline_facility": None}

    per_cell = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_compute_one, c) for c in cells]
        for fut in as_completed(futures):
            per_cell.append(fut.result())

    def _weighted_mean(time_field):
        reach = [r for r in per_cell if r.get(time_field) is not None]
        pop = sum(r["population"] for r in reach)
        if pop <= 0:
            return None
        return sum(r[time_field] * r["population"] for r in reach) / pop

    def _catchment(facility_field):
        out = {}
        for r in per_cell:
            key = _facility_key(r.get(facility_field))
            if key is not None:
                out[key] = out.get(key, 0.0) + r["population"]
        return {k: round(v, 1) for k, v in out.items()}

    total_population = sum(r["population"] for r in per_cell)
    mean_baseline = _weighted_mean("baseline_time_min")
    summary = {
        "n_cells": len(per_cell),
        "total_population": round(total_population, 1),
        "mean_baseline_time_min": round(mean_baseline, 2) if mean_baseline is not None else None,
        "baseline_catchment_population": _catchment("baseline_facility"),
    }

    if blocked_points:
        mean_flooded = _weighted_mean("flooded_time_min")
        pop_cutoff = sum(r["population"] for r in per_cell if r.get("cut_off"))
        summary["mean_flooded_time_min"] = round(mean_flooded, 2) if mean_flooded is not None else None
        summary["pct_change"] = (
            round((mean_flooded - mean_baseline) / mean_baseline * 100, 2)
            if mean_baseline not in (None, 0) and mean_flooded is not None else None
        )
        summary["pop_cutoff"] = round(pop_cutoff, 1)
        summary["flooded_catchment_population"] = _catchment("flooded_facility")

    return {"per_cell": per_cell, "summary": summary}


def status():
    """Raw Valhalla /status response - use to confirm tiles are built and
    the service is actually serving (not just the container being up)."""
    try:
        resp = requests.get(f"{VALHALLA_URL}/status", timeout=5)
    except requests.exceptions.RequestException as e:
        raise AccessibilityEngineError(f"Could not reach Valhalla at {VALHALLA_URL}/status: {e}")
    return resp.json()
