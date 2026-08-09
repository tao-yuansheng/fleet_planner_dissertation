# PDPTW MCTS Dispatcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the ZEEFLEET dispatcher around a single Pickup-and-Delivery cost engine so MCTS optimizes the true driven cost (pickup + delivery legs, load-on-board, deadlines), replacing the current destination-only two-step system.

**Architecture:** A solver-agnostic PDP route engine (`pdp_route.py`) provides `Route`, `try_insert`, and `route_cost` — the single source of cost truth. MCTS (`mcts_dispatcher.py`) decides which vehicle each order goes to, scoring moves by the engine's marginal insertion cost; rollouts complete via greedy cheapest-insertion. A rolling wrapper (`run_daily_batch.py`) projects vehicle state forward and locks committed work. 2-opt becomes a final polish.

**Tech Stack:** Python 3, pytest, pandas, pgeocode. No new third-party dependencies.

**Spec:** `docs/superpowers/specs/2026-05-20-pdptw-mcts-dispatcher-design.md`

---

## Conventions for every task

- **Working directory:** `E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics`
- **Python executable:** `E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe`
- **Run a test (PowerShell):**
  ```powershell
  & "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/<file>::<test> -v
  ```
- **Git note:** This folder is **not** currently a git repo. If `git status` errors, run `git init` once before the first commit, or skip the commit steps and rely on the reviewer checkpoints. Commit messages below assume a repo exists.
- **Imports already available** from `profitability_report.profitability_report_merged`:
  - `_haversine_km(lat1, lon1, lat2, lon2) -> float`
  - `_load_cost_rates(path) -> dict`
  - `_normalise_type_key(asset_type) -> str`
  - `_rate_bundle(cost_rates, key) -> dict` with keys `fuel_gbp_per_mile`, `driver_mileage_gbp_per_mile`

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `pdp_route.py` | **Create** | `Stop`, `Route`, `InsertionResult`; distance/cost/load/time helpers; `try_insert`; `cheapest_insertion` |
| `mcts_dispatcher.py` | **Rewrite core** | `BatchState` carrying route snapshots; MCTS phases driven by the engine; output contract |
| `route_sequencer.py` | **Retain + extend** | Keep `_two_opt`; add `polish_route_stops` used as final cleanup |
| `run_daily_batch.py` | **Modify** | `_build_orders` carries pallets + real deadline; project committed vehicle position + remaining capacity |
| `simulation/greedy.py` | **Rewrite** | Greedy baseline becomes cheapest-insertion on the same engine |
| `simulation/data_loader.py` | **Verify only** | Already emits `goods_pallet_spaces`, `time_window_end`; no change expected |
| `tests/test_pdp_route.py` | **Create** | Engine unit tests |
| `tests/test_mcts_dispatcher.py` | **Update** | New state shape + behavior tests |
| `tests/test_run_daily_batch.py` | **Create** | Committed-position projection tests |

---

## Order & vehicle field contract (used throughout)

An **order** dict has: `order_id`, `origin_lat`, `origin_lon`, `dest_lat`, `dest_lon`, `goods_weight_kg`, `goods_pallet_spaces` (int ≥ 1), `time_window_start` (ISO str), `time_window_end` (ISO str — the hard delivery deadline).

A **vehicle** dict has: `vehicle_id`, `asset_type`, `capacity_kg`, `capacity_pallets`, `last_lat`, `last_lon`.

---

## Task 1: PDP route engine — `Stop`, `Route`, distance & cost

**Files:**
- Create: `pdp_route.py`
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pdp_route.py
import os
from datetime import datetime, timezone
from pdp_route import Stop, Route, route_distance_km, route_cost
from profitability_report.profitability_report_merged import _load_cost_rates

RATES_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'profitability_report', 'vehicle_cost_rates.json'
)


def test_route_distance_sums_legs_through_stops():
    stops = [
        Stop(order_id='O1', lat=51.5, lon=0.0, stop_type='pickup'),
        Stop(order_id='O1', lat=51.5, lon=1.0, stop_type='delivery'),
    ]
    # start (51.5, 0.0) -> pickup same point (~0) -> delivery (51.5,1.0) (~69km)
    km = route_distance_km(51.5, 0.0, stops)
    assert 60.0 < km < 80.0


def test_route_cost_uses_asset_rate():
    cost_rates = _load_cost_rates(RATES_PATH)
    route = Route(
        vehicle_id='V1', start_lat=51.5, start_lon=0.0,
        capacity_kg=5000.0, capacity_pallets=12, asset_type='Lorry',
        start_time=datetime(2026, 1, 5, 9, tzinfo=timezone.utc),
        stops=[Stop('O1', 51.5, 0.0, 'pickup'), Stop('O1', 51.5, 1.0, 'delivery')],
    )
    cost = route_cost(route, cost_rates)
    # Lorry: (0.33 + 0.60)/mile; ~69km -> ~43mi -> ~40 GBP
    assert 30.0 < cost < 55.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_pdp_route.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pdp_route'`

- [ ] **Step 3: Write minimal implementation**

```python
# pdp_route.py
"""
Pickup-and-Delivery route engine — the single source of cost truth.

A Route is one vehicle's plan: an ordered list of pickup/delivery Stops
starting from the vehicle's current (or projected) position. Distance, cost,
load-on-board and arrival times are all computed here so that search,
rollout, and reporting share one consistent model.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from profitability_report.profitability_report_merged import (
    _haversine_km,
    _normalise_type_key,
    _rate_bundle,
)

KM_TO_MILES = 0.621371
AVG_SPEED_KMH = 50.0  # flat road-speed assumption for arrival-time estimates


@dataclass
class Stop:
    order_id: str
    lat: float
    lon: float
    stop_type: str  # 'pickup' or 'delivery'


@dataclass
class Route:
    vehicle_id: str
    start_lat: float
    start_lon: float
    capacity_kg: float
    capacity_pallets: float
    asset_type: str
    start_time: datetime
    stops: list = field(default_factory=list)


def route_distance_km(start_lat: float, start_lon: float, stops: list) -> float:
    total = 0.0
    prev_lat, prev_lon = start_lat, start_lon
    for stop in stops:
        total += _haversine_km(prev_lat, prev_lon, stop.lat, stop.lon)
        prev_lat, prev_lon = stop.lat, stop.lon
    return total


def route_cost(route: Route, cost_rates: dict) -> float:
    km = route_distance_km(route.start_lat, route.start_lon, route.stops)
    miles = km * KM_TO_MILES
    rates = _rate_bundle(cost_rates, _normalise_type_key(route.asset_type))
    return (rates['fuel_gbp_per_mile'] + rates['driver_mileage_gbp_per_mile']) * miles
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_pdp_route.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add pdp_route.py tests/test_pdp_route.py
git commit -m "feat: add PDP route engine with distance and cost"
```

---

## Task 2: Load-on-board tracking + capacity feasibility

**Files:**
- Modify: `pdp_route.py`
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_pdp_route.py
from pdp_route import load_profile, feasible_load


def _orders():
    return {
        'O1': {'goods_weight_kg': 3000.0, 'goods_pallet_spaces': 6},
        'O2': {'goods_weight_kg': 3000.0, 'goods_pallet_spaces': 6},
    }


def test_load_profile_rises_at_pickup_falls_at_delivery():
    stops = [
        Stop('O1', 51.5, 0.0, 'pickup'),
        Stop('O1', 51.6, 0.0, 'delivery'),
    ]
    profile = load_profile(stops, _orders())
    assert profile == [(3000.0, 6), (0.0, 0)]


