"""OSRM warm-up for a planner run.

Collects every coordinate the run can route between — vehicle positions, depot
anchors, candidate service points, and two-point (direct / hub-drop) origins —
and pre-warms the shared OSRM matrix over them so `route_costs.road_km` resolves
from in-memory lookups instead of stalling on lazy per-pair live queries.
"""
from __future__ import annotations

import pandas as pd

from freight_planner.shared.config import DEPOT_ANCHORS
from freight_planner import geocode, route_costs


def _coord(value) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def run_coords(candidate_df: pd.DataFrame, vehicle_df: pd.DataFrame, postcode_cache: dict) -> list[tuple[float, float]]:
    coords: set[tuple[float, float]] = set()

    if vehicle_df is not None and not vehicle_df.empty:
        for r in vehicle_df.itertuples(index=False):
            lat, lon = _coord(getattr(r, "current_lat", None)), _coord(getattr(r, "current_lon", None))
            if lat is not None and lon is not None:
                coords.add((lat, lon))

    for anchor in DEPOT_ANCHORS.values():
        coords.add((float(anchor[0]), float(anchor[1])))

    if candidate_df is not None and not candidate_df.empty:
        for r in candidate_df.itertuples(index=False):
            c = geocode.coords(str(getattr(r, "service_pc", "") or ""), postcode_cache)
            if c:
                coords.add((float(c[0]), float(c[1])))
            olat, olon = _coord(getattr(r, "origin_lat", None)), _coord(getattr(r, "origin_lon", None))
            if olat is not None and olon is not None:
                coords.add((olat, olon))

    return list(coords)


def warm_osrm_for_run(
    candidate_df: pd.DataFrame,
    vehicle_df: pd.DataFrame,
    postcode_cache: dict,
    osrm_url: str | None = None,
    cache_path=None,
):
    """Pre-warm + install the OSRM router for a run; returns (router, n_coords)."""
    coords = run_coords(candidate_df, vehicle_df, postcode_cache)
    router = route_costs.warm_and_install_osrm(
        coords, cache_path=cache_path, osrm_url=osrm_url)
    return router, len(coords)
