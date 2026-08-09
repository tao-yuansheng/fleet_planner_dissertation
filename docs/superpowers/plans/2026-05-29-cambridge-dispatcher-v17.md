# Cambridge Dispatcher v1.7 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace haversine × 1.3 + 50 km/h with OSRM road routing + truck-calibrated durations in the Cambridge dispatcher, and rebuild the planned on-time metric on real per-stop arrival times.

**Architecture:** New `simulation/routing.py` exposes a `Router` protocol with `HaversineRouter` (default, parity-preserving) and `OSRMRouter` (cache + `/table`). `vrptw_engine.py` holds a module-level router singleton; all distance/duration math flows through it. `_estimated_return_time` becomes `_walk_schedule` returning per-stop arrivals; `compute_planned_on_time` in backtest reads those instead of the old linear proxy.

**Tech Stack:** Python 3.11, pytest, OSRM (Docker, GB extract pre-built at `E:/BEAT/osrm/`), urllib (stdlib HTTP).

**Project Constraint — NO COMMITS:** Per the user's explicit instruction, this work stays local. **Do NOT run `git commit`, `git push`, or any state-modifying git command at any point.** Every step that the writing-plans skill template ordinarily ends with "Commit" is replaced with "Save and continue". The implementation is validated locally via pytest and backtest runs only.

**Spec:** [`docs/superpowers/specs/2026-05-29-cambridge-dispatcher-v17-design.md`](../specs/2026-05-29-cambridge-dispatcher-v17-design.md)

**Working directory:** `e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics/` (all paths below are relative to this unless absolute).

**Test runner:** `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest <path> -v` (the imports use `sys.path.insert` to find `simulation/`, run from this dir).

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `simulation/routing.py` | NEW | `Router` protocol, `HaversineRouter`, `OSRMRouter`, cache I/O, singleton accessors |
| `simulation/vrptw_engine.py` | MODIFY | Replace haversine + speed constant with router calls; rename `_estimated_return_time` → `_walk_schedule`; add `RouteSchedule` dataclass |
| `simulation/vrptw_alns.py` | MODIFY | Inject `arrival_iso` into each stop dict in route output |
| `legacy_pdptw/routing.py` | MODIFY | Re-export from `simulation.routing` (single source of truth) |
| `cambridge/config.py` | MODIFY | Add `OSRM_URL`, `OSRM_ENABLED` (env-var driven) |
| `cambridge/dispatcher.py` | MODIFY | At start of `run_day_multi_trip`, call `install_osrm_router(...)` when `OSRM_ENABLED` |
| `cambridge/backtest.py` | MODIFY | Rewrite `compute_planned_on_time` to consume `arrival_iso`; delete linear proxy |
| `tests/test_routing.py` | NEW | Haversine parity, OSRMRouter cache hit/miss/fallback, mocked `/table` |
| `tests/test_vrptw_engine.py` | MODIFY | Update for `_walk_schedule` return shape; add default-router behavior tests |
| `tests/cambridge/test_backtest.py` | MODIFY | Update `compute_planned_on_time` tests to use `arrival_iso` |

---

## Task 1: Routing module skeleton — `Router` protocol + `HaversineRouter`

**Files:**
- Create: `simulation/routing.py`
- Create: `tests/test_routing.py`

- [ ] **Step 1.1: Write the failing parity test**

```python
# tests/test_routing.py
import os
import sys
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'simulation'))

from routing import HaversineRouter
from profitability_report.profitability_report_merged import _haversine_km


def test_haversine_router_distance_matches_legacy_factor():
    """HaversineRouter.distance_km must equal _haversine_km * 1.3 (the current
    ROAD_DISTANCE_FACTOR). This is the parity contract: swapping the router
    into vrptw_engine cannot change any pre-OSRM numbers."""
    r = HaversineRouter()
    # London → Cambridge depot (approx)
    km = r.distance_km(51.5074, -0.1278, 52.10172, 0.16229)
    expected = _haversine_km(51.5074, -0.1278, 52.10172, 0.16229) * 1.3
    assert math.isclose(km, expected, abs_tol=0.001)


def test_haversine_router_duration_matches_50kmh_constant():
    """duration_h = distance_km / 50 (the current AVG_SPEED_KMH)."""
    r = HaversineRouter()
    h = r.duration_h(51.5074, -0.1278, 52.10172, 0.16229)
    expected_km = _haversine_km(51.5074, -0.1278, 52.10172, 0.16229) * 1.3
    assert math.isclose(h, expected_km / 50.0, abs_tol=0.0001)


def test_haversine_router_identical_coords_returns_zero():
    r = HaversineRouter()
    assert r.distance_km(52.1, 0.16, 52.1, 0.16) == 0.0
    assert r.duration_h(52.1, 0.16, 52.1, 0.16) == 0.0


def test_haversine_router_custom_factor_overrides_default():
    """Allow tests/callers to dial in a non-default factor."""
    r = HaversineRouter(road_factor=1.5, avg_speed_kmh=60.0)
    km = r.distance_km(51.5, -0.1, 52.1, 0.2)
    expected = _haversine_km(51.5, -0.1, 52.1, 0.2) * 1.5
    assert math.isclose(km, expected, abs_tol=0.001)
    assert math.isclose(r.duration_h(51.5, -0.1, 52.1, 0.2), expected / 60.0, abs_tol=0.0001)
```

- [ ] **Step 1.2: Run the test — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_routing.py -v`
Expected: `ModuleNotFoundError: No module named 'routing'` or `ImportError: cannot import name 'HaversineRouter'`.

- [ ] **Step 1.3: Create `simulation/routing.py` with protocol + HaversineRouter**

```python
# simulation/routing.py
"""Router protocol + default HaversineRouter.

All distance/duration math in vrptw_engine flows through a single Router
instance held as a module-level singleton (see set_router/get_router). The
default HaversineRouter preserves the pre-v1.7 behaviour (haversine × 1.3,
constant 50 km/h). OSRMRouter is added in Task 3.
"""
import os
import sys
from typing import Protocol

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from profitability_report.profitability_report_merged import _haversine_km

# Defaults reproduce the pre-v1.7 vrptw_engine constants.
DEFAULT_ROAD_FACTOR = 1.3
DEFAULT_AVG_SPEED_KMH = 50.0


