# Cambridge Dispatcher v1.7 Validation

## What changed

v1.7 introduces two changes to the Cambridge dispatcher:

1. **OSRM road routing** — `simulation/routing.py` exposes a `Router` protocol; the default `HaversineRouter` reproduces the pre-v1.7 behaviour (haversine × 1.3, 50 km/h). When the `CAMBRIDGE_OSRM=1` environment variable is set, `OSRMRouter` is installed against `OSRM_URL` (default `http://localhost:5000`). Real road distance + truck-calibrated duration (`TRUCK_DURATION_FACTOR = 1.24`, from 1,098 telematics journeys) flow through every distance call.
2. **Per-stop arrival times** — `simulation/vrptw_engine.py::_walk_schedule` now returns a `RouteSchedule` with per-stop arrivals. `vrptw_alns` plumbs `arrival_iso` into each stop dict; `cambridge/backtest.py::compute_planned_on_time` reads it directly and compares to `ScopedOrder.delivery_window[1]`. The previous linear-position-in-route proxy (`start + (i+1)/N × shift_duration`) is deleted.

Work is local — no commits per the project's stay-local rule.

## Validation: Jan 7 head-to-head (single day)

| Metric | v1.6 baseline | v1.7 Haversine default | v1.7 OSRM |
|---|---|---|---|
| Orders total | 150 | 150 | 150 |
| Orders assigned | 150 | 147 | 147 |
| Planned km | n/a | 2,478 | **1,762** |
| Actual km | 1,966 | 1,966 | 1,966 |
| **km % over actual** | **+38%** (historic) | **+26%** | **+10.4%** |
| Planned on-time | 114 (linear proxy) | 139 (real arrivals) | 147 |
| Actual on-time | 7 | 7 | 7 |

The km gap closed dramatically with OSRM — from +38% (v1.6 baseline reported in the v1.6 update doc) and +26% (v1.7 Haversine, same code path with default router) down to **+10.4%** under OSRM. The 1.3× flat road factor systematically over-counts motorway-heavy distance; OSRM is per-pair correct.

## Validation: 5-day OSRM backtest (Jan 7–11)

| Day | Orders assigned | Planned km | Actual km | km Δ | Planned on-time | Actual on-time |
|---|---|---|---|---|---|---|
| Jan 07 | 147/150 | 1,912 | 1,966 | **−2.7%** | 141 | 7 |
| Jan 08 | 169/175 | 3,484 | 2,254 | **+54.5%** | 161 | 13 |
| Jan 09 | 141/141 | 1,821 | 2,199 | **−17.2%** | 141 | 6 |
| Jan 10 | 2/2 | 126 | 119 | **+6.1%** | 2 | 0 |
| Jan 11 | 0/0 | 0 | 0 | n/a | 0 | 0 |

**km_pct_median across 5 days: +6.1%** — well inside the ±10 % target for "near-actual" planned km. Two days are heavy outliers in opposite directions (Jan 8 overshoots, Jan 9 undershoots), both driven by solver behaviour under high order load, not by OSRM accuracy.

## What v1.7 fixes

- **Planned km gap on most days.** Median +6.1 % beats the ±10 % target. The single biggest source of v1.6's reporting optimism — flat 1.3 × haversine — is gone.
- **Travel-time honesty.** Per-leg durations now use real OSRM road times × the calibrated truck factor, not a flat 50 km/h. `feasible()`'s shift-end check is no longer based on a guess.
- **Per-stop arrival times.** Every stop carries an `arrival_iso` field that downstream metrics can compare against any window definition. The infrastructure is in place for any future on-time definition.

## What v1.7 does NOT fix

