# Freight Planner Remaining Implementation Spec

This document is a handoff spec for completing the new `freight_planner`
pipeline. It assumes the current Phase 0 data spine already exists and should
be kept as the foundation.

## Current State

The planner currently builds forward-mode planning inputs from Qargo, verified
leg logic, postcode/geocode data, vehicle master data, and Cambridge rules.

Run from `BackEnd/logistics`:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -B -m freight_planner.build_phase0 --start 2026-01-05 --end 2026-01-10
```

Default mode is:

- `responsibility-mode = forward_structural`
- `date-basis = planning_window`

Backtest validation mode is explicit:

```powershell
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -B -m freight_planner.build_phase0 --start 2026-01-05 --end 2026-01-10 --responsibility-mode backtest_verified --date-basis planning_window --manifest fleet_replay_exports\plan_manifest_2026-01-05_to_2026-01-10.csv
```

Current test command:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -B -m pytest tests\freight_planner -q -p no:cacheprovider
```

Latest known result: `29 passed`.

Current outputs are written under:

```text
freight_planner/out/forward_structural/planning_window/
```

Important generated files:

- `demand_records.csv`
- `verified_responsibility.csv`
- `movement_legs.csv`
- `vehicle_states.csv`
- `candidate_jobs.csv`
- `vehicle_job_compatibility.csv`
- `job_options.csv`
- `freight_states_initial.csv`
- `ledger_violations_all_runnable.csv`
- `validation_report.md`

Recent smoke output for Jan 5-10:

```text
demand records:          2578
movement legs:           2814
vehicles:                  79
candidate jobs:          2649
runnable candidates:     2614
compatibility pairs:   206506
OK pairs:              203456
job options:             2649
cross-depot-only jobs:    117
ledger issues:              0
manual/ambiguous:           74
```

## Non-Negotiable Invariants

- Forward mode is the operational target. Historical telematics is for leg
  verification and backtest validation, not for choosing future vehicle
  assignments.
- Empty historical resource fields must not mean `NO_RESOURCES` in forward
  planning. They mean the order is structurally classified or manually
  ambiguous.
- No freight can be delivered before it physically exists at the pickup,
  depot, hub, or staging location.
- Crossdock delivery must require its pickup predecessor unless it is explicitly
  prestaged before the planning horizon.
- Palletline/Hazchem imports and exports must be represented as physical
  freight movements, not collapsed into a single misleading class.
- `master_max_tonnes` from Supatrak is gross vehicle metadata, not payload
  capacity. Do not use it as the load limit unless tare/payload conversion is
  added deliberately.
- Depot ownership is a preference and cost signal, not a hard wall. A vehicle
  from another depot can be considered if it is physically feasible.
- Ambiguous partial fleet orders must remain visible in accounting outputs but
  should not be auto-routed without a reliable leg definition.
- Pre-horizon legs can be visible for dependency accounting, but they are not
  runnable jobs.
- The old `LOCAL` / `TOUR` / `TRUNK` labels may be useful diagnostics, but they
  must not be the primary gating mechanism in the new planner.

## What Already Exists

### Demand Model

Implemented in `demand.py`.

Responsibilities:

- load Qargo order rows;
- classify responsibility in forward or backtest mode;
- preserve exclusions and manual rows as accounting-visible records;
- apply verified leg correction only when explicitly requested by mode.

Do not add historical assignment hindsight to `forward_structural`.

### Movement Legs

Implemented in `legs.py`.

Responsibilities:

- convert demand records into physical legs;
- split Palletline/Hazchem/customer work into actual pickup, delivery, trunk,
  and accounting legs;
- mark bad geocode, massive unsupported, and accounting-only rows.

The movement leg table is the canonical physical work model.

### Candidate Jobs

Implemented in `jobs.py`.

Responsibilities:

- convert dispatchable movement legs into planner jobs;
- expose hard blockers;
- expose dependency metadata:
  `PRODUCES_DEPOT_FREIGHT`, `REQUIRES_PRIOR_PICKUP`,
  `PRESTAGED_DELIVERY`, `PICKUP_TERMINAL`, `NONE_DIRECT`.