class Router(Protocol):
    def distance_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float: ...
    def duration_h(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float: ...


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

    def duration_h(self, lat1, lon1, lat2, lon2):
        return self.distance_km(lat1, lon1, lat2, lon2) / self.avg_speed_kmh


# --- Module-level singleton ---
_active_router: Router = HaversineRouter()


def set_router(router: Router) -> None:
    """Install `router` as the active routing provider. Call before solver runs."""
    global _active_router
    _active_router = router


def get_router() -> Router:
    return _active_router


def reset_router() -> None:
    """Restore the default HaversineRouter — useful for test teardown."""
    global _active_router
    _active_router = HaversineRouter()
```

- [ ] **Step 1.4: Run the test — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_routing.py -v`
Expected: 4 tests pass.

- [ ] **Step 1.5: Save and continue (no commit, per project constraint)**

---

## Task 2: Wire router singleton into `vrptw_engine` and replace haversine call sites

**Files:**
- Modify: `simulation/vrptw_engine.py`
- Modify: `tests/test_vrptw_engine.py`

The four current `_haversine_km × ROAD_DISTANCE_FACTOR` call sites are:
- `route_distance_km` (lines ~98–100)
- `_estimated_return_time` (lines ~149–154) — also uses `/AVG_SPEED_KMH`
- `try_insert` depot-distance guard (line ~170)
- `compute_direct_run` (lines ~224–227)

After this task, none of these compute distance directly — they call `_router.distance_km`. The module-level `ROAD_DISTANCE_FACTOR` and `AVG_SPEED_KMH` constants are **deleted** to prevent silent reintroduction. `KM_TO_MILES` stays (it's not a routing constant).

- [ ] **Step 2.1: Write a parity test**

Add to `tests/test_vrptw_engine.py`:

```python
def test_route_distance_with_default_router_matches_legacy():
    """With the default HaversineRouter installed (the implicit state at
    module import), route_distance_km on a 3-stop route must produce the same
    value the engine produced before v1.7. The exact number is captured here
    as a regression guard."""
    from routing import reset_router
    reset_router()  # ensure clean state
    stops = [
        _stop(order_id='A', lat=52.20, lon=0.16),
        _stop(order_id='B', lat=52.05, lon=0.30),
        _stop(order_id='C', lat=52.15, lon=-0.10),
    ]
    route = _route(stops=stops)
    km = route_distance_km(route)
    # Computed manually against haversine × 1.3 — recompute if these constants
    # change. Numbers are in km, depot at (52.10172, 0.16229).
    # depot→A: ~11.0 km × 1.3 = 14.3
    # A→B:     ~19.7 km × 1.3 = 25.6
    # B→C:     ~21.2 km × 1.3 = 27.6
    # C→depot: ~18.5 km × 1.3 = 24.0
    # Total: ~91.5 km (recompute if a coord changes)
    assert 88.0 < km < 95.0


def test_set_router_changes_distance_calculation():
    """Custom router with different road factor must produce different km."""
    from routing import HaversineRouter, set_router, reset_router
    stop = _stop(lat=52.20, lon=0.16)
    route = _route(stops=[stop])
    set_router(HaversineRouter(road_factor=1.0))  # straight line
    km_no_factor = route_distance_km(route)
    reset_router()
    km_default = route_distance_km(route)
    assert km_default > km_no_factor
    # ratio must equal 1.3 (default road factor)
    import math
    assert math.isclose(km_default / km_no_factor, 1.3, abs_tol=0.01)
```

- [ ] **Step 2.2: Run the new tests — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_engine.py::test_set_router_changes_distance_calculation -v`
Expected: `ModuleNotFoundError: No module named 'routing'` (the engine hasn't been wired yet).

- [ ] **Step 2.3: Edit `simulation/vrptw_engine.py` — replace haversine call sites**

Apply the following edits:

**A. Top of file — replace `ROAD_DISTANCE_FACTOR` and `AVG_SPEED_KMH` constants with a router import.**

Replace:

```python
KM_TO_MILES = 0.621371
AVG_SPEED_KMH = 50.0

VEHICLE_ACTIVATION_COST: float = 150.0
UNASSIGNED_PENALTY: float = 50_000.0
_SERVICE_HOURS_PER_STOP: float = 20.0 / 60.0
# UK urban/suburban road distance is ~30% longer than straight-line haversine.
# Applied to route_distance_km so planned km and fuel cost reflect real roads.
ROAD_DISTANCE_FACTOR: float = 1.3
```

with:

```python
KM_TO_MILES = 0.621371

VEHICLE_ACTIVATION_COST: float = 150.0
UNASSIGNED_PENALTY: float = 50_000.0
_SERVICE_HOURS_PER_STOP: float = 20.0 / 60.0

# All distance/duration math flows through the active Router. The default is
# HaversineRouter which reproduces the pre-v1.7 behaviour (haversine × 1.3,
# 50 km/h). Override at runtime with simulation.routing.set_router(...).
from routing import get_router as _get_router
```

**B. `route_distance_km` — call router.distance_km, drop the ROAD_DISTANCE_FACTOR multiply.**

Replace:

```python
def route_distance_km(route: DeliveryRoute) -> float:
    """Closed-loop km: depot → each stop in order → back to depot.

    Applies ROAD_DISTANCE_FACTOR to convert haversine to estimated road km.
    """
    if not route.stops:
        return 0.0
    total = 0.0
    prev_lat, prev_lon = route.depot_lat, route.depot_lon
    for stop in route.stops:
        total += _haversine_km(prev_lat, prev_lon, stop.lat, stop.lon)
        prev_lat, prev_lon = stop.lat, stop.lon
    total += _haversine_km(prev_lat, prev_lon, route.depot_lat, route.depot_lon)
    return total * ROAD_DISTANCE_FACTOR
```

with:

```python
def route_distance_km(route: DeliveryRoute) -> float:
    """Closed-loop km: depot → each stop in order → back to depot.

    Distance per leg is delegated to the active Router (HaversineRouter by
    default; OSRMRouter when OSRM is enabled).
    """
    if not route.stops:
        return 0.0
    router = _get_router()
    total = 0.0
    prev_lat, prev_lon = route.depot_lat, route.depot_lon
    for stop in route.stops:
        total += router.distance_km(prev_lat, prev_lon, stop.lat, stop.lon)
        prev_lat, prev_lon = stop.lat, stop.lon
    total += router.distance_km(prev_lat, prev_lon, route.depot_lat, route.depot_lon)
    return total
```

**C. `_estimated_return_time` — call router.duration_h.**

Replace:

```python
def _estimated_return_time(route: DeliveryRoute) -> datetime:
    """Walk the route in time and return estimated depot-return datetime."""
    t = route.shift_start
    prev_lat, prev_lon = route.depot_lat, route.depot_lon
    for stop in route.stops:
        leg_h = (_haversine_km(prev_lat, prev_lon, stop.lat, stop.lon)
                 * ROAD_DISTANCE_FACTOR / AVG_SPEED_KMH)
        t += timedelta(hours=leg_h + _svc(stop))
        prev_lat, prev_lon = stop.lat, stop.lon
    return_h = (_haversine_km(prev_lat, prev_lon, route.depot_lat, route.depot_lon)
                * ROAD_DISTANCE_FACTOR / AVG_SPEED_KMH)
    return t + timedelta(hours=return_h)
```

with (note: full `_walk_schedule` arrives in Task 4; this is the interim leg-time replacement):

```python
def _estimated_return_time(route: DeliveryRoute) -> datetime:
    """Walk the route in time and return estimated depot-return datetime.

    Per-leg travel time delegated to the active Router.duration_h.
    """
    router = _get_router()
    t = route.shift_start
    prev_lat, prev_lon = route.depot_lat, route.depot_lon
    for stop in route.stops:
        leg_h = router.duration_h(prev_lat, prev_lon, stop.lat, stop.lon)
        t += timedelta(hours=leg_h + _svc(stop))
        prev_lat, prev_lon = stop.lat, stop.lon
    return_h = router.duration_h(prev_lat, prev_lon, route.depot_lat, route.depot_lon)
    return t + timedelta(hours=return_h)
```

**D. `try_insert` depot-distance guard.**

Replace:

```python
    if max_depot_km is not None:
        if _haversine_km(route.depot_lat, route.depot_lon, stop.lat, stop.lon) > max_depot_km:
            return None
```

with:

```python
    if max_depot_km is not None:
        if _get_router().distance_km(route.depot_lat, route.depot_lon, stop.lat, stop.lon) > max_depot_km:
            return None
```

**E. `compute_direct_run`.**

Replace:

```python
    km = ((_haversine_km(depot_lat, depot_lon, origin_lat, origin_lon)
           + _haversine_km(origin_lat, origin_lon, dest_lat, dest_lon)
           + _haversine_km(dest_lat, dest_lon, depot_lat, depot_lon))
          * ROAD_DISTANCE_FACTOR)
```

with:

```python
    r = _get_router()
    km = (r.distance_km(depot_lat, depot_lon, origin_lat, origin_lon)
          + r.distance_km(origin_lat, origin_lon, dest_lat, dest_lon)
          + r.distance_km(dest_lat, dest_lon, depot_lat, depot_lon))
```

**F. Remove the now-unused `_haversine_km` import** if no other call sites remain. Search the file for `_haversine_km`. After edits A–E, it should appear in the import line only — delete that import. Keep `_normalise_type_key` and `_rate_bundle`.

The import currently reads:

```python
from profitability_report.profitability_report_merged import (
    _haversine_km,
    _normalise_type_key,
    _rate_bundle,
)
```

Replace with:

```python
from profitability_report.profitability_report_merged import (
    _normalise_type_key,
    _rate_bundle,
)
```

- [ ] **Step 2.4: Run all engine tests**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_engine.py tests/test_routing.py -v`
Expected: all tests pass (including the new parity tests from Step 2.1, plus the pre-existing tests since default router preserves behaviour).

- [ ] **Step 2.5: Run the full suite to catch downstream breakage**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest -v 2>&1 | tail -30`
Expected: pre-existing pass count holds (~131 + 6 new = ~137). If any test breaks, it means a call site outside the file imports `ROAD_DISTANCE_FACTOR` or `AVG_SPEED_KMH` directly — search for those names and update.

- [ ] **Step 2.6: Save and continue (no commit)**

---

## Task 3: Add `OSRMRouter` + cache I/O to `simulation/routing.py`

**Files:**
- Modify: `simulation/routing.py`
- Modify: `tests/test_routing.py`

Port `OSRMRouter`, `load_cache`, `save_cache`, `query_osrm_table`, `build_osrm_matrix`, `install_osrm_router`, `unique_coords`, `coord_key`, `pair_key`, `TRUCK_DURATION_FACTOR`, `DEFAULT_OSRM_URL`, `DEFAULT_MAX_TABLE_SIZE`, and `CACHE_PATH` from `legacy_pdptw/routing.py` (lines 16–151 in the existing file). The implementation is unchanged — the only difference is that `set_router` lives in the same module, so the legacy `from pdp_route import ... set_router` becomes a local call.

- [ ] **Step 3.1: Write the OSRMRouter tests**

Append to `tests/test_routing.py`:

```python
from unittest.mock import patch


def test_osrm_router_returns_cached_distance():
    from routing import OSRMRouter, HaversineRouter, pair_key
    matrix = {pair_key(52.1, 0.16, 52.2, 0.30): (42.5, 0.85)}
    r = OSRMRouter(matrix=matrix, fallback=HaversineRouter())
    assert r.distance_km(52.1, 0.16, 52.2, 0.30) == 42.5
    assert r.duration_h(52.1, 0.16, 52.2, 0.30) == 0.85
    assert r.fallback_count == 0


def test_osrm_router_falls_back_when_pair_missing():
    from routing import OSRMRouter, HaversineRouter
    r = OSRMRouter(matrix={}, fallback=HaversineRouter())
    km = r.distance_km(52.1, 0.16, 52.2, 0.30)
    # Falls back to haversine × 1.3
    assert km > 0
    assert r.fallback_count == 1


def test_osrm_router_identical_coords_returns_zero():
    from routing import OSRMRouter, HaversineRouter
    r = OSRMRouter(matrix={}, fallback=HaversineRouter())
    assert r.distance_km(52.1, 0.16, 52.1, 0.16) == 0.0
    assert r.fallback_count == 0  # zero is special-cased, not a fallback


def test_query_osrm_table_uses_truck_factor_on_durations():
    """Mock _http_get_json so test runs without a live OSRM server."""
    from routing import build_osrm_matrix, TRUCK_DURATION_FACTOR
    fake_table = {
        'code': 'Ok',
        'distances': [[0, 50000], [50000, 0]],          # metres
        'durations': [[0, 3600], [3600, 0]],            # seconds
    }
    with patch('routing._http_get_json', return_value=fake_table):
        cache = {}
        coords = [(52.1, 0.16), (52.2, 0.30)]
        build_osrm_matrix(coords, cache, 'http://fake', max_table_size=100)
    # 50 000 m → 50.0 km; 3600 s → 1.0 h × 1.24 truck factor = 1.24 h
    assert len(cache) == 1
    (km, h) = next(iter(cache.values()))
    assert km == 50.0
    assert abs(h - TRUCK_DURATION_FACTOR) < 1e-6


def test_cache_roundtrip(tmp_path):
    from routing import save_cache, load_cache, pair_key
    cache = {pair_key(52.1, 0.16, 52.2, 0.30): (42.5, 0.85)}
    path = tmp_path / 'cache.json'
    save_cache(path, cache)
    loaded = load_cache(path)
    assert loaded == cache
```

- [ ] **Step 3.2: Run new tests — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_routing.py::test_osrm_router_returns_cached_distance -v`
Expected: `ImportError: cannot import name 'OSRMRouter' from 'routing'`.

- [ ] **Step 3.3: Append the OSRMRouter implementation to `simulation/routing.py`**

Add to the end of the file:

```python
# ============================================================================
# OSRMRouter — real road distance/duration from a self-hosted OSRM server.
# Ported from legacy_pdptw/routing.py (single source of truth as of v1.7).
# ============================================================================
import json
import urllib.request
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parents[1] / 'data' / 'Output' / 'osrm_cache.json'
DEFAULT_OSRM_URL = 'http://localhost:5000'
TRUCK_DURATION_FACTOR = 1.24  # OSRM car free-flow → truck time; calibrated on 1098 telematics journeys
DEFAULT_MAX_TABLE_SIZE = 100


def coord_key(lat: float, lon: float) -> str:
    return f'{lat:.5f},{lon:.5f}'


def pair_key(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple:
    """Order-independent key for a coordinate pair (symmetric distances)."""
    return tuple(sorted((coord_key(lat1, lon1), coord_key(lat2, lon2))))


class OSRMRouter:
    """O(1) lookups from a {pair_key: (km, hours)} matrix; identical coords
    return 0; any missing pair falls back to `fallback` and increments
    fallback_count."""
    def __init__(self, matrix: dict, fallback=None):
        self.matrix = matrix
        self.fallback = fallback or HaversineRouter()
        self.fallback_count = 0

    def _lookup(self, lat1, lon1, lat2, lon2, idx):
        if (lat1, lon1) == (lat2, lon2):
            return 0.0
        entry = self.matrix.get(pair_key(lat1, lon1, lat2, lon2))
        if entry is None:
            self.fallback_count += 1
            fn = self.fallback.distance_km if idx == 0 else self.fallback.duration_h
            return fn(lat1, lon1, lat2, lon2)
        return entry[idx]

    def distance_km(self, lat1, lon1, lat2, lon2):
        return self._lookup(lat1, lon1, lat2, lon2, 0)

    def duration_h(self, lat1, lon1, lat2, lon2):
        return self._lookup(lat1, lon1, lat2, lon2, 1)


def _http_get_json(url: str) -> dict:
    """Thin wrapper so tests can monkeypatch HTTP."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def query_osrm_table(coords: list, osrm_url: str) -> tuple:
    """Query OSRM /table for the full matrix over `coords` (list of (lat, lon)).
    Returns (distances_metres, durations_seconds) as 2D lists. Raises on non-Ok."""
    coord_str = ';'.join(f'{lon},{lat}' for lat, lon in coords)
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
    only for uncached pairs. Mutates and returns `cache`."""
    chunk = max(1, max_table_size // 2)
    blocks = [coords[i:i + chunk] for i in range(0, len(coords), chunk)]

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
                    h = (durs[i][j] / 3600.0) * TRUCK_DURATION_FACTOR
                    cache[key] = (km, h)
    return cache


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
    router = OSRMRouter(matrix=cache, fallback=HaversineRouter())
    set_router(router)
    return router
```

- [ ] **Step 3.4: Run the new tests — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_routing.py -v`
Expected: all 9 tests pass (4 from Task 1 + 5 new).

- [ ] **Step 3.5: Save and continue (no commit)**

---

## Task 4: Add `RouteSchedule` and convert `_estimated_return_time` → `_walk_schedule`

**Files:**
- Modify: `simulation/vrptw_engine.py`
- Modify: `tests/test_vrptw_engine.py`

The new `_walk_schedule` returns a `RouteSchedule` containing per-stop arrival times plus the depot-return time. `feasible()` reads just the return time. A new exported helper `route_schedule(route) -> RouteSchedule` is the public face that `vrptw_alns` will call in Task 5.

- [ ] **Step 4.1: Write the failing schedule test**

Add to `tests/test_vrptw_engine.py`:

```python
def test_route_schedule_returns_arrival_per_stop():
    """RouteSchedule.arrivals must contain one entry per stop, keyed by order_id."""
    from vrptw_engine import route_schedule, RouteSchedule
    from routing import reset_router
    reset_router()
    stops = [
        _stop(order_id='A', lat=52.20, lon=0.16),
        _stop(order_id='B', lat=52.05, lon=0.30),
    ]
    route = _route(stops=stops)
    sched = route_schedule(route)
    assert isinstance(sched, RouteSchedule)
    assert set(sched.arrivals.keys()) == {'A', 'B'}
    # Arrivals must be strictly increasing.
    assert sched.arrivals['A'] < sched.arrivals['B']
    # Return time is strictly after the last arrival.
    assert sched.return_time > sched.arrivals['B']
    # All arrivals are at or after shift_start.
    assert sched.arrivals['A'] >= route.shift_start


def test_route_schedule_empty_route_returns_empty_arrivals():
    from vrptw_engine import route_schedule
    from routing import reset_router
    reset_router()
    route = _route(stops=[])
    sched = route_schedule(route)
    assert sched.arrivals == {}
    assert sched.return_time == route.shift_start


def test_feasible_still_uses_return_time():
    """Refactoring _estimated_return_time must not change feasible()'s answer."""
    from routing import reset_router
    reset_router()
    stops = [_stop(order_id=f'O{i}', lat=52.2, lon=0.16) for i in range(3)]
    route = _route(stops=stops)
    # Default 11-hour shift accommodates 3 stops near depot
    assert feasible(route) is True
```

- [ ] **Step 4.2: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_engine.py::test_route_schedule_returns_arrival_per_stop -v`
Expected: `ImportError: cannot import name 'route_schedule'`.

- [ ] **Step 4.3: Add `RouteSchedule` dataclass to `simulation/vrptw_engine.py`**

After the `InsertionResult` dataclass (around line 86), insert:

```python
@dataclass
class RouteSchedule:
    """Per-stop arrival times and depot return time for one route.

    arrivals: order_id -> arrival datetime at the stop (before service starts)
    return_time: estimated depot-return datetime
    """
    arrivals: dict
    return_time: 'datetime'
```

- [ ] **Step 4.4: Replace `_estimated_return_time` with `_walk_schedule` and add `route_schedule` public alias**

Replace:

```python
def _estimated_return_time(route: DeliveryRoute) -> datetime:
    """Walk the route in time and return estimated depot-return datetime.

    Per-leg travel time delegated to the active Router.duration_h.
    """
    router = _get_router()
    t = route.shift_start
    prev_lat, prev_lon = route.depot_lat, route.depot_lon
    for stop in route.stops:
        leg_h = router.duration_h(prev_lat, prev_lon, stop.lat, stop.lon)
        t += timedelta(hours=leg_h + _svc(stop))
        prev_lat, prev_lon = stop.lat, stop.lon
    return_h = router.duration_h(prev_lat, prev_lon, route.depot_lat, route.depot_lon)
    return t + timedelta(hours=return_h)
```

with:

```python
def _walk_schedule(route: DeliveryRoute) -> RouteSchedule:
    """Walk the route in time. Returns per-stop arrivals plus depot return time.

    Arrival at stop k = shift_start + sum of (leg + service) up to and
    including the travel leg into k. Service at stop k pushes the clock
    forward for stop k+1 but does not change stop k's arrival.
    """
    router = _get_router()
    arrivals: dict = {}
    t = route.shift_start
    prev_lat, prev_lon = route.depot_lat, route.depot_lon
    for stop in route.stops:
        leg_h = router.duration_h(prev_lat, prev_lon, stop.lat, stop.lon)
        t += timedelta(hours=leg_h)
        arrivals[stop.order_id] = t
        t += timedelta(hours=_svc(stop))
        prev_lat, prev_lon = stop.lat, stop.lon
    return_h = router.duration_h(prev_lat, prev_lon, route.depot_lat, route.depot_lon)
    return RouteSchedule(arrivals=arrivals, return_time=t + timedelta(hours=return_h))


def route_schedule(route: DeliveryRoute) -> RouteSchedule:
    """Public alias of _walk_schedule for callers outside this module."""
    return _walk_schedule(route)


def _estimated_return_time(route: DeliveryRoute) -> datetime:
    """Deprecated thin wrapper kept so external imports keep working until they
    migrate. New code should call route_schedule(route).return_time."""
    return _walk_schedule(route).return_time
```

- [ ] **Step 4.5: Run schedule tests — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_engine.py -v`
Expected: all engine tests pass (existing ones still green because `_estimated_return_time` wrapper preserves the contract; new schedule tests green).

- [ ] **Step 4.6: Save and continue (no commit)**

---

## Task 5: Thread `arrival_iso` through `vrptw_alns` route output

**Files:**
- Modify: `simulation/vrptw_alns.py`
- Modify: `tests/test_vrptw_engine.py` (or add a new `tests/test_vrptw_alns.py` if simpler)

Each stop dict in `out_routes[vid]['stops']` gains an `arrival_iso` key.

- [ ] **Step 5.1: Write the failing test**

Add a new file `tests/test_vrptw_alns_output.py`:

```python
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'simulation'))

