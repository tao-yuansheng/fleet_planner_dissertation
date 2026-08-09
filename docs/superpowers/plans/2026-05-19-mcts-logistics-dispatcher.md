# MCTS Logistics Dispatcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a two-stage MCTS freight dispatcher that assigns Qargo orders to ZEEFLEET vehicles (Stage 1) and sequences stops with 2-opt (Stage 2), outputting a structured JSON dict for the web UI.

**Architecture:** Depth-N MCTS with immutable `BatchState` frozen dataclasses and SP-UCB selection in `mcts_dispatcher.py`; a standalone nearest-neighbour + 2-opt route sequencer in `route_sequencer.py`. Both files import from existing logistics modules — no reimplementation of cost rates, haversine, or registration normalisation.

**Tech Stack:** Python 3.10+ standard library, existing `profitability_report/profitability_report_merged.py` (haversine, cost rates), `data_audit.py` (_normalise_reg). No new dependencies.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `logistics/route_sequencer.py` | Create | Stop dataclass, nearest-neighbour init, 2-opt improvement, `sequence_routes` public interface |
| `logistics/mcts_dispatcher.py` | Create | BatchState, MCTSNode, SP-UCB, candidate generation, tree ops, reward evaluation, `run_batch` public interface |
| `logistics/tests/test_route_sequencer.py` | Create | 5 unit tests for route sequencer |
| `logistics/tests/test_mcts_dispatcher.py` | Create | 10 unit + integration tests for dispatcher |

All commands run from `E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics\`.

---

## Task 1: Route Sequencer — Stop dataclass + nearest-neighbour

**Files:**
- Create: `logistics/route_sequencer.py`
- Create: `logistics/tests/test_route_sequencer.py`

- [ ] **Step 1: Write the failing test**

```python
# logistics/tests/test_route_sequencer.py
from route_sequencer import Stop, _nearest_neighbour


def test_nearest_neighbour_ordering():
    # Start at lon=-0.5; A(0,0), B(0,1), C(0,2) — greedy picks A then B then C
    stops = [
        Stop(order_id='C', lat=0.0, lon=2.0, stop_type='delivery'),
        Stop(order_id='A', lat=0.0, lon=0.0, stop_type='delivery'),
        Stop(order_id='B', lat=0.0, lon=1.0, stop_type='delivery'),
    ]
    result = _nearest_neighbour(stops, start_lat=0.0, start_lon=-0.5)
    assert [s.order_id for s in result] == ['A', 'B', 'C']
```

- [ ] **Step 2: Run test to verify it fails**

```
cd E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics
python -m pytest tests/test_route_sequencer.py::test_nearest_neighbour_ordering -v
```

Expected: `ModuleNotFoundError: No module named 'route_sequencer'`

- [ ] **Step 3: Create `route_sequencer.py` with Stop dataclass and `_nearest_neighbour`**

```python
# logistics/route_sequencer.py
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dataclasses import dataclass
from profitability_report.profitability_report_merged import _haversine_km


@dataclass
class Stop:
    order_id: str
    lat: float
    lon: float
    stop_type: str  # "pickup" or "delivery"


def _nearest_neighbour(stops: list, start_lat: float, start_lon: float) -> list:
    remaining = list(stops)
    route = []
    cur_lat, cur_lon = start_lat, start_lon
    while remaining:
        nearest = min(remaining, key=lambda s: _haversine_km(cur_lat, cur_lon, s.lat, s.lon))
        route.append(nearest)
        cur_lat, cur_lon = nearest.lat, nearest.lon
        remaining.remove(nearest)
    return route
```

- [ ] **Step 4: Run test to verify it passes**

```
python -m pytest tests/test_route_sequencer.py::test_nearest_neighbour_ordering -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```
git add logistics/route_sequencer.py logistics/tests/test_route_sequencer.py
git commit -m "feat(logistics): add route sequencer Stop dataclass and nearest-neighbour init"
```

---

## Task 2: Route Sequencer — 2-opt + `sequence_routes`

**Files:**
- Modify: `logistics/route_sequencer.py`
- Modify: `logistics/tests/test_route_sequencer.py`

- [ ] **Step 1: Add remaining 4 tests to `test_route_sequencer.py`**

