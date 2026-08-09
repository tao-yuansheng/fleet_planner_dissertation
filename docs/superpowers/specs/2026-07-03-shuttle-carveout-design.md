# Mega-Shipper Shuttle Carve-Out + Zero-Cost Merge Sweep (K1) — Design

**Date:** 2026-07-03
**Status:** approved direction (stakeholder picked "both"); spec for review
**Owner problem:** QUEST_LOG K1 — like-for-like the plan runs +9%/+20% km/order vs reality.
Root cause (session 2026-07-03a): territorial overlap dominated by mega-shipper scatter.
The greedy seed hands a high-volume address's pallets to every passing route; single-job
ALNS moves cannot undo route-shape overlap (replay-proven km-neutral).

**Evidence anchors:**
- CB9 8QP (Haverhill shipper): 313/259 pickups/wk, 204/143 pallets/DAY ≈ 8/5.5 artic loads,
  scattered over 17-18 plan vehicles/day. Telematics: exactly 5 dedicated fleet vehicles/day.
- Next tier: ST4 8JB (62/43 pal/day), SG8 5RL (56/38), AL7 3UB (27/22).
- Plan overlap 2.44 routes/postcode-district-day vs reality ~1.6.
- Merge replay (scratchpad k1_merge_replay.py): 811 split-pair merges → 306 TRIP_CAPACITY,
  106 TRIP_SHIFT, 76 TRIP_TIME_WINDOW, 302 FEASIBLE with net km −99 (i.e. km-neutral).

Two components, independently measurable:

---

## Component 1 — Shuttle carve-out (pre-seed)

### What

Before the greedy seed assigns anything, detect address-days whose same-direction volume
justifies dedicated full-truckload round trips ("shuttles"), build those trips directly,
and leave only the residual (<1 load) to the general pool. Mirrors the real operation's
dedicated CB9/ST4 crews. Shuttle-assigned jobs are pinned: ALNS may top up shuttle trips
but may never scatter shuttle jobs back out.

### Where

`freight_planner/route_seed.py :: run_route_seed_plan` — new pre-loop phase between
`ordered = sorted(...)` and the main insertion loop. New helper module
`freight_planner/shuttle.py` holds the detection/packing logic (pure functions, unit-testable);
`route_seed.py` applies it (routes dict, ledger, rejected bookkeeping stay where they live today).

### Detection rule

Group runnable candidate jobs by `(service_date, service_pc, leg_kind, anchor_depot)` where:
- `leg_kind` ∈ {CUSTOMER_PICKUP, CUSTOMER_DELIVERY} only (DIRECT_CUSTOMER_MOVE and HUB_DROP
  are two-point moves — excluded);
- `anchor_depot` = `target_depot` for pickups, `source_depot` for deliveries (the depot end
  of the shuttle);
- jobs with a `hard_blocker` are never considered (same filter as the main loop).

A group **qualifies** when `sum(pallets) >= SHUTTLE_MIN_PALLETS` (config, default 26.0 = one
artic load). No hardcoded address list — CB9 8QP qualifies automatically on heavy days and
drops out on light ones.

### Packing

Compute the group's eligible vehicles first (below); bin size = `capacity_pallets` of the
largest eligible class with at least one available vehicle that day. Then first-fit-decreasing
(jobs sorted by pallets descending) into bins of that size. A bin becomes a shuttle trip only
when `bin_pallets >= SHUTTLE_MIN_FILL * bin_size` (config, default 0.9 — a nearly-full load;
exact-full is unattainable with 1-5-pallet jobs). Jobs in the leftover partial bin stay in
`ordered` and ride the general network exactly as today (the "95% not 100%" principle). kg is
checked by the evaluator, not the packer — a bin that busts `capacity_kg` fails evaluation
and dissolves back to the pool (see fallback).

### Vehicle assignment

Eligible vehicles for a group = vehicles that appear in the compatibility OK-set
(`_ok_options`) of **every** job in the bin, whose depot (the depot field the seed/cross-depot
semantics already use on the vehicles frame) is `anchor_depot`, and whose `(vehicle_id, day)`
is not in `excluded_vehicle_days` (tour reservations). Order: tractors first by
`capacity_pallets` descending, then rigids.

Assign bins to vehicles round-robin: append the bin as one trip to the vehicle's day and
run `evaluate_day` (real physics: shift, breaks, reload dwell, driving cap). Keep while
feasible — one tractor legitimately runs 2-3 shuttle round trips/day like the real CB9 crew.
`_trip_cap` (telematics multi-trip prior) is **bypassed for shuttle trips** — the cap exists
to stop implausible general multi-trip days, but a dedicated shuttle is exactly the
operation where repeat trips are the observed reality; `evaluate_day`'s duty/driving caps
are the honest limit. When no vehicle can take a bin, its jobs dissolve back into `ordered`
(no coverage loss possible by construction).

### Ledger + bookkeeping

Carved jobs get the same ledger transitions as the main loop (`pickup_to_depot` /
`deliver_from_depot`) and the same freight-readiness gate for deliveries
(`ledger.exists_at_depot` else the job stays in the pool). Days are processed in date order
so readiness behaves as in the main loop. Carved job_ids are removed from `ordered` before
the main loop runs.

`RouteSeedResult` gains `shuttle_job_ids: set[str]` (default empty). Seed trips built here
land in the same `routes` dict — the main seed loop may still insert compatible general work
into a shuttle trip's spare capacity via the existing `best_insertion` scan (free top-up).

