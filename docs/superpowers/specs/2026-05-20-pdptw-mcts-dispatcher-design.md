# ZEEFLEET PDPTW Dispatcher Redesign — Design Spec

**Date:** 2026-05-20
**Working folder:** `E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics`
**Status:** Approved design, ready for implementation planning

---

## 1. Problem Statement

**Operational goal:** Given a set of upcoming freight orders and a fleet of vehicles, assign orders to vehicles and sequence each vehicle's stops so as to **minimize total operational cost** (fuel + driver mileage), subject to capacity, pickup-before-delivery precedence, and delivery deadlines.

**Formal classification:** Pickup-and-Delivery Problem with Time Windows and Capacity (**PDPTW**), with **consolidation** — a vehicle may carry several orders' goods simultaneously.

**Solver chosen:** Monte Carlo Tree Search (MCTS) as the solution algorithm, optimizing the *correct* route cost. Large Neighborhood Search (LNS) is a known stronger alternative for cost and is deliberately kept as a low-cost future swap (see §8).

---

## 2. Why the Current System Is Being Replaced

The existing two-step system (MCTS assignment → 2-opt sequencing) has a structural flaw: the two steps optimize *different* objectives.

| Problem | Current behavior | Consequence |
|---|---|---|
| No pickup legs | Cost = vehicle position → delivery destinations only | Ignores the cost of collecting goods; bundling decisions are based on a fictional route |
| Wrong stop order during search | Stops scored in assignment (deadline) order | MCTS optimizes a distorted cost; 2-opt fixes the route only *after* assignment is locked |
| No load-on-board tracking | Only cumulative capacity checked at assignment time | Cannot model consolidation correctly (load rising/falling along a route) |
| Time windows not enforced in routing | Used only as a sort key | Deadlines can be violated by the produced routes |

The redesign unifies all cost accounting into one engine so MCTS optimizes exactly what gets driven.

---

## 3. Architecture Overview

Four layers, each with one responsibility:

1. **PDP route engine** (`Route`, `try_insert`, `route_cost`) — solver-agnostic. The single source of cost truth.
2. **MCTS solver** — decides which vehicle each order goes to, driven by the engine's marginal cost.
3. **Rolling re-optimization wrapper** — runs 3–4× per day, projects vehicle state forward, locks committed work.
4. **2-opt polish** — final intra-route cleanup on an already-correct plan.

Plus a **simulation/comparison harness** (greedy cheapest-insertion baseline on identical accounting).

```
orders + vehicles + committed routes
        │
        ▼
  Rolling wrapper ── projects vehicle start position + remaining capacity
        │
        ▼
  MCTS solver ───── uses ──► PDP route engine (try_insert / route_cost)
        │                         ▲
        ▼                         │
  2-opt polish ───────────────────┘ (route_cost)
        │
        ▼
  output JSON (assignments, routes, meta)
```

---

## 4. The Shared Route Engine (core)

Solver-agnostic. This is where correctness lives.

### `Route`
Represents one vehicle's plan:
- `vehicle_id`
- `start_lat`, `start_lon` — current GPS, or projected position after committed work
- `capacity_kg`, `capacity_pallets` — vehicle limits
- `asset_type` — for per-mile rate lookup
- `stops` — ordered list of `Stop`, each tagged `pickup` or `delivery` and linked to an `order_id`
- Derived (cached, recomputed on mutation): `total_distance_km`, `load_after` (kg + pallets on board after each stop), `arrival_time` at each stop

### `try_insert(order, route) -> InsertionResult`
The most important function. Attempts to insert an order's **pickup and delivery** into a route, evaluating all valid (pickup_index, delivery_index) pairs with pickup_index < delivery_index. For each candidate it checks:
- **Capacity:** load on board (kg AND pallets) never exceeds vehicle limits at any stop between pickup and delivery.
- **Precedence:** pickup occurs before delivery.
- **Time window:** delivery arrival time ≤ order deadline. (Pickup ready-time enforced only if/when intra-day timing data is available; see §7 caveat.)

Returns the **minimum added distance cost** and the resulting stop positions, or `infeasible` if no valid placement exists.

### `route_cost(route) -> float`
Total distance × per-mile rate for the vehicle's asset type:
`(fuel_gbp_per_mile + driver_mileage_gbp_per_mile) × total_distance_km × 0.621371`.
Rates loaded from `profitability_report/vehicle_cost_rates.json` by `_normalise_type_key(asset_type)`. **One formula, used everywhere** — search, rollout, and reporting.

### Engine decisions (defaults, toggleable)
- **Depot return:** route cost does **not** include an empty return leg after the last delivery (cost = start → all stops). Matches current behavior; toggle available.
- **Time windows:** **hard constraint.** Infeasible insertions are rejected; an order with no feasible vehicle goes unassigned.

---

## 5. MCTS Solver

The tree decides **which vehicle each order goes to**; the engine decides **where in the route** and what it costs.

### State — immutable `BatchState`
- `routes` — current `Route` object per vehicle (committed stops already baked in)
- `unassigned` — frozenset of order_ids still to place
- `assigned` — decisions made so far

