# Stranded-Order Backhaul Repair — Design

**Date:** 2026-07-02
**Status:** SHIPPED 2026-07-02 (validated: wk1 99.7%/tail 0, wk2 99.8%/tail 2, 0 violations;
flagship 27623f3d rides TOUR:X8GNW day-3 homebound). Three spec deltas discovered at execution:
(1) tour-planned deliveries whose pickup stranded only bounce at COMMIT — the repair pulls them
off their host tours pre-commit; (2) seed-time strand reasons are the last insertion failure
(mostly SHIFT), not NO_FEASIBLE_ROUTE — eligibility matches all insertion-failure reasons (safe:
both-stranded pairs are beyond ALNS); (3) Mode 1 needs an economics bound (added km ≤ standalone
round trip) or it drags drops onto wrong-direction sweeps. Bonus: removed ~8k km/wk of phantom
work (tours driving to doomed stops; ALNS inserting doomed pickups). KPI: superseded legs no
longer gate order completion.
**Author:** brainstormed with stakeholder, grounded in X8RNW telematics

## Problem

The residual in-universe unassigned tail is one uniform pattern: **multi-day FULL_FLEET
orders resolved as XDOCK whose far pickup strands** (`NO_FEASIBLE_ROUTE`) **and whose
delivery cascades** (`DELIVERY_BEFORE_PICKUP`). Current tail:

- wk1 (12–17): 7 orders — 2× NE42→SG1/RM20 **full 26-pallet loads**, 5× ST4→London (2–7 pal).
- wk2 (19–24): 9 orders — 8× ST4→NW/London (0–9 pal), 1 unrelated CB22/SN5 case (out of scope).

**Ground truth (telematics + Qargo):** the real X8RNW served order `27623f3d`
(NE42→SG1, 26 pal) as a **DIRECT backhaul on its Scotland sweep's empty return leg**
(Jan 14–16: CB22 → Stoke ST4 load-stop → Fife/Glasgow/Ayrshire drops → collect 26 pal
at Prudhoe NE42 → overnight Retford → deliver Stevenage homebound; 1,720 km, peak
load ≤26 at all times). The ST4→London/NW orders are the Stoke shipper's distribution
— common origin, natural multi-drop DIRECT runs.

**Why not fix at resolve time:** tried 2026-07-01, net-negative, reverted — stranding
is not knowable before tours exist; the broad rule flipped 74 orders/wk (only ~6
stranded) and doubled tour km. The repair must be **post-seed and strictly targeted**.

## Design (Approach A — approved)

A `repair_stranded_orders` step inside `run_multiday_seed_plan`
(`freight_planner/tour_plan.py`), after the daily seed, **before the tour ledger
commit**, gated by a new config knob `STRANDED_REPAIR_ENABLED: bool = True` (beside
the other tour knobs in `cambridge/config.py`).

### Eligibility (the anti-bloat guarantee)

An order is repairable iff **its XDOCK `CUSTOMER_PICKUP` stranded with
`NO_FEASIBLE_ROUTE` AND its `CUSTOMER_DELIVERY` is also unassigned** (any reason).
Both legs must be unserved — never repair a partially-served order. This is ~7–9
orders/week by construction (vs the 74/wk the reverted resolver flipped).

### Synthesized DIRECT job

Per repairable order, build one `RouteJob`:
- `job_id = f"RD:{order_id}"`, `leg_kind = DIRECT_CUSTOMER_MOVE`
- origin lat/lon = the pickup leg's service coords; dest lat/lon = the delivery
  leg's service coords (both from the compatibility frame)
- pallets/kg from the leg records
- **due** = delivery leg `service_date` (deadline, enforced by `due_offsets`/LATE)
- **ready** = pickup leg `service_date` (the day the shipper has it; NO +1 — a
  DIRECT collects at the origin mid-tour, no depot staging involved)

Ledger physics: freight is `AT_CUSTOMER_ORIGIN` (its pickup never ran), and the
existing tour-commit path already handles `DIRECT_CUSTOMER_MOVE` via
`deliver_direct` — no ledger changes.

### Mode 1 — attach to an existing tour (the backhaul)

For each existing `TourAssignment`, try inserting the DIRECT job at **every
position** and re-evaluate with `evaluate_tour` (capacity peak, two-cap day split,
LATE). Accept the feasible insertion with the **lowest added km** across all tours.
Constraints:

- **Per-stop readiness:** `evaluate_tour` gains an optional `floor_offsets`
  (job_id → earliest day offset, mirror of `due_offsets`): a stop reached on a day
  **before** its floor is infeasible (`EARLY`). The repair passes
  `floor = (ready − tour_start).days` — this is what lets the NE42 collection
  (ready 01-16) ride a tour that *departed* 01-15 and reaches Prudhoe on day 1.
  Tours where `due < tour_start` are skipped outright.
- **Reservation extension:** if the insertion grows `evaluation.days`, the extra
  `(vehicle, day)` slots must be free in `reserved`; extend on acceptance and
  update the `TourAssignment` (jobs, evaluation, days).
- The 26-pallet NE42 load only fits where the tour is empty — the peak-load model
  finds the backhaul slot naturally (transient DIRECT load after the drops).

### Mode 2 — batch the leftovers into new DIRECT tours

Jobs no tour accepts go through the **existing machinery**: `build_tours`
(due/ready gates as-is — the departure-based readiness gate is conservative for
DIRECTs but correct) → `_resolve`/`resolve_cluster` (ST4-origin DIRECTs are
depot-loadable: origin is at the Stoke yard, inside `TOUR_ORIGIN_AT_DEPOT_RADIUS_KM`)
→ start day = `max(min_due, max_ready)` (existing safety net) →
`select_tour_vehicle` over vehicles free across the span, any depot (reality used a
Cambridge tractor for Stoke work). No vehicle / infeasible → the order stays
unassigned with its original reason — honest failure.

### Accounting

- Repaired orders' superseded XDOCK legs get reason **`REPAIRED_DIRECT`** in the
  unassigned table (analogous to `BEFORE_PLANNING_START`: legs unplanned, order
  served). Thread a `repaired_order_ids` set through the seed result into the
  manifest's unassigned-reason assignment.
- Coverage counts the order as assigned automatically (the DIRECT record is
  selected). Phantom validator: DIRECT counts as the delivery. Temporal validator:
  only pairs `CUSTOMER_PICKUP`+`CUSTOMER_DELIVERY` legs — repaired orders have
  neither planned, so no false pairing.
- Emit: repaired jobs need a `job_meta` candidate-dict (`leg_id=f"{order_id}:RD"`,
  `preferred_start_node/end_node = CUSTOMER`, origin pc/coords for the viz
  collect-stop) so the existing tour emit writes them like any DIRECT tour stop.

### Error handling

- Missing coords / missing counterpart leg / order not in freight states → skip
  (leave stranded, original reason).
- Repair must never touch orders outside the eligibility set, never displace an
  already-assigned job, and never shrink an existing tour.

## Files

- `freight_planner/tours.py` — `floor_offsets` in `evaluate_tour` (+ `EARLY`
  reason); `try_insert_tour_job(vehicle, jobs, cand, due_offsets, floor_offsets)`
  position-insertion helper.
- `freight_planner/tour_plan.py` — `repair_stranded_orders` step (eligibility,
  synthesis, Mode 1, Mode 2, reservation bookkeeping, `repaired_order_ids` in the
  result).
- `freight_planner/manifest.py` — `REPAIRED_DIRECT` reason override for superseded
  legs of repaired orders.
- `cambridge/config.py` — `STRANDED_REPAIR_ENABLED: bool = True`.
- Tests: `tests/freight_planner/test_tours.py` (floor/EARLY, insertion helper),
  `tests/freight_planner/test_tour_plan.py` (end-to-end repair: stranded XDOCK pair
  → DIRECT on a tour; attach case + batch case + no-vehicle honest-failure case),
  manifest reason test.

## Testing & validation

Unit (TDD): floor_offsets EARLY; try_insert_tour_job picks a feasible backhaul
position for a 26-pallet DIRECT only after the drops; end-to-end: a far pickup that
strands daily + its delivery → repaired onto an existing tour (Mode 1); a common
origin pair with no passing tour → one new DIRECT tour (Mode 2); no free vehicle →
stays unassigned with original reason; manifest shows `REPAIRED_DIRECT`.

Full runs (both weeks), acceptance:
- NFR+DBP tail ≤ 2/week (the SN5 case may remain); coverage wk1 → ~99.7%, wk2 → ~99.8%.
- Total km rise bounded (~+1–2k/wk); tour km must NOT balloon (failed-resolver
  anti-pattern was +17–21% total / 2× tour km).
- 0 temporal / 0 ledger / 0 phantom; 642+ tests green.
- Flagship check: `27623f3d` (NE42→SG1) rides a northbound tour's return leg.

## Constraints

- **No `git commit` this session** (standing stakeholder instruction).
- Viz regeneration: `trip_app` only.