def test_feasible_load_true_when_within_capacity():
    stops = [
        Stop('O1', 51.5, 0.0, 'pickup'),
        Stop('O1', 51.6, 0.0, 'delivery'),
    ]
    assert feasible_load(stops, _orders(), capacity_kg=5000.0, capacity_pallets=12) is True


def test_feasible_load_false_when_two_loads_overlap_over_capacity():
    # Both picked up before either delivered -> 6000kg / 12 pallets on board at once
    stops = [
        Stop('O1', 51.5, 0.0, 'pickup'),
        Stop('O2', 51.5, 0.0, 'pickup'),
        Stop('O1', 51.6, 0.0, 'delivery'),
        Stop('O2', 51.6, 0.0, 'delivery'),
    ]
    # 6000kg exceeds 5000kg capacity
    assert feasible_load(stops, _orders(), capacity_kg=5000.0, capacity_pallets=12) is False
    # But a bigger truck (8000kg, 14 pallets) can carry both at once
    assert feasible_load(stops, _orders(), capacity_kg=8000.0, capacity_pallets=14) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_pdp_route.py -k load -v`
Expected: FAIL with `ImportError: cannot import name 'load_profile'`

- [ ] **Step 3: Write minimal implementation**

```python
# Append to pdp_route.py
def load_profile(stops: list, orders: dict) -> list:
    """Return [(kg_on_board, pallets_on_board), ...] after each stop in order."""
    profile = []
    kg = 0.0
    pallets = 0
    for stop in stops:
        order = orders[stop.order_id]
        if stop.stop_type == 'pickup':
            kg += order['goods_weight_kg']
            pallets += order.get('goods_pallet_spaces', 0)
        else:  # delivery
            kg -= order['goods_weight_kg']
            pallets -= order.get('goods_pallet_spaces', 0)
        profile.append((kg, pallets))
    return profile


def feasible_load(stops: list, orders: dict,
                  capacity_kg: float, capacity_pallets: float) -> bool:
    """True if load on board never exceeds capacity at any stop."""
    for kg, pallets in load_profile(stops, orders):
        if kg > capacity_kg + 1e-6:
            return False
        if pallets > capacity_pallets + 1e-6:
            return False
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_pdp_route.py -k load -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add pdp_route.py tests/test_pdp_route.py
git commit -m "feat: add load-on-board tracking and capacity feasibility"
```

---

## Task 3: Arrival times + deadline feasibility

**Files:**
- Modify: `pdp_route.py`
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_pdp_route.py
from datetime import datetime, timezone
from pdp_route import arrival_times, feasible_deadlines


def test_arrival_times_advance_by_distance_over_speed():
    start = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    stops = [Stop('O1', 51.5, 1.0, 'delivery')]  # ~69km from (51.5, 0.0)
    arrivals = arrival_times(51.5, 0.0, stops, start)
    # ~69km at 50km/h -> ~1.38h -> arrival ~10:23
    delta_h = (arrivals[0] - start).total_seconds() / 3600
    assert 1.2 < delta_h < 1.6


def test_feasible_deadlines_rejects_late_delivery():
    start = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    orders = {'O1': {'time_window_end': '2026-01-05T09:30:00+00:00'}}  # too soon
    stops = [Stop('O1', 51.5, 0.0, 'pickup'), Stop('O1', 51.5, 1.0, 'delivery')]
    assert feasible_deadlines(51.5, 0.0, stops, start, orders) is False


def test_feasible_deadlines_accepts_on_time_delivery():
    start = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    orders = {'O1': {'time_window_end': '2026-01-05T17:00:00+00:00'}}
    stops = [Stop('O1', 51.5, 0.0, 'pickup'), Stop('O1', 51.5, 1.0, 'delivery')]
    assert feasible_deadlines(51.5, 0.0, stops, start, orders) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_pdp_route.py -k deadline -v`
Expected: FAIL with `ImportError: cannot import name 'arrival_times'`

- [ ] **Step 3: Write minimal implementation**

```python
# Append to pdp_route.py
def arrival_times(start_lat: float, start_lon: float,
                  stops: list, start_time: datetime) -> list:
    """Return estimated arrival datetime at each stop, using AVG_SPEED_KMH."""
    arrivals = []
    cum_km = 0.0
    prev_lat, prev_lon = start_lat, start_lon
    for stop in stops:
        cum_km += _haversine_km(prev_lat, prev_lon, stop.lat, stop.lon)
        hours = cum_km / AVG_SPEED_KMH
        arrivals.append(start_time + timedelta(hours=hours))
        prev_lat, prev_lon = stop.lat, stop.lon
    return arrivals


def feasible_deadlines(start_lat: float, start_lon: float, stops: list,
                       start_time: datetime, orders: dict) -> bool:
    """True if every delivery arrives at or before its order's deadline."""
    arrivals = arrival_times(start_lat, start_lon, stops, start_time)
    for stop, arrival in zip(stops, arrivals):
        if stop.stop_type != 'delivery':
            continue
        deadline_str = orders[stop.order_id].get('time_window_end')
        if not deadline_str:
            continue
        deadline = datetime.fromisoformat(deadline_str.replace('Z', '+00:00'))
        if arrival > deadline:
            return False
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_pdp_route.py -k deadline -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add pdp_route.py tests/test_pdp_route.py
git commit -m "feat: add arrival-time estimation and deadline feasibility"
```

---

## Task 4: `try_insert` — cheapest feasible placement of a pickup+delivery pair

**Files:**
- Modify: `pdp_route.py`
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_pdp_route.py
from pdp_route import InsertionResult, try_insert


def test_try_insert_into_empty_route_appends_pickup_then_delivery():
    cost_rates = _load_cost_rates(RATES_PATH)
    route = Route('V1', 51.5, 0.0, 5000.0, 12, 'Lorry',
                  datetime(2026, 1, 5, 9, tzinfo=timezone.utc), stops=[])
    orders = {'O1': {'origin_lat': 51.5, 'origin_lon': 0.5,
                     'dest_lat': 51.5, 'dest_lon': 1.0,
                     'goods_weight_kg': 1000.0, 'goods_pallet_spaces': 2,
                     'time_window_end': '2026-01-05T23:00:00+00:00'}}
    result = try_insert(route, 'O1', orders, cost_rates)
    assert result is not None
    assert [s.stop_type for s in result.stops] == ['pickup', 'delivery']
    assert result.added_cost > 0


def test_try_insert_rejects_when_capacity_exceeded():
    cost_rates = _load_cost_rates(RATES_PATH)
    route = Route('V1', 51.5, 0.0, 1000.0, 2, 'Lorry',
                  datetime(2026, 1, 5, 9, tzinfo=timezone.utc), stops=[])
    orders = {'O1': {'origin_lat': 51.5, 'origin_lon': 0.5,
                     'dest_lat': 51.5, 'dest_lon': 1.0,
                     'goods_weight_kg': 5000.0, 'goods_pallet_spaces': 10,
                     'time_window_end': '2026-01-05T23:00:00+00:00'}}
    assert try_insert(route, 'O1', orders, cost_rates) is None


def test_try_insert_rejects_when_deadline_impossible():
    cost_rates = _load_cost_rates(RATES_PATH)
    route = Route('V1', 51.5, 0.0, 5000.0, 12, 'Lorry',
                  datetime(2026, 1, 5, 9, tzinfo=timezone.utc), stops=[])
    orders = {'O1': {'origin_lat': 51.5, 'origin_lon': 0.5,
                     'dest_lat': 53.0, 'dest_lon': 5.0,  # far away
                     'goods_weight_kg': 1000.0, 'goods_pallet_spaces': 2,
                     'time_window_end': '2026-01-05T09:05:00+00:00'}}  # 5 min
    assert try_insert(route, 'O1', orders, cost_rates) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_pdp_route.py -k try_insert -v`
Expected: FAIL with `ImportError: cannot import name 'InsertionResult'`

- [ ] **Step 3: Write minimal implementation**

```python
# Append to pdp_route.py
@dataclass
class InsertionResult:
    added_cost: float
    stops: list  # the full new stop list if this insertion is applied