```python
# append to logistics/tests/test_route_sequencer.py
from route_sequencer import Stop, _nearest_neighbour, _two_opt, sequence_routes


def test_two_opt_improves_crossed_route():
    # A(0,0) -> C(0,2) -> B(0,1) -> D(0,3) is crossed — 2-opt should fix to A->B->C->D
    stops = [
        Stop('A', 0.0, 0.0, 'delivery'),
        Stop('C', 0.0, 2.0, 'delivery'),
        Stop('B', 0.0, 1.0, 'delivery'),
        Stop('D', 0.0, 3.0, 'delivery'),
    ]
    result = _two_opt(list(stops))
    assert [s.order_id for s in result] == ['A', 'B', 'C', 'D']


def test_two_opt_already_optimal():
    stops = [
        Stop('A', 0.0, 0.0, 'delivery'),
        Stop('B', 0.0, 1.0, 'delivery'),
        Stop('C', 0.0, 2.0, 'delivery'),
    ]
    result = _two_opt(list(stops))
    assert [s.order_id for s in result] == ['A', 'B', 'C']


def test_sequence_routes_groups_by_vehicle():
    assignments = [('O1', 'V1'), ('O2', 'V1'), ('O3', 'V2'), ('O4', 'V2')]
    orders = {
        'O1': {'dest_lat': 51.5, 'dest_lon': -0.1},
        'O2': {'dest_lat': 51.6, 'dest_lon': -0.2},
        'O3': {'dest_lat': 52.0, 'dest_lon': -1.0},
        'O4': {'dest_lat': 52.1, 'dest_lon': -1.1},
    }
    vehicles = {
        'V1': {'last_lat': 51.4, 'last_lon': -0.05},
        'V2': {'last_lat': 51.9, 'last_lon': -0.9},
    }
    result = sequence_routes(assignments, orders, vehicles)
    assert set(result.keys()) == {'V1', 'V2'}
    assert len(result['V1']['stops']) == 2
    assert len(result['V2']['stops']) == 2


def test_sequence_routes_distance_positive():
    assignments = [('O1', 'V1'), ('O2', 'V1')]
    orders = {
        'O1': {'dest_lat': 51.5, 'dest_lon': -0.1},
        'O2': {'dest_lat': 51.6, 'dest_lon': -0.2},
    }
    vehicles = {'V1': {'last_lat': 51.4, 'last_lon': -0.05}}
    result = sequence_routes(assignments, orders, vehicles)
    assert result['V1']['distance_km'] > 0
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_route_sequencer.py -v
```

Expected: `test_nearest_neighbour_ordering PASSED`, all others `FAILED` with `ImportError: cannot import name '_two_opt'`

- [ ] **Step 3: Add `_two_opt_gain`, `_two_opt`, and `sequence_routes` to `route_sequencer.py`**

```python
# append to logistics/route_sequencer.py

def _two_opt_gain(route: list, i: int, j: int) -> float:
    a, b = route[i - 1], route[i]
    c = route[j - 1]
    d = route[j] if j < len(route) else route[0]
    before = _haversine_km(a.lat, a.lon, b.lat, b.lon) + _haversine_km(c.lat, c.lon, d.lat, d.lon)
    after  = _haversine_km(a.lat, a.lon, c.lat, c.lon) + _haversine_km(b.lat, b.lon, d.lat, d.lon)
    return before - after


def _two_opt(route: list) -> list:
    if len(route) < 3:
        return route
    improved = True
    while improved:
        improved = False
        for i in range(1, len(route) - 1):
            for j in range(i + 1, len(route)):
                if _two_opt_gain(route, i, j) > 0:
                    route[i:j] = route[i:j][::-1]
                    improved = True
    return route


def sequence_routes(
    assignments: list,  # [(order_id, vehicle_id), ...]
    orders: dict,       # order_id -> order dict with dest_lat, dest_lon
    vehicles: dict,     # vehicle_id -> vehicle dict with last_lat, last_lon
) -> dict:              # vehicle_id -> {"stops": [...], "distance_km": float}
    vehicle_orders: dict = {}
    for order_id, vehicle_id in assignments:
        vehicle_orders.setdefault(vehicle_id, []).append(order_id)

    result = {}
    for vehicle_id, order_ids in vehicle_orders.items():
        veh = vehicles[vehicle_id]
        stops = [
            Stop(order_id=oid, lat=orders[oid]['dest_lat'],
                 lon=orders[oid]['dest_lon'], stop_type='delivery')
            for oid in order_ids
        ]
        route = _nearest_neighbour(stops, veh['last_lat'], veh['last_lon'])
        route = _two_opt(route)

        total_km = 0.0
        prev_lat, prev_lon = veh['last_lat'], veh['last_lon']
        for stop in route:
            total_km += _haversine_km(prev_lat, prev_lon, stop.lat, stop.lon)
            prev_lat, prev_lon = stop.lat, stop.lon

        result[vehicle_id] = {
            'stops': [
                {'order_id': s.order_id, 'lat': s.lat, 'lon': s.lon, 'type': s.stop_type}
                for s in route
            ],
            'distance_km': round(total_km, 3),
        }
    return result
```

- [ ] **Step 4: Run all route sequencer tests**

```
python -m pytest tests/test_route_sequencer.py -v
```

Expected: 5 tests `PASSED`

- [ ] **Step 5: Commit**

```
git add logistics/route_sequencer.py logistics/tests/test_route_sequencer.py
git commit -m "feat(logistics): add 2-opt improvement and sequence_routes public interface"
```

---

## Task 3: MCTS — BatchState + MCTSNode dataclasses

**Files:**
- Create: `logistics/mcts_dispatcher.py`
- Create: `logistics/tests/test_mcts_dispatcher.py`

