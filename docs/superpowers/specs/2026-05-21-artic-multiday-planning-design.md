# Artic Multi-Day Planning — Design

**Date:** 2026-05-21
**Status:** Approved design, ready for implementation plan

## Goal

Extend the dispatcher so that articulated vehicles (artics) can run **multi-day routes** — staying out overnight and resuming the next morning — while rigid vehicles keep their current **same-day, return-to-depot** behaviour. Orders continue to be prioritised by deadline, now across a multi-day horizon.

## Why (verified from telematics)

January 2026 overnight GPS (pings 23:00–05:00, distance to assigned depot) shows two clearly distinct operating patterns:

| Fleet | Vehicles | Home overnight | Verdict |
|---|---|---|---|
| Rigids | 37 | **92%** (away nights median only 17 km — local home-parking, not real trips) | Same-day, return home. Keep single-day model. |
| Artics | 33 | **65%** (Bedford artics just 40%; away nights median 59 km, p90 150 km, max 472 km; 20 trips of 4+ nights, longest 15) | Materially multi-day. Needs the extension. |

Both fleets confirm a structural fact: depots are **home-based, not interchangeable** — only 4% (artics) / 17% (rigids) of away-nights ended at a *sister* depot. So the return anchor is always the truck's **own** assigned depot. The "return to nearest depot / re-home freely" idea is rejected by the data.

## Key insight: this is a *time* model, not a *distance* model

The closed-loop change already merged (`route_distance_km` returns `depot → stops → depot`) computes the **correct total mileage** for an artic too: it returns home exactly once, at job end. Multi-day does **not** change total distance or per-mile cost. What it adds is a **scheduling layer over time**: when the daily driving cap is hit, the clock jumps to the next morning (the truck parks where it stopped, no extra travel), which pushes later arrival times and therefore changes which deadlines are feasible.

So the core of this work is a `schedule_route()` function that the deadline-feasibility check and arrival-time reporting both run through, branching on vehicle class.

## Design decisions (locked)

| Parameter | Decision | Value |
|---|---|---|
| Daily limit | Driving-hours cap | `MAX_DRIVING_HOURS_PER_DAY = 9.0` (≈450 km at 50 km/h) |
| Shift start | Fixed morning start | `SHIFT_START_HOUR = 6` (06:00 local) |
| Horizon | Auto from deadlines, capped | `min(furthest deadline, 7 days)` |
| Re-planning | Rolling: commit day 0, replan rest | reuse intra-day rolling-window pattern |
| Fleet split | Feasibility-driven, no hard rule | rigids must fit one shift; artics may span days |
| Vehicle class | Derived from `AssetType` | `multi_day = (asset_type == 'Tractor Unit')` — verified: every artic circuit is exclusively Tractor Unit, no rigid circuit contains one |
| Overnight-out cost | Per-night allowance (lever to bias toward same-day) | `NIGHT_OUT_COST_GBP = 30.0` (configurable; set to real subsistence rate) |
| Overnight position | Park at last stop, no travel during rest | — |

## Components

### 1. Vehicle class flag — `simulation/data_loader.py`

`build_vehicles()` adds `multi_day: bool` to each vehicle record, derived as `asset_type == 'Tractor Unit'`. No other change; `depot_lat/depot_lon` (home depot) remain the return anchor for both classes.

### 2. Time-scheduling engine — `pdp_route.py`

New constants: `MAX_DRIVING_HOURS_PER_DAY = 9.0`, `SHIFT_START_HOUR = 6`, `NIGHT_OUT_COST_GBP = 30.0`. Existing `AVG_SPEED_KMH = 50.0` unchanged.

New helper:

```python
def _next_shift_start(clock: datetime) -> datetime:
    """06:00 on the day after `clock`."""
    nxt = (clock + timedelta(days=1)).replace(
        hour=SHIFT_START_HOUR, minute=0, second=0, microsecond=0)
    return nxt
```

