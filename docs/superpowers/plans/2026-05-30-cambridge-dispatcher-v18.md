# Cambridge Dispatcher v1.8 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Cambridge dispatcher respect per-stop delivery windows, reconcile on-time metrics on planned and actual using the proper 3-case Qargo rule, separate activation from fuel cost, scale service time with pallets, verify tractor pool against telematics, and fix postcode Jaccard via an actual-side stop matcher.

**Architecture:** Five focused tasks plus validation. Each touches a small surface; all changes preserve the v1.7 router seam and OSRM/Haversine compatibility.

**Tech Stack:** Python 3.11, pytest, pandas, OSRM (Docker, optional).

**Project Constraint — NO COMMITS:** Per user instruction, work stays local. **Never run `git commit`, `git add`, `git push`, or any state-modifying git command.** Each task ends with "Save and continue" — no commit step.

**Spec:** [`docs/superpowers/specs/2026-05-30-cambridge-dispatcher-v18-design.md`](../specs/2026-05-30-cambridge-dispatcher-v18-design.md)

**Working directory:** `e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics/`

**Test runner:** `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest <path> -v`

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `cambridge/on_time.py` | NEW | Shared `parse_window_end()` and `is_on_time()` helpers (3-case rule) — used by both planned and actual paths |
| `cambridge/scope.py` | MODIFY | Capture `destination_time_window_value` on `ScopedOrder` |
| `cambridge/dispatcher.py` | MODIFY | Compute per-stop `service_h` from pallets; pass `window_end` and `time_window_value` to `DeliveryStop` |
| `cambridge/backtest.py` | MODIFY | Rewrite `compute_planned_on_time` via shared helper; report cost split + lateness + dest_districts from telematics |
| `cambridge/config.py` | MODIFY | Add `LATENESS_PENALTY_GBP_PER_MIN`, `SERVICE_BASE_MIN`, `SERVICE_PER_PALLET_MIN`, `DELIVERED_STATUSES`; replace `CB22_TRACTORS` literal with JSON loader |
| `simulation/vrptw_engine.py` | MODIFY | Add `window_end` to `DeliveryStop`; extend `RouteSchedule` with `lateness_minutes`; add `LATENESS_PENALTY` + `set_lateness_penalty`; modify `_walk_schedule` and `fleet_objective` |
| `simulation/vrptw_alns.py` | MODIFY | Emit `activation_gbp`, `fuel_gbp`, `lateness_minutes`, `window_end_iso` per stop in route output |
| `simulation/actuals_loader.py` | MODIFY | Rewrite `_qargo_actuals` (correct field + status filter + 3-case rule); add `vehicle_regs` to `_jigsaw_fuel_gbp`; add `actual_dest_districts()` |
| `investigations/derive_v18_parameters.py` | NEW | Tractor pool home-depot test → `tractors_derived.json` |
| `data/Output/cambridge/tractors_derived.json` | NEW (generated) | Per-tractor overnight-at-CB22 percentage |
| `tests/test_on_time.py` | NEW | 3-case rule unit tests |
| `tests/test_vrptw_engine.py` | MODIFY | Add window/lateness tests |
| `tests/test_vrptw_alns_output.py` | MODIFY | Add cost-split + lateness emission tests |
| `tests/cambridge/test_backtest.py` | MODIFY | Update for new on-time logic; add cost-split and Jaccard tests |
| `tests/cambridge/test_dispatcher.py` | MODIFY | Add per-pallet service-time test |
| `tests/cambridge/test_scope.py` | MODIFY | Add window-value capture test |
| `tests/test_actuals_loader.py` | NEW | 3-case rule + status filter + Jigsaw filter + dwell detection tests |

---

## Task 1: Shared on-time helper module (3-case rule)

**Files:**
- Create: `cambridge/on_time.py`
- Create: `tests/test_on_time.py`

This is the foundation — both planned and actual paths call into it.

- [ ] **Step 1.1: Write the failing tests**

```python
# tests/test_on_time.py
import os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from cambridge.on_time import is_on_time, parse_window_end


def _dt(s):
    return datetime.fromisoformat(s)


def test_parse_window_end_range_format():
    """'09:00 - 12:00' returns 12:00 on the requested date."""
    req = _dt('2026-01-07 00:00:00')
    end = parse_window_end('09:00 - 12:00', req)
    assert end == _dt('2026-01-07 12:00:00')


def test_parse_window_end_single_time():
    """'17:00:00' (single time) returns 17:00 on the requested date."""
    req = _dt('2026-01-07 00:00:00')
    end = parse_window_end('17:00:00', req)
    assert end == _dt('2026-01-07 17:00:00')


def test_parse_window_end_returns_none_on_invalid():
    req = _dt('2026-01-07 00:00:00')
    assert parse_window_end('garbage', req) is None
    assert parse_window_end(None, req) is None
    assert parse_window_end('', req) is None


def test_is_on_time_case_a_window_value_within():
    """Case A: window value provided, arrival inside → on-time."""
    arrival = _dt('2026-01-07 11:30:00')
    req     = _dt('2026-01-07 09:00:00')
    win     = '09:00 - 12:00'
    assert is_on_time(arrival, req, win) is True


def test_is_on_time_case_a_window_value_after():
    """Case A: window value provided, arrival past window end → late."""
    arrival = _dt('2026-01-07 12:30:00')
    req     = _dt('2026-01-07 09:00:00')
    win     = '09:00 - 12:00'
    assert is_on_time(arrival, req, win) is False


def test_is_on_time_case_b_timed_request_before():
    """Case B: no window, requested has real time, arrival at or before → on-time."""
    arrival = _dt('2026-01-07 11:30:00')
    req     = _dt('2026-01-07 12:00:00')
    assert is_on_time(arrival, req, None) is True


def test_is_on_time_case_b_timed_request_after():
    """Case B: no window, requested has real time, arrival after → late."""
    arrival = _dt('2026-01-07 12:30:00')
    req     = _dt('2026-01-07 12:00:00')
    assert is_on_time(arrival, req, None) is False


def test_is_on_time_case_c_date_only_same_day():
    """Case C: requested 00:00 (date-only). Any arrival that same day → on-time."""
    arrival = _dt('2026-01-07 13:53:02')
    req     = _dt('2026-01-07 00:00:00')
    assert is_on_time(arrival, req, None) is True


def test_is_on_time_case_c_date_only_next_day():
    """Case C: requested 00:00 (date-only). Arrival next day → late."""
    arrival = _dt('2026-01-08 09:00:00')
    req     = _dt('2026-01-07 00:00:00')
    assert is_on_time(arrival, req, None) is False


def test_is_on_time_returns_none_when_arrival_missing():
    req = _dt('2026-01-07 09:00:00')
    assert is_on_time(None, req, None) is None
    assert is_on_time(req, None, None) is None
```

- [ ] **Step 1.2: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_on_time.py -v`
Expected: `ModuleNotFoundError: cambridge.on_time`.

- [ ] **Step 1.3: Implement `cambridge/on_time.py`**

```python
# cambridge/on_time.py
"""Three-case on-time rule shared by planned and actual paths.

Per Qargo data dictionary + observed semantics:
  Case A — destination_time_window_value populated (e.g. '09:00 - 12:00' or
           '17:00:00'): on-time = arrival <= window_end on the requested date.
  Case B — no window value but requested_start has time != 00:00:
           on-time = arrival <= requested_start.
  Case C — no window, requested_start = 00:00 (date-only marker):
           on-time = arrival.date() <= requested_start.date().
"""
from datetime import datetime, time, timedelta


