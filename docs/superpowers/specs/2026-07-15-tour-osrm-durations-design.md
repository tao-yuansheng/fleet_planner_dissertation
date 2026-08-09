# Tour OSRM Durations — Design

**Date:** 2026-07-15
**Status:** Approved (design); implementation pending
**Scope:** Piece 1 of 2. (Piece 2 — decoupling tour-eligibility from tour-route membership so corridors can board arbitrary non-tour legs, incl. via dynamic micro-insertion — is a separate later spec.)

## Problem

Tour classification and tour scheduling are timed on speed models that the rest of
the pipeline abandoned when OSRM road-type durations shipped (spec
`2026-07-09-osrm-durations-v1_1-design.md`). Three inconsistent speeds coexist today:

- **`is_tour_only` gate** ([tours.py:129-134](../../../freight_planner/tours.py)) times legs with
  `drive_minutes(road_km(...))` — a flat **50 km/h**. It never consults OSRM.
- **`evaluate_tour` executor** ([tours.py:324,403](../../../freight_planner/tours.py)) times legs with
  `longhaul_drive_minutes(leg_km)` — a flat **80 km/h** straight-line.
- **The daily router** already uses `road_minutes` → **OSRM per-road-type durations**
  (motorway legs ~80–90 km/h), the realistic model.

Consequence: the gate declares a tour when a same-day round trip can't fit the driving
cap *at 50 km/h*, but the trip is actually driven far faster. The effective boundary sits
at **~250 road-km one-way** when, timed at real motorway speed, it should sit near
**~425 km**. Orders in the 250–425 km band are needlessly pulled into dedicated tours and
then run as same-day out-and-backs (observed: the 276 km Wrexham and 280 km Macclesfield
runs on 2026-01-14). The gate's own docstring claims it "uses the local road model (the
daily seed's reachability)" — a contract that broke when the daily seed moved to OSRM.

## Goal

Time both the tour gate and the tour executor with the same OSRM per-road-type durations
the daily router uses, so "cannot be served there-and-back in a day" means what it says.
Behavior changes **only when OSRM is active** (production). Offline/tests are byte-identical.

## Design

### Change 1 — `is_tour_only` uses OSRM durations

Replace each `drive_minutes(road_km(a, b))` in `is_tour_only` with a gated segment timer,
applied per segment (the `depot → dest → depot` round trip; and the
`depot → origin → dest → depot` triangle for two-point directs/hub-drops):

```python
def _gate_minutes(a_lat, a_lon, b_lat, b_lon, vehicle_type):
    if config.TOUR_OSRM_DURATIONS:
        return road_minutes(a_lat, a_lon, b_lat, b_lon, vehicle_type)
    return drive_minutes(road_km(a_lat, a_lon, b_lat, b_lon))
```

`road_minutes` already falls back to the *identical* `drive_minutes(road_km(...))` when OSRM
is absent, so with the flag ON and no OSRM router the gate is **byte-identical (50 km/h)**;
only the OSRM path changes (boundary widens ~250→~425 km). With the flag OFF it is exactly
current behavior in all cases.

### Change 2 — `evaluate_tour` uses OSRM durations

`evaluate_tour` currently computes leg time as `longhaul_drive_minutes(leg_km)`. Introduce a
`_leg_minutes` helper that mirrors `_leg_km`'s structure (summing both segments for a
two-point direct) and picks the timer via a shared `_seg_minutes`:

```python
def _osrm_active():
    # same predicate road_minutes uses internally
    return (config.USE_OSRM_DURATIONS and _active_router is not None
            and hasattr(_active_router, "duration_h"))

def _seg_minutes(a_lat, a_lon, b_lat, b_lon, vehicle_type):
    if config.TOUR_OSRM_DURATIONS and _osrm_active():
        return road_minutes(a_lat, a_lon, b_lat, b_lon, vehicle_type)
    return longhaul_drive_minutes(road_km(a_lat, a_lon, b_lat, b_lon))   # 80 km/h fallback

def _leg_minutes(prev_lat, prev_lon, job, vehicle_type):
    if (job.leg_kind in _TWO_POINT_KINDS
            and job.origin_lat is not None and job.origin_lon is not None):
        return (_seg_minutes(prev_lat, prev_lon, job.origin_lat, job.origin_lon, vehicle_type)
                + _seg_minutes(job.origin_lat, job.origin_lon, job.lat, job.lon, vehicle_type))
    return _seg_minutes(prev_lat, prev_lon, job.lat, job.lon, vehicle_type)
```