- [ ] **Step 1: Write the failing test**

```python
# logistics/tests/test_mcts_dispatcher.py
from mcts_dispatcher import BatchState, MCTSNode


def test_batch_state_immutable():
    # Frozen dataclass — any attempt to set an attribute raises FrozenInstanceError
    state = BatchState(
        assigned=(('O1', 'V1'),),
        unassigned=frozenset({'O2'}),
        vehicle_loads=(('V1', 1000.0),),
    )
    import pytest
    with pytest.raises(Exception):  # FrozenInstanceError is a subclass of AttributeError
        state.assigned = ()
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/test_mcts_dispatcher.py::test_batch_state_immutable -v
```

Expected: `ModuleNotFoundError: No module named 'mcts_dispatcher'`

- [ ] **Step 3: Create `mcts_dispatcher.py` with BatchState and MCTSNode**

```python
# logistics/mcts_dispatcher.py
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import log, sqrt
from typing import FrozenSet

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from profitability_report.profitability_report_merged import (
    _haversine_km,
    _load_cost_rates,
    _normalise_type_key,
    _rate_bundle,
)
from route_sequencer import sequence_routes

RATES_JSON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'profitability_report',
    'vehicle_cost_rates.json',
)
KM_TO_MILES = 0.621371
DEFAULT_CONFIG = {
    'time_budget_seconds': 30,
    'horizon_hours': 8,
    'exploration_constant': 1.414,
    'sp_ucb_d': 0.1,
    'max_candidates': 8,
}


@dataclass(frozen=True)
class BatchState:
    assigned: tuple        # ((order_id, vehicle_id), ...) — decisions made so far
    unassigned: FrozenSet  # frozenset of order_ids still to place
    vehicle_loads: tuple   # ((vehicle_id, current_kg), ...) — capacity tracking


@dataclass
class MCTSNode:
    state: BatchState
    parent: object         # MCTSNode | None
    action: object         # (order_id, vehicle_id) | None
    children: list = field(default_factory=list)
    visits: int = 0
    total_reward: float = 0.0
    reward_sq_sum: float = 0.0  # for SP-UCB variance term
```

- [ ] **Step 4: Run test to verify it passes**

```
python -m pytest tests/test_mcts_dispatcher.py::test_batch_state_immutable -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```
git add logistics/mcts_dispatcher.py logistics/tests/test_mcts_dispatcher.py
git commit -m "feat(logistics): add BatchState and MCTSNode dataclasses"
```

---

## Task 4: MCTS — SP-UCB + candidate generation

**Files:**
- Modify: `logistics/mcts_dispatcher.py`
- Modify: `logistics/tests/test_mcts_dispatcher.py`

- [ ] **Step 1: Add 5 tests to `test_mcts_dispatcher.py`**

```python
# append to logistics/tests/test_mcts_dispatcher.py
from mcts_dispatcher import BatchState, MCTSNode, _sp_ucb, _candidate_vehicles


def test_sp_ucb_unvisited_returns_inf():
    node = MCTSNode(state=None, parent=None, action=None, visits=0)
    assert _sp_ucb(node, parent_visits=10, C=1.414, D=0.1) == float('inf')


def test_sp_ucb_exploitation_vs_exploration():
    # High-Q, many-visits node vs low-Q, few-visits node
    high_q = MCTSNode(state=None, parent=None, action=None,
                      visits=100, total_reward=90.0, reward_sq_sum=81.0)
    low_q  = MCTSNode(state=None, parent=None, action=None,
                      visits=3,   total_reward=0.6,  reward_sq_sum=0.12)
    # At very low C, exploitation dominates — high_q wins
    assert _sp_ucb(high_q, 103, C=0.01, D=0.001) > _sp_ucb(low_q, 103, C=0.01, D=0.001)
    # At very high C, exploration dominates — low_q wins
    assert _sp_ucb(low_q,  103, C=100.0, D=0.001) > _sp_ucb(high_q, 103, C=100.0, D=0.001)


def test_candidate_vehicles_capacity_filter():
    order = {'order_id': 'O1', 'goods_weight_kg': 10000.0,
             'origin_lat': 51.5, 'origin_lon': -0.1}
    vehicles = {
        'V_small': {'capacity_kg': 5000.0,  'last_lat': 51.5, 'last_lon': -0.1},
        'V_large': {'capacity_kg': 20000.0, 'last_lat': 51.5, 'last_lon': -0.1},
    }
    state = BatchState(assigned=(), unassigned=frozenset({'O1'}), vehicle_loads=())
    result = _candidate_vehicles('O1', {'O1': order}, vehicles, state)
    assert 'V_small' not in result
    assert 'V_large' in result


