---
name: mcts-logistics-dispatcher
description: Design spec for the ZEEFLEET logistics MCTS dispatcher — Approach C: depth-N MCTS with immutable BatchState snapshots and a separate 2-opt route sequencer
metadata:
  type: project
---

# MCTS Logistics Dispatcher — Design Spec

**Date:** 2026-05-19  
**Context:** Phase 2 of the ZEEFLEET MCTS Rolling Freight Dispatch System. Data audit (Phase 1) is complete — `data_audit.py` confirmed 78.2% vehicle match rate across Qargo/Supatrak/Jigsaw. This spec covers the dispatcher itself.  
**Reference:** `BackEnd/ES/simulated_environment/mcts_dispatcher.py` — used as structural template; upgraded for freight (depth-N, SP-UCB, immutable state).

---

## 1. Architecture Overview

Two new files inside `logistics/`:

```
logistics/
├── mcts_dispatcher.py        # MCTS search, node tree, UCB selection, rollout
└── route_sequencer.py        # Stage-2: nearest-neighbour init → 2-opt refinement
```

Both import from existing modules — no reimplementation:

| Import | From | Used for |
|---|---|---|
| `_haversine_km` | `profitability_report/profitability_report_merged.py` | Distance between stops |
| `_load_cost_rates`, `_rate_bundle`, `_normalise_type_key` | same | Per-type cost rates |
| `_normalise_reg` | `data_audit.py` | Registration normalisation on load |

**Data flow:**

```
Input dict (orders + vehicles + telematics)
        │
        ▼
MCTSDispatcher.run_batch()
  1. Filter orders by time window → active_orders list
  2. Lock committed orders → pre-populate BatchState at root
  3. Sort remaining by time-window tightness (hardest first)
  4. Run MCTS for 30s wall-clock
        │
        ▼
  Best BatchState (order → vehicle assignments)
        │
        ▼
route_sequencer.sequence_routes()
  For each vehicle: nearest-neighbour → 2-opt
        │
        ▼
Output dict: {assignments, routes, meta}
```

**Module boundaries:**
- `mcts_dispatcher.py` knows nothing about stop ordering — it only assigns orders to vehicles
- `route_sequencer.py` knows nothing about MCTS — it receives a list of stops and returns the best sequence
- The output shape is fixed so the web UI can consume it without knowing which module produced what

---

## 2. Core Data Structures

**BatchState** — immutable snapshot passed between nodes:

```python
from typing import FrozenSet
from dataclasses import dataclass

@dataclass(frozen=True)
class BatchState:
    assigned: tuple          # ((order_id, vehicle_id), ...) — decisions made so far
    unassigned: FrozenSet    # frozenset of order_ids still to place
    vehicle_loads: tuple     # ((vehicle_id, current_kg), ...) — capacity tracking
```

Frozen dataclass means every `expand()` creates a new object — no backpropagation corruption across a 150-deep tree.

**MCTSNode:**

```python
@dataclass
class MCTSNode:
    state: BatchState
    parent: 'MCTSNode | None'
    action: tuple | None        # (order_id, vehicle_id) that produced this node
    children: list
    visits: int = 0
    total_reward: float = 0.0
    reward_sq_sum: float = 0.0  # for SP-UCB variance term
```

**Input schema** (what `run_batch()` expects):

```python
{
  "orders": [
    {"order_id": "...", "origin_lat": float, "origin_lon": float,
     "dest_lat": float, "dest_lon": float,
     "goods_weight_kg": float, "time_window_start": "ISO8601",
     "time_window_end": "ISO8601"}
  ],
  "vehicles": [
    {"vehicle_id": "...", "registration": "...", "asset_type": "Lorry",
     "capacity_kg": float, "last_lat": float, "last_lon": float}
  ],
  "committed_assignments": [   # locked from previous batch — empty list on first run
    {"order_id": "...", "vehicle_id": "..."}
  ],
  "config": {
    "time_budget_seconds": 30,
    "horizon_hours": 8,
    "exploration_constant": 1.414,
    "sp_ucb_d": 0.1          # SP-UCB D parameter — reward scale estimate
  }
}
```

