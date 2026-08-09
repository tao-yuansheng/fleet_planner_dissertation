# Single-Day Route Validation — Design

- **Date:** 2026-06-29
- **Status:** Approved (pending spec review)
- **Topic:** Extend `viz_app` to validate one operating day's plan against the actual fleet operation and against the pre-ALNS seed.

## Problem

The planner produces routes, but we have no in-tool way to ask "is this day's plan *credible*?" Two reference points matter:
1. **Reality** — what the fleet actually drove that day (telematics).
2. **The pre-optimization seed** — what the greedy insertion produced before ALNS, so we can see what optimization bought.

Today the viz shows only the planned routes. We want a single-day scorecard overlaying both references.

## Scope

**In:**
- Per-vehicle and fleet-total **planned vs actual km**.
- **Vehicle-days** planned vs actual (did we open the same trucks?).
- **Seed vs ALNS delta**: km, generalized cost, number of moves.

**Out (deferred):** per-type (rigid/artic) km split, telematics trace overlay, territory (Jaccard) / timing overlap, coverage-vs-verified-served. These are follow-ups, not in this build.

## Decisions

- **Actual-km basis:** *total vehicle-day km* from telematics (what the vehicle actually drove all day, regardless of which orders), matching the old `export_plan_replay` `actual_km`.
- **Delivery:** extend the existing `viz_app` (in-map scorecard), not a standalone report.
- **Run unit:** a *fresh single-day planner run* (`--start = --end = the day`) so the seed/ALNS and vehicle-day numbers are cleanly that-day. Accepts the caveat that single-day runs form no multi-day tours.
- **Output location:** `freight_planner/out/validation/…` — a new folder **parallel to `forward_structural/`** under `out/`. Concretely the run lands at
  `freight_planner/out/validation/forward_structural/planning_window/2026-01-15_to_2026-01-15/{plan,reports}`
  (the inner `forward_structural/planning_window` nesting is `run_alns`'s fixed path convention; the top-level `validation` is the sibling folder requested).

## Design

Three small, well-bounded pieces plus tests.

### 1. `freight_planner/vehicle_actuals.py` (new — the actuals source)

- `actual_km_by_vehicle(day: date) -> dict[str, float]` — loads telematics pings via `operational_analysis.fleet_replay_data.load_day(day)`, groups by `AssetName`, sums haversine between consecutive (time-sorted) pings. This is the established method (`export_plan_replay.py:2053`).
- `actual_vehicle_days(day: date) -> set[str]` — `AssetName`s that actually moved (> 1 km) that day.
- **Depends on:** `fleet_replay_data` + haversine only. **Identity:** plan `vehicle_id` == telematics `AssetName` (direct match).

### 2. `run_alns` persists the seed→ALNS delta (tiny change)

- `run_alns` already computes `km_before/after`, `cost_before/after` (added with the per-type cost work), and `accepted_moves`.
- Write `plan/validation_metrics.json`:
  `{ "seed_km", "alns_km", "seed_cost", "alns_cost", "moves", "planned_vehicle_days" }`.
- No new computation — just persist what's already in the `RouteSeedImprovement` object. `planned_vehicle_days` = distinct `(vehicle_id, day)` in the final plan.

### 3. `viz_app` validation panel (the main change)

- `build_plan_data(..., with_actuals: bool = False)` (CLI `--validate`):
  - per-vehicle **planned km** = sum of `leg_km` from `route_stops` (already available);
  - per-vehicle **actual km** = `vehicle_actuals.actual_km_by_vehicle(date)`;
  - reads `plan/validation_metrics.json` for the optimizer delta.
  - Produces a `validation` dict:
    - per-trip: `{planned_km, actual_km, delta}`
    - fleet: `{planned_km, actual_km, planned_veh_days, actual_veh_days}`
    - optimizer: `{seed_km, alns_km, seed_cost, alns_cost, moves}`
- **Sidebar:** each trip gains an **actual km** line next to planned.
- **New scorecard panel** (top of sidebar): fleet planned-vs-actual km (abs + %), vehicle-days planned-vs-actual, seed→ALNS (km / cost / moves).
- `render_html` adds the scorecard markup + the per-trip actual line.

### Data flow

1. `run_alns --start 2026-01-15 --end 2026-01-15 --out-dir freight_planner/out/validation`
   → `…/plan/` + `…/plan/validation_metrics.json`.
2. `viz_app --plan-dir …/plan --date 2026-01-15 --validate`
   → HTML with the scorecard + per-trip actuals.

## Testing (TDD)

- `vehicle_actuals`: haversine-sum over a small synthetic ping day returns the expected per-vehicle km; `actual_vehicle_days` applies the >1 km move threshold.
- `viz_app`: `build_plan_data(with_actuals=True)` attaches a correct `validation` dict (fixture plan + monkeypatched `actual_km_by_vehicle`); per-vehicle planned/actual joined correctly; reads `validation_metrics.json`.
- `run_alns`: writes `validation_metrics.json` with the seed/alns/cost/moves fields.

## Caveats surfaced in the UI (not hidden)

- Actual = haversine-of-GPS-pings; planned = OSRM road km — comparable for dense pings, but a slight method gap.
- A single-day run forms no multi-day tours, so far multi-day orders may sit unassigned that day (expected for daily-route validation).
- Planned vehicles with no telematics that day show actual = "no telematics," not 0-driven.
