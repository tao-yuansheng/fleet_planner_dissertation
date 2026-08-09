# Cambridge Dispatcher v1.5 — Per-Vehicle Profile + Multi-Trip Dispatch
**Audience:** Project teammates and future-self
**Date:** 2026-05-29
**Status:** Draft for review — supersedes Sections 3 and 5 of [`cambridge-dispatcher-design.md`](cambridge-dispatcher-design.md)
**Source data:** [`../data/Output/cambridge/vehicle_profiles_derived.json`](../data/Output/cambridge/vehicle_profiles_derived.json) and [`../data/Output/cambridge/trip_profile_derived.json`](../data/Output/cambridge/trip_profile_derived.json)

---

## Why this update

The v1 dispatcher matched km and vehicle count well (+2 %, 0 delta on 2026-01-07) but **assigned only 76 of 150 in-scope orders** to its 11 rigids. The diagnostic revealed three v1 assumptions that don't match the real Cambridge fleet:

1. **Uniform vehicle capacity** (`2,500 kg / 10 pallets` for every rigid). Real per-vehicle capacity ranges from `8 pallets / 2,800 kg` (Mini Truck AY18JWA) to `33 pallets / 12,800 kg` (heavy hauler AR05DEX) — a 4× spread.
2. **Uniform shift hours** (`06:00 → 18:00` for every rigid every day). Real per-vehicle median shifts range from 8.4 h (BF65WBY) to 12.7 h (AR05DEX), with start times varying from 05:19 to 07:43.
3. **Single trip per vehicle per day.** Telematics shows **42 % of vehicle-days have ≥ 2 depot returns** (one mid-day reload). Some rigids (HX66DUH, BF65WBY) multi-trip on 73 % of days; T888RNW multi-trips on 0 %.

v1.5 replaces these three assumptions with telematics- and Qargo-derived values. No new solver code; the changes are in `config.py` and the orchestration inside `dispatcher.py` and `backtest.py`.

---

## Section 1 — Per-vehicle profile (replaces `VEHICLE_CAPACITY_KG`, `OPERATING_DAY_END`)

A new `VEHICLE_PROFILES` dict in `cambridge/config.py`, populated directly from `vehicle_profiles_derived.json`. Excerpt:

```python
VEHICLE_PROFILES = {
    'HX66DUH': {
        'asset_type':              'Lorry',
        'capacity_kg_per_trip':    6_933,   # observed p95 daily kg / median trips
        'capacity_pallets_per_trip': 15,    # observed p95 daily pallets / median trips
        'shift_start':             time(7, 15),
        'shift_end':               time(17, 39),
        'median_trips_per_day':    2,
        'multi_trip_share':        0.73,
    },
    'T888RNW': {
        'asset_type':              'Lorry',
        'capacity_kg_per_trip':    1_664,
        'capacity_pallets_per_trip': 6,
        'shift_start':             time(5, 19),
        'shift_end':               time(17, 20),
        'median_trips_per_day':    1,
        'multi_trip_share':        0.0,
    },
    'AY18JWA': {
        'asset_type':              'Mini Truck',
        'capacity_kg_per_trip':    2_812,
        'capacity_pallets_per_trip': 8,
        'shift_start':             time(7, 34),
        'shift_end':               time(16, 48),
        'median_trips_per_day':    2,
        'multi_trip_share':        0.67,
    },
    # ... eight more
}
```

The values come straight from the derivation script. No hand-tuning.

When the dispatcher builds a `DeliveryRoute` for each rigid, it reads from this dict instead of hard-coded constants. Existing `simulation/vrptw_engine.DeliveryRoute` already accepts `capacity_kg`, `capacity_pallets`, `shift_start`, `shift_end` — no engine change.

---

## Section 2 — Multi-trip dispatch model (replaces single-event `run_day`)

The solver itself doesn't change; we already have `run_event` for a single planning event. v1.5 makes `run_day` orchestrate **up to two `run_event` calls per multi-trip rigid per day**.

### Per-rigid trip classification

For each rigid, derived from `VEHICLE_PROFILES`:

- `multi_trip_share >= 0.40` → **multi-trip rigid** (gets two events: morning + afternoon)
- `multi_trip_share < 0.40` → **single-trip rigid** (one event covering the full shift, using the rigid's *daily* total capacity — `kg_per_trip × median_trips`)

The 40 % threshold cleanly separates the 6 multi-trip rigids (HX66DUH, BF65WBY, AY18JWA, LN67SWJ, T88GNW, T88RNW) from the 5 single-trip rigids (T888RNW, M88GNW, AR05DEX, L88GNW, W88RNW).

### Two-event orchestration

```
run_day(day, scoped_orders, ...):
    multi_rigids   = [r for r in CB22 rigids if profile.multi_trip_share >= 0.40]
    single_rigids  = [r for r in CB22 rigids if profile.multi_trip_share <  0.40]

    # --- Event A: 06:00 morning push ---
    rigids_for_A = single_rigids  # full shift, daily capacity
                 + multi_rigids   # capped at first-trip shift end, per-trip capacity
    output_A = run_event(orders_ready_at_06, rigids_for_A, planning_time=06:00)

    # --- Event B: mid-day reload (multi-trip rigids only) ---
    returned_rigids = [r for r in multi_rigids
                       if r appeared in output_A.routes
                       and r.predicted_return_time + DEPOT_DWELL_MIN <= profile.shift_end]
    rigids_for_B = [r reloaded with per-trip capacity refreshed,
                    shift_start = predicted_return + DEPOT_DWELL_MIN,
                    shift_end   = profile.shift_end]
    output_B = run_event(unassigned_after_A, rigids_for_B, planning_time=event_B_time)

    return merge(output_A, output_B)
```

### Constants for v1.5

From `trip_profile_derived.json`:

| Constant | Value | Source |
|---|---|---|
| `MULTI_TRIP_THRESHOLD` | 0.40 | clean separation in the 11 vehicles |
| `DEPOT_DWELL_MIN` | 42 | median inter-trip dwell across 242 observations |
| `EVENT_B_DEFAULT_HOUR` | 12 | derived from median trip start + dwell |
| `DEFAULT_TRIP_DURATION_H` | 4.1 | median across 601 trips |

Per-vehicle event-B times can override the default by reading `profile.shift_end - DEFAULT_TRIP_DURATION_H` if more precision is needed in v1.6.

---

## Section 3 — Backtest mode (telematics-grounded) vs forward mode (profile-grounded)

The two modes diverge on **how event-A's shift-end and event-B's start time are set**:

| Mode | Event A shift-end | Event B start | Use case |
|---|---|---|---|
| **Backtest** | Per (vehicle, day): the actual first depot-return time from telematics | Per (vehicle, day): actual second-trip start time from telematics | Validating against historical days |
| **Forward** | Per-vehicle: `shift_start + DEFAULT_TRIP_DURATION_H` from profile | Per-vehicle: event A shift-end + `DEPOT_DWELL_MIN` | Live planning when telematics doesn't exist yet |

The dispatcher takes a `mode` parameter; backtest mode also takes the day's telematics so it can derive trip times per (vehicle, day).

---

## Section 4 — Capacity model: per-trip vs daily

| Rigid type | Capacity passed to event A | Capacity passed to event B |
|---|---|---|
| **Multi-trip rigid** | per-trip (`capacity_pallets_per_trip`) | per-trip (refreshed) |
| **Single-trip rigid** | daily total (`per_trip × median_trips_per_day`) | — (no event B) |

For HX66DUH (multi-trip, 2 trips/day, p95 per trip = 15 pallets): two events × 15 pallets = 30 pallets/day total. This matches the observed p95 daily load (`derived_capacity_pallets_p95 ≈ 30`).

For T888RNW (single-trip, p95 per trip = 6 pallets): one event × 6 pallets = 6 pallets/day total. Matches observed daily load.

For AR05DEX (single-trip but high-capacity, 33 pallets per trip): one event × 33 pallets = 33 pallets/day. The 12.7 h shift gives the solver plenty of time to build that route.

---

## Section 5 — What does NOT change in v1.5

Explicit non-goals:

- **The solver (`simulation/vrptw_alns`)** — unchanged. v1.5 calls the existing `run_event` two times where needed; the solver remains a black-box single-closed-loop VRPTW.
- **Cost function** — still `VEHICLE_ACTIVATION_COST + fuel_rate × km`. The £150 activation cost is still artificial (it forces consolidation); we accept that planned £ totals are not directly comparable to Jigsaw real spend. v2.x can revisit.
- **Scope filter (item 3)** — the 43 % of Cambridge-fleet orders excluded by the v1 scope filter stay excluded. Addressed separately in v1.6.
- **Tractor pool (item 4)** — `CB22_TRACTORS` list still unverified. v1.6.
- **FULL_FLEET collection planning** — `collection_planner.py` still uses the same per-origin profiles. No change.
- **`rolling_dispatcher.py` integration** — still not used. v1.5 is a fixed 2-event-max model, not full rolling-horizon. v2.

---

## Section 6 — Files affected

| Path | Change |
|---|---|
| `cambridge/config.py` | Add `VEHICLE_PROFILES`, `MULTI_TRIP_THRESHOLD`, `DEPOT_DWELL_MIN`, `EVENT_B_DEFAULT_HOUR`, `DEFAULT_TRIP_DURATION_H`. Loaded from `vehicle_profiles_derived.json` at import. |
| `cambridge/dispatcher.py` | New `run_day_multi_trip(day, orders, rigids, trips, postcode_cache, mode, telematics_df=None)`. Old `run_day` kept as thin wrapper that delegates. |
| `cambridge/backtest.py` | `run_day_backtest` passes telematics + mode=`'backtest'`. Vehicle construction reads from `VEHICLE_PROFILES`. |
| `cambridge/__main__.py` | No interface change. |
| `tests/cambridge/test_dispatcher.py` | Add tests for multi-trip routing + per-vehicle profile lookup. |
| `tests/cambridge/test_backtest.py` | Update fixture rigid construction to use profile-derived values. |

No changes to `simulation/`.

---

## Section 7 — Open questions

1. **Multi-trip threshold of 0.40** — clean separation in the current 11, but if a 12th rigid joined the fleet with `multi_trip_share = 0.35`, the binary split would mis-route it. Defensible default for v1.5; reconsider in v1.6.
2. **Event A and Event B both run the solver from scratch.** Event B sees `unassigned_after_A`, but its solver call is independent — it doesn't know about Event A's routes. This is OK because the solver only places orders on `rigids_for_B`. But there's no warm-start carry-over. For a v1.5 production-quality run, may want to seed Event B with insights from Event A.
3. **Single-trip rigids with daily-aggregate capacity (e.g. AR05DEX = 33 pallets in one route)** — the solver will build a single closed loop with 33 deliveries. Service-time alone (33 × 20 min = 11 h) approaches the shift budget. If service-time + drive-time blows the shift, the solver will drop orders. Accept the v1.5 behaviour; revisit if it produces frequent drops.
4. **Profile drift over time.** The profile is derived from Jan-Feb 2026 telematics. Fleet operations may change; the script should be rerun monthly or on a calendar trigger. Not enforced in v1.5; add a `profile_generated_at` timestamp inside the JSON.

---

## Section 8 — Validation expectations

Once v1.5 is implemented, re-run the smoke test (`python -m cambridge --date 2026-01-07`). Expected changes vs v1:

| Metric | v1 | v1.5 expected |
|---|---|---|
| orders_assigned | 76 | **≈ 130–150** (close to scope filter's 150) |
| total_km (planned) | 1,924 | similar or slightly higher (more orders fit) |
| vehicles_used | 11 | 11 (same fleet) |
| km delta vs actual | +2 % | **probably still within ±10 %** |
| assignment_pp_delta | +60 pp | **near zero** |
| day_pass | False | **possible True** if fuel + on-time also clear |

If `orders_assigned` doesn't close in to 130+, something in the multi-trip model is wrong (likely the event-B shift budget or the per-trip capacity). That's our debugging signal.

---

## Section 9 — v1.6+ roadmap (deferred)

| Item | Why deferred |
|---|---|
| Scope-filter widening (item 3) — investigate the 832 "sub_other" + 644 "geographic" exclusions | Needs a focused diagnostic pass first |
| Tractor pool verification (item 4) — re-derive `CB22_TRACTORS` from telematics | Same approach as the rigid study |
| Multi-trip support for 3+ trips/day (10 % of days) | Adds solver-level complexity; v1.5 caps at 2 |
| Full rolling-horizon (Palletline mid-day trunks) | Needs `freight_tracker.py` integration |
| Cost-function revision (drop the artificial £150 activation, add fuel-only mode) | Once v1.5 passes day_pass, this is the next gap |