def test_candidate_vehicles_type_filter():
    # Optional asset_type field on order — when present, vehicles must match
    order = {'order_id': 'O1', 'goods_weight_kg': 100.0,
             'origin_lat': 51.5, 'origin_lon': -0.1, 'asset_type': 'Lorry'}
    vehicles = {
        'V_tractor': {'asset_type': 'Tractor Unit', 'capacity_kg': 5000.0,
                      'last_lat': 51.5, 'last_lon': -0.1},
        'V_lorry':   {'asset_type': 'Lorry',         'capacity_kg': 5000.0,
                      'last_lat': 51.6, 'last_lon': -0.2},
    }
    state = BatchState(assigned=(), unassigned=frozenset({'O1'}), vehicle_loads=())
    result = _candidate_vehicles('O1', {'O1': order}, vehicles, state)
    assert 'V_tractor' not in result
    assert 'V_lorry' in result


def test_candidate_vehicles_proximity_sort():
    order = {'order_id': 'O1', 'goods_weight_kg': 100.0,
             'origin_lat': 51.5, 'origin_lon': 0.0}
    vehicles = {
        'V_far':  {'capacity_kg': 5000.0, 'last_lat': 52.5, 'last_lon': 0.0},
        'V_near': {'capacity_kg': 5000.0, 'last_lat': 51.5, 'last_lon': 0.1},
    }
    state = BatchState(assigned=(), unassigned=frozenset({'O1'}), vehicle_loads=())
    result = _candidate_vehicles('O1', {'O1': order}, vehicles, state, max_candidates=8)
    assert result[0] == 'V_near'
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_mcts_dispatcher.py -v
```

Expected: `test_batch_state_immutable PASSED`, all new tests `FAILED` with `ImportError`

- [ ] **Step 3: Add `_sp_ucb` and `_candidate_vehicles` to `mcts_dispatcher.py`**

```python
# append to logistics/mcts_dispatcher.py

def _sp_ucb(node: MCTSNode, parent_visits: int, C: float, D: float) -> float:
    if node.visits == 0:
        return float('inf')
    Q = node.total_reward / node.visits
    variance = max(0.0, (node.reward_sq_sum / node.visits) - Q ** 2)
    return Q + C * sqrt(log(parent_visits) / node.visits) + sqrt((variance + D) / node.visits)


def _candidate_vehicles(
    order_id: str,
    orders: dict,
    vehicles: dict,
    state: BatchState,
    max_candidates: int = 8,
) -> list:
    order = orders[order_id]
    loads = dict(state.vehicle_loads)
    candidates = []
    for vid, veh in vehicles.items():
        capacity = veh.get('capacity_kg', float('inf'))
        current_load = loads.get(vid, 0.0)
        if current_load + order['goods_weight_kg'] > capacity:
            continue
        # Optional type filter — only applied when order specifies a required asset_type
        if order.get('asset_type') and veh.get('asset_type') != order['asset_type']:
            continue
        dist = _haversine_km(
            veh['last_lat'], veh['last_lon'],
            order['origin_lat'], order['origin_lon'],
        )
        candidates.append((dist, vid))
    candidates.sort()
    return [vid for _, vid in candidates[:max_candidates]]
```

- [ ] **Step 4: Run all tests so far**

```
python -m pytest tests/test_mcts_dispatcher.py -v
```

Expected: 6 tests `PASSED`

- [ ] **Step 5: Commit**

```
git add logistics/mcts_dispatcher.py logistics/tests/test_mcts_dispatcher.py
git commit -m "feat(logistics): add SP-UCB selection and candidate vehicle generation"
```

---

## Task 5: MCTS — tree operations (select, expand, simulate, backpropagate)

**Files:**
- Modify: `logistics/mcts_dispatcher.py`

No new tests in this task — these are internal functions exercised by the integration tests in Task 7.

- [ ] **Step 1: Add helper functions `_next_order` and `_apply_action`**

```python
# append to logistics/mcts_dispatcher.py

def _next_order(state: BatchState, order_sequence: list):
    """Return the first order in order_sequence that is still unassigned."""
    for oid in order_sequence:
        if oid in state.unassigned:
            return oid
    return None


def _apply_action(state: BatchState, action: tuple, orders: dict) -> BatchState:
    order_id, vehicle_id = action
    new_assigned = state.assigned + (action,)
    new_unassigned = state.unassigned - {order_id}
    if vehicle_id is None:
        new_loads = state.vehicle_loads
    else:
        loads = dict(state.vehicle_loads)
        loads[vehicle_id] = loads.get(vehicle_id, 0.0) + orders[order_id]['goods_weight_kg']
        new_loads = tuple(sorted(loads.items()))
    return BatchState(
        assigned=new_assigned,
        unassigned=new_unassigned,
        vehicle_loads=new_loads,
    )
```

- [ ] **Step 2: Add `_select`, `_expand`, `_simulate`, `_backpropagate`, `_best_assignment`**

```python
# append to logistics/mcts_dispatcher.py