def try_insert(route: Route, order_id: str, orders: dict,
               cost_rates: dict) -> 'InsertionResult | None':
    """
    Find the cheapest feasible way to insert order_id's pickup and delivery
    into route.stops. Returns the best InsertionResult, or None if no placement
    satisfies capacity, precedence, and deadline constraints.
    """
    order = orders[order_id]
    pickup = Stop(order_id, order['origin_lat'], order['origin_lon'], 'pickup')
    delivery = Stop(order_id, order['dest_lat'], order['dest_lon'], 'delivery')

    base_cost = route_cost(route, cost_rates)
    n = len(route.stops)
    best = None

    for i in range(n + 1):                 # pickup position
        for j in range(i + 1, n + 2):      # delivery position (after pickup)
            candidate = list(route.stops)
            candidate.insert(i, pickup)
            candidate.insert(j, delivery)

            if not feasible_load(candidate, orders, route.capacity_kg, route.capacity_pallets):
                continue
            if not feasible_deadlines(route.start_lat, route.start_lon, candidate,
                                      route.start_time, orders):
                continue

            cand_km = route_distance_km(route.start_lat, route.start_lon, candidate)
            miles = cand_km * KM_TO_MILES
            rates = _rate_bundle(cost_rates, _normalise_type_key(route.asset_type))
            cand_cost = (rates['fuel_gbp_per_mile'] + rates['driver_mileage_gbp_per_mile']) * miles
            added = cand_cost - base_cost

            if best is None or added < best.added_cost:
                best = InsertionResult(added_cost=added, stops=candidate)

    return best
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_pdp_route.py -k try_insert -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add pdp_route.py tests/test_pdp_route.py
git commit -m "feat: add try_insert for cheapest feasible pickup+delivery placement"
```

---

## Task 5: `cheapest_insertion` — pick the best vehicle for one order

**Files:**
- Modify: `pdp_route.py`
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_pdp_route.py
from pdp_route import cheapest_insertion


def test_cheapest_insertion_picks_nearest_capable_vehicle():
    cost_rates = _load_cost_rates(RATES_PATH)
    start = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    routes = {
        'V_near': Route('V_near', 51.5, 0.4, 5000.0, 12, 'Lorry', start, stops=[]),
        'V_far':  Route('V_far',  53.0, 0.0, 5000.0, 12, 'Lorry', start, stops=[]),
    }
    orders = {'O1': {'origin_lat': 51.5, 'origin_lon': 0.5,
                     'dest_lat': 51.5, 'dest_lon': 0.6,
                     'goods_weight_kg': 1000.0, 'goods_pallet_spaces': 2,
                     'time_window_end': '2026-01-05T23:00:00+00:00'}}
    vid, result = cheapest_insertion('O1', orders, routes, cost_rates)
    assert vid == 'V_near'
    assert result is not None


def test_cheapest_insertion_returns_none_when_no_vehicle_feasible():
    cost_rates = _load_cost_rates(RATES_PATH)
    start = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    routes = {'V1': Route('V1', 51.5, 0.0, 500.0, 1, 'Lorry', start, stops=[])}
    orders = {'O1': {'origin_lat': 51.5, 'origin_lon': 0.5,
                     'dest_lat': 51.5, 'dest_lon': 0.6,
                     'goods_weight_kg': 5000.0, 'goods_pallet_spaces': 10,
                     'time_window_end': '2026-01-05T23:00:00+00:00'}}
    vid, result = cheapest_insertion('O1', orders, routes, cost_rates)
    assert vid is None
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_pdp_route.py -k cheapest -v`
Expected: FAIL with `ImportError: cannot import name 'cheapest_insertion'`

- [ ] **Step 3: Write minimal implementation**

```python
# Append to pdp_route.py
def cheapest_insertion(order_id: str, orders: dict, routes: dict,
                       cost_rates: dict) -> tuple:
    """
    Across all vehicle routes, return (vehicle_id, InsertionResult) for the
    cheapest feasible insertion of order_id, or (None, None) if none feasible.
    """
    best_vid = None
    best_result = None
    for vid, route in routes.items():
        result = try_insert(route, order_id, orders, cost_rates)
        if result is None:
            continue
        if best_result is None or result.added_cost < best_result.added_cost:
            best_vid = vid
            best_result = result
    return best_vid, best_result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_pdp_route.py -k cheapest -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add pdp_route.py tests/test_pdp_route.py
git commit -m "feat: add cheapest_insertion vehicle selector"
```

---

## Task 6: `route_sequencer` polish entrypoint

Keep the existing `_two_opt` but expose a function that polishes a list of `pdp_route.Stop` objects **without breaking pickup-before-delivery precedence**. 2-opt segment reversal can reorder a delivery before its pickup, so the polish must reject any reversal that violates precedence.

**Files:**
- Modify: `route_sequencer.py`
- Test: `tests/test_route_sequencer.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_route_sequencer.py
from pdp_route import Stop as PDPStop
from route_sequencer import polish_route_stops


def test_polish_preserves_pickup_before_delivery():
    stops = [
        PDPStop('O1', 51.5, 0.0, 'pickup'),
        PDPStop('O2', 51.5, 0.2, 'pickup'),
        PDPStop('O1', 51.5, 0.4, 'delivery'),
        PDPStop('O2', 51.5, 0.6, 'delivery'),
    ]
    polished = polish_route_stops(stops, start_lat=51.5, start_lon=0.0)
    # Every pickup must precede its own delivery
    for oid in ('O1', 'O2'):
        idx = [k for k, s in enumerate(polished) if s.order_id == oid]
        types = [polished[k].stop_type for k in idx]
        assert types == ['pickup', 'delivery']


def test_polish_returns_same_set_of_stops():
    stops = [
        PDPStop('O1', 51.5, 0.0, 'pickup'),
        PDPStop('O1', 51.5, 1.0, 'delivery'),
    ]
    polished = polish_route_stops(stops, start_lat=51.5, start_lon=0.0)
    assert {(s.order_id, s.stop_type) for s in polished} == {(s.order_id, s.stop_type) for s in stops}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_route_sequencer.py -k polish -v`
Expected: FAIL with `ImportError: cannot import name 'polish_route_stops'`

- [ ] **Step 3: Write minimal implementation**

```python
# Append to route_sequencer.py
def _precedence_ok(stops: list) -> bool:
    """True if every pickup precedes its matching delivery."""
    seen_pickup = set()
    for s in stops:
        if s.stop_type == 'pickup':
            seen_pickup.add(s.order_id)
        else:  # delivery
            if s.order_id not in seen_pickup:
                return False
    return True


def polish_route_stops(stops: list, start_lat: float, start_lon: float) -> list:
    """
    2-opt improvement on a list of pdp_route.Stop objects, rejecting any
    reversal that would place a delivery before its pickup. Stops carry
    .lat/.lon/.stop_type/.order_id (compatible with route_sequencer.Stop).
    """
    route = list(stops)
    if len(route) < 3:
        return route
    improved = True
    while improved:
        improved = False
        for i in range(1, len(route) - 1):
            for j in range(i + 1, len(route)):
                if _two_opt_gain(route, i, j) > 0:
                    candidate = route[:i] + route[i:j][::-1] + route[j:]
                    if _precedence_ok(candidate):
                        route = candidate
                        improved = True
    return route
```

> Note: `_two_opt_gain` already exists in this file and reads `.lat`/`.lon`, which `pdp_route.Stop` provides — no change needed there.