**Output schema:**

```python
{
  "assignments": [{"order_id": "...", "vehicle_id": "..."}],
  "routes": {
    "vehicle_id": {
      "stops": [{"order_id": "...", "lat": float, "lon": float, "type": "pickup|delivery"}],
      "estimated_cost_gbp": float,
      "total_distance_km": float
    }
  },
  "meta": {
    "mcts_iterations": int,
    "elapsed_seconds": float,
    "orders_unassigned": int   # >0 means fleet was insufficient for this batch
  }
}
```

---

## 3. MCTS Algorithm

**SP-UCB selection** — used at every non-leaf node to pick which child to explore next:

```python
def _sp_ucb(node: MCTSNode, parent_visits: int, C: float, D: float) -> float:
    if node.visits == 0:
        return float('inf')
    Q = node.total_reward / node.visits
    variance = (node.reward_sq_sum / node.visits) - Q ** 2
    return Q + C * sqrt(log(parent_visits) / node.visits) + sqrt((variance + D) / node.visits)
```

SP-UCB replaces the ES dispatcher's UCB1 because freight rewards are noisier than ES incident scores — route cost varies with geography, load, and vehicle type, so the variance term prevents over-exploitation of early-sampled routes.

**Candidate generation** — the branch-pruning step that keeps the tree tractable:

```python
def _candidate_vehicles(order, vehicles, state, max_candidates=8) -> list:
    # 1. Filter: vehicle type must match order's derived asset_type
    # 2. Filter: vehicle must have remaining capacity >= order.goods_weight_kg
    # 3. Sort by haversine_km(vehicle.last_pos, order.origin) ascending
    # 4. Return top max_candidates
```

This reduces effective branching from 78 fleet vehicles to ~5–10 per order, keeping the tree within 30s budget even at depth 150+.

**One MCTS iteration:**

```
SELECT   — walk tree via SP-UCB until a node has untried actions
EXPAND   — pick one untried (order, vehicle) action, create child with new BatchState
SIMULATE — greedy rollout: for each remaining order, assign to nearest compatible vehicle
BACKPROP — walk back to root, updating visits, total_reward, reward_sq_sum
```

**Reward function** (called at end of rollout):

```python
def _evaluate(state: BatchState, vehicles: dict, orders: dict, cost_rates: dict) -> float:
    total_cost = 0.0
    for vehicle_id, stop_list in _group_stops(state).items():
        asset_type = vehicles[vehicle_id]['asset_type']
        rates = _rate_bundle(cost_rates, _normalise_type_key(asset_type))
        route_km = _route_distance_km(stop_list)
        route_miles = route_km * 0.621371
        total_cost += (rates['fuel_gbp_per_mile'] + rates['driver_mileage_gbp_per_mile']) * route_miles
    # Revenue term omitted — Qargo revenue coverage is ~46%, insufficient for optimisation
    # Uncomment and test when revenue data coverage improves beyond ~80%:
    # total_cost -= sum(orders[oid].get('revenue_gbp', 0.0) for oid, _ in state.assigned)
    return -total_cost   # MCTS maximises reward; cost minimises, so negate
```

**Main loop:**

```python
def _mcts_search(root: MCTSNode, time_budget_s: float, C: float, D: float) -> MCTSNode:
    deadline = time.monotonic() + time_budget_s
    iterations = 0
    while time.monotonic() < deadline:
        node = _select(root, C, D)
        if not node.state.unassigned:
            reward = _evaluate(node.state, ...)
        else:
            child = _expand(node)
            reward = _simulate(child)
        _backpropagate(node, reward)
        iterations += 1
    return _best_assignment(root), iterations  # best = highest visit count child
```

---

## 4. Route Sequencer (Stage 2)

`route_sequencer.py` is a standalone module — it has no knowledge of MCTS.

**Stop representation:**