from vrptw_alns import run_vrptw
from routing import reset_router


def test_run_vrptw_emits_arrival_iso_per_stop():
    """Each stop in out_routes[vid]['stops'] must carry an arrival_iso
    (ISO-format string) so backtest.compute_planned_on_time can compare
    arrivals to delivery windows directly."""
    reset_router()
    T0 = datetime(2026, 1, 7, 6, 0, tzinfo=timezone.utc)
    T_END = T0 + timedelta(hours=11)
    orders = [
        {'order_id': 'A', 'dest_lat': 52.20, 'dest_lon': 0.16,
         'weight_kg': 500.0, 'pallets': 1.0},
        {'order_id': 'B', 'dest_lat': 52.05, 'dest_lon': 0.30,
         'weight_kg': 500.0, 'pallets': 1.0},
    ]
    vehicles = [
        {'vehicle_id': 'V1',
         'depot_lat': 52.10172, 'depot_lon': 0.16229,
         'shift_start': T0, 'shift_end': T_END,
         'capacity_kg': 10000.0, 'capacity_pallets': 26, 'asset_type': 'Lorry'},
    ]
    result = run_vrptw(orders, vehicles, time_budget=2.0)
    routes = result['routes']
    assert routes  # at least one route with stops
    for vid, route in routes.items():
        for stop in route['stops']:
            assert 'arrival_iso' in stop, f"stop {stop['order_id']} missing arrival_iso"
            # arrival_iso should be parseable as ISO datetime
            datetime.fromisoformat(stop['arrival_iso'])