- [ ] **Step 4: Run test to verify it passes**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_route_sequencer.py -v`
Expected: PASS (existing 6 tests + 2 new = 8 passed)

- [ ] **Step 5: Commit**

```bash
git add route_sequencer.py tests/test_route_sequencer.py
git commit -m "feat: add precedence-safe 2-opt polish for PDP stops"
```

---

## Task 7: MCTS state refactor — `BatchState` carries route snapshots; `_evaluate` via engine

This rewrites the core of `mcts_dispatcher.py`. `BatchState` now stores, per vehicle, an immutable tuple of `(order_id, stop_type)` entries (a route snapshot). Lat/lon are looked up from `orders`/`vehicles` when a `Route` is reconstructed. The reward is negative total fleet cost minus an unassigned penalty.

**Files:**
- Modify: `mcts_dispatcher.py` (replace `BatchState`, `_apply_action`, `_evaluate`, `_candidate_vehicles`; keep `_sp_ucb`, `MCTSNode`, `_backpropagate`)
- Test: `tests/test_mcts_dispatcher.py`

- [ ] **Step 1: Write the failing test**

**First, delete the obsolete tests** in `tests/test_mcts_dispatcher.py` that reference symbols this rewrite removes — `_candidate_vehicles` and the `vehicle_loads` field no longer exist:
- `test_candidate_vehicles_capacity_filter`
- `test_candidate_vehicles_type_filter`
- `test_candidate_vehicles_proximity_sort`
- `test_evaluate_cost_decreases_with_shorter_route` (old 4-arg `_evaluate`)
- the old `test_batch_state_immutable` (uses `vehicle_loads=`)
- the three old `test_run_batch_*` (replaced in Task 8)

Also remove `_candidate_vehicles` from the import line. Then write the new tests:

```python
# Replace the top imports and the state/evaluate tests in tests/test_mcts_dispatcher.py.
# New imports:
import os
import pytest
from datetime import datetime, timezone
from mcts_dispatcher import (
    BatchState, MCTSNode, _sp_ucb, _evaluate, _build_routes, UNASSIGNED_PENALTY,
)
from profitability_report.profitability_report_merged import _load_cost_rates

RATES_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'profitability_report', 'vehicle_cost_rates.json'
)


def test_batch_state_immutable():
    state = BatchState(
        assigned=(('O1', 'V1'),),
        unassigned=frozenset({'O2'}),
        routes=(('V1', (('O1', 'pickup'), ('O1', 'delivery'))),),
    )
    with pytest.raises(Exception):
        state.assigned = ()


def test_evaluate_penalizes_unassigned_orders():
    orders = {
        'O1': {'origin_lat': 51.5, 'origin_lon': 0.0, 'dest_lat': 51.5, 'dest_lon': 0.1,
               'goods_weight_kg': 100.0, 'goods_pallet_spaces': 1,
               'time_window_start': '2026-01-05T09:00:00+00:00',
               'time_window_end': '2026-01-05T23:00:00+00:00'},
        'O2': {'origin_lat': 51.5, 'origin_lon': 0.0, 'dest_lat': 51.5, 'dest_lon': 0.1,
               'goods_weight_kg': 100.0, 'goods_pallet_spaces': 1,
               'time_window_start': '2026-01-05T09:00:00+00:00',
               'time_window_end': '2026-01-05T23:00:00+00:00'},
    }
    vehicles = {'V1': {'vehicle_id': 'V1', 'asset_type': 'Lorry', 'capacity_kg': 5000.0,
                       'capacity_pallets': 12, 'last_lat': 51.5, 'last_lon': 0.0}}
    cost_rates = _load_cost_rates(RATES_PATH)
    start = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)

    both = BatchState(
        assigned=(('O1', 'V1'), ('O2', 'V1')),
        unassigned=frozenset(),
        routes=(('V1', (('O1', 'pickup'), ('O1', 'delivery'),
                        ('O2', 'pickup'), ('O2', 'delivery'))),),
    )
    one_dropped = BatchState(
        assigned=(('O1', 'V1'),),
        unassigned=frozenset({'O2'}),
        routes=(('V1', (('O1', 'pickup'), ('O1', 'delivery'))),),
    )
    # Serving both should score higher than dropping one (penalty applied)
    assert _evaluate(both, orders, vehicles, cost_rates, start) > \
           _evaluate(one_dropped, orders, vehicles, cost_rates, start)


def test_build_routes_reconstructs_stops_with_coordinates():
    orders = {'O1': {'origin_lat': 51.5, 'origin_lon': 0.0,
                     'dest_lat': 51.6, 'dest_lon': 0.1,
                     'goods_weight_kg': 100.0, 'goods_pallet_spaces': 1,
                     'time_window_end': '2026-01-05T23:00:00+00:00'}}
    vehicles = {'V1': {'vehicle_id': 'V1', 'asset_type': 'Lorry', 'capacity_kg': 5000.0,
                       'capacity_pallets': 12, 'last_lat': 51.5, 'last_lon': 0.0}}
    start = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    routes = _build_routes(
        (('V1', (('O1', 'pickup'), ('O1', 'delivery'))),), orders, vehicles, start)
    assert routes['V1'].stops[0].lat == 51.5  # pickup at origin
    assert routes['V1'].stops[1].lat == 51.6  # delivery at dest
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_mcts_dispatcher.py -k "evaluate or build_routes or immutable" -v`
Expected: FAIL with `ImportError: cannot import name '_build_routes'`

- [ ] **Step 3: Write minimal implementation**

Replace the imports, `BatchState`, and add `_build_routes`/`_evaluate` in `mcts_dispatcher.py`. Keep `_sp_ucb`, `MCTSNode`, `_backpropagate`, `_best_assignment` as they are.

```python
# mcts_dispatcher.py — top section
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import log, sqrt
from typing import FrozenSet

from profitability_report.profitability_report_merged import (
    _load_cost_rates,
    _normalise_type_key,
    _rate_bundle,
)
from pdp_route import Route, Stop, route_cost, try_insert, cheapest_insertion
from route_sequencer import polish_route_stops

RATES_JSON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'profitability_report',
    'vehicle_cost_rates.json',
)
KM_TO_MILES = 0.621371
UNASSIGNED_PENALTY = 10_000.0  # GBP per dropped order; above any realistic single-route cost

DEFAULT_CONFIG = {
    'time_budget_seconds': 30,
    'horizon_hours': 8,
    'exploration_constant': 1.414,
    'sp_ucb_d': 0.1,
    'max_candidates': 8,
}


@dataclass(frozen=True)
class BatchState:
    assigned: tuple          # ((order_id, vehicle_id), ...)
    unassigned: FrozenSet    # frozenset of order_ids still to place
    routes: tuple            # ((vehicle_id, ((order_id, stop_type), ...)), ...)


def _build_routes(route_snapshots: tuple, orders: dict, vehicles: dict,
                  start_time: datetime) -> dict:
    """Reconstruct {vehicle_id: Route} from immutable snapshots, looking up coords."""
    routes = {}
    for vid, stop_entries in route_snapshots:
        veh = vehicles[vid]
        stops = []
        for order_id, stop_type in stop_entries:
            order = orders[order_id]
            if stop_type == 'pickup':
                lat, lon = order['origin_lat'], order['origin_lon']
            else:
                lat, lon = order['dest_lat'], order['dest_lon']
            stops.append(Stop(order_id, lat, lon, stop_type))
        routes[vid] = Route(
            vehicle_id=vid,
            start_lat=veh['last_lat'], start_lon=veh['last_lon'],
            capacity_kg=veh['capacity_kg'],
            capacity_pallets=veh.get('capacity_pallets', float('inf')),
            asset_type=veh.get('asset_type', 'default'),
            start_time=start_time,
            stops=stops,
        )
    return routes


