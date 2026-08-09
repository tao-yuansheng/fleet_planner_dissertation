"""Milestone 4: route cost primitives.

Distance, drive time, and service time are the physical-cost building blocks
the route evaluator walks through a sequence. Road calibration is shared with
the compatibility screen; customer service uses a fixed vehicle-type visit
allowance.

Distance flows through `road_km`. By default (no router installed) it is haversine
× road factor, which keeps tests deterministic and is the safe offline fallback.
A runner can install a real road router (OSRM, via `install_osrm_router`) at
startup so the whole evaluator/seed/ALNS optimises honest road distance without
any other code change — every km consumer already calls `road_km`.
"""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Protocol

from freight_planner.shared.config import (
    AVG_SPEED_KMH,
    ROAD_DISTANCE_FACTOR,
    customer_service_minutes,
)
from freight_planner import config
from freight_planner.config import DRIVE_BREAK_AFTER_MIN, DRIVE_BREAK_MIN

EARTH_RADIUS_KM = 6371.0


class RoadRouter(Protocol):
    def distance_km(self, a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float: ...


# Module-level routing provider. None => the legacy haversine × factor formula.
# Not thread-safe: install once at process startup (matches simulation.routing).
_active_router: RoadRouter | None = None

# Memo of road_km by (a_lat, a_lon, b_lat, b_lon). The ALNS re-evaluates the *same*
# customer-to-customer legs millions of times; without this each call rebuilds the
# router's coord/pair cache keys from scratch (profiled: ~15% of evaluate_route).
# Distances are a pure function of the coords for a fixed router, so the memo is
# result-preserving. It MUST be dropped whenever the router changes (below), else a
# prior router's distances would leak.
_km_cache: dict[tuple[float, float, float, float], float] = {}
# Parallel memo for OSRM per-type drive minutes (v1.1). Keyed by (coords + type).
# Cleared with _km_cache whenever the router changes, for the same result-preserving
# reason: a prior router's durations must not leak.
_min_cache: dict[tuple, float] = {}

# In-process memo of the on-disk OSRM pair cache, keyed by resolved path.
# `warm_and_install_osrm` is called once per epoch of a rolling run; without
# this it re-reads and re-JSON-parses the WHOLE cache file every single
# epoch (millions of entries by the end of a month-long run) purely to add
# a handful of new pairs. The dict is mutated in place by build_osrm_matrix,
# so within one process it only ever grows — safe to keep alive across calls.
# Cleared by reset_router() so tests and fresh runs never see a stale cache.
_disk_cache_by_path: dict[str, dict] = {}


def set_router(router: RoadRouter | None) -> None:
    """Install `router` as the active road-distance provider (or None to clear)."""
    global _active_router
    _active_router = router
    _km_cache.clear()
    _min_cache.clear()


def reset_router() -> None:
    """Restore the default haversine × road-factor model."""
    global _active_router
    _active_router = None
    _km_cache.clear()
    _min_cache.clear()
    _disk_cache_by_path.clear()


def get_router() -> RoadRouter | None:
    return _active_router


def install_osrm_router(cache_path=None, osrm_url: str | None = None) -> RoadRouter:
    """Install the shared OSRM router (real road distance) as the active provider.

    Reuses `simulation.routing` — its 29 MB pair-keyed cache and live-query +
    haversine fallback — so freight_planner and the simulation share one source
    of truth. Imported lazily so the common offline path never pulls OSRM deps.
    """
    from freight_planner.shared.routing import (
        CACHE_PATH,
        DEFAULT_OSRM_URL,
        HaversineRouter,
        OSRMRouter,
        load_cache,
    )

    cache = load_cache(cache_path or CACHE_PATH)
    router = OSRMRouter(matrix=cache, fallback=HaversineRouter(), osrm_url=osrm_url or DEFAULT_OSRM_URL)
    set_router(router)
    return router


def warm_and_install_osrm(coords, cache_path=None, osrm_url: str | None = None) -> RoadRouter:
    """Pre-warm the OSRM matrix over `coords` (batched /table), persist it, then
    install the router so the whole run routes from in-memory lookups.

    A full window touches many coord pairs the shared cache may not yet hold;
    lazy per-pair live queries stall the run. One batch build up front (and a
    cache save) makes subsequent lookups O(1) and amortises across runs.
    """
    from freight_planner.shared.routing import (
        CACHE_PATH,
        DEFAULT_OSRM_URL,
        HaversineRouter,
        OSRMRouter,
        build_osrm_matrix,
        load_cache,
        save_cache,
    )

    cpath = str(cache_path or CACHE_PATH)
    url = osrm_url or DEFAULT_OSRM_URL
    cache = _disk_cache_by_path.get(cpath)
    if cache is None:
        cache = load_cache(cpath)
        _disk_cache_by_path[cpath] = cache
    points = [(float(a), float(b)) for a, b in coords]
    before = len(cache)
    if points:
        build_osrm_matrix(points, cache, url)
        if len(cache) != before:
            save_cache(cpath, cache)
    if (isinstance(_active_router, OSRMRouter) and _active_router.matrix is cache
            and _active_router.osrm_url == url):
        # Same growing matrix AND same OSRM endpoint already installed this
        # process — reinstalling would only wipe the _km_cache/_min_cache
        # memo for no reason. A different osrm_url must still reinstall so
        # a live endpoint failover isn't silently ignored.
        return _active_router
    router = OSRMRouter(matrix=cache, fallback=HaversineRouter(), osrm_url=url)
    set_router(router)
    return router


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    dlat = radians(b_lat - a_lat)
    dlon = radians(b_lon - a_lon)
    x = sin(dlat / 2) ** 2 + cos(radians(a_lat)) * cos(radians(b_lat)) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(x))


