# Real Road Routing (OSRM) — Design

**Date:** 2026-05-22
**Status:** Approved design, ready for implementation plan

## Goal

Replace the dispatcher's straight-line distance (`_haversine_km`) and flat 50 km/h time model with **real road distance and duration** from a self-hosted OSRM server, so cost and schedule/deadline decisions reflect how trucks actually move on roads. Distance (drives cost) and duration (drives scheduling) become independent quantities.

## Why

The cost engine currently estimates distance as the great-circle line between points and time as `distance / 50 km/h`. Both are systematically optimistic: real roads are ~1.3× longer and trucks don't hold a flat 50 km/h. This optimism drives unrealistic plans (over-consolidation, infeasible days passing feasibility). Real road routing removes the bias. Combined with the per-stop service time already added, it makes plans operationally trustworthy — a prerequisite for the Phase 3 rolling multi-day layer, whose cross-day vehicle-position and free-time state depend directly on accurate distances and durations.

## Decisions (locked during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Engine | **Vanilla OSRM** (car network) + optional truck speed adjustment | Orders geocode to postcode-district **centroids** (~3–4 km). HGV restrictions bite at street level, which centroid input can't exploit. Real-road geometry is the win; HGV profile is a future upgrade behind the same interface once door-level addresses exist. |
| Cost | Free / self-hosted only | No paid APIs. |
| Runtime | **Live OSRM server (Docker) + persistent on-disk cache** | Query OSRM only for coordinate pairs not already cached; cache mirrors the existing `postcode_cache.json` pattern. |
| Fallback | **Warn + Haversine per unroutable pair** | Dispatch always completes; degraded pairs are counted and surfaced. |
| Integration | **Module-level routing provider** in `pdp_route` | Zero signature churn across the engine and dispatchers; Haversine default keeps current behaviour and all tests green. |
| Toggle | `--routing osrm\|haversine`, default `haversine` | Run a day both ways to quantify the difference; default stays current behaviour until the server is provisioned. |

## Architecture