Candidate jobs are not assignments. They are work items available to the
planner.

### Vehicle States

Implemented in `vehicles.py`.

Responsibilities:

- create initial vehicle state records;
- keep home depot, current node, vehicle type, capacity, and shift metadata;
- separate observed/default payload capacity from Supatrak gross metadata.

These are starting states only. The remaining planner must mutate vehicle
state over time as routes are committed.

### Compatibility And Options

Implemented in `compatibility.py` and `options.py`.

Responsibilities:

- build a full vehicle-job compatibility matrix;
- flag capacity and simple reachability blockers;
- summarize best same-depot and cross-depot options per job.

This is not route feasibility. It is a screening layer. A job with an OK
compatibility pair may still fail once route sequencing, driver time, and
freight dependencies are enforced.

### Ledger Validation

Implemented in `ledger.py`.

Responsibilities:

- validate that selected jobs do not deliver freight without predecessor
  pickup;
- expose ledger violations with a stable schema.

This currently validates a selected job set. The remaining work is to build the
stateful execution ledger that applies selected jobs over time.

## Remaining Work: Implementation Order

### Milestone 1: Selected Plan Schema

Goal: introduce a first-class plan output, separate from input candidates.

Add:

- `plan_schema.py`
- `selected_plan.csv` output
- tests in `tests/freight_planner/test_plan_schema.py`

Minimum selected plan fields:

- `plan_id`
- `route_id`
- `vehicle_id`
- `vehicle_home_depot`
- `service_date`
- `sequence`
- `job_id`
- `leg_id`
- `order_id`
- `leg_kind`
- `origin_node`
- `destination_node`
- `planned_arrive`
- `planned_depart`
- `planned_km`
- `planned_drive_minutes`
- `load_pallets_after`
- `load_kg_after`
- `freight_state_before`
- `freight_state_after`
- `assignment_reason`

Acceptance criteria:

- selected plan rows can be generated from a small synthetic job set;
- duplicate assignment of the same `job_id` is rejected;
- a selected delivery without required pickup predecessor fails ledger
  validation.

### Milestone 2: Planning State Engine

Goal: create mutable vehicle and freight state over the horizon.

Add:

- `planner_state.py`
- `freight_ledger.py`
- tests in `tests/freight_planner/test_planner_state.py`

State must track:

- each vehicle's current node, location, date/time, remaining shift, assigned
  route, and current load;
- freight availability by node and date;
- selected jobs;
- completed jobs;
- rejected jobs with reason;
- depot inventory produced by pickups and consumed by deliveries.

Freight transitions:

- customer pickup creates freight at vehicle/depot/hub depending on route
  completion;
- depot delivery consumes freight from depot inventory;
- direct movement consumes at origin and produces at destination in one route;
- trunk pickup/drop must move freight through the hub/depot node, not through a
  fake accounting shortcut.

Acceptance criteria:

- crossdock delivery before pickup is rejected;
- crossdock delivery after pickup is accepted;
- prestaged delivery is accepted only when initial state says freight exists;
- inventory cannot go negative;
- vehicle load cannot exceed pallet or weight capacity.

### Milestone 3: First Feasible Seed Planner

Goal: create a simple but physical plan before optimization.

Add:

- `seed_planner.py`
- CLI flag in `build_phase0.py` or a new `run_seed.py`
- output `selected_plan_seed.csv`
- tests in `tests/freight_planner/test_seed_planner.py`

Initial heuristic:

1. Use only runnable candidate jobs.
2. Prioritize hard-deadline jobs, required pickups for future deliveries,
   direct customer moves, then flexible jobs.
3. Prefer same-depot vehicle options.
4. Allow cross-depot options when same-depot options do not exist or create a
   large miss penalty.
5. Apply one job at a time through `planner_state`.
6. Reject with explicit reason when no feasible vehicle route insertion exists.

This does not need to be globally optimal. It must be physically valid and
explain every miss.

Acceptance criteria:

- every selected row passes ledger validation;
- every unselected job has a reason;
- cross-depot-only jobs are attempted, not silently ignored;
- no route exceeds capacity, shift, or dependency constraints;
- output can be compared to old manifest coverage.

### Milestone 4: Route Sequencing Adapter

Goal: replace one-job-per-vehicle assignment with real multi-stop routing.

Add:

- `routing_adapter.py`
- `route_costs.py`
- tests in `tests/freight_planner/test_routing_adapter.py`

The adapter may start simple, but the interface should support the existing
VRPTW/ALNS solver later.

Required interface:

```python
evaluate_route(vehicle_state, ordered_jobs, freight_state) -> RouteEvaluation
try_insert_job(route, candidate_job, position_or_policy) -> RouteEvaluation
```

`RouteEvaluation` should include:

- feasible flag;
- failure reason;
- total km;
- total drive minutes;
- service minutes;
- wait minutes;
- route start/end time;
- load timeline;
- node timeline.

Acceptance criteria:

- route evaluator catches capacity violations after intermediate pickups;
- route evaluator catches impossible time windows;
- route evaluator computes nonzero travel between different geocoded nodes;
- route evaluator can accept both same-depot and cross-depot route starts.

### Milestone 5: Existing ALNS Integration

Goal: use the existing daily VRPTW/ALNS machinery as a routing improvement tool,
not as the top-level architecture.

Add:

- adapter around `simulation/vrptw_alns.py` or existing Cambridge dispatcher
  route builder;
- tests that use small synthetic routes and do not require full data files.

Rules:

- the planner remains responsible for freight dependencies and horizon state;
- ALNS can improve route ordering and vehicle allocation within a candidate
  subproblem;
- ALNS must not create a delivery of unavailable freight;
- ALNS must return rejected/unrouted jobs with reasons.

Acceptance criteria:

- seed plan and ALNS-improved plan use the same selected job schema;
- ALNS improvement cannot reduce served jobs unless explicitly accepted by a
  cost policy;
- route cost improvement is reported separately from coverage improvement.

### Milestone 6: Cross-Depot Resource Allocation

Goal: make idle vehicle use explicit and costed.

Add to planner state and route evaluator:

- vehicle repositioning cost;
- route end node and return-to-depot policy;
- optional temporary depot assignment for a route;
- overnight / sleep-out eligibility where relevant.

Policy:

- same-depot is preferred by lower cost;
- cross-depot is allowed when it prevents an otherwise avoidable miss;
- cross-depot route must include the real travel from current vehicle position;
- if vehicle does not return home, the next day starts from the actual end node
  unless a repositioning movement is planned.

Acceptance criteria:

- the 117 cross-depot-only jobs in Jan 5-10 are either assigned or rejected
  with physical reasons;
- no job is rejected solely because its home depot had no vehicle if another
  depot had a feasible vehicle;
- validation report shows cross-depot assignments and repositioning km.

### Milestone 7: Direct Versus Crossdock Decisions

Goal: let the planner choose whether flexible freight moves direct or via depot,
instead of copying historical direct/crossdock structure.

Add:

- option generation for alternate movement patterns;
- mutually exclusive option groups by `order_id`;
- selected option tracking in the plan schema.

For eligible full-fleet demand, generate:

- direct customer-to-customer option;
- pickup-to-depot plus depot-to-delivery option;
- possibly depot-transfer option if origin and destination are better served
  by different depots.

Rules:

- urgent same-day or tight-window jobs may only have direct options;
- crossdock delivery date must be within the order's service promise;
- choosing one option group cancels the other option group's jobs.

Acceptance criteria:

- planner never selects both direct and crossdock alternatives for the same
  demand;
- crossdock option requires inventory handoff;
- direct option can be chosen when it is cheaper and feasible;
- output reports selected mode and rejected alternatives.

### Milestone 8: Multiday And Tour Planning

Goal: replace old Phase 1 tour precommitment with true horizon resource
planning.

Add:

- multiday route support in `planner_state.py`;
- overnight vehicle state;
- route spans over multiple service dates;
- tests in `tests/freight_planner/test_multiday_planner.py`.

Rules:

- a vehicle unavailable on day N because it is on a multiday route must not be
  offered to day N's local work;
- a multiday route must be built from actual freight available at departure;
- route geography and elapsed time must constrain which stops can be packed
  together;
- a rejected multiday candidate must release the vehicle and jobs back to the
  general pool.

Acceptance criteria:

- Scotland-style far deliveries are not over-consolidated just because pallet
  count and date span fit;
- far overflow jobs are either assigned to a feasible multiday route or marked
  with a clear no-feasible-tour reason;
- no unrouted tour can block a vehicle from normal dispatch.

### Milestone 9: Manifest And KPI Output

Goal: produce operator-readable outputs from the new selected plan.

Add:

- `manifest.py`
- `kpi.py`
- output files:
  - `plan_manifest_new.csv`
  - `unassigned_jobs.csv`
  - `vehicle_routes.csv`
  - `depot_inventory_timeline.csv`
  - `kpi_summary.md`

Manifest must distinguish:

- customer pickup;
- customer delivery;
- direct customer move;
- inbound trunk;
- outbound trunk;
- accounting/manual row;
- rejected/manual/excluded row.

KPI accounting:

- raw order count;
- excluded count by reason;
- ambiguous/manual count;
- in-universe demand count;
- runnable candidate jobs;
- assigned candidate jobs;
- assigned orders;
- unassigned by physical reason;
- planned km;
- cross-depot km;
- vehicle-days used;
- same-day vs multiday work;
- phantom-delivery count, expected zero.

Acceptance criteria:

- old manifest comparison can be run, but new KPI is not defined by matching
  old plan labels;
- every raw demand row appears in some accounting output;
- assignment rate denominator is explicit and reproducible;
- no accounting-only row is counted as a routed job.

## Split Loads And Massive Orders

Current status: massive orders are blocked as `MASSIVE_UNSUPPORTED` when they
exceed single-vehicle practical limits.

Remaining design:

- add `split_loads.py`;
- create child jobs for an oversized order;
- require all child jobs to preserve the same commercial order identity;
- allow partial assignment only if the business rule accepts partial service;
- otherwise mark the whole order failed when not all children can be planned.

Initial rule suggestion:

- split by pallet count first;
- respect weight capacity too;
- do not split hazardous/specialist orders unless explicitly allowed;
- expose `split_group_id`, `split_index`, and `split_count`.

Acceptance criteria:

- a 34-pallet order can be represented as two or more vehicle jobs;
- the manifest still shows one commercial order with multiple planned vehicle
  legs;
- partial split completion is visible and not counted as full service unless
  policy allows it.

## Open Design Questions

These should be settled before heavy optimization work:

- What is the real forward-mode source of "our responsibility" for ambiguous
  partial fleet orders when telematics is unavailable?
- Are depot-to-depot transfers allowed as planned freight movements, planned
  vehicle repositioning, or both?
- How should dock capacity and depot storage limits be represented?
- Are trunk vehicles and trunk departures fixed schedules, flexible resources,
  or external services?
- What customer service-time model should be used? **Resolved:** fixed dwell per
  distinct visit by vehicle type (van/rigid 15 minutes; tractor 30 minutes),
  independent of pallet count; contiguous orders at one address share a dwell.
- What is the planning commitment horizon: same day fixed, next day mostly
  fixed, later days flexible?
- Which orders may be split across vehicles?
- Which vehicles may sleep out, and what cost/constraint applies?
- What is the acceptable runtime for a weekly plan?

## Suggested Validation Windows

Use historical windows only to validate physical realism and coverage. They are
not the source of future planning decisions.

Recommended smoke windows:

- Jan 5-10: mixed week, known unassigned and idle resource symptoms.
- Jan 13-16: previous crossdock and phantom-delivery investigations.
- Jan 14 single-day: Bedford depletion and long-haul pressure.

For each window, report:

- assignment denominator and exclusions;
- selected jobs;
- unassigned by reason;
- cross-depot-only jobs assigned/rejected;
- massive unsupported count;
- ledger violations;
- planned vehicle usage versus historical vehicle usage;
- examples for top three miss reasons.

## Warnings For The Next Agent

- Do not edit the old Cambridge pipeline unless the user explicitly asks. The
  new folder is intended to isolate the redesign.
- Do not treat `job_options.csv` as a plan. It only summarizes possible
  vehicle choices.
- Do not treat `vehicle_job_compatibility.csv` as route feasibility. It is a
  fast screen, not a route.
- Do not use historical Qargo `resource` fields in forward planning logic.
- Do not silently drop accounting/manual rows. They matter for assignment-rate
  explanation.
- On Windows, use `PYTHONDONTWRITEBYTECODE=1`, `python -B`, and pytest
  `-p no:cacheprovider` to avoid pycache/cache write problems.

## Definition Of Done

The new freight planner is ready for serious comparison when it can:

- load a planning window in forward mode;
- build demand, movement legs, vehicles, candidate jobs, compatibility, and
  options;
- choose a physically valid selected plan;
- maintain vehicle and freight state across days;
- prevent phantom deliveries;
- use cross-depot vehicles when physically sensible;
- represent unassigned work with specific reasons;
- output a manifest and KPI report with a clear assignment denominator;
- pass all `tests/freight_planner` tests;
- run Jan 5-10 and Jan 13-16 smoke windows without ledger violations.

## Milestone 10: Multi-Trip Per Day Dispatch (mostly built)

### Problem

The planner currently dispatches **one route per `(vehicle, day)`** 鈥?a single
`depot -> stops -> depot` trip (`route_seed.routes[(vid, day)]`). Deliveries are
all loaded at the depot at route start (`routing_adapter.evaluate_route`
`initial_load`), so a vehicle delivers **at most one capacity-load per day**.

The real fleet multi-trips heavily and the telematics already measured it, but
**no planner code reads it**:

- `median_trips_per_day` and `multi_trip_share` are computed in `vehicles.py`
  (from Supatrak) and written to `inputs/vehicle_states.csv`, then ignored.
- 35 of 79 vehicles do >= 2 trips/day (median); mean `multi_trip_share` ~0.58;
  extremes HX17CVV 10 trips/day (100% multi-trip), BF65WBY 5/day, several at 3.

Effect: each vehicle's daily delivery throughput is capped at one load, which
**under-utilizes the fleet, inflates the apparent vehicle need, and inflates
`NO_FEASIBLE_ROUTE` rejections** (a "full" vehicle in the model could go out
again in reality).

### Current implementation boundary

Partially implemented on 2026-06-25 in the new `freight_planner` route-seed path:

- `routing_adapter.evaluate_day()` can chain multiple depot-loop trips for one
  `(vehicle, day)`. Capacity resets per trip; elapsed time and drive minutes
  accumulate across the day.
- `route_seed.run_route_seed_plan()` can insert into existing trips or open a new
  trip when the vehicle-day still has enough time. A second trip is treated as a
  fallback move so the greedy seed does not strand work just to reduce vehicle-days.
- `alns.improve_route_seed()` now consumes `route_trips`, keeps `(vehicle, day)` as
  a list of depot-loop trips, tries insertion into existing trips or a new trip,
  and re-evaluates the whole vehicle-day with `evaluate_day()` before accepting
  a move. Per-trip route totals are exported as `ROUTE:...#Tn`.
- `SelectedPlanRecord.trip_index`, selected-plan exports, manifest rows, and
  route stops are trip-aware. Depot start/return rows are emitted per trip.
- Same-day crossdock can now be physically represented as pickup in trip 1 and
  delivery in trip 2 when that is the best feasible placement.

Still outstanding:

- `NO_FEASIBLE_ROUTE` can still hide whether the blocker was trip count, shift,
  driving cap, or ordinary route insertion in some paths. Rejection reason
  propagation should be refined.