`evaluate_tour` uses `_leg_minutes(prev_lat, prev_lon, job, vehicle.vehicle_type)` for `dm`
and `_seg_minutes(prev_lat, prev_lon, home_lat, home_lon, vehicle.vehicle_type)` for the
return `back_dm`. **Distance (`_leg_km`, km accounting) is unchanged** — only time changes.
When OSRM is inactive the executor keeps its current **80 km/h** fallback, so tour tests stay
byte-identical.

**Note the deliberate offline asymmetry:** with the flag ON but no OSRM, the gate falls back
to 50 km/h and the executor to 80 km/h — each preserving its *current* offline behavior for
test stability. With OSRM active (production) both use `road_minutes`, so they finally agree.

### What propagates automatically

`TourStop.arrive_minute`/`depart_minute` and the day-split decisions all derive from the leg
time. So changing `dm`/`back_dm` updates, from this one change: the tour boundary (fewer
tours), the day count (a 2-day tour that only needed 80 km/h straight-line may collapse to 1
day at real motorway speed), the emitted stop times, and utilization. No emission or KPI
code needs a separate edit.

### Flag & ablation

New `config.TOUR_OSRM_DURATIONS: bool = True`. `--no-tour-osrm-durations`
(BooleanOptionalAction, default None → config) on `run_rolling` and `run_alns`, wired through
the existing `_apply_vehicle_day_cost_flags`-style setter. The flag gates *only* the tour
timers, so validation can hold the daily router on OSRM constant while toggling the tour side.
Flag OFF = byte-identical to today.

### Coverage safety-net (existing)

Widening the gate moves 250–425 km orders into the daily pool. Two existing layers keep
coverage safe: (1) the daily ALNS serve-first objective seats most of them; (2) any
`NO_FEASIBLE_ROUTE` daily failure is already collected by the **stranded-backhaul repair**
([tour_plan.py:499-669](../../../freight_planner/tour_plan.py), `_STRAND_PICK` includes
`NO_FEASIBLE_ROUTE`), which re-forms a tour carry. So a loosened gate cannot silently strand a
far order. This is verified empirically on the week validation — no new safety-net code.

## Testing (TDD)

Unit (with a stub OSRM router exposing `duration_h`/`distance_km`):

- `is_tour_only` returns `True` for a ~300 km one-way order offline but `False` under the stub
  router (boundary moved out); flag OFF ⇒ `True` in both (current behavior).
- `evaluate_tour` times a single 400 km leg by the stub's duration, not `400/80*60`; flag OFF
  or no router ⇒ byte-identical to today.
- A ~550 km round-trip tour that splits into 2 days offline is 1 day under the stub router.
- Two-point direct: `_leg_minutes` sums both segment durations.

Integration:

- A far order the daily pool cannot seat still ends up served via the stranded-backhaul repair
  (coverage held).

## Validation

Week `2026-01-12 → 2026-01-18`, ON vs `--no-tour-osrm-durations`, daily router on OSRM in both.
Report: tour count, tour vehicle-days, total vehicle-days, planned km, **coverage (assigned
orders — must be identical)**, service ledger (ON_TIME/SLIPPED/UNSERVED). Expected: fewer
tours, tour vehicle-days down-or-flat, coverage identical, service neutral.

## Out of scope / caveats

- **Viz anchor.** The map synthesizes times for stop-less depot-out/return legs at
  `TOUR_ANCHOR_KMH = 80` (`viz_timeline_maplogic.cjs`). No KPI depends on it; aligning it to
  OSRM is a small viz follow-up, not part of this piece.
- **Mid-leg overnight.** `MULTIDAY_MIDLEG_OVERNIGHT` (default OFF) interpolates a split point
  assuming time ∝ km; under OSRM durations that is approximate. Deferred while the feature is
  off; note in the plan.
- **Piece 2** (corridors boarding non-tour legs; broadening the flag-off intraday tour-attach
  spine to opportunistic local/direct/pickup insertion) is a separate spec after this ships.

## Success criteria

- Flag OFF ⇒ byte-identical output.
- Flag ON + OSRM ⇒ tour boundary reflects OSRM durations; fewer tours; coverage identical on
  the validation week; service ledger neutral.
- Full unit + integration suite green.
