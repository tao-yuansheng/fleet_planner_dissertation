# Cambridge Dispatcher v1.7 — OSRM Routing + Honest Lateness

**Status:** design
**Predecessor:** v1.6 (100% order closure, 150/150 on Jan 7; on-time and km metrics optimistic)
**Goal:** Replace haversine × 1.3 + 50 km/h with real OSRM road distance and truck-calibrated duration, and rebuild the on-time/late metric on actual per-stop arrival times so reported figures stop overstating reality.

---

## Why v1.7

v1.6 achieved the project's main goal — every Cambridge-fleet rigid order is dispatched, with multi-trip and shift overrun. But the validation report overstates two things:

| Symptom (Jan 7) | Today's mechanism | Why it lies |
|---|---|---|
| Planned km = +38 % over actual | `_haversine_km × 1.3` for every leg | Flat 1.3× factor under-counts dense urban diversions and over-counts motorway hauls |
| Planned on-time = 114, actual = 7 | Linear position-in-route proxy (`start + (i+1)/N × shift`) | Proxy assumes evenly spaced arrivals and never compares the actual depart-time + travel + service against the window end |

Both gaps are about reporting honesty, not order completeness. v1.7 fixes both without changing the dispatch logic.

## Architecture

A single new module `simulation/routing.py` exposes a `Router` protocol with `distance_km(lat1, lon1, lat2, lon2) -> float` and `duration_h(lat1, lon1, lat2, lon2) -> float`. Two implementations live alongside it: `HaversineRouter` (preserves current behavior, default) and `OSRMRouter` (pair cache + `/table` queries, ported from `legacy_pdptw/routing.py`). The active router is a module-level singleton in `simulation/vrptw_engine.py`, set via `set_router(router)`; all four current `_haversine_km` call sites and the `AVG_SPEED_KMH` divisions go through it.

`_estimated_return_time` is extended to also produce a per-stop arrival map. `feasible()` keeps using just the return time; the new `compute_planned_on_time` consumes the arrival map and compares each stop's arrival to its `delivery_window` end. One scheduler, three consumers.

## Design Decisions

### D1 — Router seam: module-level singleton

`simulation/routing.py` exports `Router` protocol, `HaversineRouter`, `OSRMRouter`, and `set_router/get_router` symbols. `simulation/vrptw_engine.py` imports `get_router` and replaces every `_haversine_km(a, b, c, d) * ROAD_DISTANCE_FACTOR` with `_router.distance_km(a, b, c, d)`. Every `... / AVG_SPEED_KMH` becomes `_router.duration_h(...)`.

Trade-off accepted: global state across the process is fine — we never run two router configs concurrently.

`ROAD_DISTANCE_FACTOR` and `AVG_SPEED_KMH` move *into* `HaversineRouter` as constructor args (defaults preserve current behavior). The module-level constants are deleted from `vrptw_engine.py` to prevent stale call sites.

### D2 — Per-stop arrival schedule from a single scheduler

Rename `_estimated_return_time` → `_walk_schedule(route) -> RouteSchedule` where:

```python
@dataclass
class RouteSchedule:
    arrivals: dict[str, datetime]   # order_id -> arrival datetime at that stop
    depart_after: dict[str, datetime]  # order_id -> service-completion datetime
    return_time: datetime
```

`feasible()` now reads `_walk_schedule(route).return_time`. `compute_planned_on_time` in `backtest.py` accepts route dicts that carry `stop_arrivals: {order_id: iso_string}` injected by `run_event`, and compares against `ScopedOrder.delivery_window[1]`.

The linear-position proxy is **deleted**, not deprecated. Two scheduling paths drift; one doesn't.

### D3 — Shared OSRM cache

The legacy cache at `data/Output/osrm_cache.json` uses `tuple(sorted((coord_key, coord_key))) -> [km, hours]`. New `simulation/routing.py` reads/writes the same file with the same schema. `legacy_pdptw/routing.py` keeps its existing imports working via thin re-exports (`from simulation.routing import OSRMRouter, load_cache, save_cache`). Single warm pool.

Cache writes are batched at end of `run_period` (not per-day), to avoid 5+ open/close cycles on a multi-day backtest.

### D4 — Truck duration: reuse calibrated factor

`TRUCK_DURATION_FACTOR = 1.24` (from 1,098 telematics journeys, recorded in legacy routing.py) is carried over verbatim. Recalibration on Cambridge-only data is a v1.8 concern, not v1.7.

## Files Touched