def _select(
    root: MCTSNode,
    order_sequence: list,
    orders: dict,
    vehicles: dict,
    C: float,
    D: float,
    max_candidates: int,
) -> MCTSNode:
    node = root
    while node.state.unassigned:
        order_id = _next_order(node.state, order_sequence)
        if order_id is None:
            break
        candidates = _candidate_vehicles(order_id, orders, vehicles, node.state, max_candidates)
        tried = {child.action for child in node.children}
        untried = [vid for vid in candidates if (order_id, vid) not in tried]
        skip_tried = (order_id, None) in tried
        # Return this node if there are unexplored actions
        if untried or (not candidates and not skip_tried):
            return node
        if not node.children:
            return node
        node = max(node.children, key=lambda c: _sp_ucb(c, node.visits, C, D))
    return node


def _expand(
    node: MCTSNode,
    order_sequence: list,
    orders: dict,
    vehicles: dict,
    max_candidates: int,
) -> MCTSNode:
    order_id = _next_order(node.state, order_sequence)
    candidates = _candidate_vehicles(order_id, orders, vehicles, node.state, max_candidates)
    tried = {child.action for child in node.children}
    untried = [vid for vid in candidates if (order_id, vid) not in tried]
    action = (order_id, untried[0]) if untried else (order_id, None)
    new_state = _apply_action(node.state, action, orders)
    child = MCTSNode(state=new_state, parent=node, action=action)
    node.children.append(child)
    return child


def _simulate(
    node: MCTSNode,
    order_sequence: list,
    orders: dict,
    vehicles: dict,
    cost_rates: dict,
    max_candidates: int,
) -> float:
    state = node.state
    for order_id in order_sequence:
        if order_id not in state.unassigned:
            continue
        candidates = _candidate_vehicles(order_id, orders, vehicles, state, max_candidates)
        if not candidates:
            # No compatible vehicle — leave this order unassigned
            state = BatchState(
                assigned=state.assigned,
                unassigned=state.unassigned - {order_id},
                vehicle_loads=state.vehicle_loads,
            )
        else:
            state = _apply_action(state, (order_id, candidates[0]), orders)
    return _evaluate(state, orders, vehicles, cost_rates)


def _backpropagate(node: MCTSNode, reward: float) -> None:
    while node is not None:
        node.visits += 1
        node.total_reward += reward
        node.reward_sq_sum += reward * reward
        node = node.parent


def _best_assignment(root: MCTSNode) -> MCTSNode:
    if not root.children:
        return root
    return max(root.children, key=lambda c: c.visits)
```

- [ ] **Step 3: Verify existing tests still pass**

```
python -m pytest tests/test_mcts_dispatcher.py -v
```

Expected: 6 tests `PASSED` (no regressions)

- [ ] **Step 4: Commit**

```
git add logistics/mcts_dispatcher.py
git commit -m "feat(logistics): add MCTS tree operations: select, expand, simulate, backpropagate"
```

---

## Task 6: MCTS — reward evaluation

**Files:**
- Modify: `logistics/mcts_dispatcher.py`
- Modify: `logistics/tests/test_mcts_dispatcher.py`

- [ ] **Step 1: Add `test_evaluate_cost_decreases_with_shorter_route` to the test file**

```python
# append to logistics/tests/test_mcts_dispatcher.py
import os
from mcts_dispatcher import BatchState, _evaluate, _load_cost_rates


def test_evaluate_cost_decreases_with_shorter_route():
    orders = {
        'O_near': {'dest_lat': 51.5, 'dest_lon': 0.0, 'goods_weight_kg': 100.0},
        'O_far':  {'dest_lat': 51.5, 'dest_lon': 1.0, 'goods_weight_kg': 100.0},
    }
    vehicles = {
        'V1': {'asset_type': 'Lorry', 'last_lat': 51.5, 'last_lon': 0.0, 'capacity_kg': 5000.0},
    }
    rates_path = os.path.join(
        os.path.dirname(__file__), '..', 'profitability_report', 'vehicle_cost_rates.json'
    )
    cost_rates = _load_cost_rates(rates_path)

    # V1 starts at (51.5, 0.0); O_near is at same position (distance ~0km)
    short_state = BatchState(
        assigned=(('O_near', 'V1'),),
        unassigned=frozenset(),
        vehicle_loads=(('V1', 100.0),),
    )
    # V1 starts at (51.5, 0.0); O_far is at (51.5, 1.0) — ~78km away
    long_state = BatchState(
        assigned=(('O_far', 'V1'),),
        unassigned=frozenset(),
        vehicle_loads=(('V1', 100.0),),
    )

    reward_short = _evaluate(short_state, orders, vehicles, cost_rates)
    reward_long  = _evaluate(long_state,  orders, vehicles, cost_rates)

    # Shorter route costs less → higher reward (less negative)
    assert reward_short > reward_long
```

- [ ] **Step 2: Run to verify it fails**

```
python -m pytest tests/test_mcts_dispatcher.py::test_evaluate_cost_decreases_with_shorter_route -v
```

Expected: `FAILED` with `ImportError: cannot import name '_evaluate'`

- [ ] **Step 3: Add `_group_stops`, `_route_distance_km`, and `_evaluate` to `mcts_dispatcher.py`**

```python
# append to logistics/mcts_dispatcher.py

