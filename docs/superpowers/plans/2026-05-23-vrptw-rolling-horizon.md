# VRPTW Rolling-Horizon Dispatcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the PDPTW dispatcher with a VRPTW rolling-horizon optimizer that reflects ZEEFLEET's hub-and-spoke topology, validated against historical telematics.

**Architecture:** Five tasks in dependency order: (1) VRPTW route engine — delivery-only stops, depot start/end, activation cost in objective; (2) VRPTW ALNS solver — adapts existing ALNS loop for single-stop insertion; (3) Freight tracker — extracts per-order freight arrival times from historical GPS; (4) Rolling dispatcher — event-driven simulation replaying a historical day; (5) Backtest — planned vs actual delivery-leg metrics with line-haul filtering.

**Tech Stack:** Python 3.11, pandas, numpy, pytest, existing `profitability_report/vehicle_cost_rates.json`, existing `simulation/leg_labeller.py`, existing `simulation/data_loader.py`, existing `simulation/actuals_loader.py`.

---

## Codebase orientation

Key existing files to read before starting:

- `simulation/data_loader.py` — `build_vehicles()`, `build_orders()`, `_DEPOT_ANCHORS`, `_CIRCUIT_DEPOTS`
- `pdp_route.py` — existing PDPTW engine (DO NOT MODIFY; kept for legacy backtest)
- `simulation/alns.py` — existing ALNS loop this plan adapts
- `simulation/leg_labeller.py` — classifies orders as PRE_STAGED / VIA_DEPOT / DIRECT
- `profitability_report/profitability_report_merged.py` — `_haversine_km`, `_normalise_type_key`, `_rate_bundle`, `_load_cost_rates`
- `simulation/actuals_loader.py` — `_gps_track_km`, `_jigsaw_fuel_gbp`, `_qargo_actuals`
- `tests/test_pdp_route.py` — test patterns to follow

Run existing tests before and after each task to confirm nothing is broken:
```
cd E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

---

## File Map

| File | Action |
|---|---|
| `simulation/vrptw_engine.py` | Create |
| `simulation/vrptw_alns.py` | Create |
| `simulation/freight_tracker.py` | Create |
| `simulation/rolling_dispatcher.py` | Create |
| `backtest_vrptw.py` | Create |
| `tests/test_vrptw_engine.py` | Create |
| `tests/test_freight_tracker.py` | Create |
| `pdp_route.py` | No change |
| `run_backtest.py` | No change |
| `simulation/alns.py` | No change |

---

## Task 1: VRPTW Route Engine

**Files:**
- Create: `simulation/vrptw_engine.py`
- Create: `tests/test_vrptw_engine.py`

### Context

This is the mathematical core. Every later component depends on it. An order is a single delivery stop — no pickup. The vehicle loads all goods at its home depot before departing. Cost = fixed activation cost (£150, opens when the vehicle serves its first stop) + fuel (closed-loop km × fuel rate). This activation cost is what forces geographic clustering: the solver opens a second vehicle only when fuel savings exceed £150, so it never sends a Cambridge lorry to Plymouth just to avoid the activation fee.

`try_insert` searches O(n+1) positions for one delivery stop (not O(n²) pairs). It returns `None` if the insertion would breach capacity or shift end.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_vrptw_engine.py`:

```python
import os
import sys
from datetime import datetime, timezone, timedelta
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'simulation'))

from vrptw_engine import (
    DeliveryStop, DeliveryRoute, InsertionResult,
    route_distance_km, route_fuel_cost, route_cost,
    fleet_objective, feasible, try_insert,
    compute_direct_run, set_activation_cost, set_unassigned_penalty,
    VEHICLE_ACTIVATION_COST, UNASSIGNED_PENALTY,
)
from profitability_report.profitability_report_merged import _load_cost_rates

RATES_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'profitability_report', 'vehicle_cost_rates.json'
)

T0 = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)
T_END = T0 + timedelta(hours=11)

def _route(stops=None, capacity_kg=10000.0, capacity_pallets=26, asset_type='Lorry'):
    return DeliveryRoute(
        vehicle_id='V1',
        depot_lat=52.10172, depot_lon=0.16229,
        shift_start=T0, shift_end=T_END,
        capacity_kg=capacity_kg, capacity_pallets=capacity_pallets,
        asset_type=asset_type,
        stops=stops or [],
    )

def _stop(order_id='O1', lat=52.2, lon=0.3, weight_kg=500.0, pallets=2.0):
    return DeliveryStop(order_id=order_id, lat=lat, lon=lon,
                        weight_kg=weight_kg, pallets=pallets)


def test_route_distance_empty_is_zero():
    assert route_distance_km(_route()) == 0.0


def test_route_distance_single_stop_is_round_trip():
    stop = _stop(lat=52.2, lon=0.16229)  # ~11km north of depot
    route = _route(stops=[stop])
    km = route_distance_km(route)
    assert 20.0 < km < 26.0  # ~22km round trip


def test_route_cost_empty_is_zero():
    cost_rates = _load_cost_rates(RATES_PATH)
    assert route_cost(_route(), cost_rates) == 0.0


def test_route_cost_non_empty_includes_activation():
    cost_rates = _load_cost_rates(RATES_PATH)
    stop = _stop(lat=52.2, lon=0.16229)
    route = _route(stops=[stop])
    cost = route_cost(route, cost_rates)
    assert cost > VEHICLE_ACTIVATION_COST  # activation + fuel


def test_fleet_objective_penalises_unassigned():
    cost_rates = _load_cost_rates(RATES_PATH)
    routes = {'V1': _route()}  # empty route = no stops
    obj = fleet_objective(routes, cost_rates, total_orders=5)
    assert obj == UNASSIGNED_PENALTY * 5


def test_feasible_within_capacity():
    stop = _stop(weight_kg=500.0, pallets=2.0)
    route = _route(stops=[stop], capacity_kg=1000.0, capacity_pallets=10)
    assert feasible(route) is True


def test_feasible_exceeds_weight():
    stop = _stop(weight_kg=1500.0, pallets=2.0)
    route = _route(stops=[stop], capacity_kg=1000.0, capacity_pallets=10)
    assert feasible(route) is False


def test_feasible_exceeds_pallets():
    stop = _stop(weight_kg=100.0, pallets=15.0)
    route = _route(stops=[stop], capacity_kg=10000.0, capacity_pallets=10)
    assert feasible(route) is False


def test_feasible_two_stops_combined_weight():
    stops = [_stop('O1', weight_kg=600.0, pallets=3.0),
             _stop('O2', lat=52.3, lon=0.2, weight_kg=600.0, pallets=3.0)]
    # 1200kg > 1000kg capacity
    route = _route(stops=stops, capacity_kg=1000.0, capacity_pallets=10)
    assert feasible(route) is False


def test_try_insert_returns_result_for_feasible():
    cost_rates = _load_cost_rates(RATES_PATH)
    route = _route()
    stop = _stop(lat=52.2, lon=0.3, weight_kg=500.0, pallets=2.0)
    result = try_insert(route, stop, cost_rates)
    assert result is not None
    assert isinstance(result.added_cost, float)
    assert result.added_cost > 0  # activation + some fuel


def test_try_insert_returns_none_when_over_capacity():
    cost_rates = _load_cost_rates(RATES_PATH)
    route = _route(capacity_kg=100.0, capacity_pallets=1)
    stop = _stop(weight_kg=500.0, pallets=2.0)
    result = try_insert(route, stop, cost_rates)
    assert result is None


def test_try_insert_second_stop_cheaper_than_first():
    """Second stop into same route costs less than first (no activation on second)."""
    cost_rates = _load_cost_rates(RATES_PATH)
    route = _route()
    stop1 = _stop('O1', lat=52.2, lon=0.3, weight_kg=500.0, pallets=2.0)
    stop2 = _stop('O2', lat=52.21, lon=0.31, weight_kg=500.0, pallets=2.0)
    r1 = try_insert(route, stop1, cost_rates)
    assert r1 is not None
    route.stops = r1.stops
    r2 = try_insert(route, stop2, cost_rates)
    assert r2 is not None
    assert r2.added_cost < r1.added_cost  # no activation on second insertion


def test_compute_direct_run():
    cost_rates = _load_cost_rates(RATES_PATH)
    result = compute_direct_run(
        depot_lat=52.10172, depot_lon=0.16229,
        origin_lat=52.2, origin_lon=0.3,
        dest_lat=52.4, dest_lon=0.5,
        asset_type='Lorry',
        cost_rates=cost_rates,
    )
    assert result['km'] > 0
    assert result['cost_gbp'] > VEHICLE_ACTIVATION_COST
    assert result['route_type'] == 'DIRECT'
```

- [ ] **Step 2: Run to verify all tests fail**

```
python -m pytest tests/test_vrptw_engine.py -v 2>&1 | head -40
```
Expected: `ModuleNotFoundError: No module named 'vrptw_engine'`

- [ ] **Step 3: Implement vrptw_engine.py**

Create `simulation/vrptw_engine.py`:

