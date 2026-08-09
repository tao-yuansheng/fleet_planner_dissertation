# simulation/routing.py
"""Router protocol + default HaversineRouter.

All distance/duration math in vrptw_engine flows through a single Router
instance held as a module-level singleton (see set_router/get_router). The
default HaversineRouter preserves the pre-v1.7 behaviour (haversine × 1.3,
constant 50 km/h). OSRMRouter is added in Task 3.
"""
import json
import urllib.request
from pathlib import Path
from typing import Protocol

import math

from freight_planner.shared.paths import LOGISTICS_ROOT


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate distance in km between two (lat, lon) points (WGS84).
    Inlined verbatim from profitability_report.profitability_report_merged —
    its only use here (2026-07-13 separation)."""
    R = 6371  # Earth radius km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

# Defaults reproduce the pre-v1.7 vrptw_engine constants.
DEFAULT_ROAD_FACTOR = 1.3
DEFAULT_AVG_SPEED_KMH = 50.0


class Router(Protocol):
    def distance_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float: ...
    def duration_h(self, lat1: float, lon1: float, lat2: float, lon2: float, depart_time=None) -> float: ...


class HaversineRouter:
    """Haversine straight-line × road-factor; duration = distance / avg-speed.

    Preserves pre-v1.7 vrptw_engine behaviour at default settings.
    """
    def __init__(self,
                 road_factor: float = DEFAULT_ROAD_FACTOR,
                 avg_speed_kmh: float = DEFAULT_AVG_SPEED_KMH):
        self.road_factor = road_factor
        self.avg_speed_kmh = avg_speed_kmh

    def distance_km(self, lat1, lon1, lat2, lon2):
        if (lat1, lon1) == (lat2, lon2):
            return 0.0
        return _haversine_km(lat1, lon1, lat2, lon2) * self.road_factor

    def duration_h(self, lat1, lon1, lat2, lon2, depart_time=None):
        return self.distance_km(lat1, lon1, lat2, lon2) / self.avg_speed_kmh


# --- Module-level TOD multiplier ---
_tod_multiplier: list | None = None


def set_tod_multiplier(vec: list) -> None:
    """Install a 24-element list of hour multipliers."""
    global _tod_multiplier
    _tod_multiplier = vec


def get_tod_multiplier() -> list | None:
    return _tod_multiplier


def reset_tod_multiplier() -> None:
    """Restore TOD multiplier to None — useful for test teardown."""
    global _tod_multiplier
    _tod_multiplier = None


# --- Module-level singleton ---
_active_router: Router = HaversineRouter()


def set_router(router: Router) -> None:
    """Install `router` as the active routing provider. Call before solver runs.

    Not thread-safe: assumes a single setter at process startup or between
    sequential batch runs, not concurrent reconfiguration from worker threads.
    """
    global _active_router
    _active_router = router


def get_router() -> Router:
    return _active_router


def reset_router() -> None:
    """Restore the default HaversineRouter — useful for test teardown."""
    global _active_router
    _active_router = HaversineRouter()


# ============================================================================
# OSRMRouter — real road distance/duration from a self-hosted OSRM server.
# Ported from legacy_pdptw/routing.py (single source of truth as of v1.7).
# ============================================================================
CACHE_PATH = LOGISTICS_ROOT / 'data' / 'Output' / 'osrm_cache.json'
DEFAULT_OSRM_URL = 'http://localhost:5000'
TRUCK_DURATION_FACTOR = 1.24  # OSRM car free-flow → truck time; calibrated on 1098 telematics journeys
DEFAULT_MAX_TABLE_SIZE = 100
TOD_MULTIPLIER_DEFAULT_PATH = LOGISTICS_ROOT / 'data' / 'Output' / 'cambridge' / 'tod_multiplier.json'


def coord_key(lat: float, lon: float) -> str:
    return f'{lat:.5f},{lon:.5f}'


def pair_key(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple:
    """Order-independent key for a coordinate pair (symmetric distances)."""
    return tuple(sorted((coord_key(lat1, lon1), coord_key(lat2, lon2))))


class OSRMRouter:
    """O(1) lookups from a {pair_key: (km, hours)} matrix; identical coords
    return 0; any missing pair is queried live from the OSRM server (and cached
    in-memory for subsequent calls).  Falls back to haversine only if the live
    query also fails, incrementing fallback_count."""
    def __init__(self, matrix: dict, fallback=None,
                 osrm_url: str = DEFAULT_OSRM_URL):
        self.matrix = matrix
        self.fallback = fallback or HaversineRouter()
        self.fallback_count = 0
        self.osrm_url = osrm_url

    def _lookup(self, lat1, lon1, lat2, lon2, idx):
        if (lat1, lon1) == (lat2, lon2):
            return 0.0
        k = pair_key(lat1, lon1, lat2, lon2)
        entry = self.matrix.get(k)
        if entry is None:
            # Pair was missed during bulk matrix build (coord added after
            # install_osrm_router, or a transient OSRM timeout during build).
            # Query the live server so we never silently use haversine for a
            # pair that OSRM can actually route — haversine can be wildly
            # wrong for long-haul legs and will cause the solver to plan
            # physically impossible routes.
            try:
                dists, durs = query_osrm_table(
                    [(lat1, lon1), (lat2, lon2)], self.osrm_url
                )
                km = dists[0][1] / 1000.0
                h  = (durs[0][1] / 3600.0) * TRUCK_DURATION_FACTOR
                # Apply the same zero-guard as build_osrm_matrix: if OSRM
                # returns near-zero for clearly distant points, use haversine.
                hvs_km = self.fallback.distance_km(lat1, lon1, lat2, lon2)
                if km < max(1.0, hvs_km * 0.1):
                    km = hvs_km
                    h  = self.fallback.duration_h(lat1, lon1, lat2, lon2)
                entry = (km, h)
                self.matrix[k] = entry   # cache in-memory for this session
            except Exception:
                # OSRM unreachable — degrade gracefully to haversine.
                self.fallback_count += 1
                fn = (self.fallback.distance_km if idx == 0
                      else self.fallback.duration_h)
                return fn(lat1, lon1, lat2, lon2)
        return entry[idx]

    def distance_km(self, lat1, lon1, lat2, lon2):
        return self._lookup(lat1, lon1, lat2, lon2, 0)

    def duration_h(self, lat1, lon1, lat2, lon2, depart_time=None):
        base = self._lookup(lat1, lon1, lat2, lon2, 1)
        if depart_time is None or get_tod_multiplier() is None:
            return base
        return base * get_tod_multiplier()[depart_time.hour]


def _http_get_json(url: str) -> dict:
    """Thin wrapper so tests can monkeypatch HTTP."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def query_osrm_table(coords: list, osrm_url: str) -> tuple:
    """Query OSRM /table for the full matrix over `coords` (list of (lat, lon)).
    Returns (distances_metres, durations_seconds) as 2D lists. Raises on non-Ok."""
    # fixed-point ONLY: default float formatting renders |lon| < 1e-4 (Greenwich-
    # meridian postcodes) in scientific notation, which OSRM rejects with HTTP 400
    coord_str = ';'.join(f'{lon:.6f},{lat:.6f}' for lat, lon in coords)
    url = f'{osrm_url}/table/v1/driving/{coord_str}?annotations=distance,duration'
    data = _http_get_json(url)
    if data.get('code') != 'Ok':
        raise RuntimeError(f"OSRM /table returned {data.get('code')}")
    return data['distances'], data['durations']


