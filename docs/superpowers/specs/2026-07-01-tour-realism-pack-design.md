# Tour Realism Pack — Design

**Date:** 2026-07-01
**Status:** SHIPPED 2026-07-02 (validated: 0 temporal/ledger violations both weeks; tour vehicle-days
−21/−18; wk2 coverage +5 orders). One spec claim was WRONG and was fixed during validation: "tour start =
earliest due is availability-safe" does NOT hold for batches mixing prestaged and pickup-fed freight — the
old dwell was accidentally enforcing availability. Shipped with an additional **freight-readiness gate**
(`build_tours(ready_by_job=...)`: max(ready) ≤ min(due) per batch; fed jobs ready the day after their
predecessor pickup) plus a start-day safety net (`max(min_due, max_ready)`), and a latent
`manifest.build_route_stops` fix (per-stop `service_date` was flattened to the route's first record).
**Author:** brainstormed with stakeholder (from the operator-perspective review)

## Problem

The multi-day tour model is materially softer than the daily model, in four ways an
operator would feel on the road:

1. **Dwell days are free.** `evaluate_tour` idles the vehicle in-region whenever a
   stop's due day is later than the day the sweep reaches it (`due_offsets` dwell),
   and `tour_plan.py` reserves those idle vehicle-days at zero objective cost.
   Stakeholder ruling: **dwell is wasted time and resource; the due date is a
   deadline, not an appointment — early delivery is OK.**
2. **Tour stops have zero service time.** `evaluate_tour` counts only driving
   minutes; a 12-drop tour day is under-budgeted by ~4 hours, silently turning
   "2-day" tours into real 3-day ones.
3. **Tour distance is straight-line.** `_leg_km` and the return leg use raw
   `haversine_km` while daily routes use OSRM road distance — the headline
   planned-km KPI mixes real and straight-line km, and tour day-splitting is
   correspondingly generous (~15–25% optimistic).
4. **The emitted plan hides all of it.** The tour emit writes every stop with the
   tour's start date and a placeholder `12:00:00` arrive/depart, so per-day
   schedules (and any dwell) are invisible in `route_stops.csv` — no runsheet, no
   measurability.

## Decision record

- **Due-date semantics (stakeholder):** early delivery OK. A stop may be served any
  day up to and including its due day; serving after it is infeasible (`LATE`).
- Tour start day stays **"earliest due date in the batch"** (unchanged). That rule is
  what makes today's plans ledger-clean (all delivery freight is at the depot by
  then); starting earlier would need availability-aware logic — out of scope.
- Approach chosen: **surgical realism fixes inside the existing evaluator/emit**
  (Approach A). Rejected: dwell-only minimal fix (leaves the clock dishonest);
  full rebuild of tours as chained `evaluate_route` days (rework >> gain, YAGNI).

## Changes

All in `freight_planner/tours.py`, `freight_planner/tour_plan.py`, and
`cambridge/config.py` (tour knobs are already consolidated there).

### 1. Dwell → lateness constraint (`evaluate_tour`)

Delete the dwell block:

```python
# REMOVED
due_day = int(due_offsets.get(job.job_id, 0)) if due_offsets else 0
if due_day > day_index:
    day_index = due_day
    day_drive = 0.0
```

Replace with a hard lateness check (only for jobs with a known due date):

```python
if due_offsets and job.job_id in due_offsets and day_index > int(due_offsets[job.job_id]):
    return _infeasible_tour("LATE")
```