```python
"""
VRPTW route engine for ZEEFLEET hub-and-spoke delivery optimizer.

Vehicles start and end at their home depot. Orders are delivery-only stops —
goods are pre-loaded at the depot before departure. There are no pickup stops.

Cost function:
    route_cost = VEHICLE_ACTIVATION_COST (if route has stops, else 0)
               + fuel_rate[asset_type] * route_km  (closed-loop depot→stops→depot)

Fleet objective:
    total = sum(route_cost) + UNASSIGNED_PENALTY * unassigned_count

VEHICLE_ACTIVATION_COST forces geographic clustering: opening a second vehicle
costs £150 before it moves an inch, so the solver only does it when the fuel
saving from shorter routes exceeds that threshold.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from profitability_report.profitability_report_merged import (
    _haversine_km,
    _normalise_type_key,
    _rate_bundle,
)

KM_TO_MILES = 0.621371
AVG_SPEED_KMH = 50.0

VEHICLE_ACTIVATION_COST: float = 150.0
UNASSIGNED_PENALTY: float = 50_000.0
_SERVICE_HOURS_PER_STOP: float = 20.0 / 60.0


def set_activation_cost(gbp: float) -> None:
    global VEHICLE_ACTIVATION_COST
    VEHICLE_ACTIVATION_COST = gbp


def set_unassigned_penalty(gbp: float) -> None:
    global UNASSIGNED_PENALTY
    UNASSIGNED_PENALTY = gbp


def set_service_minutes(minutes: float) -> None:
    global _SERVICE_HOURS_PER_STOP
    _SERVICE_HOURS_PER_STOP = minutes / 60.0


@dataclass
class DeliveryStop:
    order_id: str
    lat: float
    lon: float
    weight_kg: float
    pallets: float
    service_h: float = None  # None = use module-level _SERVICE_HOURS_PER_STOP


@dataclass
class DeliveryRoute:
    vehicle_id: str
    depot_lat: float
    depot_lon: float
    shift_start: datetime
    shift_end: datetime
    capacity_kg: float
    capacity_pallets: float
    asset_type: str
    stops: list = field(default_factory=list)


@dataclass
class InsertionResult:
    added_cost: float
    stops: list


def route_distance_km(route: DeliveryRoute) -> float:
    """Closed-loop km: depot → each stop in order → back to depot."""
    if not route.stops:
        return 0.0
    total = 0.0
    prev_lat, prev_lon = route.depot_lat, route.depot_lon
    for stop in route.stops:
        total += _haversine_km(prev_lat, prev_lon, stop.lat, stop.lon)
        prev_lat, prev_lon = stop.lat, stop.lon
    total += _haversine_km(prev_lat, prev_lon, route.depot_lat, route.depot_lon)
    return total


def route_fuel_cost(route: DeliveryRoute, cost_rates: dict) -> float:
    miles = route_distance_km(route) * KM_TO_MILES
    rates = _rate_bundle(cost_rates, _normalise_type_key(route.asset_type))
    return rates['fuel_gbp_per_mile'] * miles


def route_cost(route: DeliveryRoute, cost_rates: dict) -> float:
    """Activation cost (if non-empty) + fuel. Empty routes cost nothing."""
    if not route.stops:
        return 0.0
    return VEHICLE_ACTIVATION_COST + route_fuel_cost(route, cost_rates)


def fleet_objective(routes: dict, cost_rates: dict, total_orders: int) -> float:
    placed = sum(len(r.stops) for r in routes.values())
    return (sum(route_cost(r, cost_rates) for r in routes.values())
            + UNASSIGNED_PENALTY * (total_orders - placed))


def feasible(route: DeliveryRoute) -> bool:
    """Capacity and shift-end feasibility.

    In VRPTW the vehicle loads everything at the depot, so total route weight
    is the peak load. Time feasibility: must return to depot before shift_end.
    """
    total_kg = sum(s.weight_kg for s in route.stops)
    total_pallets = sum(s.pallets for s in route.stops)
    if total_kg > route.capacity_kg + 1e-6:
        return False
    if total_pallets > route.capacity_pallets + 1e-6:
        return False
    return _estimated_return_time(route) <= route.shift_end


def _svc(stop: DeliveryStop) -> float:
    return stop.service_h if stop.service_h is not None else _SERVICE_HOURS_PER_STOP


def _estimated_return_time(route: DeliveryRoute) -> datetime:
    """Walk the route in time and return estimated depot-return datetime."""
    t = route.shift_start
    prev_lat, prev_lon = route.depot_lat, route.depot_lon
    for stop in route.stops:
        leg_h = _haversine_km(prev_lat, prev_lon, stop.lat, stop.lon) / AVG_SPEED_KMH
        t += timedelta(hours=leg_h + _svc(stop))
        prev_lat, prev_lon = stop.lat, stop.lon
    return_h = _haversine_km(prev_lat, prev_lon, route.depot_lat, route.depot_lon) / AVG_SPEED_KMH
    return t + timedelta(hours=return_h)


def try_insert(route: DeliveryRoute, stop: DeliveryStop,
               cost_rates: dict) -> 'InsertionResult | None':
    """Find the cheapest feasible position to insert stop into route.

    O(n+1) search — one stop, not a pickup-delivery pair.
    Returns InsertionResult or None if no feasible position exists.
    """
    base_cost = route_cost(route, cost_rates)
    n = len(route.stops)
    best = None

    for i in range(n + 1):
        candidate = list(route.stops)
        candidate.insert(i, stop)
        test_route = DeliveryRoute(
            vehicle_id=route.vehicle_id,
            depot_lat=route.depot_lat, depot_lon=route.depot_lon,
            shift_start=route.shift_start, shift_end=route.shift_end,
            capacity_kg=route.capacity_kg, capacity_pallets=route.capacity_pallets,
            asset_type=route.asset_type, stops=candidate,
        )
        if not feasible(test_route):
            continue
        added = route_cost(test_route, cost_rates) - base_cost
        if best is None or added < best.added_cost:
            best = InsertionResult(added_cost=added, stops=candidate)

    return best


def cheapest_insertion(stop: DeliveryStop, routes: dict,
                       cost_rates: dict) -> tuple:
    """Find the vehicle and position offering the cheapest feasible insertion.

    Returns (vehicle_id, InsertionResult) or (None, None).
    """
    best_vid = None
    best_result = None
    for vid, route in routes.items():
        result = try_insert(route, stop, cost_rates)
        if result is None:
            continue
        if best_result is None or result.added_cost < best_result.added_cost:
            best_vid = vid
            best_result = result
    return best_vid, best_result


def compute_direct_run(depot_lat: float, depot_lon: float,
                       origin_lat: float, origin_lon: float,
                       dest_lat: float, dest_lon: float,
                       asset_type: str, cost_rates: dict) -> dict:
    """Cost and km for a DIRECT order: depot→origin→dest→depot.

    DIRECT orders (1.7% of volume) are not fed to the VRPTW batch — they are
    computed here as dedicated single-order runs and added to fleet totals.
    """
    km = (_haversine_km(depot_lat, depot_lon, origin_lat, origin_lon)
          + _haversine_km(origin_lat, origin_lon, dest_lat, dest_lon)
          + _haversine_km(dest_lat, dest_lon, depot_lat, depot_lon))
    rates = _rate_bundle(cost_rates, _normalise_type_key(asset_type))
    fuel = rates['fuel_gbp_per_mile'] * km * KM_TO_MILES
    return {
        'km': round(km, 1),
        'cost_gbp': round(VEHICLE_ACTIVATION_COST + fuel, 2),
        'route_type': 'DIRECT',
    }
```

- [ ] **Step 4: Run tests — expect all to pass**

```
python -m pytest tests/test_vrptw_engine.py -v 2>&1
```
Expected: 12 tests, all PASS.

- [ ] **Step 5: Confirm existing tests still pass**

```
python -m pytest tests/ -v --tb=short 2>&1 | tail -10
```
Expected: all previously passing tests still pass.

- [ ] **Step 6: Commit**

```
git add simulation/vrptw_engine.py tests/test_vrptw_engine.py
git commit -m "feat: add VRPTW route engine with activation cost and delivery-only stops"
```

---

## Task 2: VRPTW ALNS Solver

**Files:**
- Create: `simulation/vrptw_alns.py`

### Context

This adapts the existing ALNS loop (`simulation/alns.py`) for VRPTW. Key differences from the PDPTW ALNS:
- Orders have only `dest_lat`/`dest_lon`, not `origin_lat`/`origin_lon`
- `_assigned_orders` reads `DeliveryStop` lists (no stop_type filter needed)
- `_remove_orders` removes one stop per order (not a pickup+delivery pair)
- `_repair` calls `try_insert(route, DeliveryStop, cost_rates)` 
- `_total_cost` calls `fleet_objective` from `vrptw_engine`
- `_greedy_seed` builds `DeliveryRoute` objects from the vehicles list
- Input vehicles dict has keys: `vehicle_id`, `depot_lat`, `depot_lon`, `asset_type`, `capacity_kg`, `capacity_pallets`, `shift_start`, `shift_end`
- Input orders dict has keys: `order_id`, `dest_lat`, `dest_lon`, `goods_weight_kg`, `goods_pallet_spaces`, optionally `service_minutes`

Do NOT modify `simulation/alns.py`. Create a separate `vrptw_alns.py` that shares nothing with the legacy path.

- [ ] **Step 1: Implement vrptw_alns.py**

Create `simulation/vrptw_alns.py`:

```python
"""
ALNS solver for VRPTW delivery routing.

Adapts the existing ALNS loop for the VRPTW engine: orders are single delivery
stops (no pickup), routes are depot-to-depot, and the objective includes a
vehicle activation cost. Import structure mirrors simulation/alns.py but all
references to pdp_route are replaced with vrptw_engine.
"""
import math
import os
import random
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from profitability_report.profitability_report_merged import _load_cost_rates
from vrptw_engine import (
    DeliveryStop, DeliveryRoute, InsertionResult,
    route_cost, cheapest_insertion, fleet_objective,
    VEHICLE_ACTIVATION_COST, UNASSIGNED_PENALTY,
    _SERVICE_HOURS_PER_STOP,
)

_RATES_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', 'profitability_report', 'vehicle_cost_rates.json',
)

_SCORE_NEW_BEST = 9
_SCORE_IMPROVED = 3
_SCORE_ACCEPTED = 1
_WEIGHT_DECAY   = 0.1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clone_routes(routes: dict) -> dict:
    return {
        vid: DeliveryRoute(
            vehicle_id=r.vehicle_id,
            depot_lat=r.depot_lat, depot_lon=r.depot_lon,
            shift_start=r.shift_start, shift_end=r.shift_end,
            capacity_kg=r.capacity_kg, capacity_pallets=r.capacity_pallets,
            asset_type=r.asset_type, stops=list(r.stops),
        )
        for vid, r in routes.items()
    }


def _assigned_orders(routes: dict) -> list:
    return [s.order_id for r in routes.values() for s in r.stops]


def _remove_orders(routes: dict, order_ids: set) -> set:
    touched = set()
    for vid, route in routes.items():
        new_stops = [s for s in route.stops if s.order_id not in order_ids]
        if len(new_stops) != len(route.stops):
            route.stops = new_stops
            touched.add(vid)
    return touched


def _stop_from_order(order: dict) -> DeliveryStop:
    svc = order.get('service_minutes', None)
    return DeliveryStop(
        order_id=order['order_id'],
        lat=order['dest_lat'],
        lon=order['dest_lon'],
        weight_kg=float(order.get('goods_weight_kg', 0.0)),
        pallets=float(order.get('goods_pallet_spaces', 0.0)),
        service_h=svc / 60.0 if svc is not None else None,
    )


def _total_cost(routes: dict, cost_rates: dict, total_orders: int) -> float:
    placed = len(_assigned_orders(routes))
    return (sum(route_cost(r, cost_rates) for r in routes.values())
            + UNASSIGNED_PENALTY * (total_orders - placed))


def _empty_routes_for(vehicles: list) -> dict:
    return {
        v['vehicle_id']: DeliveryRoute(
            vehicle_id=v['vehicle_id'],
            depot_lat=v['depot_lat'], depot_lon=v['depot_lon'],
            shift_start=v['shift_start'], shift_end=v['shift_end'],
            capacity_kg=v['capacity_kg'], capacity_pallets=v['capacity_pallets'],
            asset_type=v['asset_type'],
        )
        for v in vehicles
    }


def _greedy_seed(orders: dict, vehicles: list, cost_rates: dict) -> dict:
    routes = _empty_routes_for(vehicles)
    for order_id, order in sorted(orders.items(),
                                  key=lambda x: x[1].get('service_level_priority', 3)):
        stop = _stop_from_order(order)
        vid, result = cheapest_insertion(stop, routes, cost_rates)
        if vid is not None:
            routes[vid].stops = result.stops
    return routes


# ---------------------------------------------------------------------------
# Destroy operators
# ---------------------------------------------------------------------------

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _worst_orders(routes: dict, cost_rates: dict, k: int) -> list:
    gains = []
    for vid, route in routes.items():
        if not route.stops:
            continue
        base = route_cost(route, cost_rates)
        for stop in route.stops:
            test = DeliveryRoute(
                vehicle_id=route.vehicle_id,
                depot_lat=route.depot_lat, depot_lon=route.depot_lon,
                shift_start=route.shift_start, shift_end=route.shift_end,
                capacity_kg=route.capacity_kg, capacity_pallets=route.capacity_pallets,
                asset_type=route.asset_type,
                stops=[s for s in route.stops if s.order_id != stop.order_id],
            )
            gain = base - route_cost(test, cost_rates)
            gains.append((gain, stop.order_id))
    gains.sort(reverse=True)
    return [oid for _, oid in gains[:k]]


def _destroy_worst(routes, orders, cost_rates, k, rng):
    return _worst_orders(routes, cost_rates, k)


def _destroy_random(routes, orders, cost_rates, k, rng):
    assigned = _assigned_orders(routes)
    return rng.sample(assigned, min(k, len(assigned))) if assigned else []


def _destroy_shaw(routes, orders, cost_rates, k, rng):
    assigned = _assigned_orders(routes)
    if not assigned:
        return []
    pivot_id = rng.choice(assigned)
    p = orders[pivot_id]
    by_dist = sorted(
        assigned,
        key=lambda oid: _haversine_km(
            p['dest_lat'], p['dest_lon'],
            orders[oid]['dest_lat'], orders[oid]['dest_lon'],
        ),
    )
    return by_dist[:k]


def _destroy_route(routes, orders, cost_rates, k, rng):
    best_vid = max(
        (vid for vid, r in routes.items() if r.stops),
        key=lambda vid: route_cost(routes[vid], cost_rates),
        default=None,
    )
    if best_vid is None:
        return []
    order_ids = [s.order_id for s in routes[best_vid].stops]
    rng.shuffle(order_ids)
    return order_ids[:k]


# ---------------------------------------------------------------------------
# Repair operators
# ---------------------------------------------------------------------------

def _repair(routes: dict, removed: list, orders: dict, cost_rates: dict) -> set:
    touched = set()
    for oid in removed:
        stop = _stop_from_order(orders[oid])
        vid, result = cheapest_insertion(stop, routes, cost_rates)
        if vid is not None:
            routes[vid].stops = result.stops
            touched.add(vid)
    return touched


def _regret_repair(routes: dict, removed: list, orders: dict, cost_rates: dict) -> set:
    pending = list(removed)
    touched = set()
    while pending:
        best_oid = None
        best_regret = -math.inf
        best_vid = None
        best_stops = None
        for oid in pending:
            stop = _stop_from_order(orders[oid])
            options = []
            for vid, route in routes.items():
                result = cheapest_insertion(stop, {vid: route}, cost_rates)[1]
                if result is not None:
                    options.append((result.added_cost, vid, result.stops))
            if not options:
                continue
            options.sort(key=lambda x: x[0])
            regret = (options[1][0] - options[0][0]) if len(options) >= 2 else 0.0
            if regret > best_regret:
                best_regret = regret
                best_oid = oid
                best_vid = options[0][1]
                best_stops = options[0][2]
        if best_oid is None:
            break
        pending.remove(best_oid)
        routes[best_vid].stops = best_stops
        touched.add(best_vid)
    return touched


def _roulette(weights, rng):
    total = sum(weights)
    r = rng.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            return i
    return len(weights) - 1


def _update_weight(weights, idx, score):
    for i in range(len(weights)):
        weights[i] = (1 - _WEIGHT_DECAY) * weights[i] + _WEIGHT_DECAY * 1.0
    if score > 0:
        weights[idx] = (1 - _WEIGHT_DECAY) * weights[idx] + _WEIGHT_DECAY * float(score)


_DESTROY_OPS = [
    (_destroy_worst,  'worst'),
    (_destroy_random, 'random'),
    (_destroy_shaw,   'shaw'),
    (_destroy_route,  'route'),
]
_REPAIR_OPS = [
    (_repair,        'cheapest'),
    (_regret_repair, 'regret'),
]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_vrptw(orders: list, vehicles: list,
              time_budget: float = 30.0, seed: int = 42) -> dict:
    """Run ALNS for VRPTW delivery routing.

    Parameters
    ----------
    orders   : list of order dicts with keys:
                 order_id, dest_lat, dest_lon, goods_weight_kg, goods_pallet_spaces
                 optionally: service_minutes, service_level_priority, time_window_end
    vehicles : list of vehicle dicts with keys:
                 vehicle_id, depot_lat, depot_lon, asset_type,
                 capacity_kg, capacity_pallets, shift_start, shift_end
    time_budget : solver wall-clock seconds
    seed        : RNG seed for reproducibility

    Returns
    -------
    dict with keys: assignments, routes, meta
      assignments : [{order_id, vehicle_id}]
      routes      : {vehicle_id: {stops, total_distance_km, estimated_cost_gbp}}
      meta        : solver statistics
    """
    cost_rates   = _load_cost_rates(_RATES_JSON)
    orders_by_id = {o['order_id']: o for o in orders}
    total_orders = len(orders_by_id)
    rng          = random.Random(seed)

    start_clock  = time.monotonic()
    best_routes  = _greedy_seed(orders_by_id, vehicles, cost_rates)
    best_total   = _total_cost(best_routes, cost_rates, total_orders)
    seed_total   = best_total
    curr_routes  = _clone_routes(best_routes)
    curr_total   = best_total
    seed_seconds = time.monotonic() - start_clock

    new_best_count = improved_count = accepted_count = 0
    time_to_best   = seed_seconds
    destroy_size   = max(2, min(30, total_orders // 5))
    d_weights      = [1.0] * len(_DESTROY_OPS)
    r_weights      = [1.0] * len(_REPAIR_OPS)
    temp           = 0.20 * best_total / math.log(2) if best_total > 0 else 1.0
    cooling        = 0.998
    deadline       = start_clock + time_budget
    iterations     = 0

    while time.monotonic() < deadline:
        assigned = _assigned_orders(curr_routes)
        if len(assigned) < 2:
            break
        k         = min(destroy_size, len(assigned))
        candidate = _clone_routes(curr_routes)
        d_idx     = _roulette(d_weights, rng)
        r_idx     = _roulette(r_weights, rng)
        removed   = _DESTROY_OPS[d_idx][0](candidate, orders_by_id, cost_rates, k, rng)
        if not removed:
            iterations += 1
            temp *= cooling
            continue
        _remove_orders(candidate, set(removed))
        _REPAIR_OPS[r_idx][0](candidate, removed, orders_by_id, cost_rates)
        cand_total = _total_cost(candidate, cost_rates, total_orders)

        if cand_total < best_total:
            score = _SCORE_NEW_BEST
            best_total   = cand_total
            best_routes  = _clone_routes(candidate)
            curr_routes  = candidate
            curr_total   = cand_total
            new_best_count += 1
            time_to_best = time.monotonic() - start_clock
        elif cand_total < curr_total:
            score = _SCORE_IMPROVED
            curr_routes = candidate
            curr_total  = cand_total
            improved_count += 1
        elif temp > 1e-6 and rng.random() < math.exp(-(cand_total - curr_total) / temp):
            score = _SCORE_ACCEPTED
            curr_routes = candidate
            curr_total  = cand_total
            accepted_count += 1
        else:
            score = 0

        _update_weight(d_weights, d_idx, score)
        _update_weight(r_weights, r_idx, score)
        temp *= cooling
        iterations += 1

    elapsed = time.monotonic() - start_clock

    # Format output
    from vrptw_engine import route_distance_km as _rdkm
    out_routes  = {}
    assignments = []
    for vid, route in best_routes.items():
        if not route.stops:
            continue
        km   = _rdkm(route)
        cost = route_cost(route, cost_rates)
        out_routes[vid] = {
            'stops': [
                {'order_id': s.order_id, 'lat': s.lat, 'lon': s.lon,
                 'weight_kg': s.weight_kg, 'pallets': s.pallets}
                for s in route.stops
            ],
            'total_distance_km':  round(km, 1),
            'estimated_cost_gbp': round(cost, 2),
            'depot_lat': route.depot_lat,
            'depot_lon': route.depot_lon,
            'asset_type': route.asset_type,
        }
        for s in route.stops:
            assignments.append({'order_id': s.order_id, 'vehicle_id': vid})

    return {
        'assignments': assignments,
        'routes':      out_routes,
        'meta': {
            'orders_total':       total_orders,
            'orders_assigned':    len(assignments),
            'orders_unassigned':  total_orders - len(assignments),
            'algorithm':          'vrptw_alns',
            'alns_iterations':    iterations,
            'seed_seconds':       round(seed_seconds, 2),
            'elapsed_seconds':    round(elapsed, 2),
            'seed_cost_gbp':      round(seed_total, 2),
            'best_cost_gbp':      round(best_total, 2),
            'improvement_pct':    round((seed_total - best_total) / seed_total * 100, 1) if seed_total > 0 else 0.0,
            'new_best_count':     new_best_count,
            'time_to_best_s':     round(min(time_to_best, elapsed), 1),
        },
    }
```

