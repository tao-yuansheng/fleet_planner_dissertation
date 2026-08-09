# Cambridge Dispatcher v2.0 Design

**Date:** 2026-06-01
**Status:** Approved (user-selected recommended options for all 3 brainstorming questions)

## Goal

Make the Cambridge dispatcher honour two realities it currently ignores:

1. **Time-of-day road speed** — peak vs off-peak vs night changes how long the same OSRM leg takes.
2. **Cumulative driver-hours across events** — a multi-trip vehicle can today silently exceed the 13 h UK on-duty cap because the v1.9 rule only checks per-event, and the same physical vehicle appears in the output as `<reg>` + `<reg>_E2` (separate routes).

These are scoped as **v2.0** because together they are the first set of changes where the solver model itself (not just objective / output) changes shape.

## Non-goals

- HGV-specific OSRM profile (still car-profile + truck factor).
- Refuelling stops.
- Hub-and-spoke groupage routing.
- Per-vehicle service-area constraints.

---

## Architecture

### Part A — Time-of-day speed

- New per-hour multiplier vector `tod_multiplier[0..23]`, normalised so the daily mean = 1.0. Calibrated from Supatrak Jan–Feb 2026 HGV pings.
- Multiplier is **relative to the existing 1.24× baseline** that is already baked into the OSRM cache. No cache rebuild. A 1.0 multiplier at 14:00 means "exactly the cached baseline duration"; 1.15 at 08:00 means "15% slower than baseline".
- `Router.duration_h` gains an optional `depart_time: datetime | None = None` argument. `HaversineRouter` ignores it; `OSRMRouter` multiplies cached hours by `tod_multiplier[depart_time.hour]` when supplied.
- `vrptw_engine._walk_schedule` already has the running clock `t` per leg — passes it as `depart_time` for both inter-stop legs and the depot-return leg.
- The multiplier vector ships as `data/Output/cambridge/tod_multiplier.json` (calibrated artefact, like `vehicle_profiles_derived.json`).

### Part B — Cumulative on-duty cap + output collapse

- **Bookkeeping fix in `cambridge/dispatcher.py:run_day_multi_trip`.**
  Today the post-event budget update at line 594-600 decrements `shift_remaining_h[vid]` by a fixed `DEFAULT_TRIP_DURATION_H + DEPOT_DWELL_MIN`. Replace with the route's actual `on_duty_minutes / 60` plus depot-dwell.
- **Cumulative cap propagation.** In `build_rigid_for_event` (mode = 'forward' for now; backtest still uses telematics start/end), the next event's `shift_end` is clipped so that `(shift_end - shift_start) ≤ remaining_budget` **and** the total cumulative on-duty across events for this vehicle ≤ `MAX_ON_DUTY_HOURS = 13`. This means a vehicle that did 11 h on Event A can only do 2 h on Event B before being dropped from `shift_remaining_h`.
- **Output collapse.** After `run_day_multi_trip` finishes, a new helper merges routes that share a base `vehicle_id` (strip `_E\d+` suffix):
  - Concatenate `stops` in time order (arrival_iso).
  - Sum `total_distance_km`, `activation_gbp`, `fuel_gbp`, `driver_gbp`, `estimated_cost_gbp`, `lateness_minutes`, `driving_minutes`, `on_duty_minutes`.
  - Single `shift_start_iso` (min) / `return_time_iso` (max).
  - Activation cost is summed (so two events on the same vehicle pay the £500 twice for now — matches the cost model).
- **Vehicles-used metric** counts unique base ids.

### Where the new code lives

