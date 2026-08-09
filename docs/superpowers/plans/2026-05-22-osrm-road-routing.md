# OSRM Real Road Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dispatcher's straight-line distance + flat-speed time with real road distance and duration from a self-hosted OSRM server, behind a swappable routing provider that defaults to the existing Haversine behaviour.

**Architecture:** A module-level `Router` provider in `pdp_route.py` (default `HaversineRouter` = current behaviour). The two engine functions that consume distance/time — `route_distance_km` (cost) and `schedule_route` (duration) — call the provider. A new `routing.py` holds the `OSRMRouter`, a per-batch matrix built from a chunked OSRM `/table` query, and a persistent on-disk pair cache, with per-pair Haversine fallback. The runner installs the OSRM router via `set_router()` when `--routing osrm` is passed.

**Tech Stack:** Python 3, pytest. OSRM via HTTP (stdlib `urllib`, no new dependency). OSRM server itself is self-hosted via Docker (documented infra, not app code).

**Spec:** `docs/superpowers/specs/2026-05-22-osrm-road-routing-design.md`

**Project conventions for this plan:**
- **Python interpreter:** `E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe`
- **Run tests/commands from:** `E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics`
- **NO GIT COMMITS.** Not a git repo; no-commit workflow. End each task on green tests; mark complete in your tracker.
- **No live OSRM server in tests.** Every OSRM call goes through `query_osrm_table`, which tests monkeypatch. Never hit a real server from the suite.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `pdp_route.py` | Cost/time engine + routing provider seam | Add `Router`, `HaversineRouter`, module `_router` + `set_router`/`get_router`; route `route_distance_km` and `schedule_route` through the provider |
| `routing.py` (new) | OSRM client, matrix build, persistent cache, `OSRMRouter` | Create |
| `run_daily_batch.py` | Runner | Add `--routing`/`--osrm-url`, build + install router, report fallback count |
| `tests/test_pdp_route.py` | Engine tests | Add provider-seam tests |
| `tests/test_routing.py` (new) | Routing-layer tests | Create |
| `docs/osrm-setup.md` (new) | OSRM Docker provisioning | Create (documentation) |

---

## Task 1: Routing provider seam in the engine

**Files:**
- Modify: `pdp_route.py` (add classes + globals after the constants block, before `@dataclass class Stop` at line 29)
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pdp_route.py`:

```python
from pdp_route import (Router, HaversineRouter, set_router, get_router,
                       AVG_SPEED_KMH as _AVG)
from profitability_report.profitability_report_merged import _haversine_km as _hav


def test_default_router_is_haversine():
    assert isinstance(get_router(), HaversineRouter)


def test_haversine_router_reproduces_legacy_numbers():
    r = HaversineRouter()
    assert r.distance_km(51.5, 0.0, 51.5, 1.0) == _hav(51.5, 0.0, 51.5, 1.0)
    assert r.duration_h(51.5, 0.0, 51.5, 1.0) == _hav(51.5, 0.0, 51.5, 1.0) / _AVG