- The route seed currently uses a coverage-preserving penalty before opening an
  extra trip on an already-used vehicle. This avoids over-compression but means
  multi-trip is used mainly as fallback when an idle feasible vehicle-day is not
  better.
- A staged `ALNS-1 -> repair rejected -> ALNS-2` flow now exists, but the
  insertion-only repair did not recover any Jan 5-10 rejects. The remaining
  misses are therefore not just a missing post-pass; they require a stronger
  search neighborhood that can trade assigned and unassigned work together.

### Goal

Let a `(vehicle, day)` hold an **ordered sequence of trips**. Each trip loads at
the depot, runs `depot -> stops -> depot`, returns, and reloads. Per-trip
capacity binds (reset at the depot); the **day** is bounded by the shift window
(elapsed) and the daily driving cap (`MAX_DRIVING_H_PER_DAY` = 9h), with a fixed
reload/turnaround dwell between trips. `median_trips_per_day` is a soft guide; a
hard max-trips cap prevents runaway.

### Model / data-structure changes

- `routing_adapter.py`: add `evaluate_day(vehicle, trips: list[list[RouteJob]])`
  that chains trips - trip k departs the depot at the running clock (after trip
  k-1 returns + a `DEPOT_RELOAD_MIN` dwell), runs `evaluate_route` per trip,
  accumulates drive/service/wait, and checks **day-level** shift end + driving
  cap. Returns per-trip evaluations + day totals + feasibility (new reason
  `SHIFT`/`DRIVING_CAP`). Per-trip capacity is unchanged (`evaluate_route`).
- `route_seed.py`: change `routes` from `(vid, day) -> one route` to
  `(vid, day) -> list[trip]`. `best_insertion` tries inserting the job into each
  **existing trip** (best position) AND into a **new trip** (only if the day's
  shift/driving budget can still open one and trips < max). Pick the lowest
  marginal-km feasible option. Ledger logic is unchanged (per-job).
- `plan_schema.SelectedPlanRecord`: add `trip_index` (which trip within the
  vehicle-day). `plan_records` + `manifest.build_route_stops` emit a
  `depot_start`/`depot_return` per trip so the map shows each loop.
- `vehicles.py`: surface `median_trips_per_day` as the soft cap; add a global
  hard cap (e.g. 12). Config: `DEPOT_RELOAD_MIN` (handling/turnaround, pick from
  data or default ~30-45 min); `EVENT_B_DEFAULT_HOUR=12` already exists as a
  mid-day reload anchor.

### Important side benefit: same-day crossdock falls out naturally

A pickup in **trip 1** stages freight at the depot; a delivery in **trip 2**
(which departs only after trip 1 returns) can then carry it. This is exactly the
intra-day staging that M8b's `staged_delivery_start` only approximated - so
multi-trip is the proper home for same-day XDOCK, and M8b's heuristic floor can
be replaced by real trip timing.

### Integration notes / scope

- **Tours are exclusive**: a tour vehicle-day is a multi-day commitment; multi-
  trip applies only to daily (non-reserved) vehicle-days. Keep the reserved-
  vehicle-day exclusion.
- **ALNS**: moves are trip-aware and re-score the whole vehicle-day. Keep this
  invariant for future operators; do not flatten trips before repair.
- **Pre-pass / idle detection** (tour_plan busyness) should count trips, not
  routes.
- **Coverage search**: the next real extension should move from fixed-served-set
  ALNS to coverage-aware ALNS, where the state contains both assigned and
  currently unassigned jobs and operators can insert, swap, or eject work.

### Implementation guardrails

- Do not model a second trip as a second vehicle-day. A vehicle-day remains one
  resource; trips are child loops within it.
- Do not reset driver hours between trips. Capacity resets at depot; elapsed time
  and drive minutes accumulate across the day.
- Do not allow a delivery in trip 2 to consume freight from a pickup in trip 1
  until trip 1 has returned to the depot and completed reload/dock dwell.
- Do not let ALNS move jobs across trips unless it re-evaluates the whole
  vehicle-day, because the move can change later trip departure times.
- Keep tour-reserved vehicle-days excluded from daily multi-trip construction.