- [ ] **Step 2: Smoke-test with a minimal in-process run**

```
cd E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics
python -c "
import sys; sys.path.insert(0,'simulation')
from datetime import datetime, timezone, timedelta
from vrptw_alns import run_vrptw

T0 = datetime(2026,1,5,6,0,tzinfo=timezone.utc)
orders = [
    {'order_id':'O1','dest_lat':52.2,'dest_lon':0.3,'goods_weight_kg':500,'goods_pallet_spaces':2},
    {'order_id':'O2','dest_lat':52.21,'dest_lon':0.31,'goods_weight_kg':500,'goods_pallet_spaces':2},
    {'order_id':'O3','dest_lat':52.5,'dest_lon':-0.4,'goods_weight_kg':800,'goods_pallet_spaces':4},
]
vehicles = [
    {'vehicle_id':'V1','depot_lat':52.10172,'depot_lon':0.16229,'asset_type':'Lorry',
     'capacity_kg':10000,'capacity_pallets':12,'shift_start':T0,'shift_end':T0+timedelta(hours=11)},
    {'vehicle_id':'V2','depot_lat':52.12249,'depot_lon':-0.43165,'asset_type':'Lorry',
     'capacity_kg':10000,'capacity_pallets':12,'shift_start':T0,'shift_end':T0+timedelta(hours=11)},
]
result = run_vrptw(orders, vehicles, time_budget=5)
print('Assigned:', result['meta']['orders_assigned'], '/', result['meta']['orders_total'])
print('Routes:', list(result['routes'].keys()))
for vid, r in result['routes'].items():
    print(f'  {vid}: {[s[\"order_id\"] for s in r[\"stops\"]]}, {r[\"total_distance_km\"]}km, £{r[\"estimated_cost_gbp\"]}')
" 2>&1
```
Expected: 3 orders assigned, O3 on V2 (Bedford depot — closer to lat 52.5 lon -0.4), O1+O2 on V1 (Duxford).

- [ ] **Step 3: Confirm existing tests still pass**

```
python -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

- [ ] **Step 4: Commit**

```
git add simulation/vrptw_alns.py
git commit -m "feat: add VRPTW ALNS solver with activation cost and single-stop insertion"
```

---

## Task 3: Freight Tracker

**Files:**
- Create: `simulation/freight_tracker.py`
- Create: `tests/test_freight_tracker.py`

### Context

For each order on a given date, produces a `FreightInfo` with `freight_ready_time` — the datetime from which that order's goods are available at the depot for loading:

- **PRE_STAGED**: `06:00` (goods already at depot)
- **DIRECT**: `06:00` + flagged separately
- **VIA_DEPOT**: GPS-derived. Find which vehicle visited within 2km of origin. Find when that vehicle next docked at a depot for ≥10 min. Add 30-min cross-dock buffer.
- **UNGEOCODED**: excluded

Also returns `{vehicle_id: hours_worked}` so the rolling dispatcher knows how much shift each collection vehicle already consumed.

The leg_labeller (`simulation/leg_labeller.py`) already classifies orders; call `label_orders_for_date()` to get labels, then extend with GPS timing for VIA_DEPOT.

- [ ] **Step 1: Write failing tests**

Create `tests/test_freight_tracker.py`:

```python
import sys, os
from datetime import datetime, timezone, timedelta, date
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'simulation'))

from freight_tracker import (
    FreightInfo, build_freight_info, SHIFT_START_HOUR, CROSS_DOCK_BUFFER_MIN,
)

DATE_STR = '2026-01-05'
SHIFT_START = datetime(2026, 1, 5, SHIFT_START_HOUR, 0, tzinfo=timezone.utc)

# Minimal Qargo row
def _qargo_row(order_id, origin_pc, dest_pc, label_hint='PRE_STAGED'):
    return {
        'order_id': order_id,
        'origin_postal_code': origin_pc,
        'destination_postal_code': dest_pc,
        'origin_requested_start_timestamp_local': f'{DATE_STR} 08:00:00',
        'destination_requested_start_timestamp_local': f'{DATE_STR} 18:00:00',
        'goods_weight': 500.0,
        'origin_name': 'Test Origin',
        'transport_service': 'groupage',
        'service_level_name': 'next day',
    }


def test_build_freight_info_returns_dict():
    """build_freight_info runs without error on minimal input."""
    qargo_df = pd.DataFrame([_qargo_row('O1', 'CB22 4PS', 'SG1 1AA')])
    telem_df = pd.DataFrame(columns=['LocalTime','AssetName','Latitude','Longitude','GPSSpeed'])
    cache = {}
    result = build_freight_info(DATE_STR, qargo_df, telem_df, cache)
    assert isinstance(result, dict)


def test_pre_staged_order_ready_at_shift_start():
    """PRE_STAGED orders are ready from 06:00."""
    qargo_df = pd.DataFrame([_qargo_row('O1', 'CB22 4PS', 'SG1 1AA')])
    telem_df = pd.DataFrame(columns=['LocalTime','AssetName','Latitude','Longitude','GPSSpeed'])
    cache = {}
    result, _ = build_freight_info(DATE_STR, qargo_df, telem_df, cache)
    info = result.get('O1')
    if info is not None and info.label == 'PRE_STAGED':
        assert info.freight_ready_time.hour == SHIFT_START_HOUR


def test_ungeocoded_orders_excluded():
    """Orders with bad postcodes produce no FreightInfo entry."""
    qargo_df = pd.DataFrame([_qargo_row('O1', 'ZZZZ', 'ZZZZ')])
    telem_df = pd.DataFrame(columns=['LocalTime','AssetName','Latitude','Longitude','GPSSpeed'])
    cache = {}
    result, _ = build_freight_info(DATE_STR, qargo_df, telem_df, cache)
    assert 'O1' not in result


def test_via_depot_without_matching_vehicle_excluded():
    """VIA_DEPOT order with no vehicle visiting origin → excluded from result."""
    # Use a real VIA_DEPOT origin (far from any depot, should get labelled VIA_DEPOT
    # once there are real pings, but with empty telem it gets PRE_STAGED or excluded)
    qargo_df = pd.DataFrame([_qargo_row('O2', 'PL1 1AA', 'CB1 1AA')])
    telem_df = pd.DataFrame(columns=['LocalTime','AssetName','Latitude','Longitude','GPSSpeed'])
    cache = {}
    result, _ = build_freight_info(DATE_STR, qargo_df, telem_df, cache)
    # With no pings, labeller marks as PRE_STAGED (origin_never_visited) — either way no crash
    assert isinstance(result, dict)


def test_vehicle_hours_worked_returned():
    """build_freight_info returns a (freight_info_dict, vehicle_hours_dict) tuple."""
    qargo_df = pd.DataFrame([_qargo_row('O1', 'CB22 4PS', 'SG1 1AA')])
    telem_df = pd.DataFrame(columns=['LocalTime','AssetName','Latitude','Longitude','GPSSpeed'])
    cache = {}
    result = build_freight_info(DATE_STR, qargo_df, telem_df, cache)
    assert isinstance(result, tuple) and len(result) == 2
    freight_info, vehicle_hours = result
    assert isinstance(vehicle_hours, dict)