| File | Change |
|---|---|
| `simulation/routing.py` | **NEW** — Router protocol, HaversineRouter, OSRMRouter, cache I/O, module-level singleton accessors |
| `simulation/vrptw_engine.py` | Replace all `_haversine_km × ROAD_DISTANCE_FACTOR` and `/ AVG_SPEED_KMH` with router calls; rename `_estimated_return_time` → `_walk_schedule`; return `RouteSchedule` |
| `simulation/vrptw_alns.py` | Inject `stop_arrivals` into route output dicts |
| `simulation/rolling_dispatcher.py` | Accept optional `router` kwarg; pass through to `set_router` before solver runs |
| `cambridge/dispatcher.py` | At start of `run_day_multi_trip`, call `set_router(OSRMRouter(...))` if `cambridge.config.OSRM_ENABLED` is true (env var `CAMBRIDGE_OSRM=1`); default keeps `HaversineRouter`. CLI-flag plumbing is out of scope for v1.7 |
| `cambridge/backtest.py` | Rewrite `compute_planned_on_time` to read `stop_arrivals` from route dict; delete linear-position proxy |
| `cambridge/config.py` | Add `OSRM_URL` (default `http://localhost:5000`), `OSRM_ENABLED` env-var check |
| `legacy_pdptw/routing.py` | Re-export `OSRMRouter`, `load_cache`, `save_cache` from `simulation.routing` (keeps legacy backtests running) |
| `tests/test_vrptw_engine.py` | Update for `_walk_schedule` shape change |
| `tests/test_routing.py` | **NEW** — HaversineRouter parity test (must match current haversine × 1.3 output to 0.01 km), OSRMRouter cache hit/miss/fallback, schedule shape |

## Validation Plan

Run after implementation:

1. **Haversine parity**: with default `HaversineRouter`, replay v1.6 Jan 7 backtest. Total planned km must match v1.6 to within 0.5 km. (Refactor should not move the number.)
2. **OSRM mode, Jan 7**: with `set_router(OSRMRouter(...))`, expect planned km to drop from current +38 % over actual to within ±10 %.
3. **Honest on-time**: with the new arrival-time metric, expect `planned_on_time` on Jan 7 to fall from 114 toward the actual 7 ± a small band. The exact number isn't the spec — but "114 out of 150" is the symptom we're killing.
4. **5-day backtest** (Jan 7–11): `planned_on_time / orders_assigned` distribution should resemble the actual on-time distribution, not be uniformly ~95 %.
5. **Tests**: full pytest suite (131 passing today) stays green. New `test_routing.py` adds parity + cache tests.

## Non-Goals

Explicitly out of scope for v1.7 (these are real concerns, deferred):

- **HGV-specific OSRM profile** — custom Lua with HGV speed limits, bridge weight bans, etc. Today's car profile × 1.24 truck factor is the agreed cost/value compromise.
- **Cambridge-only re-calibration** of `TRUCK_DURATION_FACTOR`. Reuse the 1,098-journey calibration as-is.
- **Postcode-Jaccard fix (groupage hub routing)** — Jaccard = 0.0 is an architectural gap (direct vs hub-and-spoke), not a routing-accuracy gap. OSRM doesn't move that number.
- **Scope-filter widening** (832 sub_other, 644 geographic, 517 sub_export) — separate work, listed in deferred v1.5 items.
- **Tractor pool verification** — separate, deferred.

## Operational Prerequisites

The OSRM server must be reachable at `OSRM_URL` before running with `--routing osrm`. The pre-built GB graph exists at `E:/BEAT/osrm/great-britain-latest.osrm*`. To start the server:

```bash
cd /e/BEAT/osrm
docker run -t -i -p 5000:5000 -v "${PWD}:/data" osrm/osrm-backend \
  osrm-routed --algorithm mld --max-table-size 1000 /data/great-britain-latest.osrm
```

When OSRM is unreachable, `OSRMRouter` falls back to `HaversineRouter` per-call and increments `fallback_count` (preserved from legacy behavior). Degraded runs print a warning at end of `run_period`.

## Risks

- **R1 — Cache file corruption** if two pipelines write concurrently. Mitigation: cache writes only at end of `run_period`, and the file is loaded fresh at start of each run.
- **R2 — OSRM `/table` row limit**. Current `--max-table-size 1000` and existing chunking logic in `legacy_pdptw/routing.py` handle this; the port preserves the chunking.
- **R3 — Refactor regression** in haversine parity. Mitigated by the explicit parity test (validation step 1).
- **R4 — Schedule shape change breaks unrelated code**. `_estimated_return_time` is called from `feasible()`, `try_insert`, and `vrptw_alns` move operators. All four call sites get updated; tests catch any miss.

## Success Criteria

v1.7 ships when:

1. Default-router (Haversine) parity test passes — no behavior change from v1.6.
2. OSRM-router Jan 7 backtest shows planned km within ±10 % of actual.
3. OSRM-router 5-day backtest shows `planned_on_time` distribution close to actual on-time distribution (not a uniform overstatement).
4. All existing tests pass; new routing tests pass.
5. No commits — work stays local per project constraint.