### Phases
- **Select** — walk the tree via the existing SP-UCB formula `Q + C·√(ln N / n) + √((σ² + D) / n)` (variance-aware; good for noisy cost rewards) to a node with an untried action for the next order. Orders processed hardest-first (tightest deadline), which also aids feasibility.
- **Expand** — candidate vehicles for the next order are ranked by **`try_insert` marginal cost** (not raw haversine). Branch on the top-K cheapest *feasible* insertions plus a `skip` action. Infeasible vehicles pruned here.
- **Simulate (rollout)** — complete remaining orders by greedy cheapest-insertion: each remaining order goes to whichever vehicle's `try_insert` is cheapest. Real route construction, not a destination-only shortcut.
- **Evaluate** — reward = **−(Σ route_cost over fleet) − (penalty × unassigned_count)**.
- **Backpropagate** — visits, total_reward, reward_sq_sum up to root (unchanged).

### Final selection
Most-visited path from root → then 2-opt polish within each vehicle's final route.

### Unassigned-order penalty (default)
A **flat constant** per dropped order, calibrated above any realistic single-route cost, so MCTS drops an order only when truly infeasible. Future hooks: subcontracting-rate estimate, or service-level tiering.

---

## 6. Rolling Re-optimization Wrapper

Production layer; runs 3–4× per day.

Each run:
1. **Load state** — read the day's accumulated dispatch (committed `(order_id, vehicle_id)` pairs + routes) from the prior run's output JSON.
2. **Project vehicle start positions** — a vehicle with committed stops starts from **where it will be after finishing committed work** (last committed stop location), with **remaining capacity** after committed loads. The `Route` carries committed stops, so the engine appends new orders after them.
3. **Solve** — run MCTS on only the orders new/unassigned in this window, inserting into existing routes.
4. **Merge & persist** — fold new assignments into the day's accumulated dispatch; write back out.

**Committed work is locked, not re-planned** — prior windows' assignments stay; routes are only extended. Matches real dispatcher behavior.

---

## 7. Data Caveat (carried forward, not introduced)

Historical Qargo data has no real intra-day order arrival times (~97% of `origin_requested_start_timestamp_local` are midnight). Therefore:
- In **backtesting**, windows are synthetic — the day's orders are sliced into N buckets to exercise the rolling logic.
- In **production**, real timestamps drive windows correctly.
- The code path is identical; only input timing differs.

Delivery deadlines (`destination_requested_start_timestamp_local`, 100% filled) are real and used as the hard time-window constraint. Pickup ready-times are not modeled until intra-day data improves.

---

## 8. Future Swap: LNS

Because the solver sits entirely on the shared engine, replacing MCTS with cheapest-insertion construction + Large Neighborhood Search is a contained change: swap the search driver in §5, keep §4 (engine), §6 (wrapper), and the output contract untouched. Expected benefit: 5–20% lower total cost on high-volume days; negligible difference on small days.

---

## 9. Output Contract

Preserve the existing shape (downstream code + production runner depend on it):

```python
{
  'assignments': [{'order_id': ..., 'vehicle_id': ...}, ...],
  'routes': {
    vehicle_id: {
      'stops': [
        {'order_id', 'type': 'pickup'|'delivery', 'lat', 'lon', 'load_after', 'arrival_time'},
        ...
      ],
      'total_distance_km': float,
      'estimated_cost_gbp': float,
    }
  },
  'meta': {
    'mcts_iterations': int,
    'elapsed_seconds': float,
    'orders_unassigned': int,
    'total_cost_gbp': float,
  }
}
```

New vs. today: `type` and `load_after` on each stop (pickups now appear); `total_cost_gbp` in meta.

---

## 10. Simulation / Comparison Harness

The `simulation/` folder stays. Changes:
- **Greedy baseline** upgraded to the same PDP model (greedy cheapest-insertion, hardest-first) for apples-to-apples comparison on identical cost accounting.
- **`report.py`** keeps comparing assignment rate, total cost, distance, and cost delta — now reflecting real pickup+delivery routes.

---

## 11. Testing Strategy (TDD)

Priority on the new constraint logic, where correctness lives:
- **`try_insert`** — capacity respected at every stop (kg + pallets); precedence enforced; deadline rejection; cheapest feasible slot chosen.
- **`route_cost`** — matches hand-computed multi-stop distance × rate.
- **Load-on-board** — pickup raises, delivery lowers, never exceeds capacity.
- **Rolling wrapper** — committed stops preserved; new orders append after projected position; remaining capacity correct.
- **MCTS** — prefers serving over the penalty when feasible; drops only when infeasible.
- **Regression** — existing `route_sequencer` 2-opt tests stay green as the polish step.

---

## 12. Components Summary

| Component | File (proposed) | Responsibility |
|---|---|---|
| Route engine | `pdp_route.py` (new) | `Route`, `Stop`, `try_insert`, `route_cost`, load/time tracking |
| MCTS solver | `mcts_dispatcher.py` (rewrite core) | Tree search over vehicle assignment using the engine |
| Rolling wrapper | `run_daily_batch.py` (update) | Window runs, state projection, committed-work locking |
| 2-opt polish | `route_sequencer.py` (retain) | Final intra-route cleanup |
| Greedy baseline | `simulation/greedy.py` (update) | Cheapest-insertion baseline on identical accounting |
| Reporting | `simulation/report.py` (retain) | MCTS-vs-greedy comparison |