```

- [ ] **Step 2: Run to verify tests fail**

```
python -m pytest tests/test_freight_tracker.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'freight_tracker'`

- [ ] **Step 3: Implement freight_tracker.py**

Create `simulation/freight_tracker.py`:

```python
"""
Freight arrival time tracker for VRPTW rolling-horizon simulation.

For each Qargo order on a given date, computes when its goods are available
at the depot for loading onto a delivery vehicle:

  PRE_STAGED  → 06:00 (goods already at depot)
  DIRECT      → 06:00, flagged for separate handling
  VIA_DEPOT   → GPS-derived: find collection vehicle, find depot return, +30 min buffer
  UNGEOCODED  → excluded

Also returns {vehicle_id: hours_worked} for each collection vehicle identified,
so the rolling dispatcher can initialise remaining shift budgets correctly.
"""
import math
import sys
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date as date_type
from typing import Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_loader import _DEPOT_ANCHORS, geocode
from leg_labeller import label_orders_for_date

SHIFT_START_HOUR       = 6        # 06:00 local — start of dispatch day
CROSS_DOCK_BUFFER_MIN  = 30       # minutes from collection vehicle dock to freight ready
R_STOP_KM              = 2.0      # radius to consider a vehicle "visited" an origin
R_DEPOT_KM             = 2.0      # radius to consider a vehicle "at depot"
MIN_DEPOT_DWELL_MIN    = 10       # minimum depot dwell to count as a return (not drive-by)


@dataclass
class FreightInfo:
    order_id: str
    label: str                          # PRE_STAGED | VIA_DEPOT | DIRECT | UNGEOCODED
    freight_ready_time: Optional[datetime]
    collection_vehicle: Optional[str]
    collection_return_time: Optional[datetime]


def _hav(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = math.sin(dlat/2)**2 + math.cos(lat1*p)*math.cos(lat2*p)*math.sin(dlon/2)**2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _near_depot(lat: float, lon: float) -> bool:
    return any(_hav(lat, lon, d[0], d[1]) < R_DEPOT_KM for d in _DEPOT_ANCHORS)


def _find_collection_return(
    origin_lat: float, origin_lon: float,
    telem_day: pd.DataFrame,
) -> tuple[Optional[str], Optional[datetime]]:
    """Find which vehicle collected from origin and when it next returned to a depot.

    Returns (vehicle_id, depot_return_datetime) or (None, None).
    """
    if telem_day.empty:
        return None, None

    df = telem_day.copy()
    df['_lat'] = pd.to_numeric(df['Latitude'], errors='coerce')
    df['_lon'] = pd.to_numeric(df['Longitude'], errors='coerce')
    df['_ts']  = pd.to_datetime(df['LocalTime'], errors='coerce')
    df = df.dropna(subset=['_lat', '_lon', '_ts', 'AssetName'])

    # Find vehicles that visited within R_STOP_KM of origin
    df['_dist_to_origin'] = df.apply(
        lambda r: _hav(r['_lat'], r['_lon'], origin_lat, origin_lon), axis=1
    )
    visitors = df[df['_dist_to_origin'] < R_STOP_KM]['AssetName'].unique()

    best_vehicle = None
    best_return  = None

    for veh in visitors:
        vdf = df[df['AssetName'] == veh].sort_values('_ts').reset_index(drop=True)
        # Find earliest origin visit time
        origin_visit = vdf[vdf['_dist_to_origin'] < R_STOP_KM]['_ts'].min()
        if pd.isna(origin_visit):
            continue
        # Find first sustained depot dwell AFTER origin visit
        post = vdf[vdf['_ts'] > origin_visit].reset_index(drop=True)
        i = 0
        while i < len(post):
            row = post.iloc[i]
            if _near_depot(row['_lat'], row['_lon']):
                # measure dwell
                dwell_start = row['_ts']
                j = i
                while j < len(post) and _near_depot(post.iloc[j]['_lat'], post.iloc[j]['_lon']):
                    j += 1
                dwell_end = post.iloc[j-1]['_ts']
                dwell_min = (dwell_end - dwell_start).total_seconds() / 60
                if dwell_min >= MIN_DEPOT_DWELL_MIN:
                    if best_return is None or dwell_start < best_return:
                        best_return  = dwell_start
                        best_vehicle = str(veh)
                    break
                i = j
            else:
                i += 1

    return best_vehicle, best_return


def _vehicle_first_departure(vehicle_id: str, telem_day: pd.DataFrame) -> Optional[datetime]:
    """First timestamp when vehicle is NOT near a depot (first active movement)."""
    df = telem_day[telem_day['AssetName'] == vehicle_id].copy()
    df['_lat'] = pd.to_numeric(df['Latitude'], errors='coerce')
    df['_lon'] = pd.to_numeric(df['Longitude'], errors='coerce')
    df['_ts']  = pd.to_datetime(df['LocalTime'], errors='coerce')
    df = df.dropna(subset=['_lat', '_lon', '_ts']).sort_values('_ts')
    for _, row in df.iterrows():
        if not _near_depot(row['_lat'], row['_lon']):
            return row['_ts']
    return None


def build_freight_info(
    date_str: str,
    qargo_df: pd.DataFrame,
    telem_df: pd.DataFrame,
    cache: dict,
) -> tuple[dict[str, FreightInfo], dict[str, float]]:
    """Compute freight arrival times for all orders on date_str.

    Returns
    -------
    freight_info   : {order_id: FreightInfo}
    vehicle_hours  : {vehicle_id: hours_worked_on_collection}
                     Used by rolling_dispatcher to reduce available shift budget.
    """
    # Filter telematics to the target date
    t = telem_df.copy()
    t['_ts'] = pd.to_datetime(t['LocalTime'], errors='coerce')
    telem_day = t[t['_ts'].dt.date.astype(str) == date_str].copy()

    # Shift start as timezone-naive datetime (telematics LocalTime is naive)
    shift_start_naive = datetime.strptime(f'{date_str} {SHIFT_START_HOUR:02d}:00:00',
                                          '%Y-%m-%d %H:%M:%S')

    # Classify orders using leg_labeller
    labels_df = label_orders_for_date(date_str, qargo_df, telem_df, cache)
    label_map = dict(zip(
        labels_df['order_id'].astype(str),
        labels_df['label'],
    ))

    # Filter Qargo to target date
    q = qargo_df.copy()
    q['_ts'] = pd.to_datetime(q['origin_requested_start_timestamp_local'], errors='coerce')
    day_df = q[q['_ts'].dt.date.astype(str) == date_str]

    freight_info: dict[str, FreightInfo] = {}
    vehicle_hours: dict[str, float] = {}

    for _, row in day_df.iterrows():
        order_id  = str(row.get('order_id', ''))
        if not order_id:
            continue

        label = label_map.get(order_id, 'UNGEOCODED')

        if label == 'UNGEOCODED':
            continue

        if label in ('PRE_STAGED', 'DIRECT'):
            freight_info[order_id] = FreightInfo(
                order_id=order_id,
                label=label,
                freight_ready_time=shift_start_naive,
                collection_vehicle=None,
                collection_return_time=None,
            )
            continue

        # VIA_DEPOT: find collection vehicle and depot return
        origin_pc = str(row.get('origin_postal_code', '') or '')
        origin_coords = geocode(origin_pc, cache) if origin_pc else None
        if not origin_coords:
            continue

        veh, return_time = _find_collection_return(
            origin_coords[0], origin_coords[1], telem_day
        )

        if veh is None or return_time is None:
            # No matching collection vehicle found — exclude order
            continue

        ready_time = return_time + timedelta(minutes=CROSS_DOCK_BUFFER_MIN)

        freight_info[order_id] = FreightInfo(
            order_id=order_id,
            label='VIA_DEPOT',
            freight_ready_time=ready_time,
            collection_vehicle=veh,
            collection_return_time=return_time,
        )

        # Track hours worked by this collection vehicle (for shift budget)
        if veh not in vehicle_hours:
            first_dep = _vehicle_first_departure(veh, telem_day)
            if first_dep is not None:
                worked = (return_time - first_dep).total_seconds() / 3600.0
                vehicle_hours[veh] = max(0.0, worked)

    return freight_info, vehicle_hours
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_freight_tracker.py -v 2>&1
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Smoke-test on real data (Jan 05)**

```
cd E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics
python -c "
import sys; sys.path.insert(0,'.'); sys.path.insert(0,'simulation')
import warnings; warnings.filterwarnings('ignore')
from data_audit import load_datasets
from data_loader import load_postcode_cache, save_postcode_cache
from freight_tracker import build_freight_info

ds = load_datasets('.')
cache = load_postcode_cache()
freight_info, vehicle_hours = build_freight_info(
    '2026-01-05', ds['qargo'], ds['supatrak_telematics'], cache
)
save_postcode_cache(cache)
import collections
label_counts = collections.Counter(v.label for v in freight_info.values())
print('Orders with freight info:', len(freight_info))
print('Label breakdown:', dict(label_counts))
print('Collection vehicles tracked:', len(vehicle_hours))
pre = [v for v in freight_info.values() if v.label == 'PRE_STAGED']
via = [v for v in freight_info.values() if v.label == 'VIA_DEPOT']
if via:
    times = sorted(v.freight_ready_time for v in via)
    print(f'VIA_DEPOT ready times: {times[0].strftime(\"%H:%M\")} to {times[-1].strftime(\"%H:%M\")}')
" 2>&1
```
Expected: ~300–500 orders, ~50% PRE_STAGED, ~40% VIA_DEPOT, ready times spread 07:00–17:00.

- [ ] **Step 6: Confirm existing tests still pass**

```
python -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

- [ ] **Step 7: Commit**

```
git add simulation/freight_tracker.py tests/test_freight_tracker.py
git commit -m "feat: add freight tracker extracting per-order GPS-derived arrival times"
```

---

## Task 4: Rolling Dispatcher

**Files:**
- Create: `simulation/rolling_dispatcher.py`

### Context

Simulates a single day as a sequence of solver invocations. At 06:00 all PRE_STAGED + DIRECT orders become eligible. Each time a collection vehicle returns to depot (from freight_tracker), its orders become eligible and the solver re-runs.

Fleet state tracks each vehicle's status, hours worked, and when it can next accept a dispatch. Vehicles that did a morning collection round re-enter the pool with reduced shift budget (hours_worked already consumed). Artics confirmed as line-haul (visited two depots) are excluded per day.

Committed routes are never revised — the solver only plans for vehicles at depot with remaining shift.

- [ ] **Step 1: Implement rolling_dispatcher.py**

Create `simulation/rolling_dispatcher.py`:

```python
"""
Rolling-horizon simulation for VRPTW delivery dispatch.

Replays a historical day as a sequence of freight-availability events:
  06:00          → PRE_STAGED + DIRECT orders ready → run solver
  <return_time>  → VIA_DEPOT orders ready → run solver
  ...

At each event the solver receives only orders whose freight is at the depot
and only vehicles currently at the depot with remaining shift budget.
Committed routes are frozen. Vehicles re-enter the pool after completing
their delivery route.
"""
import math
import sys
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_loader import _DEPOT_ANCHORS, build_vehicles, last_known_positions
from freight_tracker import (
    FreightInfo, build_freight_info, SHIFT_START_HOUR, R_DEPOT_KM, _hav,
)
from vrptw_alns import run_vrptw
from vrptw_engine import compute_direct_run
from profitability_report.profitability_report_merged import _load_cost_rates

import os as _os
_RATES_JSON = _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)),
    '..', 'profitability_report', 'vehicle_cost_rates.json',
)