New core function:

```python
def schedule_route(start_lat, start_lon, stops, start_time, multi_day):
    """
    Walk the route in time, inserting overnight rests when the daily driving
    cap is exhausted. Returns (arrivals, overnights) where arrivals[i] is the
    clock datetime at stops[i], or None if infeasible for this vehicle class.

    - Rigid (multi_day=False): a single leg that would exceed the remaining
      shift makes the route infeasible -> returns None. This is what pushes
      jobs that can't be done same-day onto artics (feasibility-driven split).
    - Artic (multi_day=True): when the next leg exceeds the remaining shift,
      the clock advances to the next 06:00 (truck parks at the current point,
      no travel during rest) and the shift budget resets. Legs longer than a
      full shift insert as many overnights as needed.
    """
    drive_left = MAX_DRIVING_HOURS_PER_DAY
    clock = start_time
    prev_lat, prev_lon = start_lat, start_lon
    overnights = 0
    arrivals = []
    for stop in stops:
        leg_h = _haversine_km(prev_lat, prev_lon, stop.lat, stop.lon) / AVG_SPEED_KMH
        while leg_h > drive_left:
            if not multi_day:
                return None
            clock = _next_shift_start(clock)
            overnights += 1
            # consume one full shift of the leg, then continue from the new day
            leg_h -= drive_left
            drive_left = MAX_DRIVING_HOURS_PER_DAY
        clock = clock + timedelta(hours=leg_h)
        drive_left -= leg_h
        arrivals.append(clock)
        prev_lat, prev_lon = stop.lat, stop.lon
    return arrivals, overnights
```

`feasible_deadlines()` rewritten to call `schedule_route()` and check each delivery's scheduled arrival against its deadline. Returns False if `schedule_route` returns None (rigid over one shift) or any delivery is late.

`arrival_times()` rewritten to delegate to `schedule_route()` so reported arrival times include overnight gaps.

**Signature changes (both functions gain a `multi_day` parameter):**
- `feasible_deadlines(start_lat, start_lon, stops, start_time, orders, multi_day)`
- `arrival_times(start_lat, start_lon, stops, start_time, multi_day)`

Callers already hold the `Route`, so they pass `route.asset_type == 'Tractor Unit'`:
- `try_insert()` ([pdp_route.py](../../pdp_route.py)) passes it into its `feasible_deadlines` call.
- ALNS/LNS/MCTS output builders pass it into their `arrival_times` call.
This keeps the engine the single source of the class branch and avoids adding a field to `Route`. The new `multi_day` parameter **defaults to `False`** (same-day/rigid), so existing call sites and the current test suite keep their behaviour unchanged — a single-shift schedule reproduces the old `start_time + km/speed` arithmetic exactly.

> **Note on the return leg in scheduling.** Deadline feasibility schedules only the delivery stops; the final return-to-depot leg affects total mileage/cost (already handled by the closed-loop `route_distance_km`) but not delivery arrival times, so it is excluded from the deadline schedule. For an artic, this means the truck may finish deliveries on day N and drive home on day N (or N+1) — the home-return drive is costed but does not gate any deadline.

### 3. Overnight cost in the objective — `pdp_route.py`

`route_cost()` adds the night-out allowance:

```python
def route_cost(route, cost_rates):
    mileage = _stops_cost(route.start_lat, route.start_lon, route.stops,
                          route.asset_type, cost_rates)
    multi_day = route.asset_type == 'Tractor Unit'
    sched = schedule_route(route.start_lat, route.start_lon, route.stops,
                           route.start_time, multi_day)
    overnights = sched[1] if sched else 0
    return mileage + overnights * NIGHT_OUT_COST_GBP
```

This gives the optimiser a concrete reason to prefer same-day completion and to consolidate overnight trips, without changing the per-mile cost. `Route` needs a `multi_day` field or the engine derives it from `asset_type` (chosen: derive from `asset_type`, no schema change).