def road_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    key = (a_lat, a_lon, b_lat, b_lon)
    cached = _km_cache.get(key)
    if cached is not None:
        return cached
    router = _active_router
    if router is not None:
        d = router.distance_km(a_lat, a_lon, b_lat, b_lon)
    else:
        d = haversine_km(a_lat, a_lon, b_lat, b_lon) * ROAD_DISTANCE_FACTOR
    _km_cache[key] = d
    return d


def drive_minutes(km: float) -> float:
    return (float(km) / AVG_SPEED_KMH) * 60.0 * config.TRAVEL_TIME_SLACK


def road_minutes(a_lat: float, a_lon: float, b_lat: float, b_lon: float, vehicle_type: str) -> float:
    """Drive minutes for one leg.

    With config.USE_OSRM_DURATIONS and an OSRM router installed, use the router's
    truck-adjusted duration (car × TRUCK_DURATION_FACTOR), divide that factor back
    out to recover CAR free-flow, then apply the per-vehicle-type factor. Otherwise
    (default, tests, offline) this is byte-identical to drive_minutes(road_km(...)).
    """
    from freight_planner import config
    router = _active_router
    if config.USE_OSRM_DURATIONS and router is not None and hasattr(router, "duration_h"):
        key = (a_lat, a_lon, b_lat, b_lon, str(vehicle_type).lower())
        cached = _min_cache.get(key)
        if cached is not None:
            return cached
        from freight_planner.shared.routing import TRUCK_DURATION_FACTOR
        hours = router.duration_h(a_lat, a_lon, b_lat, b_lon)
        # leg length picks the calibration band (urban-mix base vs motorway trunk)
        leg_km = road_km(a_lat, a_lon, b_lat, b_lon)
        minutes = hours / TRUCK_DURATION_FACTOR * config.duration_factor_for(vehicle_type, leg_km) * 60.0
        _min_cache[key] = minutes
        return minutes
    return drive_minutes(road_km(a_lat, a_lon, b_lat, b_lon))


def osrm_durations_active() -> bool:
    """True when OSRM per-road-type durations are enabled AND a duration-capable
    router is installed -- the exact predicate road_minutes uses to choose OSRM
    over the constant-speed fallback."""
    from freight_planner import config
    return (bool(config.USE_OSRM_DURATIONS) and _active_router is not None
            and hasattr(_active_router, "duration_h"))


def statutory_breaks(drive_since_break_min: float, drive_min: float) -> tuple[float, float]:
    """Break minutes owed while driving ``drive_min`` more, having driven
    ``drive_since_break_min`` since the last break (EU 561/2006 core rule:
    45 min after 4.5 h cumulative driving). A long leg can owe several — the
    driver stops at services mid-leg. Landing exactly on the limit owes the
    break before the NEXT drive, not this one.
    Returns (break_minutes, new_drive_since_break)."""
    total = float(drive_since_break_min) + float(drive_min)
    n = int(total // DRIVE_BREAK_AFTER_MIN)
    if n and total % DRIVE_BREAK_AFTER_MIN == 0.0:
        n -= 1
    return n * DRIVE_BREAK_MIN, total - n * DRIVE_BREAK_AFTER_MIN


def service_minutes(pallets: float, vehicle_type: str = "tractor") -> float:
    """Fixed visit dwell; ``pallets`` remains for caller API compatibility."""
    return customer_service_minutes(vehicle_type)