```

- [ ] **Step 5.2: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_alns_output.py -v`
Expected: `AssertionError: stop A missing arrival_iso`.

- [ ] **Step 5.3: Edit `simulation/vrptw_alns.py`**

Find the route-formatting block (around line 363–382). It currently reads:

```python
    from vrptw_engine import route_distance_km as _rdkm
    out_routes  = {}
    assignments = []
    for vid, route in best_routes.items():
        if not route.stops:
            continue
        km   = _rdkm(route)
        cost = route_cost(route, cost_rates)
        out_routes[vid] = {
            'stops': [
                {'order_id': s.order_id, 'lat': s.lat, 'lon': s.lon,
                 'weight_kg': s.weight_kg, 'pallets': s.pallets}
                for s in route.stops
            ],
            'total_distance_km':  round(km, 1),
            'estimated_cost_gbp': round(cost, 2),
            'depot_lat': route.depot_lat,
            'depot_lon': route.depot_lon,
            'asset_type': route.asset_type,
        }
        for s in route.stops:
            assignments.append({'order_id': s.order_id, 'vehicle_id': vid})
```

Replace with:

```python
    from vrptw_engine import route_distance_km as _rdkm, route_schedule as _rsch
    out_routes  = {}
    assignments = []
    for vid, route in best_routes.items():
        if not route.stops:
            continue
        km   = _rdkm(route)
        cost = route_cost(route, cost_rates)
        sched = _rsch(route)
        out_routes[vid] = {
            'stops': [
                {'order_id': s.order_id, 'lat': s.lat, 'lon': s.lon,
                 'weight_kg': s.weight_kg, 'pallets': s.pallets,
                 'arrival_iso': sched.arrivals[s.order_id].isoformat()}
                for s in route.stops
            ],
            'total_distance_km':  round(km, 1),
            'estimated_cost_gbp': round(cost, 2),
            'depot_lat': route.depot_lat,
            'depot_lon': route.depot_lon,
            'asset_type': route.asset_type,
            'return_time_iso': sched.return_time.isoformat(),
        }
        for s in route.stops:
            assignments.append({'order_id': s.order_id, 'vehicle_id': vid})
```