### Acceptance criteria

- A vehicle can be assigned >1 trip/day; per-trip capacity binds, day-level
  shift + driving cap bind; trips <= hard cap.
- Total delivered pallets/vehicle/day can exceed a single capacity load.
- On dense local days, coverage rises and/or vehicle count falls vs the single-
  trip baseline; **0 ledger / 0 temporal preserved**.
- A same-day pickup(trip 1) -> delivery(trip 2) is feasible and temporally valid
  (delivery departs after the pickup trip returns).
- `selected_plan`, `plan_manifest`, and `route_stops` show `trip_index` and a
  per-trip depot start/return.
- Coverage-aware ALNS can increase served jobs by inserting previously rejected
  repairable work without introducing ledger or temporal violations.

### Test plan (TDD)

- `evaluate_day` chains two trips: capacity resets per trip; shift/driving cap
  binds across trips + reload.
- seed opens a 2nd trip when a job won't fit trip 1 by capacity but the shift
  allows it; rejects when neither fits.
- a vehicle delivers ~2x its capacity across two trips in one day.
- same-day pickup->delivery across two trips: served, ledger ok, temporal ok.
- hard trip cap respected; `median_trips_per_day` used as the soft guide.
- coverage-aware ALNS can recover at least some repairable rejects on the Jan
  5-10 window or explicitly prove none are insertable even with swaps.

### Suggested sequencing

1. Done: `evaluate_day` + seed multi-trip + `trip_index` in records +
   `route_stops` per-trip loops.
2. Partially done: same-day XDOCK can be placed across trips by the seed, but
   the upstream M8b staging heuristic still exists and should be simplified once
   trip timing is the only same-day handoff model.
3. Done: ALNS trip-awareness. The synthetic regression forces two trips by
   per-trip capacity and verifies `trip_index` plus per-trip route totals survive
   ALNS export.
4. Done but weak: staged `ALNS-1 -> repair rejected -> ALNS-2`. This closes the
   structural hole where rejected jobs were outside the search, but the current
   insertion-only repair did not recover Jan 5-10 misses.
5. Remaining: unify assignment recovery into the main ALNS search so operators
   can insert rejected jobs, swap them with assigned jobs, or eject low-value
   work when that improves served count.
6. Remaining: richer rejection reasons from day/trip insertion failures.

### Next milestone: Coverage-Aware ALNS

Goal: let the main ALNS optimize **coverage first, route quality second**.

Why:

- The current ALNS improves km on the served set correctly.
- The staged repair pass proved that some misses are not recoverable by pure
  add-only insertion after routes are fixed.
- The next useful neighborhood must be able to consider a rejected job and
  reshape the assigned plan around it in one move family.

Required state changes:

- ALNS state must include:
  - assigned `(vehicle, day) -> trips`
  - unassigned repairable jobs
  - fixed non-repairable rejects (bad geocode, before-planning-start, unmet
    pickup dependency, etc.)
- The objective must rank solutions primarily by served jobs, then by route km
  or a weighted operating cost.

Required operators:

- rejected-job insertion into an existing trip
- rejected-job insertion by opening a new trip
- small ejection or swap: remove one or more assigned jobs so a rejected job can
  be inserted
- local rebuild on one vehicle-day or depot-day using a mixed assigned +
  unassigned pool

Acceptance rule:

- Never accept a move that reduces served jobs unless the search is explicitly
  inside a destroy phase and the repair phase restores or improves coverage.
- Final best-so-far comparison should be lexicographic:
  - higher served-job count wins
  - if served count ties, lower route km wins

Validation target:

- On Jan 5-10, the new ALNS should recover at least some of the current
  repairable `SHIFT` / `DRIVING_CAP` / `TIME_WINDOW` rejects or prove via
  explicit failure counts that they are not insertable even with swaps.

Effort remaining: medium. Risk areas: coverage-vs-km objective design, keeping
freight dependencies valid while letting the served set vary, and controlling
runtime once unassigned-job insertion and swap neighborhoods are added.