def _group_stops(state: BatchState) -> dict:
    """Return {vehicle_id: [order_id, ...]} for all non-skipped assignments."""
    result: dict = {}
    for order_id, vehicle_id in state.assigned:
        if vehicle_id is not None:
            result.setdefault(vehicle_id, []).append(order_id)
    return result


def _route_distance_km(
    start_lat: float, start_lon: float, stops: list
) -> float:
    """Total km from start position through each (lat, lon) stop in sequence."""
    total = 0.0
    prev_lat, prev_lon = start_lat, start_lon
    for lat, lon in stops:
        total += _haversine_km(prev_lat, prev_lon, lat, lon)
        prev_lat, prev_lon = lat, lon
    return total


def _evaluate(
    state: BatchState,
    orders: dict,
    vehicles: dict,
    cost_rates: dict,
) -> float:
    grouped = _group_stops(state)
    total_cost = 0.0
    for vehicle_id, order_ids in grouped.items():
        veh = vehicles[vehicle_id]
        asset_type = veh.get('asset_type', 'default')
        rate_key = _normalise_type_key(asset_type)
        rates = _rate_bundle(cost_rates, rate_key)
        stops = [(orders[oid]['dest_lat'], orders[oid]['dest_lon']) for oid in order_ids]
        route_km = _route_distance_km(veh['last_lat'], veh['last_lon'], stops)
        route_miles = route_km * KM_TO_MILES
        total_cost += (rates['fuel_gbp_per_mile'] + rates['driver_mileage_gbp_per_mile']) * route_miles
    # Revenue term omitted — Qargo revenue coverage ~46%, insufficient for optimisation.
    # Uncomment and test when revenue data coverage improves beyond ~80%:
    # total_cost -= sum(orders[oid].get('revenue_gbp', 0.0) for oid, _ in state.assigned)
    return -total_cost  # MCTS maximises; cost minimises, so negate
```

- [ ] **Step 4: Run all tests**

```
python -m pytest tests/test_mcts_dispatcher.py -v
```

Expected: 7 tests `PASSED`

- [ ] **Step 5: Commit**

```
git add logistics/mcts_dispatcher.py logistics/tests/test_mcts_dispatcher.py
git commit -m "feat(logistics): add reward evaluation function with commented revenue hook"
```

---

## Task 7: MCTS — `_mcts_search` + `run_batch` public interface

**Files:**
- Modify: `logistics/mcts_dispatcher.py`
- Modify: `logistics/tests/test_mcts_dispatcher.py`

- [ ] **Step 1: Add 3 integration tests**

```python
# append to logistics/tests/test_mcts_dispatcher.py
from mcts_dispatcher import run_batch


def test_run_batch_2_orders_2_vehicles():
    batch_input = {
        'orders': [
            {
                'order_id': 'O1', 'origin_lat': 51.5, 'origin_lon': -0.1,
                'dest_lat': 51.6, 'dest_lon': -0.2, 'goods_weight_kg': 1000.0,
                'time_window_start': '2026-01-05T09:00:00+00:00',
                'time_window_end':   '2026-01-05T17:00:00+00:00',
            },
            {
                'order_id': 'O2', 'origin_lat': 51.7, 'origin_lon': -0.3,
                'dest_lat': 51.8, 'dest_lon': -0.4, 'goods_weight_kg': 500.0,
                'time_window_start': '2026-01-05T09:00:00+00:00',
                'time_window_end':   '2026-01-05T17:00:00+00:00',
            },
        ],
        'vehicles': [
            {'vehicle_id': 'V1', 'registration': 'AB12CDE', 'asset_type': 'Lorry',
             'capacity_kg': 5000.0, 'last_lat': 51.5, 'last_lon': -0.1},
            {'vehicle_id': 'V2', 'registration': 'FG34HIJ', 'asset_type': 'Lorry',
             'capacity_kg': 5000.0, 'last_lat': 51.7, 'last_lon': -0.3},
        ],
        'committed_assignments': [],
        'config': {
            'time_budget_seconds': 2, 'horizon_hours': 24,
            'exploration_constant': 1.414, 'sp_ucb_d': 0.1,
        },
    }
    result = run_batch(batch_input)
    assigned_ids = {a['order_id'] for a in result['assignments']}
    assert 'O1' in assigned_ids
    assert 'O2' in assigned_ids
    assert result['meta']['orders_unassigned'] == 0
    assert len(result['routes']) > 0
    for vid, route in result['routes'].items():
        assert route['estimated_cost_gbp'] >= 0
        assert route['total_distance_km'] >= 0