def _empty_routes_for(vehicles: dict, start_time: datetime) -> dict:
    """A Route per vehicle with no stops (starting position = last GPS)."""
    return {
        vid: Route(
            vehicle_id=vid,
            start_lat=veh['last_lat'], start_lon=veh['last_lon'],
            capacity_kg=veh['capacity_kg'],
            capacity_pallets=veh.get('capacity_pallets', float('inf')),
            asset_type=veh.get('asset_type', 'default'),
            start_time=start_time,
            stops=[],
        )
        for vid, veh in vehicles.items()
    }


def _routes_to_snapshot(routes: dict) -> tuple:
    """Serialize {vehicle_id: Route} back to the immutable snapshot tuple."""
    return tuple(
        (vid, tuple((s.order_id, s.stop_type) for s in route.stops))
        for vid, route in sorted(routes.items())
        if route.stops
    )


def _evaluate(state: BatchState, orders: dict, vehicles: dict,
              cost_rates: dict, start_time: datetime) -> float:
    """Reward = -(total fleet route cost) - (penalty * unassigned count)."""
    routes = _build_routes(state.routes, orders, vehicles, start_time)
    total_cost = sum(route_cost(r, cost_rates) for r in routes.values())
    total_cost += UNASSIGNED_PENALTY * len(state.unassigned)
    return -total_cost


def _sp_ucb(node, parent_visits: int, C: float, D: float) -> float:
    if node.visits == 0:
        return float('inf')
    Q = node.total_reward / node.visits
    variance = max(0.0, (node.reward_sq_sum / node.visits) - Q ** 2)
    return Q + C * sqrt(log(parent_visits) / node.visits) + sqrt((variance + D) / node.visits)


@dataclass
class MCTSNode:
    state: BatchState
    parent: object
    action: object  # (order_id, vehicle_id) | (order_id, None) | None
    children: list = field(default_factory=list)
    visits: int = 0
    total_reward: float = 0.0
    reward_sq_sum: float = 0.0


def _backpropagate(node: MCTSNode, reward: float) -> None:
    while node is not None:
        node.visits += 1
        node.total_reward += reward
        node.reward_sq_sum += reward * reward
        node = node.parent
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_mcts_dispatcher.py -k "evaluate or build_routes or immutable" -v`
Expected: PASS (3 passed). Other tests in this file will fail until Task 8 — that is expected.

- [ ] **Step 5: Commit**

```bash
git add mcts_dispatcher.py tests/test_mcts_dispatcher.py
git commit -m "refactor: BatchState carries route snapshots; evaluate via PDP engine"
```

---

## Task 8: MCTS search loop — expand via `try_insert`, rollout via `cheapest_insertion`, output contract

Completes the rewrite: order sequencing, the four MCTS phases using the engine, final selection with 2-opt polish, and the output dict (`stops` with `type`/`load_after`/`arrival_time`, `meta.total_cost_gbp`).

**Files:**
- Modify: `mcts_dispatcher.py` (add search functions + `run_batch`)
- Test: `tests/test_mcts_dispatcher.py` (the `run_batch` tests already present, updated for new fields)

- [ ] **Step 1: Write the failing test**

```python
# Append/replace run_batch tests in tests/test_mcts_dispatcher.py
from mcts_dispatcher import run_batch


def _two_order_input(budget=2):
    return {
        'orders': [
            {'order_id': 'O1', 'origin_lat': 51.5, 'origin_lon': -0.1,
             'dest_lat': 51.6, 'dest_lon': -0.2, 'goods_weight_kg': 1000.0,
             'goods_pallet_spaces': 2,
             'time_window_start': '2026-01-05T09:00:00+00:00',
             'time_window_end': '2026-01-05T23:00:00+00:00'},
            {'order_id': 'O2', 'origin_lat': 51.7, 'origin_lon': -0.3,
             'dest_lat': 51.8, 'dest_lon': -0.4, 'goods_weight_kg': 500.0,
             'goods_pallet_spaces': 1,
             'time_window_start': '2026-01-05T09:00:00+00:00',
             'time_window_end': '2026-01-05T23:00:00+00:00'},
        ],
        'vehicles': [
            {'vehicle_id': 'V1', 'asset_type': 'Lorry', 'capacity_kg': 5000.0,
             'capacity_pallets': 12, 'last_lat': 51.5, 'last_lon': -0.1},
            {'vehicle_id': 'V2', 'asset_type': 'Lorry', 'capacity_kg': 5000.0,
             'capacity_pallets': 12, 'last_lat': 51.7, 'last_lon': -0.3},
        ],
        'committed_assignments': [],
        'config': {'time_budget_seconds': budget, 'horizon_hours': 24,
                   'exploration_constant': 1.414, 'sp_ucb_d': 0.1},
    }


def test_run_batch_assigns_both_orders_with_pickup_and_delivery_stops():
    result = run_batch(_two_order_input())
    assigned_ids = {a['order_id'] for a in result['assignments']}
    assert assigned_ids == {'O1', 'O2'}
    assert result['meta']['orders_unassigned'] == 0
    assert result['meta']['total_cost_gbp'] >= 0
    # Each route's stops carry pickup AND delivery, with new fields present
    for route in result['routes'].values():
        types = [s['type'] for s in route['stops']]
        assert 'pickup' in types and 'delivery' in types
        for s in route['stops']:
            assert 'load_after' in s and 'arrival_time' in s


def test_run_batch_committed_orders_locked():
    batch_input = _two_order_input()
    batch_input['committed_assignments'] = [{'order_id': 'O1', 'vehicle_id': 'V1'}]
    result = run_batch(batch_input)
    o1 = next(a for a in result['assignments'] if a['order_id'] == 'O1')
    assert o1['vehicle_id'] == 'V1'


def test_run_batch_drops_order_when_no_capacity():
    batch_input = _two_order_input()
    # 3 heavy orders, 2 small trucks -> at least one unassigned
    batch_input['orders'] = [
        {'order_id': f'O{i}', 'origin_lat': 51.5 + i * 0.1, 'origin_lon': -0.1,
         'dest_lat': 51.6 + i * 0.1, 'dest_lon': -0.2, 'goods_weight_kg': 4000.0,
         'goods_pallet_spaces': 10,
         'time_window_start': '2026-01-05T09:00:00+00:00',
         'time_window_end': '2026-01-05T23:00:00+00:00'}
        for i in range(3)
    ]
    result = run_batch(batch_input)
    assert result['meta']['orders_unassigned'] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_mcts_dispatcher.py -k run_batch -v`
Expected: FAIL with `AttributeError`/`KeyError` (search functions and new output fields not implemented yet)

- [ ] **Step 3: Write minimal implementation**

Append the search machinery and `run_batch` to `mcts_dispatcher.py`:

```python
# mcts_dispatcher.py — search + run_batch

def _next_order(state: BatchState, order_sequence: list):
    for oid in order_sequence:
        if oid in state.unassigned:
            return oid
    return None


def _candidate_actions(order_id, state, orders, vehicles, cost_rates,
                       start_time, max_candidates):
    """Top-K cheapest feasible (order_id, vehicle_id) insertions, then a skip."""
    routes = _build_routes(state.routes, orders, vehicles, start_time)
    for vid in vehicles:
        routes.setdefault(vid, _empty_routes_for({vid: vehicles[vid]}, start_time)[vid])
    scored = []
    for vid, route in routes.items():
        result = try_insert(route, order_id, orders, cost_rates)
        if result is not None:
            scored.append((result.added_cost, vid))
    scored.sort()
    actions = [(order_id, vid) for _, vid in scored[:max_candidates]]
    actions.append((order_id, None))  # skip
    return actions