def parse_window_end(value, requested_start: datetime) -> datetime | None:
    """Return the end-of-window datetime on the requested_start's date.

    Accepts these formats:
      - 'HH:MM - HH:MM'        (range; end is the second time)
      - 'HH:MM:SS'             (single time = window end at that time)
      - 'HH:MM'                (single time)
    Returns None for missing/unparseable input.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    end_part = s.split('-')[-1].strip()
    parts = end_part.split(':')
    try:
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    base = datetime.combine(requested_start.date(), time(0, 0),
                            tzinfo=requested_start.tzinfo)
    return base + timedelta(hours=hh, minutes=mm)


def is_on_time(arrival, requested_start, time_window_value) -> bool | None:
    """Apply the 3-case rule. Returns None when arrival or requested missing."""
    if arrival is None or requested_start is None:
        return None
    win_end = parse_window_end(time_window_value, requested_start)
    if win_end is not None:
        return arrival <= win_end
    if requested_start.time() != time(0, 0):
        return arrival <= requested_start
    return arrival.date() <= requested_start.date()
```

- [ ] **Step 1.4: Run — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_on_time.py -v`
Expected: 10 tests pass.

- [ ] **Step 1.5: Save and continue — no commit.**

---

## Task 2: Add window data to `ScopedOrder` and `DeliveryStop`

**Files:**
- Modify: `cambridge/scope.py`
- Modify: `simulation/vrptw_engine.py`
- Modify: `tests/cambridge/test_scope.py`
- Modify: `tests/test_vrptw_engine.py`

`ScopedOrder` gains a `time_window_value` field (string from Qargo, may be None). `DeliveryStop` gains `window_end` (datetime, optional, derived from `delivery_window[1]`).

- [ ] **Step 2.1: Failing test for scope window-value capture**

Add to `tests/cambridge/test_scope.py`:

```python
def test_scoped_order_captures_time_window_value():
    """If Qargo row has destination_time_window_value, ScopedOrder must carry it."""
    import pandas as pd
    from cambridge.scope import build_scoped_orders

    df = pd.DataFrame([{
        'order_id': 'A',
        'service_level_name': 'Next day',
        'destination_postcode': 'CB22 4PS',
        'origin_postcode': 'CB22 4PS',
        'destination_requested_start_timestamp_local': '2026-01-07 09:00:00',
        'destination_time_window_value': '09:00 - 12:00',
        'origin_requested_start_timestamp_local': '2026-01-07 06:00:00',
        'goods_weight': 100.0,
        'goods_pallet_spaces': 1.0,
        'resource_rigid': 'L88GNW',
    }])
    cache = {'CB22 4PS': (52.0859, 0.1717)}
    out = build_scoped_orders(df, cache)
    assert len(out) >= 1
    so = next((o for o in out if o.order_id == 'A'), None)
    assert so is not None
    assert getattr(so, 'time_window_value', None) == '09:00 - 12:00'


def test_scoped_order_time_window_value_none_when_missing():
    import pandas as pd
    from cambridge.scope import build_scoped_orders

    df = pd.DataFrame([{
        'order_id': 'B',
        'service_level_name': 'Next day',
        'destination_postcode': 'CB22 4PS',
        'origin_postcode': 'CB22 4PS',
        'destination_requested_start_timestamp_local': '2026-01-07 00:00:00',
        # no destination_time_window_value column at all
        'origin_requested_start_timestamp_local': '2026-01-07 06:00:00',
        'goods_weight': 100.0,
        'goods_pallet_spaces': 1.0,
        'resource_rigid': 'L88GNW',
    }])
    cache = {'CB22 4PS': (52.0859, 0.1717)}
    out = build_scoped_orders(df, cache)
    so = next((o for o in out if o.order_id == 'B'), None)
    assert so is not None
    assert getattr(so, 'time_window_value', None) is None
```

- [ ] **Step 2.2: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_scope.py -v -k time_window_value`
Expected: `AttributeError: 'ScopedOrder' object has no attribute 'time_window_value'` (or similar).

- [ ] **Step 2.3: Modify `cambridge/scope.py`**

Find the `ScopedOrder` dataclass (around line 90-100). Add a new optional field:

```python
@dataclass
class ScopedOrder:
    order_id: str
    name: str
    flow: FlowTag
    origin_pc: str
    destination_pc: str
    weight_kg: float
    pallets: float
    delivery_window: Tuple[datetime, datetime]
    collection_window: Optional[Tuple[datetime, datetime]]
    time_window_value: Optional[str] = None  # Qargo destination_time_window_value (e.g. '09:00 - 12:00')
```

Find `build_scoped_orders` (around line 130-160). In the `ScopedOrder(...)` construction call, add:

```python
            time_window_value=(str(row['destination_time_window_value'])
                               if 'destination_time_window_value' in row.index
                               and pd.notna(row.get('destination_time_window_value'))
                               else None),
```

- [ ] **Step 2.4: Run scope tests — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_scope.py -v`
Expected: all pass including the two new ones.

- [ ] **Step 2.5: Failing test for DeliveryStop.window_end**

Add to `tests/test_vrptw_engine.py`:

```python
def test_delivery_stop_accepts_window_end():
    """DeliveryStop now carries an optional window_end (datetime)."""
    from datetime import datetime, timezone
    s = DeliveryStop(order_id='A', lat=52.2, lon=0.16,
                     weight_kg=100.0, pallets=1.0,
                     window_end=datetime(2026, 1, 7, 18, 0, tzinfo=timezone.utc))
    assert s.window_end == datetime(2026, 1, 7, 18, 0, tzinfo=timezone.utc)


def test_delivery_stop_window_end_defaults_to_none():
    """Backward-compatible: window_end is optional."""
    s = DeliveryStop(order_id='B', lat=52.2, lon=0.16,
                     weight_kg=100.0, pallets=1.0)
    assert s.window_end is None
```

- [ ] **Step 2.6: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_engine.py -v -k window_end`
Expected: `TypeError: ... got an unexpected keyword argument 'window_end'`.

- [ ] **Step 2.7: Modify `simulation/vrptw_engine.py` — add `window_end` to `DeliveryStop`**

Replace the `DeliveryStop` dataclass:

```python
@dataclass
class DeliveryStop:
    order_id: str
    lat: float
    lon: float
    weight_kg: float
    pallets: float
    service_h: float | None = None  # None = use module-level _SERVICE_HOURS_PER_STOP
    window_end: 'datetime | None' = None  # if set, lateness penalty applies past this
```

(The string annotation on `window_end` avoids forward-ref issues — `datetime` is imported at the top of the file already.)

Actually correct it after writing — `datetime` is imported, so use the unquoted form:

```python
    window_end: datetime | None = None
```

- [ ] **Step 2.8: Run — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_engine.py -v`
Expected: pass (existing tests still green; new window_end tests green).

- [ ] **Step 2.9: Save and continue — no commit.**

---

## Task 3: `_walk_schedule` produces lateness + `fleet_objective` penalises it

**Files:**
- Modify: `simulation/vrptw_engine.py`
- Modify: `tests/test_vrptw_engine.py`

Extend `RouteSchedule` to include `lateness_minutes`. Solver objective includes a `LATENESS_PENALTY × total_lateness` term.

- [ ] **Step 3.1: Failing test**

Add to `tests/test_vrptw_engine.py`:

```python
def test_route_schedule_carries_lateness_minutes():
    from vrptw_engine import route_schedule
    from datetime import datetime, timezone, timedelta
    T0 = datetime(2026, 1, 7, 6, 0, tzinfo=timezone.utc)
    early_window_end = T0 + timedelta(minutes=10)  # window closes 10 min after shift start
    # Stop is far enough that arrival exceeds window
    stop = _stop(order_id='A', lat=52.5, lon=0.5)
    stop.window_end = early_window_end
    route = _route(stops=[stop])
    sched = route_schedule(route)
    assert sched.lateness_minutes > 0


def test_route_schedule_lateness_zero_when_no_windows():
    from vrptw_engine import route_schedule
    stop = _stop(order_id='A', lat=52.2, lon=0.16)
    route = _route(stops=[stop])
    sched = route_schedule(route)
    assert sched.lateness_minutes == 0


def test_fleet_objective_includes_lateness_penalty():
    from vrptw_engine import fleet_objective, set_lateness_penalty
    from datetime import datetime, timezone, timedelta
    T0 = datetime(2026, 1, 7, 6, 0, tzinfo=timezone.utc)

    no_window_stop = _stop(order_id='X', lat=52.2, lon=0.16)
    late_stop = _stop(order_id='Y', lat=52.5, lon=0.5)
    late_stop.window_end = T0 + timedelta(minutes=10)

    route_no = _route(stops=[no_window_stop])
    route_lt = _route(stops=[late_stop])

    cost_rates = _load_cost_rates(RATES_PATH)
    set_lateness_penalty(10.0)
    try:
        obj_no = fleet_objective({'V1': route_no}, cost_rates, total_orders=1)
        obj_lt = fleet_objective({'V1': route_lt}, cost_rates, total_orders=1)
        assert obj_lt > obj_no  # lateness adds to the objective
    finally:
        set_lateness_penalty(0.0)  # tests downstream shouldn't see this
```

- [ ] **Step 3.2: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_engine.py -v -k lateness`
Expected: `AttributeError: ... 'lateness_minutes'` or `set_lateness_penalty` import error.

- [ ] **Step 3.3: Modify `simulation/vrptw_engine.py`**

**A. Add LATENESS_PENALTY config and setter.** Just after the existing `set_service_minutes` block (around line 53-56):

```python
LATENESS_PENALTY: float = 0.0  # GBP per minute past window_end, summed across stops. Default 0 = no penalty.


def set_lateness_penalty(gbp_per_min: float) -> None:
    """Override the per-minute lateness penalty applied in fleet_objective."""
    global LATENESS_PENALTY
    LATENESS_PENALTY = gbp_per_min
```

**B. Extend `RouteSchedule` with `lateness_minutes`.** Replace its definition:

```python
@dataclass
class RouteSchedule:
    """Per-stop arrival times, depot return time, and total minutes past windows.

    arrivals: order_id -> arrival datetime (BEFORE service)
    return_time: depot-return datetime (AFTER final return leg)
    lateness_minutes: int — sum over stops of max(0, arrival - window_end) in minutes,
                       only counting stops whose window_end was set.
    """
    arrivals: dict
    return_time: datetime
    lateness_minutes: int = 0
```

**C. Modify `_walk_schedule` to compute lateness.** Replace its body:

```python
def _walk_schedule(route: DeliveryRoute) -> RouteSchedule:
    """Walk the route in time. Returns per-stop arrivals plus depot return time
    plus total minutes past each stop's window_end (if set)."""
    router = _get_router()
    arrivals: dict = {}
    t = route.shift_start
    prev_lat, prev_lon = route.depot_lat, route.depot_lon
    lateness_min = 0
    for stop in route.stops:
        leg_h = router.duration_h(prev_lat, prev_lon, stop.lat, stop.lon)
        t += timedelta(hours=leg_h)
        arrivals[stop.order_id] = t
        if getattr(stop, 'window_end', None) is not None and t > stop.window_end:
            lateness_min += int((t - stop.window_end).total_seconds() // 60)
        t += timedelta(hours=_svc(stop))
        prev_lat, prev_lon = stop.lat, stop.lon
    return_h = router.duration_h(prev_lat, prev_lon, route.depot_lat, route.depot_lon)
    return RouteSchedule(arrivals=arrivals,
                         return_time=t + timedelta(hours=return_h),
                         lateness_minutes=lateness_min)
```

**D. Modify `fleet_objective` to include the lateness penalty.** Replace its body:

```python
def fleet_objective(routes: dict, cost_rates: dict, total_orders: int) -> float:
    placed = sum(len(r.stops) for r in routes.values())
    lateness_total = sum(_walk_schedule(r).lateness_minutes for r in routes.values())
    return (sum(route_cost(r, cost_rates) for r in routes.values())
            + UNASSIGNED_PENALTY * (total_orders - placed)
            + LATENESS_PENALTY * lateness_total)
```

- [ ] **Step 3.4: Run new tests — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_engine.py tests/test_routing.py tests/test_on_time.py -v`
Expected: all pass.

- [ ] **Step 3.5: Run full suite (excluding legacy)**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest --ignore=legacy_pdptw -q`
Expected: all green. New count: previous (153) + Task 1 (10) + Task 2 (4) + Task 3 (3) = ~170.

- [ ] **Step 3.6: Save and continue — no commit.**

---

## Task 4: Wire windows + cost split + lateness through `vrptw_alns` output

**Files:**
- Modify: `simulation/vrptw_alns.py`
- Modify: `tests/test_vrptw_alns_output.py`

Each route output dict gains:
- `activation_gbp` and `fuel_gbp` (split out of the existing combined `estimated_cost_gbp`)
- `lateness_minutes` (from the route's schedule)

Each stop dict gains:
- `window_end_iso` (from `stop.window_end` if set, else `None`)

- [ ] **Step 4.1: Failing test**

Add to `tests/test_vrptw_alns_output.py`:

```python
def test_run_vrptw_emits_cost_split_per_route():
    """Each route in result['routes'] now has activation_gbp + fuel_gbp."""
    from datetime import datetime, timezone, timedelta
    T0 = datetime(2026, 1, 7, 6, 0, tzinfo=timezone.utc)
    T_END = T0 + timedelta(hours=11)
    orders = [
        {'order_id': 'A', 'dest_lat': 52.20, 'dest_lon': 0.16,
         'weight_kg': 500.0, 'pallets': 1.0},
    ]
    vehicles = [
        {'vehicle_id': 'V1', 'depot_lat': 52.10172, 'depot_lon': 0.16229,
         'shift_start': T0, 'shift_end': T_END,
         'capacity_kg': 10000.0, 'capacity_pallets': 26, 'asset_type': 'Lorry'},
    ]
    result = run_vrptw(orders, vehicles, time_budget=2.0)
    for vid, route in result['routes'].items():
        assert 'activation_gbp' in route
        assert 'fuel_gbp' in route
        assert 'lateness_minutes' in route
        # Sum approximately equals the combined cost (within rounding)
        assert abs((route['activation_gbp'] + route['fuel_gbp'])
                   - route['estimated_cost_gbp']) < 0.5


def test_run_vrptw_emits_window_end_iso_when_provided():
    """If order dicts carry destination window data, stops carry window_end_iso."""
    from datetime import datetime, timezone, timedelta
    T0 = datetime(2026, 1, 7, 6, 0, tzinfo=timezone.utc)
    T_END = T0 + timedelta(hours=11)
    orders = [
        {'order_id': 'A', 'dest_lat': 52.20, 'dest_lon': 0.16,
         'weight_kg': 500.0, 'pallets': 1.0,
         'window_end': T0 + timedelta(hours=4)},
    ]
    vehicles = [
        {'vehicle_id': 'V1', 'depot_lat': 52.10172, 'depot_lon': 0.16229,
         'shift_start': T0, 'shift_end': T_END,
         'capacity_kg': 10000.0, 'capacity_pallets': 26, 'asset_type': 'Lorry'},
    ]
    result = run_vrptw(orders, vehicles, time_budget=2.0)
    routes = result['routes']
    assert routes
    for vid, route in routes.items():
        for stop in route['stops']:
            assert 'window_end_iso' in stop
            if stop['order_id'] == 'A':
                assert stop['window_end_iso'] is not None
```

- [ ] **Step 4.2: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_alns_output.py -v`
Expected: `KeyError: 'activation_gbp'` (or similar).

- [ ] **Step 4.3: Modify `simulation/vrptw_alns.py`**

**A. Order → DeliveryStop conversion: plumb window_end.** Find where the solver converts each input order dict to a `DeliveryStop` (search for `DeliveryStop(`). Pass `window_end=order.get('window_end')` if not already present.

**B. Modify the route-formatting block (the one v1.7 already edited around lines 363-385).**

Replace the inner formatting block with:

```python
    from vrptw_engine import (
        route_distance_km as _rdkm,
        route_schedule as _rsch,
        route_fuel_cost as _rfuel,
        VEHICLE_ACTIVATION_COST,
    )
    out_routes  = {}
    assignments = []
    for vid, route in best_routes.items():
        if not route.stops:
            continue
        km    = _rdkm(route)
        fuel  = _rfuel(route, cost_rates)
        act   = VEHICLE_ACTIVATION_COST
        total = act + fuel  # equal to route_cost(route)
        sched = _rsch(route)
        out_routes[vid] = {
            'stops': [
                {'order_id': s.order_id, 'lat': s.lat, 'lon': s.lon,
                 'weight_kg': s.weight_kg, 'pallets': s.pallets,
                 'arrival_iso': sched.arrivals[s.order_id].isoformat(),
                 'window_end_iso': (s.window_end.isoformat()
                                    if getattr(s, 'window_end', None) is not None
                                    else None)}
                for s in route.stops
            ],
            'total_distance_km':  round(km, 1),
            'activation_gbp':     round(act, 2),
            'fuel_gbp':           round(fuel, 2),
            'estimated_cost_gbp': round(total, 2),
            'lateness_minutes':   sched.lateness_minutes,
            'depot_lat': route.depot_lat,
            'depot_lon': route.depot_lon,
            'asset_type': route.asset_type,
            'return_time_iso': sched.return_time.isoformat(),
        }
        for s in route.stops:
            assignments.append({'order_id': s.order_id, 'vehicle_id': vid})
```

- [ ] **Step 4.4: Run new tests — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_vrptw_alns_output.py -v`
Expected: pass.

- [ ] **Step 4.5: Save and continue — no commit.**

---

## Task 5: Cambridge dispatcher: per-pallet service time + plumb window_end

**Files:**
- Modify: `cambridge/config.py`
- Modify: `cambridge/dispatcher.py`
- Modify: `tests/cambridge/test_dispatcher.py`

`_order_to_dict` now sets `service_minutes` from the per-pallet formula and `window_end` from `ScopedOrder.delivery_window[1]`.

- [ ] **Step 5.1: Add constants to `cambridge/config.py`**

Append:

```python
# v1.8: per-stop service time scales with load.
# 10 min base (paperwork / check-in) + 6 min per pallet (tail-lift handover).
# A 0-pallet parcel drop = 10 min; a 5-pallet drop = 40 min.
SERVICE_BASE_MIN: float = 10.0
SERVICE_PER_PALLET_MIN: float = 6.0


def service_minutes_for_load(pallets: float) -> float:
    """Linear: base + per-pallet * pallets, with pallets >= 0."""
    return SERVICE_BASE_MIN + SERVICE_PER_PALLET_MIN * max(0.0, float(pallets or 0.0))


# v1.8: solver objective penalty for arriving past a stop's window_end (GBP/min).
LATENESS_PENALTY_GBP_PER_MIN: float = 1.0


# v1.8: Qargo statuses that indicate the order was actually delivered (not
# planned / in-transit / cancelled). Used to filter actual on-time computations.
DELIVERED_STATUSES = frozenset({
    'INVOICE_POSTED', 'DONT_INVOICE', 'INVOICED', 'INVOICE_READY', 'COMPLETED',
})
```

- [ ] **Step 5.2: Failing test for per-pallet service time**

Add to `tests/cambridge/test_dispatcher.py`:

```python
def test_order_to_dict_service_minutes_scales_with_pallets():
    """v1.8: service_minutes = 10 + 6 × pallets."""
    from cambridge.dispatcher import _order_to_dict
    from cambridge.scope import ScopedOrder
    from datetime import datetime, timezone

    def _so(pallets):
        return ScopedOrder(
            order_id='X', name='n', flow='PL_IMPORT',
            origin_pc='CB22 4PS', destination_pc='CB10 1AA',
            weight_kg=100.0, pallets=pallets,
            delivery_window=(datetime(2026, 1, 7, 6, 0, tzinfo=timezone.utc),
                             datetime(2026, 1, 7, 18, 0, tzinfo=timezone.utc)),
            collection_window=None,
        )
    cache = {'CB10 1AA': (52.0, 0.2)}

    parcel = _order_to_dict(_so(0), cache)
    two_pal = _order_to_dict(_so(2), cache)
    five_pal = _order_to_dict(_so(5), cache)
    assert parcel['service_minutes']  == 10.0
    assert two_pal['service_minutes'] == 22.0
    assert five_pal['service_minutes'] == 40.0


def test_order_to_dict_carries_window_end_from_scoped_order():
    from cambridge.dispatcher import _order_to_dict
    from cambridge.scope import ScopedOrder
    from datetime import datetime, timezone

    so = ScopedOrder(
        order_id='Y', name='n', flow='PL_IMPORT',
        origin_pc='CB22 4PS', destination_pc='CB10 1AA',
        weight_kg=100.0, pallets=1.0,
        delivery_window=(datetime(2026, 1, 7, 6, 0, tzinfo=timezone.utc),
                         datetime(2026, 1, 7, 18, 0, tzinfo=timezone.utc)),
        collection_window=None,
    )
    cache = {'CB10 1AA': (52.0, 0.2)}
    d = _order_to_dict(so, cache)
    assert d['window_end'] == so.delivery_window[1]
```

- [ ] **Step 5.3: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_dispatcher.py -v -k "service_minutes or window_end"`
Expected: failure (service_minutes is currently flat 20, window_end isn't carried).

- [ ] **Step 5.4: Modify `cambridge/dispatcher.py::_order_to_dict`**

Find the function (around line 84-104). Update to:

```python
def _order_to_dict(order: ScopedOrder, postcode_cache: dict) -> Optional[dict]:
    """Convert a ScopedOrder to the order-dict format expected by run_vrptw.

    Returns None if the destination postcode is not in the cache.
    """
    coords = postcode_cache.get(order.destination_pc)
    if coords is None:
        return None
    if isinstance(coords, dict):
        lat, lon = coords['lat'], coords['lon']
    else:
        lat, lon = coords
    return {
        'order_id': order.order_id,
        'dest_lat': lat,
        'dest_lon': lon,
        'goods_weight_kg': order.weight_kg,
        'goods_pallet_spaces': order.pallets,
        'service_minutes': service_minutes_for_load(order.pallets),
        'window_end': order.delivery_window[1] if order.delivery_window else None,
    }
```

Add the import at the top of `cambridge/dispatcher.py`:

```python
from cambridge.config import (
    # ... existing imports unchanged ...
    service_minutes_for_load,
    LATENESS_PENALTY_GBP_PER_MIN,
)
```

(Merge into the existing `from cambridge.config import (...)` block; don't add a duplicate import statement.)

- [ ] **Step 5.5: Install lateness penalty at start of `run_day_multi_trip`**

In `cambridge/dispatcher.py::run_day_multi_trip`, add right after the existing `if osrm_enabled():` block (or right before the first call into the solver):

```python
    # Install the v1.8 lateness penalty into the engine.
    from vrptw_engine import set_lateness_penalty
    set_lateness_penalty(LATENESS_PENALTY_GBP_PER_MIN)
```

- [ ] **Step 5.6: Plumb window_end through `vrptw_alns` order conversion**

In `simulation/vrptw_alns.py` (or wherever order dicts are converted to `DeliveryStop`), ensure the converter picks up the `window_end` key:

Find a line like `DeliveryStop(order_id=o['order_id'], ...)` (search for `DeliveryStop(`). Add `window_end=o.get('window_end')` to that call.

If the converter is in `vrptw_alns.py` near the top of `run_vrptw`, the change is straightforward. If the call site already exists in multiple places, update all of them.

- [ ] **Step 5.7: Run tests — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_dispatcher.py tests/test_vrptw_engine.py tests/test_vrptw_alns_output.py -v`
Expected: pass.

- [ ] **Step 5.8: Run full suite**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest --ignore=legacy_pdptw -q`
Expected: all green.

- [ ] **Step 5.9: Save and continue — no commit.**

---

## Task 6: Cambridge backtest — rewrite metrics

**Files:**
- Modify: `cambridge/backtest.py`
- Modify: `tests/cambridge/test_backtest.py`

`compute_planned_on_time` now uses the shared `is_on_time` helper with `time_window_value` from each scoped order. Day report adds `planned_activation_gbp`, `planned_fuel_gbp`, `planned_lateness_minutes`, and `orders_not_yet_delivered`.

- [ ] **Step 6.1: Failing tests**

Replace the existing `compute_planned_on_time` tests in `tests/cambridge/test_backtest.py` (from v1.7) with these:

```python
def test_compute_planned_on_time_uses_3_case_rule():
    """Three cases: window value, timed request, date-only."""
    from cambridge.backtest import compute_planned_on_time
    from cambridge.scope import ScopedOrder
    from datetime import datetime, timezone

    # Case A: has window value, arrival within window → on-time
    so_a = ScopedOrder(
        order_id='A', name='n', flow='PL_IMPORT',
        origin_pc='', destination_pc='CB10',
        weight_kg=100.0, pallets=1.0,
        delivery_window=(datetime(2026, 1, 7, 9, 0, tzinfo=timezone.utc),
                         datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)),
        collection_window=None,
        time_window_value='09:00 - 12:00',
    )
    # Case C: requested 00:00, arrival same day → on-time
    so_c = ScopedOrder(
        order_id='C', name='n', flow='PL_IMPORT',
        origin_pc='', destination_pc='SG6',
        weight_kg=100.0, pallets=1.0,
        delivery_window=(datetime(2026, 1, 7, 0, 0, tzinfo=timezone.utc),
                         datetime(2026, 1, 7, 18, 0, tzinfo=timezone.utc)),
        collection_window=None,
        time_window_value=None,
    )
    arr_a = datetime(2026, 1, 7, 11, 30, tzinfo=timezone.utc)  # in window
    arr_c = datetime(2026, 1, 7, 17, 0, tzinfo=timezone.utc)   # same day
    routes = {
        'V1': {'stops': [
            {'order_id': 'A', 'arrival_iso': arr_a.isoformat()},
            {'order_id': 'C', 'arrival_iso': arr_c.isoformat()},
        ]},
    }
    on_time, late = compute_planned_on_time(routes, [so_a, so_c])
    assert on_time == 2
    assert late == 0


def test_compute_planned_on_time_case_a_violation():
    from cambridge.backtest import compute_planned_on_time
    from cambridge.scope import ScopedOrder
    from datetime import datetime, timezone

    so = ScopedOrder(
        order_id='A', name='n', flow='PL_IMPORT',
        origin_pc='', destination_pc='CB10',
        weight_kg=100.0, pallets=1.0,
        delivery_window=(datetime(2026, 1, 7, 9, 0, tzinfo=timezone.utc),
                         datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)),
        collection_window=None,
        time_window_value='09:00 - 12:00',
    )
    arr = datetime(2026, 1, 7, 13, 0, tzinfo=timezone.utc)
    routes = {'V1': {'stops': [{'order_id': 'A', 'arrival_iso': arr.isoformat()}]}}
    on_time, late = compute_planned_on_time(routes, [so])
    assert on_time == 0
    assert late == 1
```

- [ ] **Step 6.2: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_backtest.py -v -k "compute_planned_on_time"`
Expected: failure (current impl doesn't use 3-case rule).

- [ ] **Step 6.3: Replace `compute_planned_on_time` body in `cambridge/backtest.py`**

```python
def compute_planned_on_time(routes: dict, scoped_orders: list) -> tuple[int, int]:
    """Count planned stops as on-time / late using the 3-case rule.

    Reads arrival_iso from each stop and the matching ScopedOrder's
    delivery_window[0] (requested_start) and time_window_value. Stops with
    no matching ScopedOrder or no arrival_iso are counted as late.
    """
    from datetime import datetime as _dt
    from cambridge.on_time import is_on_time

    scoped_by_id = {o.order_id: o for o in scoped_orders}

    on_time = 0
    late = 0
    for vid, route in routes.items():
        if not isinstance(route, dict):
            continue
        for stop in route.get('stops', []) or []:
            if not isinstance(stop, dict):
                late += 1
                continue
            so = scoped_by_id.get(stop.get('order_id'))
            arrival_iso = stop.get('arrival_iso')
            if so is None or arrival_iso is None:
                late += 1
                continue
            try:
                arrival = _dt.fromisoformat(str(arrival_iso))
            except ValueError:
                late += 1
                continue
            verdict = is_on_time(arrival, so.delivery_window[0], so.time_window_value)
            if verdict is True:
                on_time += 1
            else:
                late += 1
    return on_time, late
```

- [ ] **Step 6.4: Add cost split + lateness + not-yet-delivered to the day report**

In `cambridge/backtest.py::run_day_backtest`, find the `planned = {...}` dict construction (around line 295-305). Augment it:

```python
    # Aggregate cost split + lateness across routes
    planned_activation = 0.0
    planned_fuel = 0.0
    planned_lateness = 0
    for r in day_out.routes.values():
        if isinstance(r, dict):
            planned_activation += r.get('activation_gbp', 0.0)
            planned_fuel       += r.get('fuel_gbp', 0.0)
            planned_lateness   += r.get('lateness_minutes', 0)

    planned = {
        'total_km':                day_out.metrics.get('planned_km', 0.0),
        'vehicles_used':           day_out.metrics.get('vehicles_used', 0),
        'planned_cost_gbp':        day_out.metrics.get('planned_cost_gbp', 0.0),
        'planned_activation_gbp':  round(planned_activation, 2),
        'planned_fuel_gbp':        round(planned_fuel, 2),
        'planned_lateness_minutes':int(planned_lateness),
        'orders_total':            day_out.metrics.get('orders_total', 0),
        'orders_assigned':         day_out.metrics.get('orders_assigned', 0),
        'on_time_count':           day_out.metrics.get('orders_assigned', 0),  # backward-compat placeholder
        'planned_on_time':         planned_on_time,
        'planned_late':            planned_late,
    }
```

In the same function, find the actuals block (around line 322-328). Augment with `orders_not_yet_delivered` — this comes from `_qargo_actuals` after Task 7 (just leave a TODO comment for now, will be filled in by next task ... actually no, the plan says no TODOs. Instead, compute it here directly):

After `qargo_stats = _qargo_actuals(day_qargo, orders_input)`, add:

```python
    # v1.8: count orders requested for today but still in pre-delivery statuses
    from cambridge.config import DELIVERED_STATUSES
    not_yet = int((~day_qargo['status'].isin(DELIVERED_STATUSES)).sum()) \
              if 'status' in day_qargo.columns else 0

    actual = {
        'total_km':                actual_basic['total_km'],
        'active_vehicles':         actual_basic['active_vehicles'],
        'actual_fuel_gbp':         actual_fuel,
        'orders_actual_assigned':  orders_actual_assigned,
        'orders_actual_on_time':   orders_actual_on_time,
        'orders_not_yet_delivered': not_yet,
    }
```

- [ ] **Step 6.5: Update `print_report` to show the cost split**

Find `print_report` in `cambridge/backtest.py` (around line 252+). Find the "Fuel cost GBP" line. Replace with three lines showing the split:

```python
    print(f'  Planned activation                  {p["planned_activation_gbp"]:>9,.2f}                ')
    print(f'  Planned fuel                        {p["planned_fuel_gbp"]:>9,.2f}                ')
    print(f'  Planned total cost                  {p["planned_cost_gbp"]:>9,.2f}   '
          f'{fuel_actual_str:>9}   (vs Jigsaw)')
    print(f'  Planned lateness (min)              {p["planned_lateness_minutes"]:>9d}                ')
```

(If the current line uses GBP symbol that breaks Windows GBK terminal, mirror the existing fix — use "GBP" in label text, not the £ symbol.)

Also add a line for not-yet-delivered (after the assignment row):

```python
    print(f'  Orders not yet delivered                                  {a["orders_not_yet_delivered"]:>4d}')
```

- [ ] **Step 6.6: Run backtest tests + full suite**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_backtest.py -v`
Expected: pass.

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest --ignore=legacy_pdptw -q`
Expected: all green.

- [ ] **Step 6.7: Save and continue — no commit.**

---

## Task 7: `actuals_loader` — 3-case rule + status filter + Jigsaw filter

**Files:**
- Modify: `simulation/actuals_loader.py`
- Create: `tests/test_actuals_loader.py`

- [ ] **Step 7.1: Failing tests**

Create `tests/test_actuals_loader.py`:

```python
import os, sys
import pandas as pd
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'simulation'))

from actuals_loader import _qargo_actuals, _jigsaw_fuel_gbp


def test_qargo_actuals_filters_status_and_uses_destination_timestamp():
    """v1.8: must use destination_timestamp_local (arrival), not _end_, and
    filter status to DELIVERED_STATUSES."""
    df = pd.DataFrame([
        # Case C (date-only requested), delivered same day, status delivered → on-time
        {'order_id': 'A', 'status': 'INVOICE_POSTED',
         'destination_requested_start_timestamp_local': '2026-01-07 00:00:00',
         'destination_timestamp_local': '2026-01-07 13:53:00',
         'destination_end_timestamp_local': '2026-01-07 13:58:00',
         'destination_time_window_value': None,
         'resource_rigid': 'L88GNW'},
        # Case A (window value), delivered after window → late
        {'order_id': 'B', 'status': 'DONT_INVOICE',
         'destination_requested_start_timestamp_local': '2026-01-07 09:00:00',
         'destination_timestamp_local': '2026-01-07 13:00:00',
         'destination_end_timestamp_local': '2026-01-07 13:05:00',
         'destination_time_window_value': '09:00 - 12:00',
         'resource_rigid': 'T88GNW'},
        # Not delivered (in transit) → excluded from on-time computation
        {'order_id': 'C', 'status': 'IN_TRANSIT',
         'destination_requested_start_timestamp_local': '2026-01-07 00:00:00',
         'destination_timestamp_local': '2026-01-07 14:00:00',
         'destination_end_timestamp_local': '2026-01-07 14:00:00',
         'destination_time_window_value': None,
         'resource_rigid': 'W88RNW'},
    ])
    orders = {'A': {}, 'B': {}, 'C': {}}
    out = _qargo_actuals(df, orders)
    # 3 assigned, 2 delivered (status filter excludes C), 1 on-time (A only)
    assert out['orders_assigned_actual']  == 3
    assert out['orders_on_time_actual']   == 1
    assert out['orders_delivered_actual'] == 2  # NEW field


def test_jigsaw_filters_by_vehicle_regs():
    """v1.8: _jigsaw_fuel_gbp(jigsaw, date_str, vehicle_regs={...}) restricts to those regs."""
    df = pd.DataFrame([
        {'vehicleRegistration': 'L88GNW', 'companyProductName': 'Diesel',
         'transactionDateTime': '2026-01-07 10:00:00', 'unitPrice': 100.0,
         'quantity': 100.0, 'totalCost': 100.0},
        {'vehicleRegistration': 'X99XYZ', 'companyProductName': 'Diesel',
         'transactionDateTime': '2026-01-07 11:00:00', 'unitPrice': 100.0,
         'quantity': 50.0, 'totalCost': 50.0},
    ])
    all_fleet = _jigsaw_fuel_gbp(df, '2026-01-07')
    cb22_only = _jigsaw_fuel_gbp(df, '2026-01-07', vehicle_regs={'L88GNW'})
    assert all_fleet == 150.0
    assert cb22_only == 100.0
```

- [ ] **Step 7.2: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_actuals_loader.py -v`
Expected: failures because `_qargo_actuals` uses the wrong field and doesn't filter status; `_jigsaw_fuel_gbp` has no `vehicle_regs` parameter.

- [ ] **Step 7.3: Rewrite `_qargo_actuals` in `simulation/actuals_loader.py`**

Replace the function body (around lines 102-155):

```python
def _qargo_actuals(qargo_df: pd.DataFrame, orders: dict) -> dict:
    """Extract assignment + on-time rates using the v1.8 3-case rule.

    - Uses destination_timestamp_local (arrival = delivery time), NOT _end_.
    - Filters to DELIVERED_STATUSES for the on-time computation.
    - Uses destination_time_window_value when present.
    - Adds orders_delivered_actual to the output dict (denominator for on-time).
    """
    from cambridge.on_time import is_on_time
    from cambridge.config import DELIVERED_STATUSES

    order_ids = set(orders.keys())
    empty = {
        'orders_assigned_actual':  0,
        'assignment_rate_actual':  0.0,
        'orders_delivered_actual': 0,
        'orders_on_time_actual':   0,
        'on_time_rate_actual':     0.0,
    }
    if qargo_df.empty or not order_ids or 'order_id' not in qargo_df.columns:
        return empty

    df = qargo_df[qargo_df['order_id'].astype(str).isin(order_ids)].copy()
    if df.empty:
        return empty

    present_cols = [c for c in RESOURCE_VEHICLE_COLS if c in df.columns]
    assigned_mask = (df[present_cols].notna().any(axis=1)
                     if present_cols else pd.Series(False, index=df.index))
    orders_assigned = int(assigned_mask.sum())
    assignment_rate = orders_assigned / len(order_ids) if order_ids else 0.0

    # Filter to actually-delivered status rows for on-time computation
    if 'status' in df.columns:
        delivered_mask = assigned_mask & df['status'].isin(DELIVERED_STATUSES)
    else:
        delivered_mask = assigned_mask
    df_delivered = df[delivered_mask].copy()

    on_time = 0
    if not df_delivered.empty:
        arrivals  = pd.to_datetime(df_delivered.get('destination_timestamp_local'), errors='coerce')
        requested = pd.to_datetime(
            df_delivered.get('destination_requested_start_timestamp_local'), errors='coerce')
        windows = (df_delivered['destination_time_window_value']
                   if 'destination_time_window_value' in df_delivered.columns
                   else pd.Series([None] * len(df_delivered), index=df_delivered.index))
        for arr, req, win in zip(arrivals, requested, windows):
            arr = arr if pd.notna(arr) else None
            req = req if pd.notna(req) else None
            win = win if pd.notna(win) else None
            if is_on_time(arr, req, win) is True:
                on_time += 1

    delivered_n = int(delivered_mask.sum())
    on_time_rate = on_time / delivered_n if delivered_n else 0.0

    return {
        'orders_assigned_actual':  orders_assigned,
        'assignment_rate_actual':  round(assignment_rate, 3),
        'orders_delivered_actual': delivered_n,
        'orders_on_time_actual':   on_time,
        'on_time_rate_actual':     round(on_time_rate, 3),
    }
```

- [ ] **Step 7.4: Extend `_jigsaw_fuel_gbp` with `vehicle_regs` filter**

Find the function (around line 65-100). Update the signature and body:

```python
def _jigsaw_fuel_gbp(jigsaw_df: pd.DataFrame,
                    date_str: str,
                    vehicle_regs: set | None = None) -> float:
    """Sum diesel-only fuel spend from Jigsaw for the target date.

    If vehicle_regs is provided, filter to those registrations only (case-insensitive
    match on 'vehicleRegistration' column). When None, sum over the whole fleet
    (legacy behaviour).
    """
    if jigsaw_df.empty or 'transactionDateTime' not in jigsaw_df.columns:
        return 0.0

    df = jigsaw_df.copy()
    df['_dt'] = pd.to_datetime(df['transactionDateTime'], errors='coerce')
    df = df[df['_dt'].dt.date.astype(str) == date_str]

    if 'companyProductName' in df.columns:
        df = df[df['companyProductName'] == 'Diesel']

    unit_price = pd.to_numeric(df.get('unitPrice', pd.Series(dtype=float)), errors='coerce')
    df = df[unit_price.reindex(df.index, fill_value=0) > 0]

    if vehicle_regs is not None and 'vehicleRegistration' in df.columns:
        regs_upper = {str(r).strip().upper() for r in vehicle_regs}
        df = df[df['vehicleRegistration'].astype(str).str.strip().str.upper().isin(regs_upper)]

    if df.empty:
        return 0.0

    if 'totalCost' in df.columns and df['totalCost'].notna().any():
        return float(pd.to_numeric(df['totalCost'], errors='coerce').fillna(0).sum())
    qty = pd.to_numeric(df.get('quantity', pd.Series(dtype=float)), errors='coerce').fillna(0)
    up  = pd.to_numeric(df.get('unitPrice', pd.Series(dtype=float)), errors='coerce').fillna(0)
    return float((qty * up / 100.0).sum())
```

- [ ] **Step 7.5: Cambridge backtest passes CB22_RIGIDS to Jigsaw**

In `cambridge/backtest.py::run_day_backtest`, find the call to `_jigsaw_fuel_gbp`:

```python
    actual_fuel = _jigsaw_fuel_gbp(jigsaw_df, date_str) if jigsaw_df is not None else 0.0
```

Replace with:

```python
    from cambridge.config import CB22_RIGIDS
    actual_fuel = (_jigsaw_fuel_gbp(jigsaw_df, date_str, vehicle_regs=CB22_RIGIDS)
                   if jigsaw_df is not None else 0.0)
```

- [ ] **Step 7.6: Run tests — expect pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_actuals_loader.py tests/cambridge/test_backtest.py -v`
Expected: pass.

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest --ignore=legacy_pdptw -q`
Expected: all green.

- [ ] **Step 7.7: Save and continue — no commit.**

---

## Task 8: Tractor pool verification

**Files:**
- Create: `investigations/derive_v18_parameters.py`
- Modify: `cambridge/config.py`

- [ ] **Step 8.1: Create `investigations/derive_v18_parameters.py`**

```python
"""Derive Cambridge tractor pool from telematics overnight clustering.

Runs the same home-depot test that confirmed CB22_RIGIDS, but with
AssetType == 'Tractor Unit'. Writes data/Output/cambridge/tractors_derived.json.

Usage:
    python -m investigations.derive_v18_parameters
"""
import json
import math
from pathlib import Path
from datetime import datetime, time

import pandas as pd

from data_audit import load_datasets
from cambridge.config import CB22_DEPOT_ANCHOR

OUT_PATH = Path(__file__).resolve().parent.parent / 'data' / 'Output' / 'cambridge' / 'tractors_derived.json'

# Definitions
NIGHT_START = time(0, 0)   # midnight–05:00 window for "overnight at depot"
NIGHT_END   = time(5, 0)
DEPOT_RADIUS_KM = 2.0       # 2 km matches the observed yard offset around CB22
MIN_DAYS_OBSERVED = 10      # need at least this many days of overnight pings to qualify
QUALIFYING_SHARE = 0.90     # ≥ 90 % of overnight observations must be near CB22


def _hav_km(a1, o1, a2, o2):
    R = 6371.0
    p = math.pi / 180
    dl = (a2 - a1) * p
    do = (o2 - o1) * p
    h = math.sin(dl / 2) ** 2 + math.cos(a1 * p) * math.cos(a2 * p) * math.sin(do / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def derive_tractor_pool():
    ds = load_datasets('.')
    telem = ds['supatrak_telematics']
    vehicles = ds.get('supatrak_vehicles', pd.DataFrame())

    if vehicles.empty or 'AssetType' not in vehicles.columns:
        raise RuntimeError('Vehicle metadata not available; cannot filter by AssetType.')

    tractor_regs = set(
        vehicles[vehicles['AssetType'] == 'Tractor Unit']['AssetName'].dropna().astype(str)
    )
    print(f'Total tractor units in fleet: {len(tractor_regs)}')

    t = telem.copy()
    t['_dt'] = pd.to_datetime(t['LocalTime'], errors='coerce')
    t['_lat'] = pd.to_numeric(t['Latitude'], errors='coerce')
    t['_lon'] = pd.to_numeric(t['Longitude'], errors='coerce')
    t = t.dropna(subset=['_dt', '_lat', '_lon'])
    t = t[t['AssetName'].astype(str).isin(tractor_regs)]

    # Overnight pings: 00:00 - 05:00 local time
    t['_time'] = t['_dt'].dt.time
    night = t[(t['_time'] >= NIGHT_START) & (t['_time'] < NIGHT_END)].copy()
    night['_dist_cb22'] = night.apply(
        lambda r: _hav_km(r['_lat'], r['_lon'], *CB22_DEPOT_ANCHOR), axis=1)
    night['_at_cb22'] = night['_dist_cb22'] < DEPOT_RADIUS_KM
    night['_date'] = night['_dt'].dt.date

    result = {}
    for asset, grp in night.groupby('AssetName'):
        per_day = grp.groupby('_date')['_at_cb22'].any()
        days_seen = len(per_day)
        days_at_cb22 = int(per_day.sum())
        share = days_at_cb22 / days_seen if days_seen else 0.0
        result[str(asset)] = {
            'days_observed':      days_seen,
            'days_at_cb22':       days_at_cb22,
            'overnight_share':    round(share, 3),
            'qualifies_cb22':     (days_seen >= MIN_DAYS_OBSERVED and share >= QUALIFYING_SHARE),
        }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2))
    qualifying = [reg for reg, info in result.items() if info['qualifies_cb22']]
    borderline = [reg for reg, info in result.items()
                  if not info['qualifies_cb22'] and info['overnight_share'] >= 0.5]
    print(f'\nQualifying CB22 tractors ({len(qualifying)}): {sorted(qualifying)}')
    print(f'Borderline (50-89%): {sorted(borderline)}')
    print(f'\nWrote {OUT_PATH}')


if __name__ == '__main__':
    derive_tractor_pool()
```

- [ ] **Step 8.2: Run the script to generate the JSON**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m investigations.derive_v18_parameters`
Expected: prints qualifying tractor list and writes `data/Output/cambridge/tractors_derived.json`.

- [ ] **Step 8.3: Modify `cambridge/config.py` to load the derived list**

Replace the existing literal `CB22_TRACTORS` constant block with:

```python
# Confirmed Cambridge-fleet tractors. Derived in investigations/derive_v18_parameters.py
# by running the same telematics-based home-depot test that confirmed CB22_RIGIDS,
# filtered to AssetType == 'Tractor Unit'.
_TRACTORS_JSON = (_Path(__file__).parent.parent
                  / 'data' / 'Output' / 'cambridge' / 'tractors_derived.json')

_FALLBACK_TRACTORS = {
    'AR02DEX', 'N8GNW', 'Y88RNW', 'N88GNW', 'S88RNW', 'R88GNW',
}


def _load_tractor_pool() -> set:
    """Load qualifying tractors from the derived JSON; fall back to hardcoded set
    if the file is missing (fresh checkout before the investigation has run)."""
    if not _TRACTORS_JSON.exists():
        return set(_FALLBACK_TRACTORS)
    try:
        data = _json.loads(_TRACTORS_JSON.read_text())
        return {reg for reg, info in data.items() if info.get('qualifies_cb22')}
    except (ValueError, KeyError):
        return set(_FALLBACK_TRACTORS)


CB22_TRACTORS = _load_tractor_pool()
```

- [ ] **Step 8.4: Run full suite — expect no regression**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest --ignore=legacy_pdptw -q`
Expected: all green. The current `_FALLBACK_TRACTORS` matches the existing literal, so behaviour is unchanged on systems without the derived JSON; on systems with the JSON, only qualifying tractors are loaded.

- [ ] **Step 8.5: Save and continue — no commit.**

---

## Task 9: Actual-side stop matcher — fix postcode Jaccard

**Files:**
- Modify: `simulation/actuals_loader.py`
- Modify: `cambridge/backtest.py`
- Modify: `tests/test_actuals_loader.py`

Build `actual_dest_districts(vehicle_id, telem_df, day, postcode_cache) -> set[str]` that detects dwell points in telematics and matches them to postcode districts. Cambridge backtest calls it per active vehicle.

- [ ] **Step 9.1: Failing test**

Add to `tests/test_actuals_loader.py`:

```python
def test_actual_dest_districts_detects_dwell_and_maps_postcode(tmp_path):
    """Vehicle dwelling at coords near a known postcode should yield that district."""
    from actuals_loader import actual_dest_districts
    from datetime import date
    # postcode cache: CB10 1AA at (52.018, 0.207), SG6 1AG at (51.978, -0.225),
    # CB22 4PS (depot — should be excluded) at (52.0859, 0.1717)
    cache = {
        'CB10 1AA': (52.018, 0.207),
        'SG6 1AG':  (51.978, -0.225),
        'CB22 4PS': (52.0859, 0.1717),
    }
    # Build a fake telematics frame: 5+ consecutive pings near CB10 1AA at low speed
    pings = []
    base_time = '2026-01-07 10:00:'
    for sec in range(0, 600, 60):  # 10 min of pings, 1 per min
        pings.append({
            'AssetName': 'L88GNW',
            'LocalTime': f'2026-01-07 10:{sec // 60 + 0:02d}:00',
            'Latitude': 52.018, 'Longitude': 0.207,
            'GPSSpeed': 1.0,
        })
    # Add depot dwell (should NOT show up in dest_districts)
    for sec in range(0, 600, 60):
        pings.append({
            'AssetName': 'L88GNW',
            'LocalTime': f'2026-01-07 06:{sec // 60:02d}:00',
            'Latitude': 52.0859, 'Longitude': 0.1717,
            'GPSSpeed': 0.5,
        })
    import pandas as pd
    df = pd.DataFrame(pings)
    districts = actual_dest_districts('L88GNW', df, date(2026, 1, 7), cache)
    assert 'CB10' in districts
    assert 'CB22' not in districts  # depot excluded
```

- [ ] **Step 9.2: Run — expect failure**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_actuals_loader.py -v -k dest_districts`
Expected: `ImportError: cannot import name 'actual_dest_districts'`.

- [ ] **Step 9.3: Implement `actual_dest_districts` in `simulation/actuals_loader.py`**

Append to the end of the file:

```python
# ---------------------------------------------------------------------------
# v1.8: actual-side stop matcher — derive visited postcode districts from
# telematics dwell points, for Postcode-Jaccard level-1 metric.
# ---------------------------------------------------------------------------

_DWELL_RADIUS_KM = 0.1       # 100 m
_DWELL_MIN_MIN   = 5         # ≥ 5 min stationary
_DWELL_MAX_SPEED = 2.0       # km/h
_MATCH_RADIUS_KM = 0.5       # 500 m to nearest postcode centroid
_DEPOT_EXCLUDE_KM = 0.5      # exclude dwells within 500 m of the depot anchor


def _dwell_clusters(asset_df, max_speed=_DWELL_MAX_SPEED,
                    radius_km=_DWELL_RADIUS_KM, min_min=_DWELL_MIN_MIN):
    """Return list of (centroid_lat, centroid_lon) for each detected dwell."""
    g = asset_df.sort_values('_dt').copy()
    g = g[g['GPSSpeed'].fillna(0) < max_speed]
    clusters = []
    cluster_start_idx = None
    for i in range(len(g)):
        if i == 0:
            cluster_start_idx = i
            continue
        d_km = _haversine_km(g['_lat'].iloc[i-1], g['_lon'].iloc[i-1],
                              g['_lat'].iloc[i],   g['_lon'].iloc[i])
        if d_km > radius_km:
            # close out previous cluster
            span_min = (g['_dt'].iloc[i-1] - g['_dt'].iloc[cluster_start_idx]).total_seconds() / 60
            if span_min >= min_min:
                lat = g['_lat'].iloc[cluster_start_idx:i].mean()
                lon = g['_lon'].iloc[cluster_start_idx:i].mean()
                clusters.append((lat, lon))
            cluster_start_idx = i
    # trailing cluster
    if cluster_start_idx is not None and cluster_start_idx < len(g) - 1:
        span_min = (g['_dt'].iloc[-1] - g['_dt'].iloc[cluster_start_idx]).total_seconds() / 60
        if span_min >= min_min:
            lat = g['_lat'].iloc[cluster_start_idx:].mean()
            lon = g['_lon'].iloc[cluster_start_idx:].mean()
            clusters.append((lat, lon))
    return clusters


def _outward_part(postcode: str) -> str:
    """'CB22 4PS' → 'CB22'."""
    return str(postcode).split(' ')[0]


def actual_dest_districts(vehicle_id: str, telem_df, day, postcode_cache: dict) -> set:
    """Set of outward postcode districts (e.g. {'CB22','SG6'}) the vehicle dwelt at
    on `day`, excluding the depot. Dwell = ≥5 min within 100 m at <2 km/h. Each
    dwell centroid is matched to the nearest postcode in postcode_cache within 500 m."""
    from cambridge.config import CB22_DEPOT_ANCHOR
    import pandas as pd

    if telem_df.empty:
        return set()
    t = telem_df.copy()
    t['_dt'] = pd.to_datetime(t['LocalTime'], errors='coerce')
    t['_lat'] = pd.to_numeric(t['Latitude'], errors='coerce')
    t['_lon'] = pd.to_numeric(t['Longitude'], errors='coerce')
    t = t.dropna(subset=['_dt', '_lat', '_lon'])
    t = t[t['AssetName'].astype(str) == str(vehicle_id)]
    t = t[t['_dt'].dt.date == day]
    if t.empty:
        return set()

    # Normalise cache values to (lat, lon) tuples
    cache_norm = {}
    for pc, coords in postcode_cache.items():
        if isinstance(coords, dict):
            cache_norm[pc] = (coords['lat'], coords['lon'])
        elif coords:
            cache_norm[pc] = (coords[0], coords[1])

    districts = set()
    for lat, lon in _dwell_clusters(t):
        # Skip depot
        if _haversine_km(lat, lon, *CB22_DEPOT_ANCHOR) < _DEPOT_EXCLUDE_KM:
            continue
        # Find nearest postcode within match radius
        best_pc = None
        best_d = _MATCH_RADIUS_KM
        for pc, (plat, plon) in cache_norm.items():
            d = _haversine_km(lat, lon, plat, plon)
            if d < best_d:
                best_d = d
                best_pc = pc
        if best_pc:
            districts.add(_outward_part(best_pc))
    return districts
```

- [ ] **Step 9.4: Wire into Cambridge backtest**

In `cambridge/backtest.py::run_day_backtest`, find the `actual_per_veh` construction block (around line 357-365). Replace the `dest_districts` placeholder:

```python
    from simulation.actuals_loader import actual_dest_districts as _districts
    actual_per_veh: dict[str, dict] = {}
    for vid, km in actual_basic['per_vehicle_km'].items():
        actual_per_veh[vid] = {
            'km':    km,
            'stops': 0,                                              # placeholder until stop-matcher pulls actual count
            'depart_hour': 7,                                         # placeholder
            'return_hour': 16,                                        # placeholder
            'dest_districts': _districts(vid, telem_df, day, postcode_cache),
        }
```

- [ ] **Step 9.5: Run tests + full suite**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_actuals_loader.py -v`
Expected: pass.

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest --ignore=legacy_pdptw -q`
Expected: all green.

- [ ] **Step 9.6: Save and continue — no commit.**

---

## Task 10: Validation — Jan 7 + 5-day OSRM + write update doc

**Files:**
- No source changes. Validation runs only.

- [ ] **Step 10.1: Verify OSRM Docker is running**

```bash
curl.exe -m 5 "http://localhost:5000/route/v1/driving/0.16,52.10;0.30,52.05"
```

Expected: JSON with `"code":"Ok"`. If not running, ask the user to start the container before proceeding.

- [ ] **Step 10.2: Run derived parameters script (if not already)**

```bash
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m investigations.derive_v18_parameters
```

Expected: `tractors_derived.json` written. Note the qualifying tractor count.

- [ ] **Step 10.3: Run Jan 7 OSRM backtest**

```bash
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && \
  CAMBRIDGE_OSRM=1 python -m cambridge --date 2026-01-07 2>&1 | \
  tee investigations/v18_osrm_jan07.txt
```

Expected outcomes (note for reporting):
- Planned km approximately matches v1.7 (no routing change in v1.8).
- Planned cost report shows Activation, Fuel, and Total separately.
- `planned_on_time` near 100% (most planned arrivals are date-only same-day).
- `actual_on_time` near 99% (v1.7 was reporting 4.7% due to bug; correct value is ~98.8%).
- Postcode Jaccard ≥ 0.4 (was 0.0).
- `orders_not_yet_delivered` reported.
- Lateness penalty causes solver to reorder problematic tails (L88GNW's last two stops).

- [ ] **Step 10.4: Run 5-day OSRM backtest**

```bash
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && \
  CAMBRIDGE_OSRM=1 python -m cambridge --start 2026-01-07 --end 2026-01-11 2>&1 | \
  tee investigations/v18_osrm_5day.txt
```

Note per-day numbers.

- [ ] **Step 10.5: Write `docs/cambridge-dispatcher-v18-update.md`**

```markdown
# Cambridge Dispatcher v1.8 Validation

## What changed

- Solver now penalises arrivals past per-stop delivery windows (LATENESS_PENALTY = £1/min).
- Planned vs actual on-time both use the 3-case Qargo rule (window value / timed request / date-only).
- Fixed three pre-v1.8 actuals bugs: wrong field (`destination_end_timestamp_local` → `destination_timestamp_local`); datetime → date granularity in Case C; status filter to delivered-only.
- Cost split: planned activation + planned fuel + planned total are reported separately.
- Jigsaw filter: actual fuel is now CB22-rigid-only (was whole-fleet).
- Per-stop service time scales with pallets: 10 + 6 × pallets minutes (was flat 20).
- `CB22_TRACTORS` derived from telematics, not hardcoded.
- Postcode Jaccard now meaningful: actual_per_veh.dest_districts populated from telematics dwell matching.

## Validation (Jan 7 OSRM)
[Insert numbers from Step 10.3]

## 5-day validation (Jan 7-11 OSRM)
[Insert numbers from Step 10.4]

## What this still doesn't fix
- Hub-and-spoke groupage routing.
- Per-order trunk arrival timing (data not available).
- Driver hours / breaks / refuel.
- Heavy-day solver tuning (Jan 8 outlier).
```

Fill the placeholders with actual numbers from the runs.

- [ ] **Step 10.6: Final full-suite run**

```bash
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest --ignore=legacy_pdptw -q
```

Expected: full suite green.

- [ ] **Step 10.7: DONE — no commit.**

---

## Self-Review Notes

**Spec coverage:** Every v1.8 spec section maps to a task:
- D1 (lateness penalty) → Task 3
- D2 (3-case on-time on both sides) → Tasks 1, 6, 7
- D3 (cost split) → Tasks 4, 6
- D4 (Jigsaw filter) → Task 7
- D5 (service time per pallet) → Task 5
- D5a (tractor pool) → Task 8
- D5c (actual-side stop matcher) → Task 9

**Placeholder scan:** No TBDs. No commits. Code blocks are complete.

**Type consistency:** `RouteSchedule.lateness_minutes` (int) used in Task 3 matches Task 4's emission. `ScopedOrder.time_window_value` (Optional[str]) used in Task 2 matches Tasks 6/7's consumption. `DeliveryStop.window_end` (datetime|None) used in Task 2 matches Task 3's lateness math.