def test_run_batch_committed_orders_locked():
    batch_input = {
        'orders': [
            {
                'order_id': 'O1', 'origin_lat': 51.5, 'origin_lon': -0.1,
                'dest_lat': 51.6, 'dest_lon': -0.2, 'goods_weight_kg': 100.0,
                'time_window_start': '2026-01-05T09:00:00+00:00',
                'time_window_end':   '2026-01-05T17:00:00+00:00',
            },
            {
                'order_id': 'O2', 'origin_lat': 51.7, 'origin_lon': -0.3,
                'dest_lat': 51.8, 'dest_lon': -0.4, 'goods_weight_kg': 100.0,
                'time_window_start': '2026-01-05T09:00:00+00:00',
                'time_window_end':   '2026-01-05T17:00:00+00:00',
            },
        ],
        'vehicles': [
            {'vehicle_id': 'V1', 'registration': 'AB12CDE', 'asset_type': 'Lorry',
             'capacity_kg': 5000.0, 'last_lat': 51.5, 'last_lon': -0.1},
            {'vehicle_id': 'V2', 'registration': 'FG34HIJ', 'asset_type': 'Lorry',
             'capacity_kg': 5000.0, 'last_lat': 51.7, 'last_lon': -0.3},
        ],
        'committed_assignments': [{'order_id': 'O1', 'vehicle_id': 'V1'}],
        'config': {
            'time_budget_seconds': 2, 'horizon_hours': 24,
            'exploration_constant': 1.414, 'sp_ucb_d': 0.1,
        },
    }
    result = run_batch(batch_input)
    o1_assignment = next(a for a in result['assignments'] if a['order_id'] == 'O1')
    assert o1_assignment['vehicle_id'] == 'V1'


def test_run_batch_insufficient_fleet():
    # 3 orders at 3000kg each; each vehicle holds 5000kg max → only 1 order per vehicle
    batch_input = {
        'orders': [
            {
                'order_id': f'O{i}', 'origin_lat': 51.5 + i * 0.1, 'origin_lon': -0.1,
                'dest_lat': 51.6 + i * 0.1, 'dest_lon': -0.2, 'goods_weight_kg': 3000.0,
                'time_window_start': '2026-01-05T09:00:00+00:00',
                'time_window_end':   '2026-01-05T17:00:00+00:00',
            }
            for i in range(3)
        ],
        'vehicles': [
            {'vehicle_id': 'V1', 'registration': 'AB12CDE', 'asset_type': 'Lorry',
             'capacity_kg': 5000.0, 'last_lat': 51.5, 'last_lon': -0.1},
            {'vehicle_id': 'V2', 'registration': 'FG34HIJ', 'asset_type': 'Lorry',
             'capacity_kg': 5000.0, 'last_lat': 51.7, 'last_lon': -0.3},
        ],
        'committed_assignments': [],
        'config': {
            'time_budget_seconds': 2, 'horizon_hours': 24,
            'exploration_constant': 1.414, 'sp_ucb_d': 0.1,
        },
    }
    result = run_batch(batch_input)
    assert result['meta']['orders_unassigned'] == 1
```

- [ ] **Step 2: Run to verify they fail**

```
python -m pytest tests/test_mcts_dispatcher.py::test_run_batch_2_orders_2_vehicles -v
```

Expected: `FAILED` with `ImportError: cannot import name 'run_batch'`

- [ ] **Step 3: Add `_mcts_search` and `run_batch` to `mcts_dispatcher.py`**

```python
# append to logistics/mcts_dispatcher.py

def _mcts_search(
    root: MCTSNode,
    order_sequence: list,
    orders: dict,
    vehicles: dict,
    cost_rates: dict,
    time_budget_s: float,
    C: float,
    D: float,
    max_candidates: int,
) -> tuple:  # (best_node: MCTSNode, iterations: int)
    deadline = time.monotonic() + time_budget_s
    iterations = 0
    while time.monotonic() < deadline:
        node = _select(root, order_sequence, orders, vehicles, C, D, max_candidates)
        if not node.state.unassigned:
            reward = _evaluate(node.state, orders, vehicles, cost_rates)
            _backpropagate(node, reward)
        else:
            child = _expand(node, order_sequence, orders, vehicles, max_candidates)
            if not child.state.unassigned:
                reward = _evaluate(child.state, orders, vehicles, cost_rates)
            else:
                reward = _simulate(child, order_sequence, orders, vehicles, cost_rates, max_candidates)
            _backpropagate(child, reward)
        iterations += 1
    return _best_assignment(root), iterations


