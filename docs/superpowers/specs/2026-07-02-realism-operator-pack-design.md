# Realism + Operator Pack — Design

Date: 2026-07-02. Stakeholder-approved scope, in build order:
(1) statutory driver breaks, (2) stop-wait slack cap, (3) consolidated
whole-plan runsheets, (4) viz vehicle-type color view.

Stakeholder decisions (AskUserQuestion, 2026-07-02): slack cap = **stop-wait
cap** in daily routes; breaks = **EU 561/2006 core rule, HGVs only** (vans
exempt); runsheets = **printable HTML pack**, one artifact for the whole plan
window.

## Part 1 — Statutory driver breaks (EU 561/2006 core rule)

**Rule modeled:** after 270 min of cumulative driving, a 45-min break is
required before driving continues. Splittable 15+30 breaks, weekly limits and
reduced rests are out of scope (full tacho model deferred).

**Where:** both evaluators —
- `freight_planner/routing_adapter.py::evaluate_route` (daily routes), and
- `freight_planner/tours.py::evaluate_tour` (multi-day tours).

**Mechanics (identical in both):**
- Keep a `drive_since_break` accumulator alongside the clock.
- When a leg's drive minutes would cross the 270-min threshold (possibly more
  than once — long-haul legs alone can exceed 4.5 h), insert the required
  number of 45-min breaks into elapsed time: conceptually the driver stops at
  services mid-leg. Arithmetic:
  `n = floor((drive_since_break + dm) / 270)`; `elapsed += dm + n * 45`;
  `drive_since_break = (drive_since_break + dm) % 270`.
- Break minutes count toward the shift-end check (daily) and toward
  `TOUR_DAY_ELAPSED_CAP_MIN` (13 h duty) in tours.
- Service, waiting and reload time do NOT reset the accumulator (EU: "other
  work"). Only the inserted break resets it. In tours, a day boundary (daily
  rest) resets it to 0. In `evaluate_day`, the accumulator carries across
  trips within the vehicle-day (the 30-min reload is < 45 min, so it does not
  qualify as a break).
- **Van exemption:** `vehicle_type == "van"` skips break insertion entirely
  (≤3.5 t vehicles are outside tacho rules). Rigids and tractors get breaks.

**Surfacing:** `StopTiming` and `TourStop` gain
`break_minutes_before: float = 0.0` — the break time inserted on the way to
this stop. Runsheets (Part 3) render a break row before such stops. Emitted
`planned_arrive`/`planned_depart` shift automatically because the clock
includes breaks.

**Knobs** (cambridge/config.py, next to the existing tour knobs):
`DRIVE_BREAK_AFTER_MIN: float = 270.0`, `DRIVE_BREAK_MIN: float = 45.0`.

**Expected effect:** routes/tours that were only feasible wall-to-wall now
split, reroute, or reject honestly. Coverage may dip; report the delta, do not
iterate to hide it.

## Part 2 — Stop-wait slack cap (daily routes)

**Problem:** `evaluate_route` allows unbounded waiting at a stop for its
`earliest_start` — a driver can idle hours curbside and the plan calls it
feasible. (The tour-side equivalent, dwell, was already deleted.)

**Behavior:**
- **First stop of a trip:** waiting is converted into a later depot departure
  — the trip's `route_start` shifts so the vehicle arrives just-in-time
  (wait becomes 0; no idle anywhere). Shift-end is still enforced against the
  shifted clock. This mirrors real dispatch ("leave at 08:15, not 06:00").
- **Subsequent stops:** `wait > MAX_STOP_WAIT_MIN` makes the evaluation
  infeasible with new failure reason `EXCESS_WAIT`.
- `EXCESS_WAIT` is added to `_REPAIRABLE_REASONS` in `freight_planner/alns.py`
  so rejected jobs are retried on better routes during the search.

**Knob:** `MAX_STOP_WAIT_MIN: float = 90.0` (cambridge/config.py).