def _apply_action(state, action, orders, vehicles, cost_rates, start_time):
    order_id, vehicle_id = action
    new_assigned = state.assigned + (action,)
    new_unassigned = state.unassigned - {order_id}
    if vehicle_id is None:
        return BatchState(assigned=new_assigned, unassigned=new_unassigned,
                          routes=state.routes)
    routes = _build_routes(state.routes, orders, vehicles, start_time)
    routes.setdefault(
        vehicle_id, _empty_routes_for({vehicle_id: vehicles[vehicle_id]}, start_time)[vehicle_id])
    result = try_insert(routes[vehicle_id], order_id, orders, cost_rates)
    if result is None:
        # Infeasible after all — treat as skip
        return BatchState(assigned=state.assigned + ((order_id, None),),
                          unassigned=new_unassigned, routes=state.routes)
    routes[vehicle_id].stops = result.stops
    return BatchState(assigned=new_assigned, unassigned=new_unassigned,
                      routes=_routes_to_snapshot(routes))


def _select(root, order_sequence, orders, vehicles, cost_rates, start_time, C, D, max_candidates):
    node = root
    while node.state.unassigned:
        order_id = _next_order(node.state, order_sequence)
        if order_id is None:
            break
        actions = _candidate_actions(order_id, node.state, orders, vehicles,
                                     cost_rates, start_time, max_candidates)
        tried = {child.action for child in node.children}
        untried = [a for a in actions if a not in tried]
        if untried or not node.children:
            return node
        node = max(node.children, key=lambda c: _sp_ucb(c, node.visits, C, D))
    return node


def _expand(node, order_sequence, orders, vehicles, cost_rates, start_time, max_candidates):
    order_id = _next_order(node.state, order_sequence)
    actions = _candidate_actions(order_id, node.state, orders, vehicles,
                                 cost_rates, start_time, max_candidates)
    tried = {child.action for child in node.children}
    untried = [a for a in actions if a not in tried]
    action = untried[0] if untried else (order_id, None)
    new_state = _apply_action(node.state, action, orders, vehicles, cost_rates, start_time)
    child = MCTSNode(state=new_state, parent=node, action=action)
    node.children.append(child)
    return child


def _simulate(node, order_sequence, orders, vehicles, cost_rates, start_time):
    """Greedy cheapest-insertion rollout for remaining unassigned orders."""
    state = node.state
    routes = _build_routes(state.routes, orders, vehicles, start_time)
    for vid in vehicles:
        routes.setdefault(vid, _empty_routes_for({vid: vehicles[vid]}, start_time)[vid])
    unassigned = set(state.unassigned)
    assigned = list(state.assigned)
    for order_id in order_sequence:
        if order_id not in unassigned:
            continue
        vid, result = cheapest_insertion(order_id, orders, routes, cost_rates)
        if vid is None:
            assigned.append((order_id, None))
        else:
            routes[vid].stops = result.stops
            assigned.append((order_id, vid))
        unassigned.discard(order_id)
    final = BatchState(assigned=tuple(assigned), unassigned=frozenset(unassigned),
                       routes=_routes_to_snapshot(routes))
    return _evaluate(final, orders, vehicles, cost_rates, start_time)


def _best_assignment(root):
    node = root
    while node.children:
        node = max(node.children, key=lambda c: c.visits)
    return node


def _mcts_search(root, order_sequence, orders, vehicles, cost_rates, start_time,
                 time_budget_s, C, D, max_candidates):
    deadline = time.monotonic() + time_budget_s
    iterations = 0
    while time.monotonic() < deadline:
        node = _select(root, order_sequence, orders, vehicles, cost_rates,
                       start_time, C, D, max_candidates)
        if not node.state.unassigned:
            reward = _evaluate(node.state, orders, vehicles, cost_rates, start_time)
            _backpropagate(node, reward)
        else:
            child = _expand(node, order_sequence, orders, vehicles, cost_rates,
                            start_time, max_candidates)
            reward = _simulate(child, order_sequence, orders, vehicles, cost_rates, start_time)
            _backpropagate(child, reward)
        iterations += 1
    return _best_assignment(root), iterations