### 4. Horizon cap — `mcts_dispatcher.py` / shared planning helpers

`_planning_start_time()` is extended (or paired with `_planning_horizon()`) to compute the horizon end = `min(start_time + 7 days, furthest order deadline)`. An insertion whose scheduled arrival exceeds the horizon end is infeasible. In practice the per-order deadline check (already enforced) dominates; the 7-day cap is a backstop for orders with the 48 h fallback deadline or unusually distant windows.

### 5. Deadline prioritisation (preserved)

No new mechanism. The greedy seed already sorts by `service_level_priority` then deadline; multi-day keeps this ordering. Because `feasible_deadlines` now spans days, a tight next-day order is scheduled early in a route while a 5-day order can be placed later or deferred — the optimiser (ALNS) minimises mileage + night-out cost subject to every deadline. "Which day" emerges from sequencing rather than an explicit per-order day assignment.

### 6. Rolling commit / re-plan — `simulation/rolling.py` (Phase 3)

After a horizon plan is produced, a thin commit layer:
- marks all stops whose scheduled arrival falls on **day 0** as committed (locked);
- for each vehicle, derives carry-over state for the next run: position at the day-0 cutoff and the time it next becomes free (mid-trip artics carry their parked position + next-morning shift start);
- the next planning run seeds these committed routes (reusing the existing intra-day rolling-window lock-and-project pattern) and adds newly-arrived orders.

This reuses the rolling-window machinery already in the codebase; it is split out as Phase 3 so that Phases 1–2 (which already enable multi-day planning over a static horizon) can land and be validated first.

## Data flow

```
build_vehicles ──> vehicles carry asset_type (-> multi_day) + home depot
                              │
orders (deadlines, priority) ─┤
                              ▼
        dispatcher (ALNS/LNS/greedy) inserts orders
                              │  calls per candidate route:
                              ▼
        route_cost ──► mileage (closed loop) + overnights × night-out
        feasible_deadlines ──► schedule_route (overnight rests) ≤ deadlines
                              ▼
        best plan ──► [Phase 3] commit day 0, carry state, replan next day
```

## Error handling / edge cases

- **Rigid over one shift:** `schedule_route` returns None → route infeasible → order falls to an artic (or unassigned if none feasible, surfaced in `orders_unassigned` as today).
- **Single leg longer than a full shift (>450 km):** artics insert multiple overnights via the `while` loop; rigids reject. No mid-leg physical parking is modelled (overnight = no travel), which is acceptable since cost is mileage-based.
- **Order with no/loose deadline:** 48 h fallback (existing) plus the 7-day horizon backstop bound it.
- **Empty route:** `schedule_route` returns `([], 0)`; cost falls back to 0 via the existing empty-route guard in `route_distance_km`.

## Testing

Extend `tests/test_pdp_route.py`:
- `schedule_route` single-shift route: no overnights, arrivals match the old `start_time + km/speed`.
- `schedule_route` artic exceeding 9 h: inserts an overnight, arrivals jump to next 06:00.
- `schedule_route` rigid exceeding 9 h: returns None.
- `feasible_deadlines` multi-day: a delivery reachable only after an overnight passes if its deadline is ≥2 days out, fails if next-day.
- `route_cost` adds `NIGHT_OUT_COST_GBP` per overnight for artics, zero for rigids.
- Rigid feasibility forces an over-long order onto an artic in a mixed-fleet `cheapest_insertion`.

Regression: all 41 existing tests must still pass (single-shift schedule must reproduce current arrival/deadline behaviour exactly).

## Out of scope

- Full EU/UK driver-hours rules (breaks, weekly rest, 10 h extensions) — simplified to a daily driving cap.
- Per-stop service/handling time — schedule is drive-time only.
- Real road distances/speeds — still Haversine at 50 km/h.
- Inter-depot repositioning of trucks (data shows it's negligible).