A `Router` provides two methods — `distance_km` and `duration_h` — for any coordinate pair. `pdp_route` holds a module-level `_router` defaulting to a `HaversineRouter` (reproducing today's behaviour exactly). The runner builds an `OSRMRouter` (backed by a precomputed per-batch matrix + persistent cache) and installs it via `set_router()` before dispatch. The two engine functions that consume distance/time call the provider instead of `_haversine_km`:

- `route_distance_km` → `_router.distance_km(...)` (cost / reported km)
- `schedule_route` → `_router.duration_h(...)` for each leg (replaces `distance / AVG_SPEED_KMH`)

Everything else (`feasible_deadlines`, `_full_cost`, `try_insert`, `arrival_times`, and all four dispatchers) is unchanged because it flows through those two functions.

**Out of scope for change:** the Haversine uses in `route_sequencer.py` (`_nearest_neighbour`, `_two_opt`) and `alns.py` (`_destroy_shaw`) are *proximity heuristics* (which orders to reorder/remove), not final cost or feasibility. They stay on Haversine — relative proximity is all they need, and routing them would add matrix lookups for no accuracy gain in the result.

## Components

### 1. `Router` interface + `HaversineRouter` — `pdp_route.py`

```python
class Router:
    def distance_km(self, lat1, lon1, lat2, lon2) -> float: ...
    def duration_h(self, lat1, lon1, lat2, lon2) -> float: ...


class HaversineRouter(Router):
    """Default/fallback: straight-line distance, flat-speed duration —
    identical to the pre-OSRM behaviour."""
    def distance_km(self, lat1, lon1, lat2, lon2):
        return _haversine_km(lat1, lon1, lat2, lon2)
    def duration_h(self, lat1, lon1, lat2, lon2):
        return _haversine_km(lat1, lon1, lat2, lon2) / AVG_SPEED_KMH
```

Module-level provider with install/read accessors:

```python
_router: Router = HaversineRouter()

def set_router(router: Router) -> None:
    global _router
    _router = router

def get_router() -> Router:
    return _router
```

`route_distance_km` and `schedule_route` call `_router.distance_km` / `_router.duration_h`. With the default `HaversineRouter`, results are byte-for-byte identical to today, so the existing 58 tests pass unchanged.

### 2. `OSRMRouter` + matrix + cache — new module `routing.py`

OSRM-specific code (HTTP client, matrix build, cache, chunking) lives in a new top-level `routing.py`, keeping the core engine free of an HTTP dependency.

```python
class OSRMRouter(Router):
    """Serves O(1) lookups from a precomputed {(coord_a, coord_b): (km, h)}
    matrix. Any pair missing from the matrix falls back to HaversineRouter and
    increments fallback_count."""
    def __init__(self, matrix: dict, fallback: Router | None = None):
        self.matrix = matrix
        self.fallback = fallback or HaversineRouter()
        self.fallback_count = 0
```

Coordinates are keyed by rounding to 5 decimal places: `f"{lat:.5f},{lon:.5f}"`; a pair key is the ordered tuple of the two coord keys (driving distance is treated as symmetric for caching — see Edge cases).

**Matrix builder** `build_osrm_matrix(coords, cache, osrm_url, ...)`:
1. Take the batch's **unique** coordinates (depots + order origins + order destinations — far fewer than orders, since orders share postcode centroids).
2. Look each pair up in the persistent cache; collect the pairs still missing.
3. Query OSRM `/table` for the missing pairs (chunked — see Scale), converting metres→km and seconds→hours, applying `TRUCK_DURATION_FACTOR` to durations.
4. Merge results into the persistent cache and save; return the in-memory matrix for this batch.

**OSRM client:** `GET {osrm_url}/table/v1/driving/{lon,lat;...}?annotations=distance,duration`. Returns `distances` (m) and `durations` (s). Wrapped so it can be monkeypatched in tests (no live server in the test suite).

**Cache:** JSON at `data/Output/osrm_cache.json`, `{pair_key: [km, h]}`, loaded/saved like `postcode_cache.json`. Note: the cache grows with the square of the distinct postcode footprint; acceptable for the operation's bounded geography, revisit with a size cap if it balloons.

### 3. Truck speed adjustment

`TRUCK_DURATION_FACTOR` (default `1.0`, tunable) multiplies OSRM durations to approximate HGV speed limits (50/60 mph caps vs car 60/70) without a custom profile. Distance is unaffected. A faithful alternative — a custom OSRM Lua profile with HGV speed limits — is documented as optional infra; the factor is the lightweight default.

### 4. Runner integration — `run_daily_batch.py` (and `simulation/simulate.py`)

- New flag `--routing {osrm,haversine}` (default `haversine`) and `--osrm-url` (default `http://localhost:5000`).
- When `osrm`: gather the batch's unique coordinates, `build_osrm_matrix(...)`, construct `OSRMRouter`, call `pdp_route.set_router(router)` before dispatching.
- After dispatch: report routing source and fallback count in the console and in result `meta`, e.g. `routing: 998 OSRM / 2 Haversine fallback`.

### 5. OSRM provisioning (documented infra, not app code)

A `docs/osrm-setup.md` with the Docker steps: download a Great Britain OSM extract (Geofabrik) → `osrm-extract` with the car profile → `osrm-partition` → `osrm-customize` → `osrm-routed` (with `--max-table-size` raised). Optional: a custom Lua profile with HGV speed limits.

## Data flow

```
runner: unique coords (depots + order O/D)
   │
   ▼
build_osrm_matrix ── cache hit? ──► use cached (km,h)
   │ miss
   ▼
OSRM /table (chunked) ──► metres→km, sec→h ×TRUCK_DURATION_FACTOR ──► persist cache
   │
   ▼
OSRMRouter(matrix)  ──set_router()──►  pdp_route._router
   │
   ▼
route_distance_km → distance_km (cost)      schedule_route → duration_h (schedule/deadlines)
```

## Scale handling (≈482-order day)

- The matrix is over **unique coordinates**, not orders; shared postcode centroids collapse many orders to far fewer points.
- `/table` is queried in coordinate **chunks** sized to the server's `max-table-size`, assembling the full matrix from rectangular blocks (OSRM `sources`/`destinations` parameters). Recommend raising `--max-table-size` at server launch to reduce chunk count.
- Cold start for a new postcode set is a one-time cost; cached thereafter, so steady-state runs query little or nothing.

## Error handling / edge cases

- **Server down / network error:** matrix build fails gracefully → the run proceeds with a `HaversineRouter` (warned), or `OSRMRouter` with an empty matrix (every pair falls back). Dispatch always completes.
- **Unroutable pair** (OSRM returns null): that pair falls back to Haversine, `fallback_count++`.
- **Symmetric assumption:** the cache treats A→B and B→A as equal to halve storage and queries. OSRM driving distances are *near*-symmetric (one-way streets cause minor asymmetry); acceptable at postcode-centroid resolution. Documented as a known simplification.
- **Coordinate identity:** identical coords (distance 0) short-circuit to 0 without an OSRM call.
- **Empty/short routes:** unchanged — `route_distance_km` keeps its empty-stops guard.

## Testing

New `tests/test_routing.py` and additions to `tests/test_pdp_route.py`:
- `HaversineRouter` reproduces the legacy numbers: `distance_km == _haversine_km`, `duration_h == _haversine_km / AVG_SPEED_KMH`.
- `set_router`/`get_router` swap the provider; default is `HaversineRouter`. Tests reset to the default in teardown to preserve isolation.
- **Engine reads the provider:** inject a stub `Router` returning fixed values; assert `route_distance_km` sums the stub's distances (incl. the closed-loop return leg) and `schedule_route` uses the stub's durations (e.g., a stub making a leg exceed 9 h forces an overnight).
- `OSRMRouter` fallback: a matrix missing a pair returns the Haversine value and increments `fallback_count`.
- `build_osrm_matrix`: monkeypatch the OSRM client to return a canned `/table` response; assert unit conversions (m→km, s→h), `TRUCK_DURATION_FACTOR` application, and cache population. No live server.
- Cache load/save round-trip; chunk assembly produces the same matrix as a single query (stubbed).
- **Regression:** all existing tests pass with the default Haversine router (no behaviour change).

## Out of scope

- HGV-restriction routing (weight/height/bridge bans) and per-truck dimensions — future upgrade behind the same `Router` interface once door-level addressing exists.
- Live/historical traffic — OSRM is free-flow; `TRUCK_DURATION_FACTOR` is the only time adjustment.
- Automating OSRM server provisioning — documented manual Docker steps.
- Re-routing the proximity heuristics in `route_sequencer.py` / `alns.py`.
- Phase 3 rolling multi-day layer (separate spec); this work is sequenced before it.