| File | Change |
|---|---|
| `investigations/derive_tod_multiplier.py` | New. Reads Supatrak Jan–Feb 2026, computes hourly mean GPSSpeed for moving HGV pings (Ignition=on, GPSSpeed > 5), normalises around 1.0, writes `data/Output/cambridge/tod_multiplier.json`. |
| `simulation/routing.py` | Add optional `depart_time` to `Router` protocol, `HaversineRouter.duration_h` (ignored), `OSRMRouter.duration_h` (apply multiplier). Add `load_tod_multiplier(path)` + `set_tod_multiplier(vec)` + `get_tod_multiplier()` module functions. |
| `simulation/vrptw_engine.py` | `_walk_schedule` passes `t` as `depart_time` for each leg. `try_insert` does not need to change (it doesn't time-walk; insertion uses `route_cost(test_route)` which calls `_walk_schedule`). |
| `cambridge/dispatcher.py` | Replace fixed-budget decrement with `route.on_duty_minutes/60 + DEPOT_DWELL_MIN/60`. In `build_rigid_for_event`, accept an optional `remaining_budget_h` and `prior_on_duty_h`; clip `shift_end`. Load TOD multiplier on dispatcher init if file present. |
| `cambridge/backtest.py` | After `run_day_multi_trip`, run output collapse helper. JSON dump keys by base vid. Report `vehicles_used` reflects collapsed count. |
| `tests/test_routing.py` | New tests: TOD multiplier applies, ignored by Haversine. |
| `tests/test_vrptw_engine.py` | TOD multiplier flows through `_walk_schedule` (mock router). |
| `tests/cambridge/test_dispatcher.py` | Cumulative cap: Event B clipped when A consumed budget. Collapse: two events on same vid produce one merged route. |

---

## Calibration details (TOD multiplier)

```python
# Pseudocode for derive_tod_multiplier.py
df = read_supatrak(['20260101_to_20260131', '20260201_to_20260228'])
df = df[(df['Ignition'].str.lower() == 'on') & (df['GPSSpeed'] > 5)]
# Restrict to our-fleet rigids (use CB22_RIGIDS list) so we don't pull
# in trips by vehicles that aren't ours.
df = df[df['AssetName'].isin(CB22_RIGIDS + CB22_TRACTORS)]
df['hour'] = pd.to_datetime(df['LocalTime']).dt.hour
hourly_mean = df.groupby('hour')['GPSSpeed'].mean()
overall_mean = hourly_mean.mean()
# Multiplier is duration scaling: slower hour -> bigger multiplier
tod_multiplier = (overall_mean / hourly_mean).round(3)
# Write 24-length list indexed by hour
json.dump(tod_multiplier.tolist(), open('data/Output/cambridge/tod_multiplier.json', 'w'))
```

Sanity checks the calibration script must print:
- Hours with < 100 pings: warn (low-confidence bucket → fall back to 1.0).
- Overall mean GPSSpeed: should be in 30-50 km/h range.
- Range of multiplier: expect peak ≈ 1.10-1.20, off-peak ≈ 0.90-1.05.

---

## Output collapse rules (worked example)

Before (v1.9):
```json
"M88GNW":    {"stops": [...8 stops...], "on_duty_minutes": 600, "driver_gbp": 200, "total_distance_km": 150}
"M88GNW_E2": {"stops": [...5 stops...], "on_duty_minutes": 180, "driver_gbp":  60, "total_distance_km":  60}
```

After (v2.0):
```json
"M88GNW": {
  "stops": [...13 stops, ordered by arrival_iso...],
  "on_duty_minutes": 780,
  "driver_gbp": 260,
  "total_distance_km": 210,
  "events_combined": ["A", "B"]   // optional provenance field
}
```

A new `events_combined` array is added to surviving merged records for traceability — does not change any other field semantics.

---

## Risk / open questions

- **TOD multiplier is fleet-wide, not per-asset-type.** Lorries and 7.5t may behave differently in city peaks. Defer per-asset-type TOD until a v2.1.
- **Collapse + activation cost double-counting.** If a vehicle does 2 events it pays £500 + £500. Real-world activation is paid once. The cleaner fix is to also collapse activation at output time (sum → max). I'll leave the cost-side decision for the implementer to call out in a self-review note.
- **Backtest mode shift-end already comes from telematics, not the profile.** The cumulative cap clip therefore only applies in 'forward' mode where we set the budget. In 'backtest' mode the cap is informational — we don't override what telematics says happened. Documented as a v2.0 caveat.

---

## Acceptance criteria

1. `derive_tod_multiplier.py` produces a `tod_multiplier.json` with 24 entries, geometric mean ≈ 1.0.
2. `OSRMRouter.duration_h(... depart_time=t)` returns `cached_h * tod_multiplier[t.hour]`; returns `cached_h` when `depart_time=None`.
3. `HaversineRouter.duration_h(... depart_time=t)` ignores the argument.
4. All existing 188 tests pass; new tests added per File table above.
5. Re-run Jan-7 with v2.0: vehicles-used metric reports collapsed count (≤ physical fleet 11 for non-overflow days, equal-or-less than v1.9's count on Jan-7 specifically).
6. Cumulative on-duty per vehicle in `vehicle_plan_2026-01-07.json` ≤ 13 h × 60 = 780 min.
7. `aggregate_2026-01-07_2026-01-11.json` totals are unchanged within rounding tolerance (collapse preserves sums).

---

## Out of scope (for follow-up)

- Per-asset-type TOD multiplier.
- TOD on Haversine fallback (currently ignored — fallback already 1.0 baseline so a missing pair will be slightly fast; documented).
- Treating the £500 activation as once-per-physical-vehicle-per-day (cost model change, v2.1).