- [ ] **Step 5.4: Run new test — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_alns_output.py -v`
Expected: pass.

- [ ] **Step 5.5: Run the full suite to catch downstream regressions**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest -v 2>&1 | tail -20`
Expected: pre-existing pass count holds. If any downstream code expects a stop dict without `arrival_iso` and breaks on the new key (e.g. strict schema check), update that caller. Most consumers (cambridge/dispatcher.py's `run_event` and backtest.py) read `stops` with `.get(...)` and ignore unknown keys, so this is usually safe.

- [ ] **Step 5.6: Save and continue (no commit)**

---

## Task 6: Rewrite `compute_planned_on_time` in `cambridge/backtest.py`

**Files:**
- Modify: `cambridge/backtest.py`
- Modify: `tests/cambridge/test_backtest.py`

Delete the linear-position proxy. New implementation reads `arrival_iso` from each stop dict and compares to the `ScopedOrder.delivery_window[1]`. Orders with no matching `ScopedOrder` window are still counted as late (no window = can't prove on-time). Orders with no `arrival_iso` (route data missing it) are also counted as late and logged once per backtest run as a sanity warning.

- [ ] **Step 6.1: Write the failing test**

Find the existing tests for `compute_planned_on_time` in `tests/cambridge/test_backtest.py`. They likely build routes with `shift_start_iso`/`shift_end_iso`. The new tests use `arrival_iso` on each stop instead.

Add to `tests/cambridge/test_backtest.py`:

```python
def test_compute_planned_on_time_uses_arrival_iso():
    from cambridge.backtest import compute_planned_on_time
    from cambridge.scope import ScopedOrder
    from datetime import datetime, timezone, timedelta

    window_end_on_time = datetime(2026, 1, 7, 10, 0, tzinfo=timezone.utc)
    window_end_too_early = datetime(2026, 1, 7, 6, 30, tzinfo=timezone.utc)
    scoped = [
        ScopedOrder(order_id='A', origin_postcode='', dest_postcode='',
                    origin_lat=0.0, origin_lon=0.0, dest_lat=52.2, dest_lon=0.16,
                    weight_kg=500.0, pallets=1.0,
                    delivery_window=(datetime(2026, 1, 7, 6, 0, tzinfo=timezone.utc),
                                     window_end_on_time)),
        ScopedOrder(order_id='B', origin_postcode='', dest_postcode='',
                    origin_lat=0.0, origin_lon=0.0, dest_lat=52.05, dest_lon=0.30,
                    weight_kg=500.0, pallets=1.0,
                    delivery_window=(datetime(2026, 1, 7, 6, 0, tzinfo=timezone.utc),
                                     window_end_too_early)),
    ]
    arrival_A = datetime(2026, 1, 7, 8, 0, tzinfo=timezone.utc)   # inside window
    arrival_B = datetime(2026, 1, 7, 9, 30, tzinfo=timezone.utc)  # past window_end_too_early
    routes = {
        'V1': {
            'stops': [
                {'order_id': 'A', 'arrival_iso': arrival_A.isoformat()},
                {'order_id': 'B', 'arrival_iso': arrival_B.isoformat()},
            ],
        },
    }
    on_time, late = compute_planned_on_time(routes, scoped)
    assert on_time == 1
    assert late == 1


def test_compute_planned_on_time_counts_missing_window_as_late():
    """Stop with no matching ScopedOrder window cannot be proven on-time."""
    from cambridge.backtest import compute_planned_on_time
    from datetime import datetime, timezone

    routes = {
        'V1': {
            'stops': [
                {'order_id': 'X', 'arrival_iso':
                    datetime(2026, 1, 7, 8, 0, tzinfo=timezone.utc).isoformat()},
            ],
        },
    }
    on_time, late = compute_planned_on_time(routes, [])
    assert on_time == 0
    assert late == 1


def test_compute_planned_on_time_counts_missing_arrival_as_late():
    """Stop without arrival_iso (legacy route data) is counted as late."""
    from cambridge.backtest import compute_planned_on_time
    from cambridge.scope import ScopedOrder
    from datetime import datetime, timezone

    scoped = [
        ScopedOrder(order_id='A', origin_postcode='', dest_postcode='',
                    origin_lat=0.0, origin_lon=0.0, dest_lat=52.2, dest_lon=0.16,
                    weight_kg=500.0, pallets=1.0,
                    delivery_window=(datetime(2026, 1, 7, 6, 0, tzinfo=timezone.utc),
                                     datetime(2026, 1, 7, 18, 0, tzinfo=timezone.utc))),
    ]
    routes = {'V1': {'stops': [{'order_id': 'A'}]}}  # no arrival_iso
    on_time, late = compute_planned_on_time(routes, scoped)
    assert on_time == 0
    assert late == 1
```

Then **delete or update** any pre-existing `compute_planned_on_time` test that asserts the linear-proxy behaviour (e.g. tests passing `shift_start_iso`/`shift_end_iso` and expecting all stops on-time). Search `tests/cambridge/test_backtest.py` for `compute_planned_on_time` and remove those tests — the new metric supersedes them. If unsure which old tests rely on the proxy, run the suite first; tests that fail under the new implementation are the proxy-dependent ones.

- [ ] **Step 6.2: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_backtest.py::test_compute_planned_on_time_uses_arrival_iso -v`
Expected: test fails — current implementation still uses linear proxy and won't read `arrival_iso`.

- [ ] **Step 6.3: Replace `compute_planned_on_time` in `cambridge/backtest.py`**

Replace the whole function (lines ~180–245) with:

```python
def compute_planned_on_time(routes: dict, scoped_orders: list) -> tuple[int, int]:
    """Count planned stops as on-time (arrival ≤ delivery_window end) or late.

    Reads `arrival_iso` (ISO-format string) from each stop dict, injected by
    vrptw_alns from RouteSchedule.arrivals.

    Late conditions (any of):
      - order_id not in scoped_orders (no window to compare against)
      - stop has no arrival_iso (route data predates v1.7)
      - arrival > window_end
    """
    from datetime import datetime as _dt

    order_windows = {o.order_id: o.delivery_window[1] for o in scoped_orders}

    on_time = 0
    late = 0
    for vid, route in routes.items():
        if not isinstance(route, dict):
            continue
        stops = route.get('stops', []) or []
        for stop in stops:
            if not isinstance(stop, dict):
                late += 1
                continue
            order_id = stop.get('order_id')
            arrival_iso = stop.get('arrival_iso')
            window_end = order_windows.get(order_id) if order_id is not None else None
            if window_end is None or arrival_iso is None:
                late += 1
                continue
            try:
                arrival = _dt.fromisoformat(str(arrival_iso))
            except ValueError:
                late += 1
                continue
            if arrival <= window_end:
                on_time += 1
            else:
                late += 1
    return on_time, late
```

- [ ] **Step 6.4: Run new tests + full suite**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_backtest.py -v`
Expected: new tests pass; old proxy-dependent tests already removed per Step 6.1 guidance.

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest -v 2>&1 | tail -10`
Expected: full suite green.

- [ ] **Step 6.5: Save and continue (no commit)**

---

## Task 7: Cambridge config + dispatcher hook

**Files:**
- Modify: `cambridge/config.py`
- Modify: `cambridge/dispatcher.py`
- Modify: `tests/cambridge/test_config.py`

Add an env-var-gated OSRM install at the entry to `run_day_multi_trip`. No CLI flag — keep the surface minimal.

- [ ] **Step 7.1: Write the config + dispatcher tests**

Add to `tests/cambridge/test_config.py`:

```python
def test_osrm_enabled_default_false(monkeypatch):
    monkeypatch.delenv('CAMBRIDGE_OSRM', raising=False)
    # Re-import to pick up env state — or call the predicate directly.
    from cambridge.config import osrm_enabled
    assert osrm_enabled() is False


def test_osrm_enabled_true_when_env_set(monkeypatch):
    monkeypatch.setenv('CAMBRIDGE_OSRM', '1')
    from cambridge.config import osrm_enabled
    assert osrm_enabled() is True


def test_osrm_url_default(monkeypatch):
    monkeypatch.delenv('OSRM_URL', raising=False)
    from cambridge.config import OSRM_URL
    assert OSRM_URL == 'http://localhost:5000'
```

Add to `tests/cambridge/test_dispatcher.py` (or wherever `run_day_multi_trip` is exercised):

```python
def test_run_day_multi_trip_installs_osrm_when_env_set(monkeypatch):
    """When CAMBRIDGE_OSRM=1, run_day_multi_trip must call install_osrm_router."""
    from unittest.mock import patch
    monkeypatch.setenv('CAMBRIDGE_OSRM', '1')
    with patch('cambridge.dispatcher.install_osrm_router') as mock_install:
        # Stub run_day so we don't need real data — only test the install hook.
        with patch('cambridge.dispatcher.run_event') as mock_run_event:
            from datetime import date
            from cambridge.dispatcher import run_day_multi_trip
            mock_run_event.return_value = type('X', (), {
                'routes': {}, 'assignments': [], 'unassigned_order_ids': [],
                'metrics': {'orders_total': 0, 'orders_assigned': 0,
                            'planned_km': 0.0, 'planned_cost_gbp': 0.0,
                            'vehicles_used': 0},
            })()
            try:
                run_day_multi_trip(date(2026, 1, 7), scoped_orders=[],
                                   vehicle_profiles={}, freight_avail={},
                                   cost_rates={})
            except Exception:
                pass  # we only care that install was invoked
            assert mock_install.called


def test_run_day_multi_trip_skips_osrm_when_env_unset(monkeypatch):
    from unittest.mock import patch
    monkeypatch.delenv('CAMBRIDGE_OSRM', raising=False)
    with patch('cambridge.dispatcher.install_osrm_router') as mock_install:
        with patch('cambridge.dispatcher.run_event') as mock_run_event:
            from datetime import date
            from cambridge.dispatcher import run_day_multi_trip
            mock_run_event.return_value = type('X', (), {
                'routes': {}, 'assignments': [], 'unassigned_order_ids': [],
                'metrics': {'orders_total': 0, 'orders_assigned': 0,
                            'planned_km': 0.0, 'planned_cost_gbp': 0.0,
                            'vehicles_used': 0},
            })()
            try:
                run_day_multi_trip(date(2026, 1, 7), scoped_orders=[],
                                   vehicle_profiles={}, freight_avail={},
                                   cost_rates={})
            except Exception:
                pass
            assert not mock_install.called
```

(If the existing `run_day_multi_trip` signature differs from what's shown above — particularly parameter names — adjust the call sites in these tests to match. Read `cambridge/dispatcher.py` to confirm the actual signature before running.)

- [ ] **Step 7.2: Run — expect failures**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_config.py -v`
Expected: failures for `osrm_enabled` import and `OSRM_URL` constant.

- [ ] **Step 7.3: Add OSRM constants to `cambridge/config.py`**

Append to `cambridge/config.py`:

```python
# --- OSRM toggle (v1.7) ---
# When CAMBRIDGE_OSRM=1, run_day_multi_trip installs an OSRMRouter against
# OSRM_URL before the solver runs. Default is Haversine (pre-v1.7 behaviour).
import os as _os

OSRM_URL: str = _os.environ.get('OSRM_URL', 'http://localhost:5000')


def osrm_enabled() -> bool:
    """True iff CAMBRIDGE_OSRM env var is set to '1' (or 'true', case-insensitive)."""
    val = _os.environ.get('CAMBRIDGE_OSRM', '').strip().lower()
    return val in ('1', 'true', 'yes')
```

- [ ] **Step 7.4: Wire `install_osrm_router` into `cambridge/dispatcher.py`**

At the top of `cambridge/dispatcher.py`, ensure these imports exist (add if missing):

```python
from cambridge.config import osrm_enabled, OSRM_URL
from routing import install_osrm_router
```

Then inside `run_day_multi_trip`, at the very top of the function (before the iterate-until-done loop begins), insert:

```python
    if osrm_enabled():
        # Build the per-batch coordinate set from this day's scoped orders +
        # vehicles, warm/extend the OSRM cache, and install OSRMRouter.
        _orders_for_cache = [
            {'origin_lat': o.origin_lat, 'origin_lon': o.origin_lon,
             'dest_lat':   o.dest_lat,   'dest_lon':   o.dest_lon}
            for o in scoped_orders
        ]
        _vehicles_for_cache = [
            {'depot_lat': vp['depot_lat'], 'depot_lon': vp['depot_lon']}
            for vp in vehicle_profiles.values()
            if 'depot_lat' in vp and 'depot_lon' in vp
        ]
        install_osrm_router(_orders_for_cache, _vehicles_for_cache, osrm_url=OSRM_URL)
```

(If `vehicle_profiles` items don't carry `depot_lat`/`depot_lon` directly, derive them from your `CAMBRIDGE_DEPOT` constant — read the existing `run_day_multi_trip` to confirm where depot coords come from and adapt.)

- [ ] **Step 7.5: Run config + dispatcher tests — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_config.py tests/cambridge/test_dispatcher.py -v`
Expected: pass.

- [ ] **Step 7.6: Run the full suite**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest -v 2>&1 | tail -15`
Expected: full suite green.

- [ ] **Step 7.7: Save and continue (no commit)**

---

## Task 8: Re-export from `legacy_pdptw/routing.py` (keep legacy backtests running)

**Files:**
- Modify: `legacy_pdptw/routing.py`

The single source of truth is now `simulation/routing.py`. Legacy continues to work via re-exports — no logic in `legacy_pdptw/routing.py` anymore.

- [ ] **Step 8.1: Replace `legacy_pdptw/routing.py` contents**

Open `legacy_pdptw/routing.py` and replace **all** its current content with:

```python
"""Re-exports from simulation.routing.

As of v1.7, the routing implementation lives in simulation/routing.py — this
file is a compatibility shim so legacy_pdptw backtests keep working with their
existing `from routing import ...` imports.
"""
import os
import sys

_SIM = os.path.join(os.path.dirname(__file__), '..', 'simulation')
if _SIM not in sys.path:
    sys.path.insert(0, _SIM)

from routing import (  # noqa: F401
    HaversineRouter,
    OSRMRouter,
    Router,
    coord_key,
    pair_key,
    load_cache,
    save_cache,
    query_osrm_table,
    build_osrm_matrix,
    install_osrm_router,
    unique_coords,
    set_router,
    get_router,
    reset_router,
    CACHE_PATH,
    DEFAULT_OSRM_URL,
    DEFAULT_MAX_TABLE_SIZE,
    TRUCK_DURATION_FACTOR,
)
```

**Important:** Legacy callers also expect `set_router` to be available from `pdp_route` (the legacy module). If their `install_osrm_router` previously did `from pdp_route import set_router`, that still works untouched because we're only replacing `legacy_pdptw/routing.py`, not `pdp_route.py`. We've moved the impl, not changed legacy import patterns.

- [ ] **Step 8.2: Run any legacy tests that import from `legacy_pdptw/routing`**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest legacy_pdptw/tests/test_routing.py -v`
Expected: pass (the test imports resolve to the re-exports).

- [ ] **Step 8.3: Run the full suite**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest -v 2>&1 | tail -10`
Expected: green.

- [ ] **Step 8.4: Save and continue (no commit)**

---

## Task 9: Haversine parity validation (Jan 7 backtest must match v1.6 numbers)

**Files:**
- No source changes. This is a validation step — running existing entrypoints with the env var **unset** (default Haversine).

- [ ] **Step 9.1: Run the v1.6 Jan 7 backtest with the new code path, Haversine default**

Run:

```bash
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && \
  unset CAMBRIDGE_OSRM && \
  python -m cambridge.backtest --date 2026-01-07 2>&1 | tee /tmp/v17_jan07_haversine.txt
```

(Replace `cambridge.backtest` with whatever the existing one-day entrypoint is — check `cambridge/backtest.py`'s `__main__` block or the v1.6 plan's validation step to confirm. If there's no module-mode entrypoint, write a 6-line driver script in `investigations/v17_parity.py` that calls `run_day_backtest(date(2026, 1, 7), ...)` and `print_report(...)`.)

- [ ] **Step 9.2: Compare to v1.6 baseline**

The v1.6 baseline for Jan 7 (from the v1.6 validation summary):
- 150/150 orders assigned (100 %)
- Planned km = +38 % over actual
- on_time_count was the linear-proxy value (~114)

Under v1.7 Haversine-default:
- 150/150 orders assigned must still hold.
- Planned km must be within 0.5 % of the v1.6 number (small numerical drift from refactor allowed).
- on_time_count will be different — the metric was rebuilt. This is **expected**, not a regression.

If the orders-assigned or planned-km numbers move materially, BLOCK and investigate — likely a missed call site or a router default mismatch.

- [ ] **Step 9.3: Save the parity report**

Save the captured backtest output to `investigations/v17_parity_haversine_jan07.txt` for the record.

---

## Task 10: OSRM validation (Jan 7 backtest with real road routing)

**Files:**
- No source changes. Validation only.

**Prerequisite:** OSRM Docker server must be running on `localhost:5000`. The pre-built GB graph lives at `E:/BEAT/osrm/`. To start (Windows PowerShell, with Docker Desktop running):

```powershell
docker run -t -i -p 5000:5000 -v "E:/BEAT/osrm:/data" osrm/osrm-backend `
  osrm-routed --algorithm mld --max-table-size 1000 /data/great-britain-latest.osrm
```

If Docker Desktop isn't running, start it first.

- [ ] **Step 10.1: Verify OSRM server reachable**

Run:

```bash
curl -m 5 "http://localhost:5000/route/v1/driving/-0.12,51.5;0.16,52.1"
```

Expected: JSON with `"code":"Ok"`. If not, BLOCK and fix the server before proceeding.

- [ ] **Step 10.2: Run the Jan 7 backtest with OSRM enabled**

```bash
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && \
  CAMBRIDGE_OSRM=1 OSRM_URL=http://localhost:5000 \
  python -m cambridge.backtest --date 2026-01-07 2>&1 | tee /tmp/v17_jan07_osrm.txt
```

First run will populate the OSRM cache for all Cambridge postcodes touched on Jan 7 (expect 30 s–2 min of cache warming on first run; subsequent runs are sub-second cache hits).

- [ ] **Step 10.3: Inspect the report**

Required outcomes vs v1.6 Jan 7:
- Orders assigned: still 150/150 (OSRM changes distance, not feasibility math materially — multi-trip + overrun absorb the rest).
- Planned km: should drop from "+38 % over actual" to within ±10 % of actual.
- Planned on-time: should drop from ~114 toward the actual on-time count (~7), or at least produce a distribution that doesn't uniformly inflate.

If planned km is still > 20 % over actual, check `OSRMRouter.fallback_count` in the run output — if it's high, cache warming failed or the matrix has gaps; investigate before declaring success.

- [ ] **Step 10.4: Save the OSRM report**

Save the output to `investigations/v17_osrm_jan07.txt`.

---

## Task 11: 5-day OSRM backtest + write the v1.7 results doc

**Files:**
- Create: `cambridge/docs/cambridge-dispatcher-v17-update.md` (or co-locate next to the v1.5 update doc)
- No code changes.

- [ ] **Step 11.1: Run the 5-day backtest with OSRM**

```bash
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && \
  CAMBRIDGE_OSRM=1 \
  python -m cambridge.backtest --range 2026-01-07:2026-01-11 2>&1 | tee /tmp/v17_5day_osrm.txt
```

(Replace `--range` with whatever the existing 5-day driver looks like — check the v1.6 plan or `cambridge/backtest.py` for the period-mode entrypoint, likely `run_period`. If unsure, write a 12-line driver `investigations/v17_five_day.py` that loops dates Jan 7–11.)

- [ ] **Step 11.2: Collect per-day metrics**

For each of the 5 days, extract from the report:
- orders_assigned / orders_total
- planned_km vs actual_km (and the % delta)
- planned_on_time / planned_late
- actual on_time / late
- km_ks (Kolmogorov–Smirnov stop-count distance)

Build a small markdown table summarising these.

- [ ] **Step 11.3: Write `cambridge-dispatcher-v17-update.md`**

Create the file under `logistics/docs/` mirroring the v1.5 update doc's structure:

```markdown
# Cambridge Dispatcher v1.7 Validation

## What changed
- Replaced haversine × 1.3 + 50 km/h with OSRM road km + truck-calibrated duration.
- Rebuilt planned on-time / late on real per-stop arrival times.
- No commits — work stays local per project rule.

## Backtest results (Jan 7–11, 2026)
[Insert the per-day table here, sourced from the 5-day run output.]

## What this fixes
- Planned km gap (v1.6 was +38 % over actual on Jan 7) → ±X % on average across 5 days.
- Planned on-time (v1.6 was 114/150 vs actual 7) → now Y/150, much closer to actual.

## What this doesn't fix
- Postcode-district Jaccard stays at 0 (groupage hub routing, not a distance gap).
- Scope-filter exclusions (43 % of Cambridge-fleet orders) untouched — separate v1.5 deferred item.

## Operational notes
- OSRM server must be running on OSRM_URL (default http://localhost:5000) when CAMBRIDGE_OSRM=1.
- Cache lives at data/Output/osrm_cache.json — shared with legacy_pdptw, persists across runs.
- Set CAMBRIDGE_OSRM=0 (or unset) to fall back to Haversine for parity comparisons.
```

Fill the `[Insert ...]` blocks with actual numbers from Step 11.2.

- [ ] **Step 11.4: Final full-suite run**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest -v 2>&1 | tail -10`
Expected: full suite green.

- [ ] **Step 11.5: Save the 5-day report**

Save `/tmp/v17_5day_osrm.txt` to `investigations/v17_osrm_5day.txt`.

- [ ] **Step 11.6: Done — no commit (per project constraint)**

The v1.7 implementation is complete and validated locally. Do not run `git commit`, `git add`, or `git push`. The user will review the local changes and decide next steps.

---

## Self-Review Notes

**Spec coverage** — every section of the v1.7 spec is covered:
- D1 router seam → Tasks 1, 2
- D2 per-stop arrivals → Task 4
- D3 shared cache → Tasks 3, 8 (re-export ensures single source)
- D4 truck factor reused → Task 3 (`TRUCK_DURATION_FACTOR = 1.24`)
- Validation step 1 (Haversine parity) → Task 9
- Validation step 2 (OSRM Jan 7) → Task 10
- Validation step 3 (honest on-time) → Task 10, Task 11
- Validation step 4 (5-day) → Task 11
- Validation step 5 (test suite) → every task's "full suite run" step
- Non-goals (HGV Lua, Jaccard, scope filter) → not addressed (correctly out of scope)
- Success criteria → covered by Tasks 9–11

**Placeholder scan** — no TBD/TODO. Every code step has the full code. Validation steps that depend on the exact entrypoint format note the dependency and tell the implementer to check `cambridge/backtest.py`'s `__main__` block before running.

**Type consistency** — `RouteSchedule` defined in Task 4 is used by Task 5 (vrptw_alns calls `route_schedule`) and Task 6 (backtest reads `arrival_iso` produced by Task 5). `Router` protocol defined in Task 1 is used by Tasks 2 and 3. `OSRM_URL` and `osrm_enabled` defined in Task 7 config are used by Task 7 dispatcher. All consistent.