```python
@dataclass
class Stop:
    order_id: str
    lat: float
    lon: float
    stop_type: str   # "pickup" or "delivery"
```

**Phase 1: nearest-neighbour initialisation**

```python
def _nearest_neighbour(stops: list[Stop], start_lat: float, start_lon: float) -> list[Stop]:
    # Greedy: always go to the closest unvisited stop
    # start position = vehicle's last known GPS from Supatrak telematics
```

**Phase 2: 2-opt improvement**

```python
def _two_opt(route: list[Stop]) -> list[Stop]:
    improved = True
    while improved:
        improved = False
        for i in range(1, len(route) - 1):
            for j in range(i + 1, len(route)):
                if _two_opt_gain(route, i, j) > 0:
                    route[i:j] = route[i:j][::-1]
                    improved = True
    return route
```

No pickup-before-delivery constraint is enforced in Phase 1 — the MCTS treats each order as a single destination waypoint. If split pickup/delivery legs are required in future, this module is the only place that changes.

**Public interface:**

```python
def sequence_routes(
    assignments: list[tuple[str, str]],   # [(order_id, vehicle_id), ...]
    orders: dict,                          # order_id → order dict
    vehicles: dict,                        # vehicle_id → vehicle dict
) -> dict:                                 # vehicle_id → {"stops": [...], "distance_km": float}
```

`mcts_dispatcher.py` calls this once after MCTS returns the best `BatchState`.

---

## 5. Testing

Tests live in `logistics/tests/` alongside `test_data_audit.py`.

### test_route_sequencer.py

| Test | What it checks |
|---|---|
| `test_nearest_neighbour_ordering` | 3 stops in a line — returns them left-to-right |
| `test_two_opt_improves_crossed_route` | Crossed route (A→C→B→D) becomes uncrossed (A→B→C→D) |
| `test_two_opt_already_optimal` | Optimal route unchanged after 2-opt |
| `test_sequence_routes_groups_by_vehicle` | 4 orders, 2 vehicles — output has 2 keys with correct stop count |
| `test_sequence_routes_distance_positive` | Distance > 0 for any non-trivial route |

### test_mcts_dispatcher.py

| Test | What it checks |
|---|---|
| `test_batch_state_immutable` | Modifying a copy does not affect original |
| `test_sp_ucb_unvisited_returns_inf` | Unvisited node always selected first |
| `test_sp_ucb_exploitation_vs_exploration` | High-reward low-visit beats low-reward high-visit at correct C |
| `test_candidate_vehicles_capacity_filter` | Overloaded vehicle excluded from candidates |
| `test_candidate_vehicles_type_filter` | Wrong vehicle type excluded |
| `test_candidate_vehicles_proximity_sort` | Nearest vehicle appears first |
| `test_run_batch_2_orders_2_vehicles` | Tiny batch: 2 orders, 2 vehicles, 2s budget — returns valid assignment |
| `test_run_batch_committed_orders_locked` | Committed order stays on its vehicle after replanning |
| `test_run_batch_insufficient_fleet` | 3 orders, 2 vehicles — `meta.orders_unassigned == 1` |
| `test_evaluate_cost_decreases_with_shorter_route` | Shorter route produces better (less negative) reward |

No mocking of cost rates or haversine — tests use real imports with synthetic inputs so the full integration path is exercised.

---

## 6. Key Constraints

- Python standard library + pandas + existing logistics modules only — no new dependencies
- Time-budgeted: 30s wall-clock default, configurable via `config.time_budget_seconds`
- Vehicle capacity tracked in `BatchState.vehicle_loads` — no assignment made that would exceed it
- Committed orders from previous batch are locked at root — replanning only touches unassigned orders
- Vehicle start position: last known GPS from Supatrak telematics, not depot
- Batch scope: `origin_requested_start_timestamp_local` within `config.horizon_hours` of run time
- Orders sorted hardest-first (tightest time window) before MCTS begins — ensures difficult constraints are resolved early in the tree