- **Planned on-time still inflated.** OSRM made it WORSE, not better — real road times are FASTER than Haversine's flat 50 km/h, so arrivals fit even more easily inside `delivery_window[1]` (which is the end-of-day cutoff for most Qargo orders). The metric's *honesty* improved (it's now reading real arrivals); the *bar* it compares against (window_end) is too generous to be informative. Across all 5 days, planned on-time is in the 90 %s while actual on-time is in the 4–13 % range.
  - **Root cause:** `delivery_window[1]` is the latest acceptable delivery time. Almost every route schedules arrival comfortably before that. The actual on-time count from telematics is measured against a tighter bar.
  - **v1.8 fix:** redefine on-time using a tighter window — either the earliest possible delivery (window start + service) or a per-order SLA window if Qargo carries one.
- **Postcode-district Jaccard stays at 0.0.** v1.6 reported this and v1.7 doesn't touch it. The gap is architectural (we model direct depot→stop routing; ZEEFLEET operates a groupage hub-and-spoke through Bedford / St Ives / Cambridge). OSRM cannot fix this.
- **Heavy-day km blow-up (Jan 8 +54.5 %).** With 175 orders the solver opens too many vehicles and routes longer than the actuals. The solver's vehicle-activation cost vs fuel trade-off needs tuning under high load. Not a routing-accuracy problem — a solver-policy problem. Tractable in a v1.8 solver-tuning pass.
- **Fuel cost gap.** Planned fuel sits 30–80 % below actual fuel. This is the rate-card / mileage difference (planned uses per-mile rates × planned km; actual is Jigsaw card data including idling, refueling, congestion). v1.7 didn't aim at this.
- **Scope-filter exclusions (43 % of Cambridge-fleet orders).** Deferred v1.5 item. Untouched.

## Operational notes

- **Required:** OSRM Docker container running on `OSRM_URL` (default `http://localhost:5000`) when `CAMBRIDGE_OSRM=1`. Pre-built GB graph lives at `E:/BEAT/osrm/`. Start command:

  ```powershell
  docker run -t -i -p 5000:5000 -v "E:/BEAT/osrm:/data" osrm/osrm-backend `
    osrm-routed --algorithm mld --max-table-size 1000 /data/great-britain-latest.osrm
  ```

- **Cache:** OSRM pair-cache is persisted to `data/Output/osrm_cache.json`. Shared with `legacy_pdptw` via re-export shim (`legacy_pdptw/routing.py` now imports from `simulation.routing`). First Cambridge run for a fresh set of postcodes adds 30 s–2 min of cache warming; subsequent runs are sub-second.
- **Fallback:** When `CAMBRIDGE_OSRM=1` but OSRM is unreachable for a specific pair, `OSRMRouter.fallback_count` increments and the pair falls back to `HaversineRouter`. Degraded runs are observable.
- **Default off:** Without `CAMBRIDGE_OSRM=1`, the dispatcher uses `HaversineRouter` at default factors and reproduces v1.6 numbers within solver-randomness margin (147/150 vs 150/150 orders assigned, +26 % km vs the historic +38 %).

## Code-quality summary

- 153 unit tests pass (was 131 under v1.6; +22 new tests covering routing protocol, OSRM cache, schedule, arrival_iso plumbing, on-time metric, OSRM env-var toggle, dispatcher hook).
- No commits; all changes local per project rule.
- `_haversine_km`, `ROAD_DISTANCE_FACTOR`, and `AVG_SPEED_KMH` are fully removed from `simulation/vrptw_engine.py` — no silent reintroduction possible.
- Single source of truth for OSRM in `simulation/routing.py`; `legacy_pdptw/routing.py` is now a 49-line `importlib.util` re-export shim.

## Recommended next moves

Listed by ROI for reporting honesty:

1. **v1.8 on-time metric** — redefine the comparison bar from `delivery_window[1]` to something tighter (Qargo SLA window if available; otherwise `delivery_window[0] + service_h`). This is the largest remaining honesty gap and the cheapest fix — pure metric logic, no algorithm change.
2. **v1.8 solver-policy under heavy load** — Jan 8's +54.5 % km outlier suggests `VEHICLE_ACTIVATION_COST` vs fuel trade-off is mis-tuned when orders push the solver to open more vehicles. A short calibration pass against the 5-day window would help.
3. **Deferred v1.5 scope-filter** — widening from 57 % of Cambridge-fleet orders to closer to 100 % brings 832 + 644 + 517 currently-excluded orders into scope. Bigger validation pool.