def run_batch(batch_input: dict) -> dict:
    cfg = {**DEFAULT_CONFIG, **(batch_input.get('config') or {})}

    raw_orders   = batch_input['orders']
    raw_vehicles = batch_input['vehicles']
    committed    = batch_input.get('committed_assignments', [])

    orders   = {o['order_id']: o for o in raw_orders}
    vehicles = {v['vehicle_id']: v for v in raw_vehicles}

    # Filter orders by horizon
    now = datetime.now(timezone.utc)
    horizon_h = cfg['horizon_hours']
    active_order_ids = []
    for oid, o in orders.items():
        try:
            ts = datetime.fromisoformat(o['time_window_start'].replace('Z', '+00:00'))
            if (ts - now).total_seconds() / 3600 <= horizon_h:
                active_order_ids.append(oid)
        except (KeyError, ValueError):
            active_order_ids.append(oid)

    committed_order_ids = {ca['order_id'] for ca in committed}

    # Sort unassigned orders hardest-first (tightest time window)
    def _window_width(oid: str) -> float:
        o = orders[oid]
        try:
            start = datetime.fromisoformat(o['time_window_start'].replace('Z', '+00:00'))
            end   = datetime.fromisoformat(o['time_window_end'].replace('Z', '+00:00'))
            return (end - start).total_seconds()
        except (KeyError, ValueError):
            return float('inf')

    unassigned_ids = [oid for oid in active_order_ids if oid not in committed_order_ids]
    order_sequence = sorted(unassigned_ids, key=_window_width)

    # Build initial state from committed assignments
    initial_loads: dict = {}
    valid_committed = [
        ca for ca in committed
        if ca['order_id'] in orders and ca['vehicle_id'] in vehicles
    ]
    for ca in valid_committed:
        vid = ca['vehicle_id']
        oid = ca['order_id']
        initial_loads[vid] = initial_loads.get(vid, 0.0) + orders[oid]['goods_weight_kg']

    root_state = BatchState(
        assigned=tuple((ca['order_id'], ca['vehicle_id']) for ca in valid_committed),
        unassigned=frozenset(order_sequence),
        vehicle_loads=tuple(sorted(initial_loads.items())),
    )
    root = MCTSNode(state=root_state, parent=None, action=None)

    cost_rates = _load_cost_rates(RATES_JSON_PATH)

    start_time = time.monotonic()
    best_node, iterations = _mcts_search(
        root, order_sequence, orders, vehicles, cost_rates,
        cfg['time_budget_seconds'],
        cfg['exploration_constant'],
        cfg['sp_ucb_d'],
        cfg.get('max_candidates', 8),
    )
    elapsed = time.monotonic() - start_time

    best_state = best_node.state
    all_assignments = list(best_state.assigned)
    orders_unassigned = len(best_state.unassigned)

    routes = sequence_routes(
        [(oid, vid) for oid, vid in all_assignments if vid is not None],
        orders,
        vehicles,
    )

    for vid, route_data in routes.items():
        asset_type = vehicles[vid].get('asset_type', 'default')
        rate_key = _normalise_type_key(asset_type)
        rates = _rate_bundle(cost_rates, rate_key)
        dist_miles = route_data['distance_km'] * KM_TO_MILES
        route_data['estimated_cost_gbp'] = round(
            (rates['fuel_gbp_per_mile'] + rates['driver_mileage_gbp_per_mile']) * dist_miles, 2
        )
        route_data['total_distance_km'] = route_data.pop('distance_km')

    return {
        'assignments': [
            {'order_id': oid, 'vehicle_id': vid}
            for oid, vid in all_assignments
            if vid is not None
        ],
        'routes': routes,
        'meta': {
            'mcts_iterations': iterations,
            'elapsed_seconds': round(elapsed, 2),
            'orders_unassigned': orders_unassigned,
        },
    }
```

- [ ] **Step 4: Run all tests**

```
python -m pytest tests/test_mcts_dispatcher.py tests/test_route_sequencer.py -v
```

Expected: 15 tests `PASSED`

- [ ] **Step 5: Run full test suite including data_audit tests (regression check)**

```
python -m pytest tests/ -v
```

Expected: all tests `PASSED`

- [ ] **Step 6: Commit**

```
git add logistics/mcts_dispatcher.py logistics/tests/test_mcts_dispatcher.py
git commit -m "feat(logistics): add MCTS search loop and run_batch public interface"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Covered by |
|---|---|
| Architecture — two files, imports from existing modules | Task 3 (`mcts_dispatcher.py` file created with imports) |
| BatchState frozen dataclass | Task 3 |
| MCTSNode with reward_sq_sum | Task 3 |
| Input schema (orders, vehicles, committed_assignments, config) | Task 7 `run_batch` |
| Output schema (assignments, routes, meta) | Task 7 `run_batch` |
| SP-UCB formula with variance term | Task 4 `_sp_ucb` |
| Candidate generation: capacity + type + proximity | Task 4 `_candidate_vehicles` |
| MCTS iteration: select → expand → simulate → backprop | Task 5 |
| Reward = -route_cost, revenue commented out | Task 6 `_evaluate` |
| Time-budgeted search (wall clock) | Task 7 `_mcts_search` |
| Committed orders locked at root | Task 7 `run_batch` |
| Orders sorted by window tightness | Task 7 `run_batch` |
| Stage 2: nearest-neighbour → 2-opt | Tasks 1–2 |
| `sequence_routes` called from `run_batch` | Task 7 `run_batch` |
| `meta.orders_unassigned` | Task 7 `run_batch` |
| All 10 MCTS tests | Tasks 3–7 |
| All 5 route sequencer tests | Tasks 1–2 |