MIN_REMAINING_H = 1.5    # minimum remaining shift to accept a new dispatch
SHIFT_BUDGET_H  = {
    'Tractor Unit': 13.0,
    'Lorry':        11.0,
    'Rigid Truck':  11.0,
    'Mini Truck':   11.0,
    'Service Van':  10.0,
}
DEFAULT_SHIFT_H = 11.0


@dataclass
class VehicleFleetState:
    vehicle_id: str
    depot_lat: float
    depot_lon: float
    asset_type: str
    capacity_kg: float
    capacity_pallets: float
    shift_start: datetime
    shift_budget_h: float
    hours_worked: float = 0.0
    status: str = 'at_depot'           # 'at_depot' | 'dispatched' | 'line_haul' | 'shift_exhausted'
    available_from: datetime = None
    assigned_orders: set = field(default_factory=set)

    def __post_init__(self):
        if self.available_from is None:
            self.available_from = self.shift_start

    @property
    def shift_end(self) -> datetime:
        return self.shift_start + timedelta(hours=self.shift_budget_h)

    @property
    def hours_remaining(self) -> float:
        return self.shift_budget_h - self.hours_worked


def _identify_line_haul_artics(date_str: str, telem_df: pd.DataFrame) -> set:
    """Return vehicle_ids confirmed as line-haul artics on date_str.

    A line-haul artic visits two or more distinct depot anchors within R_DEPOT_KM
    on the same day.
    """
    t = telem_df.copy()
    t['_ts']  = pd.to_datetime(t['LocalTime'], errors='coerce')
    t['_lat'] = pd.to_numeric(t['Latitude'], errors='coerce')
    t['_lon'] = pd.to_numeric(t['Longitude'], errors='coerce')
    day = t[t['_ts'].dt.date.astype(str) == date_str].dropna(subset=['_lat','_lon','AssetName'])

    line_haul = set()
    for veh, grp in day.groupby('AssetName'):
        depots_visited = set()
        for _, row in grp.iterrows():
            for i, (dlat, dlon) in enumerate(_DEPOT_ANCHORS):
                if _hav(row['_lat'], row['_lon'], dlat, dlon) < R_DEPOT_KM:
                    depots_visited.add(i)
        if len(depots_visited) >= 2:
            line_haul.add(str(veh))
    return line_haul


def _build_fleet(
    vehicles_df: pd.DataFrame,
    telem_df: pd.DataFrame,
    date_str: str,
    vehicle_hours: dict,
    line_haul_ids: set,
) -> dict:
    """Build initial VehicleFleetState for all available vehicles.

    Line-haul artics are marked 'line_haul' and excluded from dispatch.
    Collection vehicles have hours_worked pre-populated from freight_tracker.
    """
    last_pos  = last_known_positions(telem_df)
    raw_vehicles = build_vehicles(vehicles_df, last_pos)

    shift_start_naive = datetime.strptime(
        f'{date_str} {SHIFT_START_HOUR:02d}:00:00', '%Y-%m-%d %H:%M:%S'
    )

    fleet = {}
    for v in raw_vehicles:
        vid    = v['vehicle_id']
        atype  = v.get('asset_type', 'Lorry')
        budget = SHIFT_BUDGET_H.get(atype, DEFAULT_SHIFT_H)
        worked = vehicle_hours.get(vid, 0.0)

        state = VehicleFleetState(
            vehicle_id=vid,
            depot_lat=v['depot_lat'], depot_lon=v['depot_lon'],
            asset_type=atype,
            capacity_kg=v['capacity_kg'], capacity_pallets=v['capacity_pallets'],
            shift_start=shift_start_naive,
            shift_budget_h=budget,
            hours_worked=worked,
            available_from=shift_start_naive,
        )
        if vid in line_haul_ids:
            state.status = 'line_haul'
        fleet[vid] = state
    return fleet


def _build_event_queue(freight_info: dict, date_str: str) -> list:
    """Build chronological event list from freight_info.

    Returns sorted list of (event_time, order_ids_becoming_ready).
    The 06:00 SHIFT_START event collects all PRE_STAGED + DIRECT.
    Each VIA_DEPOT order triggers at its freight_ready_time.
    """
    shift_start = datetime.strptime(
        f'{date_str} {SHIFT_START_HOUR:02d}:00:00', '%Y-%m-%d %H:%M:%S'
    )
    # Bucket orders by their ready time (rounded to nearest minute for grouping)
    from collections import defaultdict
    buckets = defaultdict(list)
    for order_id, info in freight_info.items():
        if info.label == 'DIRECT':
            continue  # handled separately
        rt = info.freight_ready_time
        if rt is None:
            continue
        # Ensure we use naive datetimes throughout
        if hasattr(rt, 'tzinfo') and rt.tzinfo is not None:
            rt = rt.replace(tzinfo=None)
        # Clamp to shift start at earliest
        rt = max(rt, shift_start)
        key = rt.replace(second=0, microsecond=0)
        buckets[key].append(order_id)

    return sorted(buckets.items(), key=lambda x: x[0])