def load_cache(path=CACHE_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, 'r') as f:
        raw = json.load(f)
    return {tuple(k.split('|')): (v[0], v[1]) for k, v in raw.items()}


def save_cache(path, cache: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {f'{k[0]}|{k[1]}': [v[0], v[1]] for k, v in cache.items()}
    with open(path, 'w') as f:
        json.dump(serialisable, f)


def unique_coords(orders: list, vehicles: list) -> list:
    """All distinct (lat, lon) the batch can touch: vehicle depots plus every
    order origin and destination."""
    seen = set()
    for v in vehicles:
        lat = v['depot_lat'] if 'depot_lat' in v else v.get('last_lat')
        lon = v['depot_lon'] if 'depot_lon' in v else v.get('last_lon')
        if lat is not None and lon is not None:
            seen.add((lat, lon))
    for o in orders:
        if 'origin_lat' in o:
            seen.add((o['origin_lat'], o['origin_lon']))
        if 'dest_lat' in o:
            seen.add((o['dest_lat'], o['dest_lon']))
    return list(seen)


def build_osrm_matrix(coords: list, cache: dict, osrm_url: str,
                      max_table_size: int = DEFAULT_MAX_TABLE_SIZE) -> dict:
    """Ensure every pair among `coords` has a (km, hours) entry, querying OSRM
    only for uncached pairs. Mutates and returns `cache`.

    Zero-distance guard: OSRM /table can return 0 m / 0 s when it cannot route
    between two points (e.g. one is outside the loaded road network). Storing
    (0, 0) silently makes the solver plan impossibly fast cross-country routes.
    For any suspiciously short result we fall back to the haversine estimate so
    the solver at least sees a conservative bound.
    """
    chunk = max(1, max_table_size // 2)
    blocks = [coords[i:i + chunk] for i in range(0, len(coords), chunk)]
    _hvs = HaversineRouter()

    for bi in range(len(blocks)):
        for bj in range(bi, len(blocks)):
            block = blocks[bi] if bi == bj else blocks[bi] + blocks[bj]
            pairs = [(block[i], block[j])
                     for i in range(len(block)) for j in range(i + 1, len(block))]
            if all(pair_key(*a, *b) in cache for a, b in pairs):
                continue
            dists, durs = query_osrm_table(block, osrm_url)
            for i in range(len(block)):
                for j in range(i + 1, len(block)):
                    key = pair_key(*block[i], *block[j])
                    if key in cache:
                        continue
                    km = dists[i][j] / 1000.0
                    h  = (durs[i][j] / 3600.0) * TRUCK_DURATION_FACTOR
                    # Guard: OSRM returns 0 m / 0 s when it can't route the pair
                    # (destination outside loaded road network). Fall back to
                    # haversine so the solver sees a conservative estimate instead
                    # of treating two distant points as co-located.
                    hvs_km = _hvs.distance_km(*block[i], *block[j])
                    if km < max(1.0, hvs_km * 0.1):
                        km = hvs_km
                        h  = _hvs.duration_h(*block[i], *block[j])
                    cache[key] = (km, h)
    return cache


def get_route_geometry(
    waypoints: list,
    osrm_url: str = DEFAULT_OSRM_URL,
    max_points: int = 600,
) -> list | None:
    """Return road-snapped [[lat, lon], ...] geometry via OSRM /route.

    Calls OSRM once with all waypoints and extracts the overview LineString.
    Returns None if OSRM is unreachable or returns an error.  The caller
    should fall back to straight-line segments in that case.

    OSRM returns coordinates as [lon, lat]; this function flips them to
    [lat, lon] for consistency with the rest of the codebase.

    ``max_points`` caps the returned array length to keep HTML exports small.
    The subsample preserves the first and last point.
    """
    coord_str = ';'.join(f'{lon:.6f},{lat:.6f}' for lat, lon in waypoints)
    url = f'{osrm_url}/route/v1/driving/{coord_str}?overview=full&geometries=geojson'
    try:
        data = _http_get_json(url)
    except Exception:
        return None
    if data.get('code') != 'Ok' or not data.get('routes'):
        return None
    try:
        coords = data['routes'][0]['geometry']['coordinates']  # [[lon, lat], ...]
    except (KeyError, IndexError):
        return None
    # Subsample while keeping first + last so the route still starts/ends at depot
    if len(coords) > max_points:
        import numpy as np
        idx = np.linspace(0, len(coords) - 1, max_points, dtype=int).tolist()
        # Ensure endpoints are included
        if idx[0] != 0:
            idx[0] = 0
        if idx[-1] != len(coords) - 1:
            idx[-1] = len(coords) - 1
        coords = [coords[i] for i in idx]
    return [[c[1], c[0]] for c in coords]  # flip to [lat, lon]


def load_tod_multiplier(path=TOD_MULTIPLIER_DEFAULT_PATH):
    """Return a 24-length list of float multipliers, or None if file missing."""
    p = Path(path)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    if not isinstance(data, list) or len(data) != 24:
        return None
    return [float(x) for x in data]


def install_osrm_router(orders: list, vehicles: list,
                        osrm_url: str = DEFAULT_OSRM_URL,
                        cache_path=CACHE_PATH,
                        max_table_size: int = DEFAULT_MAX_TABLE_SIZE) -> OSRMRouter:
    """Warm the cache for this batch's coords, install OSRMRouter as the active
    router, and return it (caller may read fallback_count after dispatch)."""
    coords = unique_coords(orders, vehicles)
    cache = load_cache(cache_path)
    build_osrm_matrix(coords, cache, osrm_url, max_table_size)
    save_cache(cache_path, cache)
    router = OSRMRouter(matrix=cache, fallback=HaversineRouter(),
                        osrm_url=osrm_url)
    set_router(router)
    return router