def _window_width(oid, orders):
    o = orders[oid]
    try:
        start = datetime.fromisoformat(o['time_window_start'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(o['time_window_end'].replace('Z', '+00:00'))
        return (end - start).total_seconds()
    except (KeyError, ValueError, AttributeError):
        return float('inf')


def run_batch(batch_input: dict) -> dict:
    from pdp_route import load_profile, arrival_times  # local import to keep top tidy

    cfg = {**DEFAULT_CONFIG, **(batch_input.get('config') or {})}
    orders = {o['order_id']: o for o in batch_input['orders']}
    vehicles = {v['vehicle_id']: v for v in batch_input['vehicles']}
    committed = batch_input.get('committed_assignments', [])

    cost_rates = _load_cost_rates(RATES_JSON_PATH)
    start_time = datetime.now(timezone.utc)

    # Build initial routes from committed assignments (locked, in pickup,delivery order)
    routes = _empty_routes_for(vehicles, start_time)
    valid_committed = [c for c in committed
                       if c['order_id'] in orders and c['vehicle_id'] in vehicles]
    for c in valid_committed:
        res = try_insert(routes[c['vehicle_id']], c['order_id'], orders, cost_rates)
        if res is not None:
            routes[c['vehicle_id']].stops = res.stops
    committed_ids = {c['order_id'] for c in valid_committed}

    order_sequence = sorted(
        [oid for oid in orders if oid not in committed_ids],
        key=lambda oid: (_window_width(oid, orders),
                         orders[oid].get('service_level_priority', 3)),
    )

    root_state = BatchState(
        assigned=tuple((c['order_id'], c['vehicle_id']) for c in valid_committed),
        unassigned=frozenset(order_sequence),
        routes=_routes_to_snapshot(routes),
    )
    root = MCTSNode(state=root_state, parent=None, action=None)

    start_clock = time.monotonic()
    if not order_sequence:
        best_node, iterations = root, 0
    else:
        best_node, iterations = _mcts_search(
            root, order_sequence, orders, vehicles, cost_rates, start_time,
            cfg['time_budget_seconds'], cfg['exploration_constant'],
            cfg['sp_ucb_d'], cfg.get('max_candidates', 8))
    elapsed = time.monotonic() - start_clock

    best = best_node.state
    skipped = sum(1 for _, vid in best.assigned if vid is None)
    orders_unassigned = len(best.unassigned) + skipped

    # Build final routes, polish each, and emit the output contract
    final_routes = _build_routes(best.routes, orders, vehicles, start_time)
    out_routes = {}
    total_cost_gbp = 0.0
    for vid, route in final_routes.items():
        if not route.stops:
            continue
        route.stops = polish_route_stops(route.stops, route.start_lat, route.start_lon)
        cost = route_cost(route, cost_rates)
        total_cost_gbp += cost
        from pdp_route import route_distance_km
        km = route_distance_km(route.start_lat, route.start_lon, route.stops)
        loads = load_profile(route.stops, orders)
        arrivals = arrival_times(route.start_lat, route.start_lon, route.stops, start_time)
        out_routes[vid] = {
            'stops': [
                {'order_id': s.order_id, 'type': s.stop_type, 'lat': s.lat, 'lon': s.lon,
                 'load_after': {'kg': loads[k][0], 'pallets': loads[k][1]},
                 'arrival_time': arrivals[k].isoformat()}
                for k, s in enumerate(route.stops)
            ],
            'total_distance_km': round(km, 1),
            'estimated_cost_gbp': round(cost, 2),
        }

    return {
        'assignments': [{'order_id': oid, 'vehicle_id': vid}
                        for oid, vid in best.assigned if vid is not None],
        'routes': out_routes,
        'meta': {
            'mcts_iterations': iterations,
            'elapsed_seconds': round(elapsed, 2),
            'orders_unassigned': orders_unassigned,
            'total_cost_gbp': round(total_cost_gbp, 2),
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_mcts_dispatcher.py -v`
Expected: PASS (all tests in file). If a deadline test is flaky at 2s budget, raise that test's `budget` to 5.

- [ ] **Step 5: Commit**

```bash
git add mcts_dispatcher.py tests/test_mcts_dispatcher.py
git commit -m "feat: MCTS search over PDP engine with pickup+delivery output"
```

> **Corrections applied during implementation** (the verbatim code above had three latent issues; the shipped code fixes them):
> 1. **Skip penalty:** `_evaluate` must penalize *both* `state.unassigned` *and* assigned entries with `vehicle_id is None` (skips). Otherwise skipping is free and MCTS assigns nothing. Shipped: `UNASSIGNED_PENALTY * (len(state.unassigned) + skipped)`.
> 2. **Planning anchor:** `run_batch` uses `_planning_start_time(orders)` (earliest `time_window_start`) instead of `datetime.now()`, so deadline feasibility is evaluated against the order windows rather than wall-clock — essential when planning historical/future windows.
> 3. **Polish re-validation:** `polish_route_stops` only enforces precedence, not capacity/deadline. `run_batch` keeps the polished order only if it stays `feasible_load` and `feasible_deadlines`, else reverts to the pre-polish (already feasible) sequence.
>
> The capacity-drop test was also corrected: a consolidating PDP delivers one order before the next pickup, so equal-size orders never overflow on capacity alone. The valid drop test uses one order larger than any vehicle's pallet capacity.

---

## Task 9: Rolling wrapper — orders carry pallets+deadline; project committed vehicle state

`run_daily_batch.py` must (a) emit `goods_pallet_spaces` and a real `time_window_end` in `_build_orders`, and (b) project each committed vehicle's start position to its last committed stop so new orders append from there.

**How committed work is handled (read before implementing):** The existing `main()` already (1) filters committed orders out of the new `orders` set and (2) merges prior-window routes back via `_merge_dispatch`. So committed orders are *not* re-planned by `run_batch` in production — they are preserved by the merge. This task adds the missing piece: moving each committed vehicle's **start position** to its last committed stop via `_project_vehicles`. **Remaining capacity resets to full** under this model because projecting "to where the vehicle will be after finishing committed work" implies all committed pickups *and* deliveries have completed, so the truck is empty. This is consistent with the deferred intra-day-timing caveat (spec §7); do not attempt to carry partial committed loads.

**Files:**
- Modify: `run_daily_batch.py` (`_build_orders`, `_build_vehicles`, and a new `_project_vehicles` step)
- Test: `tests/test_run_daily_batch.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_daily_batch.py
from run_daily_batch import _project_vehicles


def test_project_vehicles_moves_start_to_last_committed_stop():
    vehicles = {
        'V1': {'vehicle_id': 'V1', 'asset_type': 'Lorry', 'capacity_kg': 5000.0,
               'capacity_pallets': 12, 'last_lat': 51.5, 'last_lon': 0.0},
    }
    prev_dispatch = {
        'routes': {
            'V1': {'stops': [
                {'order_id': 'O1', 'type': 'pickup',   'lat': 51.6, 'lon': 0.1},
                {'order_id': 'O1', 'type': 'delivery', 'lat': 52.0, 'lon': 0.5},
            ]},
        }
    }
    projected = _project_vehicles(vehicles, prev_dispatch)
    # Start position moves to the last committed stop
    assert projected['V1']['last_lat'] == 52.0
    assert projected['V1']['last_lon'] == 0.5


def test_project_vehicles_keeps_position_when_no_committed_route():
    vehicles = {
        'V1': {'vehicle_id': 'V1', 'asset_type': 'Lorry', 'capacity_kg': 5000.0,
               'capacity_pallets': 12, 'last_lat': 51.5, 'last_lon': 0.0},
    }
    projected = _project_vehicles(vehicles, None)
    assert projected['V1']['last_lat'] == 51.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_run_daily_batch.py -v`
Expected: FAIL with `ImportError: cannot import name '_project_vehicles'`

- [ ] **Step 3: Write minimal implementation**

First add `_project_vehicles` near the other helpers in `run_daily_batch.py`:

```python
# run_daily_batch.py — add after _build_vehicles
def _project_vehicles(vehicles: dict, prev_dispatch: dict | None) -> dict:
    """
    Return a copy of vehicles with start position moved to each vehicle's last
    committed stop (where it will be after finishing committed work). Vehicles
    with no committed route keep their last-known GPS position.
    """
    projected = {vid: dict(v) for vid, v in vehicles.items()}
    if not prev_dispatch:
        return projected
    for vid, route in prev_dispatch.get('routes', {}).items():
        stops = route.get('stops', [])
        if not stops or vid not in projected:
            continue
        last = stops[-1]
        projected[vid]['last_lat'] = last['lat']
        projected[vid]['last_lon'] = last['lon']
    return projected
```

Then update `_build_orders` to emit pallets and a real deadline. Replace the `tw_val`/`ts_end` block (currently lines ~144-148) and the result dict (~154-165) with:

```python
        # Real delivery deadline; fall back to +48h (dataset median transit)
        ts_end = pd.to_datetime(
            row.get('destination_requested_start_timestamp_local'), errors='coerce')
        if pd.isna(ts_end) or ts_end <= ts_start:
            ts_end = ts_start + timedelta(hours=48)

        ts_start_utc = ts_start.replace(tzinfo=timezone.utc)
        ts_end_utc   = ts_end.replace(tzinfo=timezone.utc)

        pallets = row.get('goods_pallet_spaces')
        try:
            pallets = max(1, int(float(pallets))) if pd.notna(pallets) else 1
        except (TypeError, ValueError):
            pallets = 1

        result[order_id] = {
            'order_id':            order_id,
            'origin_lat':          origin_coords[0],
            'origin_lon':          origin_coords[1],
            'dest_lat':            dest_coords[0],
            'dest_lon':            dest_coords[1],
            'goods_weight_kg':     float(row['goods_weight']) if pd.notna(row.get('goods_weight')) else 0.0,
            'goods_pallet_spaces': pallets,
            'time_window_start':   ts_start_utc.isoformat(),
            'time_window_end':     ts_end_utc.isoformat(),
            'revenue_gbp':         float(row['total_revenue_tenant_currency'])
                                   if pd.notna(row.get('total_revenue_tenant_currency')) else 0.0,
        }
```

Also update `_build_vehicles` to include `capacity_pallets` (mirror `simulation/data_loader.py`). Add this map near the top of the file:

```python
_ASSET_TYPE_PALLETS = {
    'Tractor Unit': 26, 'Lorry': 12, 'Rigid Truck': 12, 'Mini Truck': 6, 'Service Van': 2,
}
```

And inside `_build_vehicles`, after computing `asset_type` (add the lookup) include it in the result dict:

```python
        asset_type = str(row.get('AssetType', 'Lorry'))
        result[asset_name] = {
            'vehicle_id':       asset_name,
            'registration':     asset_name,
            'asset_type':       asset_type,
            'capacity_kg':      capacity_kg,
            'capacity_pallets': _ASSET_TYPE_PALLETS.get(asset_type, 12),
            'last_lat':         pos.get('last_lat', 51.5),
            'last_lon':         pos.get('last_lon', -0.12),
        }
```

Finally, in `main()`, apply the projection before building `batch_input`. After the line `vehicles = _build_vehicles(vehicles_df, last_positions)` and after `prev_dispatch` is loaded, insert:

```python
    vehicles = _project_vehicles(vehicles, prev_dispatch)
```

(Place it immediately after `committed = _committed_from_previous(prev_dispatch)`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_run_daily_batch.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add run_daily_batch.py tests/test_run_daily_batch.py
git commit -m "feat: project committed vehicle state; orders carry pallets+deadline"
```

---

## Task 10: Greedy baseline → cheapest-insertion on the same engine

Rewrite `simulation/greedy.py` to use `cheapest_insertion` so MCTS-vs-greedy compares on identical accounting. Same input/output shape as `run_batch` (so `report.py` and `simulate.py` need no change).

**Files:**
- Rewrite: `simulation/greedy.py`
- Test: `tests/test_greedy.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_greedy.py
from simulation.greedy import run_greedy


def _orders_vehicles():
    orders = [
        {'order_id': 'O1', 'origin_lat': 51.5, 'origin_lon': -0.1,
         'dest_lat': 51.6, 'dest_lon': -0.2, 'goods_weight_kg': 1000.0,
         'goods_pallet_spaces': 2,
         'time_window_start': '2026-01-05T09:00:00+00:00',
         'time_window_end': '2026-01-05T23:00:00+00:00'},
    ]
    vehicles = [
        {'vehicle_id': 'V1', 'asset_type': 'Lorry', 'capacity_kg': 5000.0,
         'capacity_pallets': 12, 'last_lat': 51.5, 'last_lon': -0.1},
    ]
    return orders, vehicles


def test_run_greedy_assigns_order_with_pickup_and_delivery():
    orders, vehicles = _orders_vehicles()
    result = run_greedy(orders, vehicles)
    assert result['meta']['algorithm'] == 'greedy'
    assert {a['order_id'] for a in result['assignments']} == {'O1'}
    route = result['routes']['V1']
    assert [s['type'] for s in route['stops']] == ['pickup', 'delivery']
    assert route['estimated_cost_gbp'] >= 0


def test_run_greedy_drops_order_when_no_capacity():
    orders, vehicles = _orders_vehicles()
    orders[0]['goods_weight_kg'] = 99999.0
    result = run_greedy(orders, vehicles)
    assert result['meta']['orders_unassigned'] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_greedy.py -v`
Expected: FAIL (current `run_greedy` returns no `arrival_time`/pickup stops and may KeyError on new fields)

- [ ] **Step 3: Write minimal implementation**

```python
# simulation/greedy.py
"""
Greedy cheapest-insertion baseline dispatcher.

Processes orders hardest-first (tightest deadline) and inserts each into the
vehicle route where it adds least cost, using the same PDP engine and cost
rates as the MCTS dispatcher so results are directly comparable.
"""
import os
from datetime import datetime, timezone

from profitability_report.profitability_report_merged import _load_cost_rates
from pdp_route import (
    route_cost, route_distance_km, cheapest_insertion, load_profile, arrival_times,
)
from mcts_dispatcher import _empty_routes_for, _window_width
from route_sequencer import polish_route_stops

_RATES_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', 'profitability_report', 'vehicle_cost_rates.json',
)


def run_greedy(orders: list, vehicles: list) -> dict:
    cost_rates = _load_cost_rates(_RATES_JSON)
    start_time = datetime.now(timezone.utc)

    orders_by_id = {o['order_id']: o for o in orders}
    vehicles_by_id = {v['vehicle_id']: v for v in vehicles}
    routes = _empty_routes_for(vehicles_by_id, start_time)

    sequence = sorted(orders_by_id,
                      key=lambda oid: (_window_width(oid, orders_by_id),
                                       orders_by_id[oid].get('service_level_priority', 3)))

    assignments = []
    unassigned = 0
    for order_id in sequence:
        vid, result = cheapest_insertion(order_id, orders_by_id, routes, cost_rates)
        if vid is None:
            unassigned += 1
            continue
        routes[vid].stops = result.stops
        assignments.append({'order_id': order_id, 'vehicle_id': vid})

    out_routes = {}
    for vid, route in routes.items():
        if not route.stops:
            continue
        route.stops = polish_route_stops(route.stops, route.start_lat, route.start_lon)
        km = route_distance_km(route.start_lat, route.start_lon, route.stops)
        loads = load_profile(route.stops, orders_by_id)
        arrivals = arrival_times(route.start_lat, route.start_lon, route.stops, start_time)
        out_routes[vid] = {
            'stops': [
                {'order_id': s.order_id, 'type': s.stop_type, 'lat': s.lat, 'lon': s.lon,
                 'load_after': {'kg': loads[k][0], 'pallets': loads[k][1]},
                 'arrival_time': arrivals[k].isoformat()}
                for k, s in enumerate(route.stops)
            ],
            'total_distance_km':  round(km, 1),
            'estimated_cost_gbp': round(route_cost(route, cost_rates), 2),
        }

    return {
        'assignments': assignments,
        'routes': out_routes,
        'meta': {'orders_unassigned': unassigned, 'algorithm': 'greedy'},
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/test_greedy.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add simulation/greedy.py tests/test_greedy.py
git commit -m "feat: greedy baseline uses cheapest-insertion on PDP engine"
```

> **Correction applied during implementation:** greedy must anchor `start_time` the same way the MCTS dispatcher does — via `_planning_start_time(orders_by_id)` imported from `mcts_dispatcher` — not `datetime.now()`. Using wall-clock now makes historical/future deadlines infeasible and drops every order; it also breaks the apples-to-apples comparison (both dispatchers must use the same anchor). Shipped: `from mcts_dispatcher import _empty_routes_for, _window_width, _planning_start_time` and `start_time = _planning_start_time(orders_by_id)` (computed after `orders_by_id` is built).

---

## Task 11: Full regression + smoke run

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m pytest tests/ -v`
Expected: PASS — all tests across `test_pdp_route.py`, `test_route_sequencer.py`, `test_mcts_dispatcher.py`, `test_run_daily_batch.py`, `test_greedy.py`.

- [ ] **Step 2: Smoke-test a single simulated day**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" -m simulation.simulate --date 2026-01-05 --budget 10`
Expected: prints a comparison row; MCTS and greedy both report cost ≥ 0; no traceback. Confirm the printed cost is plausible (not zero, not absurdly large) and `orders_unassigned` is reported.

- [ ] **Step 3: Smoke-test a rolling production window**

Run: `& "E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe" run_daily_batch.py --date 2026-01-05 --window-start 00:00 --window-hours 24 --budget 10 --fresh`
Expected: writes `data/Output/dispatch_2026-01-05.json`; the JSON's route `stops` include `pickup` and `delivery` entries with `load_after`/`arrival_time`; `meta.total_cost_gbp` present per window.

- [ ] **Step 4: Commit any fixups**

```bash
git add -A
git commit -m "test: full regression green for PDPTW dispatcher"
```

---

## Notes for the implementer

- **DRY:** `_empty_routes_for`, `_build_routes`, `_window_width` live in `mcts_dispatcher.py` and are reused by `greedy.py`. Do not duplicate them.
- **YAGNI:** Do not add LNS, subcontracting cost, or pickup ready-time windows now — they are explicitly deferred in the spec (§7, §8). `UNASSIGNED_PENALTY` stays a flat constant.
- **Performance:** `try_insert` is O(n²) in route length and runs inside rollouts. For the budgets used (10–30s) and realistic per-vehicle route lengths this is fine. If a large day is slow, lower `max_candidates` before changing the algorithm.
- **Determinism caveat:** MCTS is time-budget bound, so iteration counts vary by machine. Tests assert on feasibility/assignment outcomes, never on exact cost or iteration count.
```
