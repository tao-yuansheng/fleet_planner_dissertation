# Two-Phase Planning Architecture

**Status:** Design — pre-implementation  
**Date:** 2026-06  
**Replaces:** Single-day dispatcher assumption in `cambridge-dispatcher-design.md`

---

## Why two phases

The existing dispatcher answers:
> *Given today's orders and today's vehicle positions, what are the best routes for today?*

The operational objective is:
> *Given the next 4–7 days of orders, how do I deploy the fleet so everything gets delivered with the least total cost?*

These are structurally different problems. A day-level solver optimising in isolation makes three systematic errors:

1. **Assumes all vehicles home each morning.** In reality, X888GNW loads at MK42 on Monday and delivers across Somerset/Devon/Cornwall over Tuesday–Wednesday, never returning mid-tour. The day-solver cannot replicate this because it has no mechanism to carry orders forward across days.

2. **Cannot reserve capacity for known future demand.** A Palletline Import order due Thursday is visible on Monday (`timestamp_created`). The day-solver for Monday ignores it; a week-level solver allocates a local vehicle-slot for it on Thursday.

3. **Re-solves vehicle assignment from scratch every morning.** This destroys inter-day structure. Vehicles end up assigned to random areas each day rather than building efficient multi-day tours.

The two-phase model separates the concerns:

| | Phase 1 — Strategic planner | Phase 2 — Tactical dispatcher |
|---|---|---|
| **Horizon** | 4–7 days | 1 day |
| **Run cadence** | Once per week, updated nightly | Every morning |
| **Question answered** | Which vehicle handles which orders over the week? | What is the exact route for each vehicle today? |
| **Output** | Weekly vehicle plan, tour manifests, capacity budgets | Exact routes with waypoints and departure times |
| **Inputs from** | Full week's order list, fleet roster | Phase 1 allocations + overnight positions |

---

## System diagram

```
 ORDER INTAKE
 ─────────────────────────────────────────────────────────
 DIRECT / PL_EXPORT   →  visible from timestamp_created
 PL_IMPORT            →  visible from timestamp_created,
                         but release_time = delivery_date-1 night (~03:00)
                         (goods arrive at B37 hub overnight before delivery)

 ┌──────────────────────────────────────────────────────────┐
 │  PHASE 1 — Weekly Strategic Planner                      │
 │                                                          │
 │  1. Classify orders: local vs. tour vs. trunk            │
 │  2. Cluster tour orders by region                        │
 │  3. Pack clusters into vehicle tours (multi-day)         │
 │  4. Allocate remaining local vehicles per depot per day  │
 │  5. Reserve PL_IMPORT capacity per depot per day         │
 │                                                          │
 │  Output: WeeklyPlan  (see data-contract.md)              │
 └──────────────┬───────────────────────────────────────────┘
                │  WeeklyPlan.daily_allocations[day]
                ▼
 ┌──────────────────────────────────────────────────────────┐
 │  PHASE 2 — Daily Tactical Dispatcher  (current VRPTW)    │
 │                                                          │
 │  Inputs:                                                 │
 │    - available_vehicles (not mid-tour)                   │
 │    - vehicle_start_positions (overnight from Phase 1)    │
 │    - pre_assigned_manifests (orders already on board)    │
 │    - local_order_pool (today's local orders to route)    │
 │                                                          │
 │  Runs: ALNS VRPTW per depot                              │
 │                                                          │
 │  Output: DayExecutionResult  (see data-contract.md)      │
 └──────────────┬───────────────────────────────────────────┘
                │  end_positions + undelivered_manifests
                ▼
 ┌──────────────────────────────────────────────────────────┐
 │  STUDENT LAYER — Dynamic Re-optimiser                    │
 │                                                          │
 │  Triggered when: new orders arrive, cancellations,       │
 │    late manifests, vehicle breakdowns                    │
 │                                                          │
 │  Problem: insert new orders into committed plan with     │
 │    minimum disruption, respecting frozen constraints     │
 │    (locked legs, committed time windows, hours driven)   │
 │                                                          │
 │  NOT a full re-run — operates on frozen + flexible zones │
 └──────────────────────────────────────────────────────────┘
```

---

## Order classification

The critical first step in Phase 1. Every order is assigned one of three dispatch classes:

### LOCAL
Deliverable as a same-day out-and-back from home depot.

- Destination reachable within ~120 km of depot
- PL_IMPORT orders are **always LOCAL** (goods not available until morning of delivery day)
- Most CB22 rigids and Bedford rigids handle this class

Criteria: `assign_depot(destination_pc) in ('CB22', 'BEDFORD', 'ST_IVES')`  
and `delivery_date == dispatch_date` (no overnight needed)

### TOUR
Requires 1–3 day vehicle deployment to remote region.

- Destination is EX, TR, PL, BA, CF, LL, DG, LA, IV, PA etc.
- Vehicle loads at depot, drives to region, delivers over 1–3 days, returns
- Handled by artic tractors (CB22 or BEDFORD)
- Vehicle carries its manifest across nights; Phase 1 tracks the overnight position

Criteria: drive time from depot > ~4h one-way  
(i.e. same-day return is infeasible within driver hours)

### TRUNK
Flows through B37 Palletline hub or LE10 Hazchem hub.

- PL_EXPORT collections are trunked to B37 each evening
- PL_IMPORT deliveries arrive from B37 each morning (these become LOCAL on delivery day)
- Trunk vehicles are scheduled by the existing `trunk_planner.py` (unchanged)

---

## Relationship to existing code

```
logistics/
├── cambridge/
│   ├── scope.py              ← add release_time, order_class to ScopedOrder
│   ├── day_coordinator.py    ← Phase 2 entry point (modified to accept DayAllocation)
│   ├── trunk_planner.py      ← unchanged
│   ├── config.py             ← unchanged
│   └── week_planner/         ← NEW — Phase 1 implementation
│       ├── __init__.py
│       ├── order_classifier.py
│       ├── tour_builder.py
│       ├── capacity_allocator.py
│       └── weekly_plan.py    ← WeeklyPlan dataclass and serialisation
│
├── multiday_backtest.py      ← updated to run Phase 1 then Phase 2 per day
└── docs/
    ├── architecture-two-phase.md   ← THIS FILE
    ├── data-contract.md            ← interface spec between phases
    └── phase1-design-options.md    ← model options and recommendation
```

---

## What does NOT change

- The VRPTW/ALNS solver (`simulation/` package) — untouched
- The trunk planner logic — untouched  
- The HTML map / Gantt replay exports — untouched
- The backtest comparison framework — extended, not replaced
- The scope filter (`scope.py`) — extended with `release_time` and `order_class`, not rewritten

---

## Key assumptions and constraints

| Assumption | Implication |
|------------|-------------|
| PL_IMPORT goods arrive at B37 hub overnight before delivery day | Release time = ~03:00 delivery_date; always LOCAL class; Phase 1 cannot schedule them as TOUR orders |
| DIRECT and PL_EXPORT orders visible from `timestamp_created` | Phase 1 can plan TOUR orders days in advance |
| Multi-day tour vehicles do not return to depot mid-tour | DayState must carry mid-tour manifests across days |
| Driver hours not yet modelled | TOUR length currently bounded by calendar days only; driver hours a future constraint |
| St Ives (PE27) is a parking facility, not a full active depot | Phase 1 does not build PE27 order pools; its vehicles are available as CB22 overflow |