def test_set_and_get_router_swaps_provider():
    class _Stub(Router):
        def distance_km(self, a, b, c, d): return 42.0
        def duration_h(self, a, b, c, d): return 1.0
    original = get_router()
    try:
        set_router(_Stub())
        assert get_router().distance_km(0, 0, 1, 1) == 42.0
    finally:
        set_router(original)  # restore isolation
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py -k "router" -v`
Expected: FAIL with `ImportError: cannot import name 'Router'`

- [ ] **Step 3: Add the provider classes and globals**

In `pdp_route.py`, insert after the constants block (after line 26, the `ARTIC_ASSET_TYPE` line) and before `@dataclass class Stop`:

```python
class Router:
    """Provides road distance (km) and duration (hours) between two points.
    Distance drives cost; duration drives scheduling/deadlines."""
    def distance_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        raise NotImplementedError
    def duration_h(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        raise NotImplementedError


class HaversineRouter(Router):
    """Default/fallback provider: straight-line distance and flat-speed
    duration — identical to the pre-OSRM behaviour."""
    def distance_km(self, lat1, lon1, lat2, lon2):
        return _haversine_km(lat1, lon1, lat2, lon2)
    def duration_h(self, lat1, lon1, lat2, lon2):
        return _haversine_km(lat1, lon1, lat2, lon2) / AVG_SPEED_KMH


_router: Router = HaversineRouter()


def set_router(router: Router) -> None:
    """Install the active routing provider (e.g. an OSRMRouter at startup)."""
    global _router
    _router = router


def get_router() -> Router:
    return _router
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py -k "router" -v`
Expected: PASS (3 tests)

---

## Task 2: Route the engine through the provider

**Files:**
- Modify: `pdp_route.py` — `route_distance_km` (lines 49-63) and the two leg-time lines in `schedule_route`
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pdp_route.py`:

```python
def test_engine_uses_installed_router_for_distance_and_duration():
    # A stub that returns a fixed 10 km / 5 h for every leg lets us verify the
    # engine reads the provider for BOTH distance (cost) and duration (schedule).
    from datetime import datetime, timezone
    from pdp_route import route_distance_km, schedule_route, Router, set_router, get_router

    class _Fixed(Router):
        def distance_km(self, a, b, c, d): return 10.0
        def duration_h(self, a, b, c, d): return 5.0

    original = get_router()
    try:
        set_router(_Fixed())
        stops = [Stop('O1', 52.0, 0.0, 'pickup'), Stop('O1', 53.0, 0.0, 'delivery')]
        # closed loop = 3 legs (start->p, p->d, d->start) x 10 km
        assert route_distance_km(52.0, 0.0, stops) == 30.0
        # each 5 h leg; 1st leg (5h) fits the 9h day, 2nd leg (5h) exceeds the
        # remaining 4h -> one overnight inserted by the duration-based schedule
        start = datetime(2026, 1, 5, 6, tzinfo=timezone.utc)
        _, overnights = schedule_route(52.0, 0.0, stops, start)
        assert overnights >= 1
    finally:
        set_router(original)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py::test_engine_uses_installed_router_for_distance_and_duration -v`
Expected: FAIL — `route_distance_km` still calls `_haversine_km` directly (returns real km, not 30.0).

- [ ] **Step 3: Route `route_distance_km` through the provider**

Replace `route_distance_km` (lines 49-63) with:

```python
def route_distance_km(start_lat: float, start_lon: float, stops: list) -> float:
    """Closed loop: depot -> each stop in order -> back to depot.

    The vehicle starts its day at the depot and must drive home after the last
    delivery, so the return leg is real mileage the optimiser should account for.
    Distances come from the active routing provider (road distance under OSRM,
    straight-line under the default Haversine).
    """
    if not stops:
        return 0.0
    router = _router
    total = 0.0
    prev_lat, prev_lon = start_lat, start_lon
    for stop in stops:
        total += router.distance_km(prev_lat, prev_lon, stop.lat, stop.lon)
        prev_lat, prev_lon = stop.lat, stop.lon
    total += router.distance_km(prev_lat, prev_lon, start_lat, start_lon)
    return total
```

- [ ] **Step 4: Route `schedule_route` leg times through the provider**

In `schedule_route`, replace the per-stop leg-time line:

```python
        leg_h = _haversine_km(prev_lat, prev_lon, stop.lat, stop.lon) / AVG_SPEED_KMH
```

with:

```python
        leg_h = _router.duration_h(prev_lat, prev_lon, stop.lat, stop.lon)
```

and replace the return-leg line:

```python
        return_h = _haversine_km(prev_lat, prev_lon, start_lat, start_lon) / AVG_SPEED_KMH
```

with:

```python
        return_h = _router.duration_h(prev_lat, prev_lon, start_lat, start_lon)
```

(Leave all surrounding logic — the `while` overnight loops, service time, arrivals — unchanged.)

- [ ] **Step 5: Run the new test plus the full engine suite**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py -q`
Expected: PASS — the new stub test plus all existing tests (the default `HaversineRouter` makes `distance_km`/`duration_h` identical to the old direct calls, so distances and overnight counts are unchanged).

---

## Task 3: `OSRMRouter` with cache lookup + Haversine fallback

**Files:**
- Create: `routing.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_routing.py`:

```python
from routing import coord_key, pair_key, OSRMRouter
from pdp_route import HaversineRouter
from profitability_report.profitability_report_merged import _haversine_km as _hav


def test_coord_key_rounds_to_5dp():
    assert coord_key(52.123456, -0.987654) == '52.12346,-0.98765'


def test_pair_key_is_symmetric():
    a = coord_key(52.0, 0.0)
    b = coord_key(53.0, 1.0)
    assert pair_key(52.0, 0.0, 53.0, 1.0) == pair_key(53.0, 1.0, 52.0, 0.0)
    assert pair_key(52.0, 0.0, 53.0, 1.0) == tuple(sorted((a, b)))


def test_osrm_router_hits_matrix():
    key = pair_key(52.0, 0.0, 53.0, 0.0)
    router = OSRMRouter(matrix={key: (111.2, 1.9)})
    assert router.distance_km(52.0, 0.0, 53.0, 0.0) == 111.2
    assert router.duration_h(52.0, 0.0, 53.0, 0.0) == 1.9
    assert router.fallback_count == 0


def test_osrm_router_zero_for_identical_coords():
    router = OSRMRouter(matrix={})
    assert router.distance_km(52.0, 0.0, 52.0, 0.0) == 0.0
    assert router.duration_h(52.0, 0.0, 52.0, 0.0) == 0.0
    assert router.fallback_count == 0


def test_osrm_router_falls_back_and_counts_on_miss():
    router = OSRMRouter(matrix={}, fallback=HaversineRouter())
    d = router.distance_km(52.0, 0.0, 53.0, 0.0)
    assert d == _hav(52.0, 0.0, 53.0, 0.0)
    assert router.fallback_count == 1
    router.duration_h(52.0, 0.0, 53.0, 0.0)
    assert router.fallback_count == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_routing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'routing'`

- [ ] **Step 3: Create `routing.py` with keys + `OSRMRouter`**

Create `routing.py`:

```python
"""Real road routing via a self-hosted OSRM server.

Builds a per-batch distance/duration matrix from OSRM's /table service, backed
by a persistent on-disk pair cache, and serves it through an OSRMRouter that
plugs into pdp_route's routing provider seam. Any pair OSRM can't supply falls
back to the straight-line HaversineRouter (counted, so degraded runs are
visible). OSRM driving distance is treated as symmetric at postcode-centroid
resolution to halve cache size and queries.
"""
import json
import urllib.request
from pathlib import Path

from pdp_route import Router, HaversineRouter

CACHE_PATH = Path(__file__).parent / 'data' / 'Output' / 'osrm_cache.json'
DEFAULT_OSRM_URL = 'http://localhost:5000'
TRUCK_DURATION_FACTOR = 1.0   # multiply OSRM car durations to approximate HGV speeds
DEFAULT_MAX_TABLE_SIZE = 100  # OSRM server's coordinate limit per /table request


def coord_key(lat: float, lon: float) -> str:
    return f'{lat:.5f},{lon:.5f}'


def pair_key(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple:
    """Order-independent key for a coordinate pair (symmetric distances)."""
    return tuple(sorted((coord_key(lat1, lon1), coord_key(lat2, lon2))))


class OSRMRouter(Router):
    """Serves O(1) lookups from a {pair_key: (km, hours)} matrix; identical
    coordinates are 0; any missing pair falls back to `fallback` and increments
    fallback_count."""
    def __init__(self, matrix: dict, fallback: Router | None = None):
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_routing.py -v`
Expected: PASS (5 tests)

---

## Task 4: OSRM `/table` client + cache load/save

**Files:**
- Modify: `routing.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routing.py`:

```python
import json as _json
import routing


def test_query_osrm_table_parses_distances_and_durations(monkeypatch):
    # Fake OSRM HTTP response: 2 coords, distances in metres, durations in seconds.
    fake = {'code': 'Ok',
            'distances': [[0.0, 111200.0], [111200.0, 0.0]],
            'durations': [[0.0, 6840.0], [6840.0, 0.0]]}
    def _fake_get(url):
        assert '/table/v1/driving/' in url
        return fake
    monkeypatch.setattr(routing, '_http_get_json', _fake_get)
    dists, durs = routing.query_osrm_table([(52.0, 0.0), (53.0, 0.0)],
                                           routing.DEFAULT_OSRM_URL)
    assert dists[0][1] == 111200.0
    assert durs[0][1] == 6840.0


def test_cache_round_trip(tmp_path):
    path = tmp_path / 'osrm_cache.json'
    key = routing.pair_key(52.0, 0.0, 53.0, 0.0)
    routing.save_cache(path, {key: (111.2, 1.9)})
    loaded = routing.load_cache(path)
    assert loaded[key] == (111.2, 1.9)


def test_load_cache_missing_file_returns_empty(tmp_path):
    assert routing.load_cache(tmp_path / 'nope.json') == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_routing.py -k "osrm_table or cache" -v`
Expected: FAIL with `AttributeError: module 'routing' has no attribute 'query_osrm_table'`

- [ ] **Step 3: Add the client and cache helpers**

Append to `routing.py`:

```python
def _http_get_json(url: str) -> dict:
    """Thin wrapper around the HTTP GET so tests can monkeypatch it."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def query_osrm_table(coords: list, osrm_url: str) -> tuple:
    """Query OSRM /table for the full matrix over `coords` (list of (lat, lon)).
    Returns (distances_metres, durations_seconds) as 2D lists. Raises on a
    non-Ok response so the caller can fall back."""
    coord_str = ';'.join(f'{lon},{lat}' for lat, lon in coords)
    url = f'{osrm_url}/table/v1/driving/{coord_str}?annotations=distance,duration'
    data = _http_get_json(url)
    if data.get('code') != 'Ok':
        raise RuntimeError(f"OSRM /table returned {data.get('code')}")
    return data['distances'], data['durations']


def load_cache(path=CACHE_PATH) -> dict:
    """Load the persistent pair cache as {pair_key_tuple: (km, hours)}."""
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, 'r') as f:
        raw = json.load(f)
    # JSON keys are strings "a|b"; restore the tuple key and tuple value.
    return {tuple(k.split('|')): (v[0], v[1]) for k, v in raw.items()}


def save_cache(path, cache: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {f'{k[0]}|{k[1]}': [v[0], v[1]] for k, v in cache.items()}
    with open(path, 'w') as f:
        json.dump(serialisable, f)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_routing.py -k "osrm_table or cache" -v`
Expected: PASS (3 tests)

---

## Task 5: Build the matrix (unique coords, chunking, conversion, cache merge)

**Files:**
- Modify: `routing.py`
- Test: `tests/test_routing.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_routing.py`:

```python
def test_unique_coords_dedupes_orders_and_vehicles():
    orders = [{'origin_lat': 52.0, 'origin_lon': 0.0, 'dest_lat': 53.0, 'dest_lon': 0.0},
              {'origin_lat': 52.0, 'origin_lon': 0.0, 'dest_lat': 54.0, 'dest_lon': 0.0}]
    vehicles = [{'depot_lat': 52.0, 'depot_lon': 0.0}]
    coords = routing.unique_coords(orders, vehicles)
    # (52,0) appears 4 times across inputs but should be unique -> 3 points total
    assert sorted(coords) == sorted([(52.0, 0.0), (53.0, 0.0), (54.0, 0.0)])


def test_build_matrix_converts_units_and_applies_truck_factor(monkeypatch, tmp_path):
    coords = [(52.0, 0.0), (53.0, 0.0)]
    fake = {'code': 'Ok',
            'distances': [[0.0, 111200.0], [111200.0, 0.0]],   # metres
            'durations': [[0.0, 7200.0], [7200.0, 0.0]]}        # seconds (2.0 h)
    monkeypatch.setattr(routing, '_http_get_json', lambda url: fake)
    monkeypatch.setattr(routing, 'TRUCK_DURATION_FACTOR', 1.5)
    cache = {}
    matrix = routing.build_osrm_matrix(coords, cache, routing.DEFAULT_OSRM_URL)
    key = routing.pair_key(52.0, 0.0, 53.0, 0.0)
    km, h = matrix[key]
    assert abs(km - 111.2) < 1e-6           # metres -> km
    assert abs(h - 2.0 * 1.5) < 1e-6        # seconds -> h, x truck factor
    assert cache[key] == matrix[key]        # merged into the persistent cache


def test_build_matrix_chunks_when_over_table_size(monkeypatch):
    # 3 coords with max_table_size=2 forces chunked block queries; every pair
    # must still end up in the matrix.
    coords = [(52.0, 0.0), (53.0, 0.0), (54.0, 0.0)]
    def _fake_query(block, osrm_url):
        n = len(block)
        dists = [[_routing_hav_km(block[i], block[j]) * 1000 for j in range(n)] for i in range(n)]
        durs = [[1.0 for _ in range(n)] for _ in range(n)]
        return dists, durs
    monkeypatch.setattr(routing, 'query_osrm_table', _fake_query)
    cache = {}
    matrix = routing.build_osrm_matrix(coords, cache, routing.DEFAULT_OSRM_URL,
                                       max_table_size=2)
    for i in range(3):
        for j in range(i + 1, 3):
            assert routing.pair_key(*coords[i], *coords[j]) in matrix


def _routing_hav_km(a, b):
    from profitability_report.profitability_report_merged import _haversine_km
    return _haversine_km(a[0], a[1], b[0], b[1])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_routing.py -k "unique_coords or build_matrix" -v`
Expected: FAIL with `AttributeError: module 'routing' has no attribute 'unique_coords'`

- [ ] **Step 3: Add `unique_coords` and `build_osrm_matrix`**

Append to `routing.py`:

```python
def unique_coords(orders: list, vehicles: list) -> list:
    """All distinct (lat, lon) the batch can touch: vehicle depots plus every
    order origin and destination. Orders share postcode centroids, so this is
    far smaller than the order count."""
    seen = set()
    for v in vehicles:
        seen.add((v['depot_lat'], v['depot_lon']))
    for o in orders:
        seen.add((o['origin_lat'], o['origin_lon']))
        seen.add((o['dest_lat'], o['dest_lon']))
    return list(seen)


def build_osrm_matrix(coords: list, cache: dict, osrm_url: str,
                      max_table_size: int = DEFAULT_MAX_TABLE_SIZE) -> dict:
    """Ensure every pair among `coords` has a (km, hours) entry, querying OSRM
    only for pairs not already in `cache`. Coordinates are processed in chunks
    sized so any two chunks' union stays within the server's table limit.
    Mutates and returns `cache` (which serves as the in-memory matrix)."""
    chunk = max(1, max_table_size // 2)
    blocks = [coords[i:i + chunk] for i in range(0, len(coords), chunk)]

    for bi in range(len(blocks)):
        for bj in range(bi, len(blocks)):
            block = blocks[bi] if bi == bj else blocks[bi] + blocks[bj]
            # Skip the OSRM call if every pair in this block is already cached.
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_routing.py -q`
Expected: PASS (all routing tests)

---

## Task 6: Runner integration (`--routing` flag, install router, report fallback)

**Files:**
- Modify: `run_daily_batch.py` — argparse block (around lines 305-321) and the section after `vehicles`/`orders` are built and before the algorithms run (around line 400)
- Test: `tests/test_routing.py` (a focused unit test for the install helper)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_routing.py`:

```python
def test_install_osrm_router_sets_provider_and_returns_count(monkeypatch, tmp_path):
    import pdp_route
    orders = [{'origin_lat': 52.0, 'origin_lon': 0.0, 'dest_lat': 53.0, 'dest_lon': 0.0}]
    vehicles = [{'depot_lat': 52.0, 'depot_lon': 0.0}]
    fake = {'code': 'Ok',
            'distances': [[0.0, 111200.0, 222400.0],
                          [111200.0, 0.0, 111200.0],
                          [222400.0, 111200.0, 0.0]],
            'durations': [[0.0, 3600.0, 7200.0],
                          [3600.0, 0.0, 3600.0],
                          [7200.0, 3600.0, 0.0]]}
    monkeypatch.setattr(routing, '_http_get_json', lambda url: fake)
    original = pdp_route.get_router()
    try:
        router = routing.install_osrm_router(orders, vehicles,
                                             osrm_url=routing.DEFAULT_OSRM_URL,
                                             cache_path=tmp_path / 'c.json')
        assert pdp_route.get_router() is router
        # all 3 unique coords routed -> a real distance, no fallback
        assert router.distance_km(52.0, 0.0, 53.0, 0.0) > 0
        assert router.fallback_count == 0
    finally:
        pdp_route.set_router(original)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_routing.py::test_install_osrm_router_sets_provider_and_returns_count -v`
Expected: FAIL with `AttributeError: module 'routing' has no attribute 'install_osrm_router'`

- [ ] **Step 3: Add `install_osrm_router` to `routing.py`**

Append to `routing.py`:

```python
from pdp_route import set_router


def install_osrm_router(orders: list, vehicles: list, osrm_url: str = DEFAULT_OSRM_URL,
                        cache_path=CACHE_PATH,
                        max_table_size: int = DEFAULT_MAX_TABLE_SIZE) -> OSRMRouter:
    """Build (or extend) the cached matrix for this batch's coordinates, install
    an OSRMRouter as the active provider, and return it. The returned router's
    fallback_count reflects misses encountered during dispatch."""
    coords = unique_coords(orders, vehicles)
    cache = load_cache(cache_path)
    build_osrm_matrix(coords, cache, osrm_url, max_table_size)
    save_cache(cache_path, cache)
    router = OSRMRouter(matrix=cache, fallback=HaversineRouter())
    set_router(router)
    return router
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_routing.py::test_install_osrm_router_sets_provider_and_returns_count -v`
Expected: PASS

- [ ] **Step 5: Wire the flag into `run_daily_batch.py`**

In the argparse block (after the `--save` argument, around line 321), add:

```python
    parser.add_argument('--routing', choices=['osrm', 'haversine'], default='haversine',
                        help='Distance/time model: real road via OSRM, or straight-line (default).')
    parser.add_argument('--osrm-url', default='http://localhost:5000',
                        help='Base URL of the self-hosted OSRM server.')
```

Then, after `orders` and `vehicles` are built and filtered (immediately before the `# Build batch input` block, around line 401), add:

```python
    osrm_fallbacks = None
    if args.routing == 'osrm':
        try:
            from routing import install_osrm_router
            router = install_osrm_router(list(orders.values()), list(vehicles.values()),
                                         osrm_url=args.osrm_url)
            print(f"  Routing: OSRM ({args.osrm_url})")
        except Exception as exc:
            print(f"  Routing: OSRM unavailable ({exc}) — falling back to Haversine")
        else:
            osrm_fallbacks = router  # read fallback_count after dispatch
    else:
        print("  Routing: Haversine (straight-line)")
```

Finally, in the `WINDOW RESULT` print block (after the `Elapsed:` line, around line 514), add:

```python
    if osrm_fallbacks is not None:
        n = osrm_fallbacks.fallback_count
        served = "all pairs" if n == 0 else f"{n} pair-lookups fell back to Haversine"
        print(f"  Routing source:      OSRM ({served})")
```

- [ ] **Step 6: Run the full suite**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/ -q`
Expected: PASS — all routing tests plus the unchanged engine/dispatcher suites.

> Manual smoke (optional, needs a running OSRM server): `python -m run_daily_batch --alns --budget 20 --window-hours 24 --date 2026-01-02 --fresh --routing osrm` should print `Routing: OSRM (...)` and a `Routing source:` line, and produce larger distances/durations than the Haversine run.

---

## Task 7: OSRM provisioning docs

**Files:**
- Create: `docs/osrm-setup.md`

- [ ] **Step 1: Write the setup doc**

Create `docs/osrm-setup.md`:

```markdown
# Self-Hosted OSRM for ZEEFLEET Routing

The dispatcher's `--routing osrm` mode queries a local OSRM server. OSRM is not
bundled; stand it up once with Docker.

## One-time build (Great Britain, car profile)

```bash
mkdir osrm && cd osrm
# 1. Download a GB extract from Geofabrik
curl -O https://download.geofabrik.de/europe/great-britain-latest.osm.pbf
# 2. Pre-process (car profile bundled in the Docker image)
docker run -t -v "${PWD}:/data" osrm/osrm-backend osrm-extract -p /opt/car.lua /data/great-britain-latest.osm.pbf
docker run -t -v "${PWD}:/data" osrm/osrm-backend osrm-partition /data/great-britain-latest.osrm
docker run -t -v "${PWD}:/data" osrm/osrm-backend osrm-customize /data/great-britain-latest.osrm
```

## Run the server

```bash
docker run -t -i -p 5000:5000 -v "${PWD}:/data" osrm/osrm-backend \
  osrm-routed --algorithm mld --max-table-size 1000 /data/great-britain-latest.osrm
```

`--max-table-size 1000` raises the per-request coordinate limit so big days need
fewer chunks (the app also chunks automatically, so any value works).

Verify: `curl "http://localhost:5000/table/v1/driving/-0.12,51.5;0.16,52.1?annotations=distance,duration"`

## Truck realism (optional)

- Quick: set `TRUCK_DURATION_FACTOR` in `routing.py` above 1.0 to scale car
  durations toward HGV speeds.
- Faithful: replace `/opt/car.lua` with a custom Lua profile that lowers max
  speeds to HGV limits (50/60 mph), then re-run extract/partition/customize.
  Full HGV restriction routing (weight/height/bridge bans) is out of scope —
  see the design spec.
```

- [ ] **Step 2: Verify the file renders**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -c "import pathlib; print(pathlib.Path('docs/osrm-setup.md').read_text()[:80])"`
Expected: prints the first heading line.

---

## Task 8: Full regression

**Files:** none (verification only)

- [ ] **Step 1: Run the entire suite**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/ -q`
Expected: PASS — the prior 58 engine/dispatcher tests (default Haversine, behaviour unchanged) plus the new provider-seam and routing tests.

---

## Notes for the implementer

- **Behaviour is unchanged until `--routing osrm` is used.** The default `HaversineRouter` makes `distance_km`/`duration_h` identical to the old direct `_haversine_km` calls, so every existing test and every default run is byte-for-byte the same.
- **Test isolation:** any test that calls `set_router` MUST restore the original in a `finally` (the templates above do). A leaked stub router would corrupt later tests.
- **No live OSRM in tests:** always monkeypatch `routing._http_get_json` (or `routing.query_osrm_table`). Never hit a real server from the suite.
- **Symmetric distance** is a deliberate simplification (`pair_key` sorts the two coord keys) — acceptable at postcode-centroid resolution; documented in the spec.
- **Out of scope (do not build):** HGV-restriction routing, live traffic, automating the Docker setup, and re-routing the Haversine *heuristics* in `route_sequencer.py` / `alns.py` (they only need relative proximity).
```