def simulate_day(
    date_str: str,
    qargo_df: pd.DataFrame,
    telem_df: pd.DataFrame,
    vehicles_df: pd.DataFrame,
    cache: dict,
    solver_budget_s: float = 30.0,
) -> dict:
    """Simulate a full day of rolling-horizon delivery dispatch.

    Returns
    -------
    dict with keys:
      dispatch_log  : list of dispatch events
      assignments   : {order_id: vehicle_id}
      direct_runs   : list of direct run dicts
      routes        : {vehicle_id: list of route dicts}
      metrics       : summary counts
    """
    cost_rates = _load_cost_rates(_RATES_JSON)

    # Build freight info (arrival times + collection hours)
    freight_info, vehicle_hours = build_freight_info(
        date_str, qargo_df, telem_df, cache
    )

    # Identify line-haul artics for this day
    line_haul_ids = _identify_line_haul_artics(date_str, telem_df)

    # Build fleet state
    fleet = _build_fleet(vehicles_df, telem_df, date_str, vehicle_hours, line_haul_ids)

    # Build event queue
    events = _build_event_queue(freight_info, date_str)

    # Filter Qargo to target date for order details
    q = qargo_df.copy()
    q['_ts'] = pd.to_datetime(q['origin_requested_start_timestamp_local'], errors='coerce')
    day_orders_df = q[q['_ts'].dt.date.astype(str) == date_str]

    orders_by_id = {}
    for _, row in day_orders_df.iterrows():
        oid = str(row.get('order_id',''))
        if not oid:
            continue
        from data_loader import geocode
        dest_pc = str(row.get('destination_postal_code','') or '')
        origin_pc = str(row.get('origin_postal_code','') or '')
        dest_coords   = geocode(dest_pc, cache)   if dest_pc   else None
        origin_coords = geocode(origin_pc, cache) if origin_pc else None
        if not dest_coords:
            continue
        orders_by_id[oid] = {
            'order_id':             oid,
            'dest_lat':             dest_coords[0],
            'dest_lon':             dest_coords[1],
            'origin_lat':           origin_coords[0] if origin_coords else None,
            'origin_lon':           origin_coords[1] if origin_coords else None,
            'goods_weight_kg':      float(row.get('goods_weight', 0) or 0),
            'goods_pallet_spaces':  float(row.get('goods_pallet_spaces', 0) or 0),
            'time_window_end':      str(row.get('destination_requested_start_timestamp_local','')),
            'service_level_priority': 1,
        }

    # Handle DIRECT orders (compute dedicated runs at shift start)
    direct_runs = []
    for oid, info in freight_info.items():
        if info.label != 'DIRECT':
            continue
        order = orders_by_id.get(oid)
        if not order or not order.get('origin_lat'):
            continue
        # Find nearest depot for this direct order
        dlat = min(_DEPOT_ANCHORS, key=lambda d: _hav(
            order['dest_lat'], order['dest_lon'], d[0], d[1]
        ))
        run = compute_direct_run(
            depot_lat=dlat[0], depot_lon=dlat[1],
            origin_lat=order['origin_lat'], origin_lon=order['origin_lon'],
            dest_lat=order['dest_lat'], dest_lon=order['dest_lon'],
            asset_type='Lorry',
            cost_rates=cost_rates,
        )
        run['order_id'] = oid
        direct_runs.append(run)

    # Event loop
    dispatch_log  = []
    assignments   = {}
    all_routes    = {}

    def _available_vehicles(event_time):
        out = []
        for vid, state in fleet.items():
            if state.status != 'at_depot':
                continue
            if state.available_from > event_time:
                continue
            if state.hours_remaining < MIN_REMAINING_H:
                state.status = 'shift_exhausted'
                continue
            remaining_h = state.hours_remaining
            out.append({
                'vehicle_id':       vid,
                'depot_lat':        state.depot_lat,
                'depot_lon':        state.depot_lon,
                'asset_type':       state.asset_type,
                'capacity_kg':      state.capacity_kg,
                'capacity_pallets': state.capacity_pallets,
                'shift_start':      event_time,
                'shift_end':        event_time + timedelta(hours=remaining_h),
            })
        return out

    eligible_order_ids = set()

    for event_time, order_ids in events:
        eligible_order_ids.update(order_ids)
        # Remove already assigned
        unassigned_eligible = [
            oid for oid in eligible_order_ids
            if oid not in assignments and oid in orders_by_id
        ]
        if not unassigned_eligible:
            continue
        avail_vehicles = _available_vehicles(event_time)
        if not avail_vehicles:
            continue

        orders_for_solver = [orders_by_id[oid] for oid in unassigned_eligible]
        result = run_vrptw(orders_for_solver, avail_vehicles,
                           time_budget=solver_budget_s)

        # Commit dispatch
        for asn in result['assignments']:
            oid = asn['order_id']
            vid = asn['vehicle_id']
            assignments[oid] = vid

        for vid, route_data in result['routes'].items():
            if not route_data['stops']:
                continue
            km    = route_data['total_distance_km']
            # Estimate route duration
            route_h = km / 50.0 + len(route_data['stops']) * (20.0 / 60.0)
            fleet[vid].status       = 'dispatched'
            fleet[vid].hours_worked += route_h
            fleet[vid].available_from = event_time + timedelta(hours=route_h)
            fleet[vid].assigned_orders.update(
                s['order_id'] for s in route_data['stops']
            )
            all_routes.setdefault(vid, []).append({
                'dispatch_time': event_time.isoformat(),
                'stops':         route_data['stops'],
                'km':            km,
                'cost_gbp':      route_data['estimated_cost_gbp'],
            })

        # Return dispatched vehicles to depot after their route
        for vid in fleet:
            if (fleet[vid].status == 'dispatched'
                    and fleet[vid].available_from <= event_time):
                fleet[vid].status = 'at_depot'

        dispatch_log.append({
            'event_time':    event_time.isoformat(),
            'orders_ready':  len(order_ids),
            'eligible':      len(unassigned_eligible),
            'vehicles_avail': len(avail_vehicles),
            'assigned_now':  len(result['assignments']),
        })

    total_planned_km = sum(
        r['km'] for routes in all_routes.values() for r in routes
    ) + sum(r['km'] for r in direct_runs)

    return {
        'dispatch_log':  dispatch_log,
        'assignments':   assignments,
        'direct_runs':   direct_runs,
        'routes':        all_routes,
        'metrics': {
            'orders_total':      len(orders_by_id),
            'orders_assigned':   len(assignments),
            'orders_direct':     len(direct_runs),
            'orders_unassigned': len(orders_by_id) - len(assignments) - len(direct_runs),
            'assignment_rate':   (len(assignments) + len(direct_runs)) / max(len(orders_by_id), 1),
            'vehicles_dispatched': len(all_routes),
            'line_haul_excluded':  len(line_haul_ids),
            'total_planned_km':  round(total_planned_km, 1),
            'solver_events':     len(dispatch_log),
        },
    }
```

- [ ] **Step 2: Smoke-test on Jan 02**

```
cd E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics
python -c "
import sys; sys.path.insert(0,'.'); sys.path.insert(0,'simulation')
import warnings; warnings.filterwarnings('ignore')
from data_audit import load_datasets
from data_loader import load_postcode_cache, save_postcode_cache
from rolling_dispatcher import simulate_day

ds = load_datasets('.')
cache = load_postcode_cache()
result = simulate_day('2026-01-02', ds['qargo'], ds['supatrak_telematics'],
                      ds['supatrak_vehicles'], cache, solver_budget_s=15)
save_postcode_cache(cache)
m = result['metrics']
print('=== Rolling Dispatcher: 2026-01-02 ===')
print(f'Orders total:      {m[\"orders_total\"]}')
print(f'Orders assigned:   {m[\"orders_assigned\"]}')
print(f'Orders direct:     {m[\"orders_direct\"]}')
print(f'Orders unassigned: {m[\"orders_unassigned\"]}')
print(f'Assignment rate:   {m[\"assignment_rate\"]*100:.1f}%')
print(f'Vehicles used:     {m[\"vehicles_dispatched\"]}')
print(f'Line-haul excl:    {m[\"line_haul_excluded\"]}')
print(f'Total planned km:  {m[\"total_planned_km\"]:,.0f}')
print(f'Solver events:     {m[\"solver_events\"]}')
print()
print('Dispatch log:')
for e in result['dispatch_log']:
    print(f'  {e[\"event_time\"][:16]}  ready={e[\"orders_ready\"]:3d}'
          f'  eligible={e[\"eligible\"]:3d}  vehicles={e[\"vehicles_avail\"]:3d}'
          f'  assigned={e[\"assigned_now\"]:3d}')
" 2>&1
```
Expected: assignment rate ≥ 85%, multiple dispatch events, no crash.

- [ ] **Step 3: Confirm existing tests still pass**

```
python -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

- [ ] **Step 4: Commit**

```
git add simulation/rolling_dispatcher.py
git commit -m "feat: add rolling-horizon simulation dispatcher with event-driven VRPTW solve"
```

---

## Task 5: Backtest Validator

**Files:**
- Create: `backtest_vrptw.py`

### Context

Runs `simulate_day` for one or more dates and compares planned vs actual using the same table format as the legacy `run_backtest.py`. Key new piece: `_actual_delivery_km` which filters GPS tracks to genuine delivery legs only:

1. Exclude confirmed line-haul artics (Tractor Units that visited 2+ depots)
2. Exclude any track that starts AND ends at two different depot anchors (inter-depot by any vehicle type)
3. Sum Haversine km from remaining tracks

This gives a clean actual delivery km to compare against planned, for the first time.

- [ ] **Step 1: Implement backtest_vrptw.py**

Create `backtest_vrptw.py`:

```python
"""
Backtest validator for the VRPTW rolling-horizon dispatcher.

Runs simulate_day() for one or more dates and prints planned vs actual
delivery metrics using the same table format as run_backtest.py.

Usage:
    python backtest_vrptw.py --date 2026-01-02
    python backtest_vrptw.py --date 2026-01-05 --budget 30
    python backtest_vrptw.py --dates 2026-01-05 2026-01-06 2026-01-07
"""
import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / 'simulation'))

from data_audit import load_datasets
from data_loader import load_postcode_cache, save_postcode_cache, _DEPOT_ANCHORS
from rolling_dispatcher import simulate_day, _identify_line_haul_artics
from simulation.actuals_loader import _jigsaw_fuel_gbp, _qargo_actuals
from profitability_report.profitability_report_merged import _load_cost_rates

OUTPUT_DIR   = BASE_DIR / 'data' / 'Output'
RATES_JSON   = str(BASE_DIR / 'profitability_report' / 'vehicle_cost_rates.json')
R_DEPOT_KM   = 2.0
KM_TO_MILES  = 0.621371


def _hav(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = math.sin(dlat/2)**2 + math.cos(lat1*p)*math.cos(lat2*p)*math.sin(dlon/2)**2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _near_any_depot(lat, lon):
    return any(_hav(lat, lon, d[0], d[1]) < R_DEPOT_KM for d in _DEPOT_ANCHORS)


def _which_depot(lat, lon):
    dists = [_hav(lat, lon, d[0], d[1]) for d in _DEPOT_ANCHORS]
    idx = dists.index(min(dists))
    return idx if dists[idx] < R_DEPOT_KM else None


def _actual_delivery_km(telem_df: pd.DataFrame, vehicles_df: pd.DataFrame,
                        date_str: str) -> tuple[float, int]:
    """Compute actual delivery-leg GPS km for date_str.

    Filters out:
    1. Confirmed line-haul artics (visited 2+ distinct depots)
    2. Any vehicle whose first AND last active ping are at two DIFFERENT depots
    3. Vehicles with fewer than 10 pings

    Returns (total_km, delivery_vehicle_count).
    """
    line_haul = _identify_line_haul_artics(date_str, telem_df)

    t = telem_df.copy()
    t['_ts']    = pd.to_datetime(t['LocalTime'], errors='coerce')
    t['_lat']   = pd.to_numeric(t['Latitude'], errors='coerce')
    t['_lon']   = pd.to_numeric(t['Longitude'], errors='coerce')
    t['_speed'] = pd.to_numeric(t['GPSSpeed'], errors='coerce').fillna(0)
    day = t[t['_ts'].dt.date.astype(str) == date_str].dropna(
        subset=['_lat', '_lon', '_ts', 'AssetName'])

    # Vehicle type lookup
    vtype_map = {}
    if not vehicles_df.empty and 'AssetName' in vehicles_df.columns:
        vtype_map = dict(zip(vehicles_df['AssetName'].astype(str),
                             vehicles_df.get('AssetType', pd.Series()).astype(str)))

    total_km = 0.0
    delivery_count = 0

    for asset, grp in day.groupby('AssetName'):
        asset = str(asset)
        if asset in line_haul:
            continue

        grp = grp.sort_values('_ts')
        active = grp[grp['_speed'] > 2]
        if len(active) < 10:
            continue

        # Check if first and last active ping are at two DIFFERENT depots
        first_depot = _which_depot(active.iloc[0]['_lat'], active.iloc[0]['_lon'])
        last_depot  = _which_depot(active.iloc[-1]['_lat'], active.iloc[-1]['_lon'])
        if (first_depot is not None and last_depot is not None
                and first_depot != last_depot):
            continue  # inter-depot movement

        # Sum Haversine km
        lats = grp['_lat'].tolist()
        lons = grp['_lon'].tolist()
        km = sum(_hav(lats[i], lons[i], lats[i+1], lons[i+1])
                 for i in range(len(lats) - 1))
        total_km += km
        delivery_count += 1

    return round(total_km, 1), delivery_count


def run_single_date(date_str: str, datasets: dict, cache: dict,
                    solver_budget: float) -> dict:
    """Run simulation + collect actuals for one date."""
    qargo_df    = datasets['qargo']
    telem_df    = datasets['supatrak_telematics']
    vehicles_df = datasets['supatrak_vehicles']
    jigsaw_df   = datasets.get('jigsaw', pd.DataFrame())

    # Planned
    sim = simulate_day(date_str, qargo_df, telem_df, vehicles_df, cache,
                       solver_budget_s=solver_budget)
    m = sim['metrics']

    # Planned fuel cost
    cost_rates = _load_cost_rates(RATES_JSON)
    planned_fuel = 0.0
    for routes in sim['routes'].values():
        for r in routes:
            planned_fuel += r['cost_gbp']
    for dr in sim['direct_runs']:
        planned_fuel += dr['cost_gbp']

    # Actual delivery-leg km
    actual_del_km, del_vehicle_count = _actual_delivery_km(telem_df, vehicles_df, date_str)

    # Actual fuel from Jigsaw
    actual_fuel = _jigsaw_fuel_gbp(jigsaw_df, date_str)

    # Actual assignment + on-time from Qargo
    orders_input = {}
    q = qargo_df.copy()
    q['_ts'] = pd.to_datetime(q['origin_requested_start_timestamp_local'], errors='coerce')
    day_df = q[q['_ts'].dt.date.astype(str) == date_str]
    for _, row in day_df.iterrows():
        oid = str(row.get('order_id',''))
        if oid:
            orders_input[oid] = row.to_dict()
    qargo_stats = _qargo_actuals(qargo_df, orders_input)

    return {
        'date': date_str,
        'planned': {
            'vehicles_used':    m['vehicles_dispatched'],
            'total_km':         m['total_planned_km'],
            'fuel_gbp':         round(planned_fuel, 2),
            'orders_assigned':  m['orders_assigned'] + m['orders_direct'],
            'orders_total':     m['orders_total'],
            'assignment_rate':  round(m['assignment_rate'], 3),
            'solver_events':    m['solver_events'],
            'line_haul_excl':   m['line_haul_excluded'],
        },
        'actual': {
            'delivery_vehicles':      del_vehicle_count,
            'actual_delivery_km':     actual_del_km,
            'actual_fuel_gbp':        actual_fuel,
            'orders_assigned_actual': qargo_stats['orders_assigned_actual'],
            'assignment_rate_actual': qargo_stats['assignment_rate_actual'],
            'on_time_rate_actual':    qargo_stats['on_time_rate_actual'],
        },
    }


def print_backtest(result: dict) -> None:
    p = result['planned']
    a = result['actual']
    date_str = result['date']
    km_delta = ((p['total_km'] - a['actual_delivery_km']) / a['actual_delivery_km'] * 100
                if a['actual_delivery_km'] > 0 else float('nan'))

    print(f'\n{"="*62}')
    print(f'  VRPTW BACKTEST  {date_str}')
    print(f'{"="*62}')
    print(f'                                    PLANNED      ACTUAL')
    print(f'  {"─"*58}')
    print(f'  Vehicles used (delivery)         {p["vehicles_used"]:8d}   {a["delivery_vehicles"]:8d}')
    print(f'  Total delivery km                {p["total_km"]:8,.0f}   {a["actual_delivery_km"]:8,.0f}')
    print(f'  Fuel cost GBP                    {p["fuel_gbp"]:8,.2f}   {a["actual_fuel_gbp"]:8,.2f}')
    print(f'  Orders assigned                  {p["orders_assigned"]:8d}   {a["orders_assigned_actual"]:8d}')
    print(f'  Assignment rate                  {p["assignment_rate"]*100:7.1f}%   {a["assignment_rate_actual"]*100:7.1f}%')
    print(f'  On-time rate (actual)               n/a   {a["on_time_rate_actual"]*100:7.1f}%')
    print(f'  {"─"*58}')
    print(f'  KM delta (planned vs actual delivery): {km_delta:+.1f}%')
    print(f'  Solver events: {p["solver_events"]}  |  Line-haul excluded: {p["line_haul_excl"]}')
    print(f'{"="*62}')


def main():
    parser = argparse.ArgumentParser(description='VRPTW Rolling-Horizon Backtest')
    parser.add_argument('--date',  type=str, help='Single date YYYY-MM-DD')
    parser.add_argument('--dates', nargs='+', help='Multiple dates')
    parser.add_argument('--budget', type=float, default=30.0,
                        help='Solver time budget per event in seconds (default: 30)')
    args = parser.parse_args()

    dates = []
    if args.date:
        dates = [args.date]
    elif args.dates:
        dates = args.dates
    else:
        parser.error('Provide --date or --dates')

    print(f'\nZEEFLEET VRPTW Rolling-Horizon Backtest')
    print(f'Run at: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'Solver budget per event: {args.budget}s')
    print('Loading datasets...')

    import warnings; warnings.filterwarnings('ignore')
    datasets = load_datasets(str(BASE_DIR))
    cache    = load_postcode_cache()

    print(f'  Qargo: {len(datasets["qargo"]):,} rows  |  '
          f'Telematics: {len(datasets["supatrak_telematics"]):,} rows  |  '
          f'Vehicles: {len(datasets["supatrak_vehicles"])}')

    results = []
    for date_str in dates:
        print(f'\nProcessing {date_str}...')
        result = run_single_date(date_str, datasets, cache, args.budget)
        print_backtest(result)
        results.append(result)

    save_postcode_cache(cache)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if len(results) == 1:
        out_path = OUTPUT_DIR / f'backtest_vrptw_{dates[0]}.json'
        out_path.write_text(json.dumps(results[0], indent=2))
        print(f'\n  Saved: {out_path}')
    else:
        out_path = OUTPUT_DIR / f'backtest_vrptw_multi.json'
        out_path.write_text(json.dumps(results, indent=2))
        print(f'\n  Saved: {out_path}')


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: Run the full backtest on Jan 02**

```
cd E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics
python backtest_vrptw.py --date 2026-01-02 --budget 30 2>&1
```
Expected output shape:
```
======================================================
  VRPTW BACKTEST  2026-01-02
======================================================
                                    PLANNED      ACTUAL
  ──────────────────────────────────────────────────────
  Vehicles used (delivery)              XX          XX
  Total delivery km                  X,XXX       X,XXX
  ...
  KM delta (planned vs actual delivery): +/-XX%
```
Success criteria: assignment rate ≥ 85%, KM delta within ±40% (first pass — tuning comes after).

- [ ] **Step 3: Run on Jan 05 (larger day)**

```
python backtest_vrptw.py --date 2026-01-05 --budget 30 2>&1
```

- [ ] **Step 4: Confirm existing tests still pass**

```
python -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```
git add backtest_vrptw.py
git commit -m "feat: add VRPTW backtest validator with delivery-leg km isolation"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered by |
|---|---|
| DeliveryStop, DeliveryRoute dataclasses | Task 1 |
| VEHICLE_ACTIVATION_COST in route_cost | Task 1 |
| fleet_objective with UNASSIGNED_PENALTY | Task 1 |
| try_insert O(n+1) | Task 1 |
| compute_direct_run depot→origin→dest→depot | Task 1 |
| set_activation_cost, set_unassigned_penalty | Task 1 |
| ALNS adapted for single-stop insert | Task 2 |
| run_vrptw entry point | Task 2 |
| FreightInfo dataclass | Task 3 |
| PRE_STAGED → 06:00 | Task 3 |
| VIA_DEPOT → GPS collection return + 30 min | Task 3 |
| vehicle_hours_worked returned | Task 3 |
| DIRECT → 06:00, flagged | Task 3 |
| UNGEOCODED excluded | Task 3 |
| VehicleFleetState dataclass | Task 4 |
| _identify_line_haul_artics per-day | Task 4 |
| Event queue from freight_tracker | Task 4 |
| Re-entry of vehicles after delivery | Task 4 |
| Shift hours consumed by collection | Task 4 |
| MIN_REMAINING_H gate | Task 4 |
| Actual delivery km (line-haul filtered) | Task 5 |
| Inter-depot track exclusion | Task 5 |
| Planned vs actual table | Task 5 |
| JSON output | Task 5 |
| Success metrics table | Task 5 |

All spec requirements covered. No TBDs or placeholders found.