**Interaction with Part 1:** breaks are inserted by driving time, waits by
window arithmetic; both extend the clock. Order of application within a leg:
drive (+breaks) → arrive → wait (capped) → service.

## Part 3 — Consolidated runsheets (printable HTML pack)

**New module** `freight_planner/runsheets.py` with a CLI
(`python -m freight_planner.runsheets --plan-dir <run>/plan --out <run>/reports/runsheets.html`)
and a hook in `run_alns`'s write-outputs stage so every run emits
`reports/runsheets.html` automatically.

**Input:** `plan/route_stops.csv` only (keeps the module decoupled from
planner internals). `manifest.build_route_stops` gains:
- a `vehicle_type` column (from the vehicle frame; needed here and by Part 4);
- `break_minutes_before` per stop (from Part 1);
- **bug fix:** a multi-day tour's `depot_return` row currently carries the
  tour start date — it must carry the actual final-day date (start + days-1).

**Output:** one self-contained HTML for the whole plan window:
- Header: plan window, plan id, fleet totals (vehicles used, km, stops).
- Per-vehicle sections ordered by depot then vehicle id: header line
  (vehicle id, type, home depot, active days, total km), then one table per
  service_date with ordered rows: depot start, stops (arrive/depart, stop
  type, order id, postcode/node, pallets/kg after, leg km), break rows
  ("45-min statutory break"), trip boundaries (reload), depot return.
- Print CSS: `page-break-before` per vehicle section so a browser print gives
  per-driver sheets; dark-on-white print palette; screen view matches the
  trip_app aesthetic.

**Not in scope:** exception queue, driver assignment, PDF generation.

## Part 4 — Viz vehicle-type color view

`freight_planner/viz_app.py`:
- Trip payload gains `vehicle_type` (now available in route_stops.csv).
- Sidebar gets a color-mode toggle: **Vehicle** (current per-vehicle palette)
  | **Type** (fixed palette: tractor `#e74c3c`, rigid `#4a9eff`, van
  `#2ecc71`; anything else grey).
- Toggling recolors trip polylines and stop markers in place and shows a
  3-chip legend in Type mode. No other behavior changes.

## Build order & validation

Build strictly in order 1 → 2 → 3 → 4 (each lands with green tests before the
next starts). TDD throughout:

- Part 1: route with >4.5 h cumulative driving gains exactly one 45-min break
  before the crossing leg (arrive shifts by 45); a single long leg needing two
  breaks gets 90; van route unchanged; tour day accumulator resets at day
  boundary; break pushes a tight duty day over `TOUR_DAY_ELAPSED_CAP_MIN` →
  infeasible/extra day; `break_minutes_before` populated.
- Part 2: first-stop wait becomes a later route start (route_start shifted,
  wait 0); mid-route wait over 90 min → `EXCESS_WAIT`; under cap → feasible
  with wait counted; `EXCESS_WAIT` rejects are repairable in ALNS.
- Part 3: runsheet HTML contains per-vehicle sections, break rows, reload
  boundaries; tour depot_return row carries final-day date; vehicle_type
  column present in route_stops.csv.
- Part 4: trip payload carries vehicle_type; DATA blob includes the type
  palette mapping (JS behavior verified by payload/textual checks, as with
  the sidebar fix).

Validation: one `run_alns` per week (12-17 and 19-24 windows, 90 s budget)
into `freight_planner/out`, `FP_ALNS_CONSERVE=1`; regenerate trip_app viz
only; report KPI deltas (coverage/km/vehicle-days) against the current
baselines (wk1 99.7% / 89.6k km; wk2 99.8% / 99.1k km) as they land — no
tuning loops (standing stakeholder instruction).

## Constraints

- **No `git commit`** (standing stakeholder instruction) — files only.
- Viz regeneration is trip_app (`viz_app.py`) only — never the folium
  `viz_map`.
- Pipeline outputs go to `freight_planner/out`.
