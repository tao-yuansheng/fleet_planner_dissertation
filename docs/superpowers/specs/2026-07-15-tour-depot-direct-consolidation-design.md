# Tour consolidation: depot-loaded directs as deliveries — Design

**Date:** 2026-07-15
**Status:** design (awaiting review)

## Problem

A single customer shipment split across three vehicles. Orders WT255676 / WT255672 / WT255678
were booked together (16 s apart, 13 Jan), share the **same pickup (the CB22 depot area), the same
destination (HU6 7QD, Hull), the same delivery window, and the same due date (15 Jan)**, and total
just **9 pallet-spaces / 2.7 t** — one small truck's load. The planner sent them on **three separate
tours** (2 rigids + a tractor), each a ~528 km round trip to the identical postcode.

## Root cause (confirmed by reproduction, not inferred)

A DIRECT move (`DIRECT_CUSTOMER_MOVE`) is modelled as an **atomic collect→deliver pair**. Isolated
reproduction with `build_tours` on the three real orders:

- **As DIRECTs:** `build_tours` returns **3 tours** (each 528 km, 1 day). Evaluating any *two*
  same-destination directs together returns **infeasible** — the atomic pairing forces two
  depot→dest→depot round trips, which exceeds the day driving cap, so the greedy blocks the second
  candidate and they never cluster.
- **As DELIVERIES:** the same three orders return **1 tour** (feasible, 1 day, 405 km, peak 9 pallets).

So the split is a **within-batcher limitation, not a cross-epoch/freezing issue** (an earlier guess
that this reproduction disproved). Deliveries already consolidate correctly; directs cannot.

When a direct's collection origin is **at the depot** the tour departs from, the collect→deliver
pairing is pointless — the load is simply picked up where the tour already starts. The code already
recognises this class (`tours._origin_at_depot`, radius `TOUR_ORIGIN_AT_DEPOT_RADIUS_KM = 8 km`,
called "depot-loadable"). Functionally, a depot-origin direct **is** a depot-loaded delivery.

## Fix

**Reclassify depot-loadable directs as deliveries for tour planning.** In
`tour_plan.run_multiday_seed_plan`, the tour-candidate loop (around `tour_plan.py:268–282`) builds a
`RouteJob` per far order via `make_route_job`, then buckets it under its anchor depot
(`_anchor_or_nearest`). Insert one step: if the job is a `DIRECT_CUSTOMER_MOVE` whose origin is
depot-loadable (`_origin_at_depot`), replace it with a delivery variant before bucketing:

```python
anchor_xy = DEPOT_ANCHORS.get(depot)          # `depot` = _anchor_or_nearest(src, c[0], c[1])
if (depot_direct_as_delivery and rjob.leg_kind == DIRECT_CUSTOMER_MOVE
        and anchor_xy is not None and _origin_at_depot(rjob, {depot: anchor_xy})):
    rjob = dataclasses.replace(rjob, leg_kind=CUSTOMER_DELIVERY, origin_lat=None, origin_lon=None)
```

`RouteJob` is frozen, so use `dataclasses.replace`. The check is against the **anchor depot
specifically** (not any depot): the freight must load where this tour actually departs, otherwise the
reclassification would drop a real collection leg. `_origin_at_depot` accepts an `anchors` dict, so
pass a single-entry `{depot: anchor_xy}`. From that point on `build_tours`,
`evaluate_tour`, and `resolve_cluster` treat it as an ordinary delivery, so same/near-destination
depot-loaded orders consolidate under the rules deliveries **already** use — cohesion
(`TOUR_COHESION_KM = 200 km`), capacity, day cap, due-date spread. No new consolidation logic and,
crucially, **no change to the tour-evaluation core** (the module a prior broad change was reverted in).

### Scope is automatic

Because they are now deliveries, they merge by the existing delivery rules. Same-destination (the
Hull case) merges; near-destination same-region ones merge within cohesion. There is no new scope knob.

### Output semantics (decided)

Consolidated depot-loaded directs **appear as depot deliveries** in the plan / board / map (no collect
origin, `stop_type = customer_delivery`). No label-restore step. The physical plan and every KPI (km,
vehicle-days, coverage) are identical to restoring the label; only the flow *category* shifts from
"direct carry" to "delivery" for these depot-loaded orders. (Chosen over a label-restore step for
simplicity.)

## Feature flag

`TOUR_DEPOT_DIRECT_AS_DELIVERY: bool = True` in `freight_planner/config.py`, threaded to
`run_multiday_seed_plan` (parameter `depot_direct_as_delivery`, default from config). A
`--no-tour-depot-direct-as-delivery` CLI ablation on `run_rolling` / `run_alns` reproduces the
pre-fix behaviour byte-for-byte — needed to A/B this fix against the January baseline currently
running (matches the `--vehicle-day-cost` / `--no-vehicle-day-cost` pattern).

## Coverage-safety

- The transform only makes **more** batches feasible. A batch that is still infeasible is left split
  by the greedy's existing `blocked` handling — **no order is ever dropped**. Lexicographic
  serve-first is unchanged.
- A single depot-loaded direct as a delivery is the same round trip it already was (its origin leg was
  ≤ 8 km, i.e. already treated as depot-loadable). Singletons are unaffected in coverage.
- **Non-depot-origin directs are untouched** — they keep the atomic collect→deliver pairing (their
  backtrack pickup is a real cost/feasibility concern; out of scope here).
- The **daily-planner path is untouched**: only `tour_candidates` are transformed, so the daily seed
  and all non-tour output are byte-identical (regression gate).

## Modeling note

Dropping the ≤ 8 km origin leg treats depot-loadable freight as loaded at the depot. This is
consistent with the existing `_origin_at_depot` "depot-loadable" concept and the resolve-cluster
"front load-stops" handling; it is a small, already-established approximation, not a new one.

## Validation plan

1. **Unit (TDD):**
   - A depot-origin `DIRECT_CUSTOMER_MOVE` candidate → a `CUSTOMER_DELIVERY` RouteJob with origin
     dropped; a **far-origin** direct is left unchanged.
   - Regression reproduction: three same-destination depot-origin directs → **1 tour** (was 3).
   - Coverage-safety: when a combined batch is genuinely infeasible (e.g. capacity), orders stay on
     separate tours and none is rejected.
2. **Flag-off ablation = byte-identical** to the pre-fix plan on a fixed window (hard gate).
3. **Integration:** re-run the 2026-01-12→18 dynamic window; assert the three Hull orders land on one
   tour, coverage KPIs unchanged (ON_TIME / NOT_PLANNED / UNSERVED), vehicle-days and km down.
4. **Month compare:** a fresh January dynamic run vs the baseline now running — report the
   vehicle-day / km delta and confirm coverage is not worse.

## Out of scope

- Non-depot-origin direct consolidation (the genuine backtrack-risk case).
- Cross-epoch tour merging (the reproduction shows the bug is within-batcher; not needed).
- The ⛺ / "single-day tour" naming question (separate, cosmetic).