`due_offsets` (job_id → days from the batch's earliest due) keeps its data shape;
its *meaning* flips from "dwell until" to "late after". Stop ordering
(due-date-first, then nearest-neighbour) is unchanged — it naturally serves the
earliest-due stop on day 0. `build_tours`' greedy accretion re-evaluates every
candidate add, so a job that would make a sweep late simply doesn't join that tour
and can form/join another. Update the module docstring (it documents the dwell).

### 2. Per-stop service time + two-cap day splitting (`evaluate_tour`)

- Each stop costs `service_minutes(job.pallets, vehicle.vehicle_type)` (the same
  load-based rigid/tractor calibration as daily routes, from
  `freight_planner.route_costs`), **doubled** for two-point kinds
  (`DIRECT_CUSTOMER_MOVE`, `HUB_DROP`) — matching `evaluate_route`. `DEPOT_LOAD`
  stops get the same treatment on their pallets field (loading isn't free).
- Day splitting becomes two-cap: a new day starts when the *next* stop would push
  **either** `day_drive > _DAY_DRIVE_CAP_MIN` (existing 10h driving cap) **or**
  `day_elapsed > TOUR_DAY_ELAPSED_CAP_MIN` (new; drive + service), provided the day
  is non-empty. The return leg applies the same two-cap check.
- New config knob beside the other tour params in `cambridge/config.py`:

```python
TOUR_DAY_ELAPSED_CAP_MIN: float = 13.0 * 60.0  # tractor duty day 07:00-20:00
TOUR_DAY_START_HOUR: int = 7                   # tour day clock starts (emit timing)
```

### 3. Honest distance (`_leg_km` + return leg)

`_leg_km` and the depot-return leg switch from `haversine_km` to
`road_km` (from `freight_planner.route_costs`): OSRM road distance when the router
is installed (all real runs), deterministic haversine × `ROAD_DISTANCE_FACTOR`
offline/tests. `MULTIDAY_AVG_SPEED_KMH = 80` is unchanged — it was always meant as
a motorway average over real km; it was previously papering over straight-line
optimism.

`is_tour_only` is untouched (it deliberately uses the *local* road model to mirror
what the daily seed can't do).

### 4. Runsheet-grade emit (`TourStop` + `tour_plan.py`)

- `TourStop` gains `arrive: str = ""` and `depart: str = ""` — **appended after the
  existing fields** so positional constructors keep working (same lesson as
  `TourEvaluation.peak_*`).
- `evaluate_tour` computes real clock times: each tour day starts at
  `TOUR_DAY_START_HOUR:00`; a stop's `arrive` = day start + elapsed (drive+service)
  within the day so far + inbound drive; `depart` = arrive + its service block.
  Times are day-relative during evaluation (day_index known per stop); the emit
  anchors them to calendar dates.
- The tour emit in `tour_plan.py` (currently
  `planned_arrive=f"{day_iso} 12:00:00"` with `day_iso` = tour start for every
  stop) writes instead, per stop: `service_date = tour_start + stop.day` and the
  real `planned_arrive`/`planned_depart` clock times. `vehicle_routes.csv` tour
  rows are unchanged (one row per tour, start date).

## Interfaces

- `TourEvaluation` shape unchanged (`days` may now be larger for the same jobs —
  honest budgeting).
- `evaluate_tour(vehicle, ordered_jobs, due_offsets)` signature unchanged.
- `TourStop` gains two appended optional fields; all existing constructors and
  consumers keep working.
- Downstream consumers of `route_stops.csv` (viz_app, validation) get strictly
  richer data: per-stop dates/times where placeholders were.

## Expected validation movements (full runs, both weeks)

- **Tour km up** (real road km replaces straight line) — headline planned km rises;
  this is honesty, not regression.
- **Some tours gain a day** (service time) → more reserved tour vehicle-days.
- **Dwell days gone** (mechanism deleted); per-day emit makes this verifiable.
- **Guardrails:** coverage holds ≥ 99.4% (wk1) / 99.3% (wk2); `NO_FEASIBLE_TOUR`
  stays 0; 0 temporal / 0 ledger violations; phantom deliveries 0. If honesty
  pushes a tour past `MAX_TOUR_DAYS_HARD` (4), it will surface as a visible
  rejection to decide on — not be hidden.

## Testing

Unit (TDD, `tests/freight_planner/test_tours.py` + `test_tour_plan.py`):
- a tour whose ordering reaches a stop after its due offset → infeasible `LATE`;
  same tour with early-OK ordering → feasible (no dwell days in `days`).
- a multi-drop day that fits the driving cap but exceeds the elapsed cap with
  service time → splits into two days.
- `_leg_km` honours an installed router (`set_router` fake) and falls back to
  haversine × factor without one.
- `TourStop.arrive/depart` populated and monotone within a day; emit writes
  per-stop `service_date = start + day` (regression: no flat start-date stops).
- Existing Scotland/peak-capacity regression tests stay green.

Validation: re-run both weeks (`run_alns`, 90s budget, `freight_planner/out`),
compare KPI summaries against the baseline above; regenerate trip_app viz only.

## Scope / out of scope

In scope: `tours.py` (evaluator, distance, TourStop), `tour_plan.py` (emit),
`cambridge/config.py` (two knobs), tests, QUEST_LOG entry.

Out of scope (later roadmap items): statutory breaks in the daily evaluator, slack
cap, vehicle catchment (B15), full runsheet/exception-queue outputs, driver layer,
availability-aware earlier tour departures, rolling replan (owned by another
contributor).

## Constraints

- **No `git commit` this session** (standing stakeholder instruction) — write files only.
- Viz regeneration is `trip_app` (`viz_app.py`) only.