### Pinning in ALNS

`improve_route_seed` / `improve_existing_solution` gain `pinned_job_ids: set[str] | None`.
Enforcement (single choke point + belt-and-braces):
- `job_ids = sorted(j for j in job_loc if j not in pinned)` — random/worst/shaw destroy
  never sample pinned jobs; post-filter `removed` after `_worst_removal`/`_shaw_removal`
  as a second guard since they walk `routes` directly;
- targeted ruination specs (`targeted_specs`) filter pinned ids out of `ruined` lists;
- `_conserve_check` (FP_ALNS_CONSERVE=1) additionally asserts every pinned id stays assigned.
`run_alns.py` passes `pinned_job_ids=seed.daily.shuttle_job_ids`.

### Run-log line

`shuttle: N address-days -> T trips / J jobs / P pallets (residual R jobs to pool)` plus a
per-address summary for the top 5 (so CB9 is visible in every run log).

---

## Component 2 — Zero-cost merge sweep (post-ALNS)

### What

After ALNS finishes and before plan records are emitted, collapse same-day same-address
split visits whose merge is feasible and **net-km ≥ 0** — exactly the replay logic. This is
an OPTICS/operational-realism pass: the replay proved the km is ~0 (net −99 over 302); the
win is fewer redundant dock visits and pickup+delivery pairs done in one visit. Reported
honestly as such.

### Where

New module `freight_planner/merge_sweep.py` ::
`apply_zero_cost_merges(routes, vrows, coords, options, excluded, pinned) -> MergeSweepResult`
(mutated routes dict, applied count, km delta, skip census). Called inside
`alns.py::improve_existing_solution` immediately before final emission (so selected records,
route_totals, km_after and the B16 conservation/emission guards all see the swept solution),
gated by `MERGE_SWEEP_ENABLED`.

### Algorithm (single greedy pass)

1. Collect customer jobs from daily routes; group by `(day, service_pc)`; keep groups
   spanning ≥2 vehicles.
2. Host = the vehicle-trip with the most stops of the group (ties: first). For each guest
   job on another vehicle:
   - skip if the guest job is pinned (shuttle jobs never move; merging INTO a shuttle trip
     as host is allowed — that's top-up);
   - skip unless host vehicle is in the guest leg's OK-set;
   - skip on depot mismatch (guest `source_depot`/`target_depot` must equal the host trip's
     anchor) or `same_order_handoff_conflict`;
   - `try_insert_job(host_veh, host_trip, guest, "best")` + `evaluate_day` on the modified
     host day; skip if infeasible;
   - net = (guest day km without job − guest day km) − (host day km delta); **apply iff
     net ≥ 0** — remove from guest trips (drop emptied trips), commit reordered host trip.
3. Re-evaluate both days after each applied merge (routes mutate; later candidates see
   current state). One pass over groups, no fixpoint iteration (bounded, predictable).
4. If either day re-evaluation is infeasible the merge is rolled back and counted
   (`ROLLBACK` census bucket) — the sweep must never degrade feasibility or coverage.

### Run-log line

`merge-sweep: applied A of C candidates (km delta D, rollbacks R) — visits collapsed V`.

---

## Config (freight_planner/config.py)

| knob | default | meaning |
|---|---|---|
| `SHUTTLE_ENABLED` | `True` | master switch for component 1 |
| `SHUTTLE_MIN_PALLETS` | `26.0` | address-day qualify threshold (one artic load) |
| `SHUTTLE_MIN_FILL` | `0.9` | a bin ships as a shuttle trip only at ≥ this fraction of vehicle capacity |
| `MERGE_SWEEP_ENABLED` | `True` | master switch for component 2 |

## Out of scope

- K2 (day-flexibility) and K3 (hub injection) — separate design conversations.
- The cluster-destroy ALNS operator (option B) — only if measurement shows residual overlap
  worth chasing.
- Any change to tours, trunk (T1), or the DIRECT/XDOCK resolver.

## Testing

TDD throughout. Unit: detection/qualify boundaries (25.9 vs 26.0 pallets), FFD packing +
partial-bin residual + min-fill boundary (bin at 0.89 vs 0.90 of capacity), vehicle-order
preference (tractor before rigid), infeasible-bin
dissolve-to-pool, ledger transitions, delivery readiness gate, `shuttle_job_ids` export,
destroy ops never return pinned ids, sweep applies net≥0 / skips net<0 / skips
capacity-time-infeasible / rolls back cleanly. Integration: `run_route_seed_plan` on a
synthetic mega-address fixture (30 pal/day across 12 jobs → 1 full shuttle trip + residual);
full suite stays green.

## Measurement / acceptance (single run per week, no tuning loops)

Rerun wk1 (2026-01-12..17) and wk2 (2026-01-19..24), report as they land:
1. coverage MUST hold (99.7% / 99.8%);
2. total km vs baselines 91,390 / 104,743 (direction expected down; magnitude honest);
3. overlap metric: mean routes per district-day (2.44 → toward 1.6);
4. CB9 8QP vehicles/day (17.6 → toward 5) + shuttle run-log line;
5. redundant same-address visits (440/492 → down; sweep census in log);
6. runsheets sanity: shuttle trips visible as repeat full-load round trips.

Regression watch: routes that lose their CB9 backhaul filler may go emptier — watch pallet
fill distribution and vehicle-days; a km *increase* with better structure metrics is a
stakeholder conversation, not a silent revert.
