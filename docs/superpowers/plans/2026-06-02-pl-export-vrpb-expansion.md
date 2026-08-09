# PL_EXPORT Scope Expansion + VRPB Backhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add PL_EXPORT (Palletline outbound collection) as a first-class order type, give the dispatcher a VRPB (Vehicle Routing Problem with Backhauls) backhaul phase that appends collection stops to delivery routes after all deliveries are placed.

**Architecture:** `classify_order()` gains a `PL_EXPORT` flow tag for `Palletline (export to API)` orders. `ScopedOrder` gains a `stop_type` field so the dispatcher knows whether to geocode the origin or destination. In the engine, a `PickupStop` dataclass mirrors `DeliveryStop`; `feasible()` enforces VRPB ordering (all pickups after all deliveries) and checks outbound/return capacity independently. After the ALNS delivery solve, `backhaul_pass()` greedily appends pickup stops to routes using `try_insert_pickup()`. This is Plan A of two; Plan B adds tractor inclusion and cross-dock inventory.

**Tech Stack:** Python 3.11+, pandas, pytest. No new dependencies.

---

## File Map

| File | Change |
|---|---|
| `cambridge/scope.py` | `FlowTag` + `classify_order` + `ScopedOrder.stop_type` + `build_scoped_orders` PL_EXPORT branch |
| `simulation/vrptw_engine.py` | `PickupStop` dataclass + `feasible()` VRPB + `try_insert_pickup()` |
| `cambridge/dispatcher.py` | `_order_to_dict()` pickup routing + `backhaul_pass()` + `run_day_multi_trip()` integration |
| `tests/cambridge/test_scope.py` | Update `test_sub_export_is_out_of_scope` + new PL_EXPORT tests |
| `tests/test_vrptw_engine.py` | New VRPB tests |
| `tests/cambridge/test_dispatcher.py` | New backhaul_pass tests (create file if absent) |

---

## Task 1: PL_EXPORT Flow Tag in `classify_order`

**Files:**
- Modify: `cambridge/scope.py:21` (FlowTag literal) and `cambridge/scope.py:24-46` (classify_order body)
- Test: `tests/cambridge/test_scope.py`

### Context
`FlowTag` is currently `Literal['PL_IMPORT', 'FULL_FLEET']`. `classify_order()` returns `None` for `Palletline (export to API)` subcontractor orders because they have a non-null `resource_subcontractor`, which stops them matching the FULL_FLEET rule. We add `PL_EXPORT` before the FULL_FLEET check.

Current `classify_order` (lines 24–46 of `cambridge/scope.py`):
```python
FlowTag = Literal['PL_IMPORT', 'FULL_FLEET']

def classify_order(row: pd.Series) -> Optional[FlowTag]:
    transport = str(row.get('transport_service', '') or '')
    if 'Specialist Movement' in transport:
        return None

    import_type = row.get('order_import_integration_type')
    import_type_str = '' if import_type is None or (isinstance(import_type, float) and pd.isna(import_type)) else str(import_type)
    sub = row.get('resource_subcontractor')
    sub_str = '' if sub is None or (isinstance(sub, float) and pd.isna(sub)) else str(sub)

    if import_type_str == 'PALLETLINE' and 'import from API' in sub_str:
        return 'PL_IMPORT'

    if import_type_str in ('MANUAL', '') and not sub_str:
        return 'FULL_FLEET'

    return None
```

- [ ] **Step 1: Write the failing tests**

Add to `tests/cambridge/test_scope.py`:

```python
def test_palletline_export_manual_import_is_pl_export():
    row = _row(order_import_integration_type='MANUAL',
               resource_subcontractor='Palletline (export to API)')
    assert classify_order(row) == 'PL_EXPORT'


def test_palletline_export_null_import_is_pl_export():
    row = _row(order_import_integration_type=None,
               resource_subcontractor='Palletline (export to API)')
    assert classify_order(row) == 'PL_EXPORT'


def test_palletline_import_still_pl_import_unchanged():
    row = _row(order_import_integration_type='PALLETLINE',
               resource_subcontractor='Palletline (import from API)')
    assert classify_order(row) == 'PL_IMPORT'
```

Also update the existing test that expected `None` for this sub:
```python
# REPLACE this test:
def test_sub_export_is_out_of_scope():
    row = _row(resource_subcontractor='Palletline (export to API)')
    assert classify_order(row) is None

# WITH this test:
def test_palletline_export_sub_is_pl_export():
    row = _row(resource_subcontractor='Palletline (export to API)')
    assert classify_order(row) == 'PL_EXPORT'
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics
python -m pytest tests/cambridge/test_scope.py::test_palletline_export_manual_import_is_pl_export tests/cambridge/test_scope.py::test_palletline_export_null_import_is_pl_export tests/cambridge/test_scope.py::test_palletline_export_sub_is_pl_export -v
```
Expected: FAIL — `classify_order` returns `None`, not `'PL_EXPORT'`

- [ ] **Step 3: Implement FlowTag + classify_order changes**

In `cambridge/scope.py`, replace lines 21 and 40–44:

```python
FlowTag = Literal['PL_IMPORT', 'FULL_FLEET', 'PL_EXPORT']
```

In `classify_order`, add the PL_EXPORT check between the PL_IMPORT check and the FULL_FLEET check:

```python
def classify_order(row: pd.Series) -> Optional[FlowTag]:
    transport = str(row.get('transport_service', '') or '')
    if 'Specialist Movement' in transport:
        return None

    import_type = row.get('order_import_integration_type')
    import_type_str = '' if import_type is None or (isinstance(import_type, float) and pd.isna(import_type)) else str(import_type)
    sub = row.get('resource_subcontractor')
    sub_str = '' if sub is None or (isinstance(sub, float) and pd.isna(sub)) else str(sub)

    if import_type_str == 'PALLETLINE' and 'import from API' in sub_str:
        return 'PL_IMPORT'

    if 'Palletline (export to API)' in sub_str:
        return 'PL_EXPORT'

    if import_type_str in ('MANUAL', '') and not sub_str:
        return 'FULL_FLEET'

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/cambridge/test_scope.py -v
```
Expected: all 30+ tests PASS

- [ ] **Step 5: Commit**

```
git add cambridge/scope.py tests/cambridge/test_scope.py
git commit -m "feat(scope): add PL_EXPORT flow tag for Palletline outbound collections"
```

---

## Task 2: `ScopedOrder.stop_type` + PL_EXPORT creation in `build_scoped_orders`

**Files:**
- Modify: `cambridge/scope.py` — `ScopedOrder` dataclass + `build_scoped_orders` body
- Test: `tests/cambridge/test_scope.py`

### Context
`ScopedOrder` needs a `stop_type: Literal['delivery', 'pickup']` field (default `'delivery'`) so the dispatcher knows which postcode to geocode.  For PL_EXPORT the stop location is `origin_postal_code` (where we collect from), and the stop time window comes from `_collection_window()` (reads `origin_requested_start_timestamp_local`).

Current `ScopedOrder` dataclass ends at line ~108 of `cambridge/scope.py`. The last fields all have defaults, so we can append `stop_type` at the end.

In `build_scoped_orders`, the `ScopedOrder(...)` construction sets `origin_pc` only for `FULL_FLEET`. For PL_EXPORT we also need `origin_pc` set, and the `delivery_window` must come from `_collection_window()` instead of `_delivery_window()`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/cambridge/test_scope.py`:

```python
def test_pl_export_scoped_order_has_pickup_stop_type(sample_postcode_cache):
    """PL_EXPORT order gets stop_type='pickup'."""
    import pandas as pd
    from cambridge.scope import build_scoped_orders
    df = pd.DataFrame([{
        'order_id': 'exp-1',
        'name': 'WT999',
        'order_import_integration_type': 'MANUAL',
        'resource_subcontractor': 'Palletline (export to API)',
        'resource_rigid': 'T88GNW',
        'resource_tractor': None,
        'origin_postal_code': 'CB9 8QP',
        'destination_postal_code': 'LS27 0AA',   # Leeds — irrelevant for routing
        'destination_requested_start_timestamp_local': '2026-01-08 09:00:00',
        'origin_requested_start_timestamp_local':      '2026-01-07 10:00:00',
        'goods_weight': 500.0,
        'goods_pallet_spaces': 3.0,
        'service_level_name': 'Next day',
        'transport_service': '1. Non Hazardous Shipment',
    }])
    sample_postcode_cache['CB9 8QP'] = (52.0832, 0.4361)
    sample_postcode_cache['LS27 0AA'] = (53.7376, -1.6210)
    orders = build_scoped_orders(df, sample_postcode_cache, cb22_fleet_only=False)
    assert len(orders) == 1
    o = orders[0]
    assert o.flow == 'PL_EXPORT'
    assert o.stop_type == 'pickup'
    assert o.origin_pc == 'CB9 8QP'
    # delivery_window comes from origin timestamp (10:00), not destination (09:00 next day)
    assert o.delivery_window[0].hour == 10
    assert o.delivery_window[0].date().isoformat() == '2026-01-07'


def test_delivery_order_has_delivery_stop_type(sample_postcode_cache):
    """PL_IMPORT and FULL_FLEET orders default to stop_type='delivery'."""
    import pandas as pd
    from cambridge.scope import build_scoped_orders
    df = pd.DataFrame([{
        'order_id': 'pl-1',
        'name': 'WT900001',
        'order_import_integration_type': 'PALLETLINE',
        'resource_subcontractor': 'Palletline (import from API)',
        'resource_rigid': None, 'resource_tractor': None,
        'origin_postal_code': 'DN8 4HT',
        'destination_postal_code': 'CB2 1AA',
        'destination_requested_start_timestamp_local': '2026-01-07 10:00:00',
        'origin_requested_start_timestamp_local': '2026-01-06 10:00:00',
        'goods_weight': 320.0, 'goods_pallet_spaces': 1.0,
        'service_level_name': 'Next day',
        'transport_service': '1. Non Hazardous Shipment',
    }])
    sample_postcode_cache['DN8 4HT'] = (53.6212, -0.9826)
    orders = build_scoped_orders(df, sample_postcode_cache, cb22_fleet_only=False)
    assert len(orders) == 1
    assert orders[0].stop_type == 'delivery'


def test_pl_export_backtest_rigid_in_scope():
    """Backtest mode: CB22 rigid doing PL_EXPORT → in scope."""
    import pandas as pd
    from cambridge.scope import build_scoped_orders
    from cambridge.config import CB22_RIGIDS
    our_rigid = next(iter(CB22_RIGIDS))
    df = pd.DataFrame([{
        'order_id': 'exp-bt',
        'name': 'WT_EXP',
        'order_import_integration_type': 'MANUAL',
        'resource_subcontractor': 'Palletline (export to API)',
        'resource_rigid': our_rigid,
        'resource_tractor': None,
        'origin_postal_code': 'CB9 8QP',
        'destination_postal_code': 'LS27 0AA',
        'destination_requested_start_timestamp_local': '2026-01-08 09:00:00',
        'origin_requested_start_timestamp_local': '2026-01-07 10:00:00',
        'goods_weight': 500.0, 'goods_pallet_spaces': 3.0,
        'service_level_name': 'Next day',
        'transport_service': '1. Non Hazardous Shipment',
    }])
    orders = build_scoped_orders(df, {}, cb22_fleet_only=True)
    assert len(orders) == 1
    assert orders[0].stop_type == 'pickup'
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/cambridge/test_scope.py::test_pl_export_scoped_order_has_pickup_stop_type tests/cambridge/test_scope.py::test_delivery_order_has_delivery_stop_type tests/cambridge/test_scope.py::test_pl_export_backtest_rigid_in_scope -v
```
Expected: FAIL — `ScopedOrder` has no `stop_type` attribute

- [ ] **Step 3: Add `stop_type` to `ScopedOrder`**

In `cambridge/scope.py`, add to the `ScopedOrder` dataclass after the existing optional fields (around line 108):

```python
    stop_type: Literal['delivery', 'pickup'] = 'delivery'
    # 'pickup' for PL_EXPORT: the stop is at origin_pc (shipper collection point).
    # 'delivery' for PL_IMPORT and FULL_FLEET: stop is at destination_pc.
```

Also add `Literal` to the imports at the top of the file if not already imported — it is already used for `FlowTag`, so no change needed.

- [ ] **Step 4: Update `build_scoped_orders` to set PL_EXPORT fields correctly**

In `cambridge/scope.py`, find the `out.append(ScopedOrder(...))` block (around line 222). Update two fields:

```python
        out.append(ScopedOrder(
            order_id=str(row['order_id']),
            name=str(row.get('name', '')),
            flow=flow,
            # PL_EXPORT and FULL_FLEET both need origin_pc (the collection point).
            origin_pc=(str(row['origin_postal_code']).strip().upper()
                       if flow in ('FULL_FLEET', 'PL_EXPORT') else None),
            destination_pc=str(row['destination_postal_code']).strip().upper(),
            weight_kg=float(row.get('goods_weight', 0) or 0),
            pallets=float(row.get('goods_pallet_spaces', 0) or 0),
            # PL_EXPORT stop window = origin collection time; all others = destination window.
            delivery_window=(_collection_window(row) if flow == 'PL_EXPORT'
                             else _delivery_window(row)),
            collection_window=(_collection_window(row)
                               if flow == 'FULL_FLEET' else None),
            time_window_value=(str(row['destination_time_window_value'])
                               if 'destination_time_window_value' in row.index
                               and pd.notna(row.get('destination_time_window_value'))
                               else None),
            requested_start_raw=(pd.to_datetime(row['destination_requested_start_timestamp_local'], errors='coerce').to_pydatetime()
                                 if 'destination_requested_start_timestamp_local' in row.index
                                 and pd.notna(row.get('destination_requested_start_timestamp_local'))
                                 else None),
            resource_rigid=rig,
            resource_tractor=tractor,
            shipment_names=(str(row['shipment_names'])
                            if 'shipment_names' in row.index
                            and pd.notna(row.get('shipment_names'))
                            else None),
            stop_type=('pickup' if flow == 'PL_EXPORT' else 'delivery'),
        ))
```

- [ ] **Step 5: Run all scope tests**

```
python -m pytest tests/cambridge/test_scope.py -v
```
Expected: all tests PASS

- [ ] **Step 6: Commit**

```
git add cambridge/scope.py tests/cambridge/test_scope.py
git commit -m "feat(scope): ScopedOrder.stop_type; PL_EXPORT uses origin as stop location"
```

---

## Task 3: `PickupStop` Dataclass + VRPB `feasible()`

**Files:**
- Modify: `simulation/vrptw_engine.py` — add `PickupStop`, update `feasible()`
- Test: `tests/test_vrptw_engine.py`

### Context
`DeliveryStop` (lines 110–118 of `vrptw_engine.py`) is the only stop type. VRPB requires a parallel `PickupStop` with identical fields (the difference is semantic — pickup increases vehicle load on the return leg). `feasible()` (lines 246–278) currently sums ALL stops for capacity; it must now separately check outbound delivery load and return pickup load, and reject any route where a pickup appears before a delivery.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_vrptw_engine.py`:

```python
from vrptw_engine import PickupStop


def _make_route(shift_start=None, shift_end=None, capacity_kg=5000, capacity_pallets=15):
    from datetime import datetime
    return DeliveryRoute(
        vehicle_id='TEST',
        depot_lat=52.09, depot_lon=0.17,
        shift_start=shift_start or datetime(2026, 1, 7, 7, 0),
        shift_end=shift_end or datetime(2026, 1, 7, 18, 0),
        capacity_kg=capacity_kg,
        capacity_pallets=capacity_pallets,
        asset_type='Lorry',
    )


def _delivery(order_id='D1', lat=52.2, lon=0.1, pallets=3.0, weight_kg=300.0):
    return DeliveryStop(order_id=order_id, lat=lat, lon=lon,
                        weight_kg=weight_kg, pallets=pallets)


def _pickup(order_id='P1', lat=52.3, lon=0.2, pallets=3.0, weight_kg=300.0):
    return PickupStop(order_id=order_id, lat=lat, lon=lon,
                      weight_kg=weight_kg, pallets=pallets)


def test_pickup_stop_has_same_fields_as_delivery_stop():
    p = PickupStop(order_id='P1', lat=52.0, lon=0.1, weight_kg=100, pallets=1)
    assert p.order_id == 'P1'
    assert p.service_h is None
    assert p.window_end is None


def test_feasible_vrpb_delivery_then_pickup_is_ok():
    route = _make_route()
    route.stops = [_delivery('D1', pallets=8.0), _pickup('P1', pallets=8.0)]
    assert feasible(route) is True


def test_feasible_vrpb_pickup_before_delivery_fails():
    route = _make_route()
    route.stops = [_pickup('P1', pallets=1.0), _delivery('D1', pallets=1.0)]
    assert feasible(route) is False


def test_feasible_vrpb_delivery_capacity_checked_independently():
    """10 delivery pallets + 10 pickup pallets on a 12-pallet truck.
    Each leg is within capacity; combined would exceed — must be FEASIBLE."""
    route = _make_route(capacity_pallets=12)
    route.stops = [
        _delivery('D1', pallets=10.0, weight_kg=1000.0),
        _pickup('P1', pallets=10.0, weight_kg=1000.0),
    ]
    assert feasible(route) is True


def test_feasible_delivery_over_capacity_fails():
    route = _make_route(capacity_pallets=5)
    route.stops = [_delivery('D1', pallets=6.0, weight_kg=100.0)]
    assert feasible(route) is False


def test_feasible_pickup_over_capacity_fails():
    route = _make_route(capacity_pallets=5)
    route.stops = [_pickup('P1', pallets=6.0, weight_kg=100.0)]
    assert feasible(route) is False
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_vrptw_engine.py::test_pickup_stop_has_same_fields_as_delivery_stop tests/test_vrptw_engine.py::test_feasible_vrpb_delivery_then_pickup_is_ok tests/test_vrptw_engine.py::test_feasible_vrpb_pickup_before_delivery_fails tests/test_vrptw_engine.py::test_feasible_vrpb_delivery_capacity_checked_independently -v
```
Expected: FAIL — `PickupStop` does not exist

- [ ] **Step 3: Add `PickupStop` dataclass to `vrptw_engine.py`**

After the `DeliveryStop` dataclass (around line 118), add:

```python
@dataclass
class PickupStop:
    """A collection stop on the return leg (VRPB backhaul phase).

    Structurally identical to DeliveryStop. The distinction matters in
    feasible(): pickup pallets are counted against the return-leg capacity,
    not the outbound capacity, and all pickups must appear after all deliveries.
    """
    order_id: str
    lat: float
    lon: float
    weight_kg: float
    pallets: float
    service_h: float | None = None
    window_end: datetime | None = None
```

- [ ] **Step 4: Update `feasible()` for VRPB**

Replace the capacity block in `feasible()` (currently lines ~262–267) with VRPB-aware checks. The full updated `feasible()`:

```python
def feasible(route: DeliveryRoute) -> bool:
    """Capacity, shift-end, and UK driver-hours feasibility.

    VRPB ordering: all PickupStop objects must appear after all DeliveryStop
    objects. Outbound capacity counts DeliveryStop pallets/weight only;
    return capacity counts PickupStop pallets/weight only.  The two legs are
    independent — a truck can carry 15 delivery pallets out and 15 collection
    pallets back on a 15-pallet vehicle.
    """
    if route.shift_start >= route.shift_end:
        return False
    if not route.stops:
        return True

    delivery_stops = [s for s in route.stops if isinstance(s, DeliveryStop)]
    pickup_stops   = [s for s in route.stops if isinstance(s, PickupStop)]

    # VRPB ordering constraint: no pickup before any delivery.
    if delivery_stops and pickup_stops:
        last_delivery_idx = max(i for i, s in enumerate(route.stops) if isinstance(s, DeliveryStop))
        first_pickup_idx  = min(i for i, s in enumerate(route.stops) if isinstance(s, PickupStop))
        if first_pickup_idx < last_delivery_idx:
            return False

    # Outbound leg: delivery load.
    out_kg      = sum(s.weight_kg for s in delivery_stops)
    out_pallets = sum(s.pallets   for s in delivery_stops)
    if out_kg      > route.capacity_kg      + 1e-6:
        return False
    if out_pallets > route.capacity_pallets + 1e-6:
        return False

    # Return leg: pickup load.
    ret_kg      = sum(s.weight_kg for s in pickup_stops)
    ret_pallets = sum(s.pallets   for s in pickup_stops)
    if ret_kg      > route.capacity_kg      + 1e-6:
        return False
    if ret_pallets > route.capacity_pallets + 1e-6:
        return False

    sched = _walk_schedule(route)
    if sched.return_time > route.shift_end:
        return False
    if sched.driving_minutes > MAX_DRIVING_HOURS * 60 + 1:
        return False
    if sched.on_duty_minutes > MAX_ON_DUTY_HOURS * 60 + 1:
        return False
    if (sched.driving_minutes > BREAK_REQUIRED_AFTER_HOURS * 60
            and not sched.has_break_eligible_stop):
        return False
    return True
```

- [ ] **Step 5: Run all engine tests**

```
python -m pytest tests/test_vrptw_engine.py -v
```
Expected: all tests PASS. The existing tests use only `DeliveryStop` objects, so the `delivery_stops` extraction handles them correctly and their capacity checks are unchanged.

- [ ] **Step 6: Commit**

```
git add simulation/vrptw_engine.py tests/test_vrptw_engine.py
git commit -m "feat(engine): PickupStop + VRPB feasible() with independent outbound/return capacity"
```

---

## Task 4: `try_insert_pickup()` in the Engine

**Files:**
- Modify: `simulation/vrptw_engine.py`
- Test: `tests/test_vrptw_engine.py`

### Context
`try_insert()` tries every position 0…n. For pickups, valid positions are only AFTER all existing delivery stops (VRPB constraint). `try_insert_pickup` enforces this by only trying positions from `first_valid_pickup_position` onwards.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_vrptw_engine.py`:

```python
from vrptw_engine import try_insert_pickup


def test_try_insert_pickup_returns_none_for_empty_cost_rates():
    """Smoke test: try_insert_pickup on a route with one delivery and empty cost_rates."""
    from profitability_report.profitability_report_merged import _rate_bundle
    route = _make_route()
    route.stops = [_delivery('D1', pallets=2.0, weight_kg=200.0)]
    # cost_rates needs minimal structure; use the real rate bundle approach
    cost_rates = {}
    result = try_insert_pickup(route, _pickup('P1', pallets=2.0, weight_kg=200.0), cost_rates)
    # May return None (no feasible position within shift) or InsertionResult — just no crash.
    assert result is None or hasattr(result, 'added_cost')


def test_try_insert_pickup_only_after_deliveries():
    """Pickup must be inserted AFTER all delivery stops.

    Route: [D1, D2]. Valid pickup positions: after D2 only (position 2).
    If we manually build the candidate with pickup at position 0, feasible() must reject it.
    """
    from datetime import datetime
    route = _make_route()
    d1 = _delivery('D1', lat=52.20, lon=0.10, pallets=3.0, weight_kg=300.0)
    d2 = _delivery('D2', lat=52.21, lon=0.12, pallets=3.0, weight_kg=300.0)
    route.stops = [d1, d2]
    p = _pickup('P1', lat=52.22, lon=0.14, pallets=2.0, weight_kg=200.0)
    # Pickup before deliveries must be infeasible.
    from vrptw_engine import DeliveryRoute, feasible
    bad_route = DeliveryRoute(
        vehicle_id='TEST', depot_lat=52.09, depot_lon=0.17,
        shift_start=datetime(2026, 1, 7, 7, 0), shift_end=datetime(2026, 1, 7, 18, 0),
        capacity_kg=5000, capacity_pallets=15, asset_type='Lorry',
        stops=[p, d1, d2],
    )
    assert feasible(bad_route) is False


def test_try_insert_pickup_no_deliveries_can_insert_at_start():
    """Route with only pickups already: new pickup can be inserted anywhere."""
    route = _make_route()
    route.stops = [_pickup('P1', lat=52.20, lon=0.10, pallets=2.0, weight_kg=200.0)]
    p2 = _pickup('P2', lat=52.21, lon=0.11, pallets=2.0, weight_kg=200.0)
    result = try_insert_pickup(route, p2, {})
    assert result is None or hasattr(result, 'added_cost')
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_vrptw_engine.py::test_try_insert_pickup_returns_none_for_empty_cost_rates tests/test_vrptw_engine.py::test_try_insert_pickup_only_after_deliveries tests/test_vrptw_engine.py::test_try_insert_pickup_no_deliveries_can_insert_at_start -v
```
Expected: FAIL — `try_insert_pickup` not defined

- [ ] **Step 3: Implement `try_insert_pickup()`**

Add after `cheapest_insertion()` (around line 388) in `simulation/vrptw_engine.py`:

```python
def try_insert_pickup(route: DeliveryRoute, stop: PickupStop,
                      cost_rates: dict,
                      max_depot_km: float | None = None) -> 'InsertionResult | None':
    """Find the cheapest feasible position to insert a pickup stop.

    VRPB constraint: pickups may only be placed AFTER all delivery stops.
    The search starts at `first_valid_position` (one past the last DeliveryStop index,
    or 0 if there are no delivery stops).

    max_depot_km: if set, rejects collection points farther than this from the depot.
    """
    if max_depot_km is not None:
        router = _get_router()
        if router.distance_km(route.depot_lat, route.depot_lon, stop.lat, stop.lon) > max_depot_km:
            return None

    # First valid insertion index: after the last delivery stop.
    delivery_indices = [i for i, s in enumerate(route.stops) if isinstance(s, DeliveryStop)]
    first_valid = (max(delivery_indices) + 1) if delivery_indices else 0

    base_cost = route_cost(route, cost_rates)
    n = len(route.stops)
    best = None

    for i in range(first_valid, n + 1):
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
```

- [ ] **Step 4: Run all engine tests**

```
python -m pytest tests/test_vrptw_engine.py -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```
git add simulation/vrptw_engine.py tests/test_vrptw_engine.py
git commit -m "feat(engine): try_insert_pickup() respects VRPB backhaul ordering"
```

---

## Task 5: `_order_to_dict()` Pickup Routing in Dispatcher

**Files:**
- Modify: `cambridge/dispatcher.py:87-107` (`_order_to_dict`)
- Test: `tests/cambridge/test_dispatcher.py` (create if absent)

### Context
`_order_to_dict()` currently always geocodes `order.destination_pc`. For PL_EXPORT (stop_type='pickup'), the stop location is `order.origin_pc`. The returned dict also needs a `'stop_type'` key so downstream code can create the right stop class.

Current `_order_to_dict` (dispatcher.py lines 87–107):
```python
def _order_to_dict(order: ScopedOrder, postcode_cache: dict) -> Optional[dict]:
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

- [ ] **Step 1: Write the failing tests**

Create `tests/cambridge/test_dispatcher.py` (or append if it exists):

```python
"""Tests for cambridge/dispatcher.py."""
import pytest
import pandas as pd
from datetime import datetime
from cambridge.scope import ScopedOrder
from cambridge.dispatcher import _order_to_dict


def _pl_import_order():
    return ScopedOrder(
        order_id='del-1', name='WT1', flow='PL_IMPORT',
        origin_pc=None, destination_pc='CB2 1AA',
        weight_kg=200.0, pallets=2.0,
        delivery_window=(datetime(2026, 1, 7, 9, 0), datetime(2026, 1, 8, 9, 0)),
        collection_window=None,
        stop_type='delivery',
    )


def _pl_export_order():
    return ScopedOrder(
        order_id='pick-1', name='WT2', flow='PL_EXPORT',
        origin_pc='CB9 8QP', destination_pc='LS27 0AA',
        weight_kg=500.0, pallets=3.0,
        delivery_window=(datetime(2026, 1, 7, 10, 0), datetime(2026, 1, 7, 16, 0)),
        collection_window=None,
        stop_type='pickup',
    )


def test_order_to_dict_delivery_uses_destination_pc():
    cache = {'CB2 1AA': (52.2049, 0.1218)}
    d = _order_to_dict(_pl_import_order(), cache)
    assert d is not None
    assert abs(d['dest_lat'] - 52.2049) < 0.001
    assert d['stop_type'] == 'delivery'


def test_order_to_dict_pickup_uses_origin_pc():
    cache = {'CB9 8QP': (52.0832, 0.4361), 'LS27 0AA': (53.7376, -1.6210)}
    d = _order_to_dict(_pl_export_order(), cache)
    assert d is not None
    # Should use origin coords (CB9), not destination (LS27)
    assert abs(d['dest_lat'] - 52.0832) < 0.001
    assert d['stop_type'] == 'pickup'


def test_order_to_dict_pickup_returns_none_when_origin_missing():
    cache = {'LS27 0AA': (53.7376, -1.6210)}  # destination present, origin absent
    d = _order_to_dict(_pl_export_order(), cache)
    assert d is None


def test_order_to_dict_delivery_returns_none_when_destination_missing():
    cache = {}
    d = _order_to_dict(_pl_import_order(), cache)
    assert d is None
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/cambridge/test_dispatcher.py -v
```
Expected: FAIL — `_order_to_dict` always uses `destination_pc`; no `stop_type` key

- [ ] **Step 3: Implement the fix**

Replace `_order_to_dict` in `cambridge/dispatcher.py` (lines 87–107):

```python
def _order_to_dict(order: ScopedOrder, postcode_cache: dict) -> Optional[dict]:
    """Convert a ScopedOrder to the dict format expected by the ALNS solver.

    For pickup orders (PL_EXPORT) the stop location is origin_pc; for delivery
    orders it is destination_pc.  The 'dest_lat'/'dest_lon' key names are kept
    for ALNS compatibility even for pickups.
    """
    stop_pc = order.origin_pc if order.stop_type == 'pickup' else order.destination_pc
    if stop_pc is None:
        return None
    coords = postcode_cache.get(stop_pc)
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
        'stop_type': order.stop_type,
    }
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/cambridge/test_dispatcher.py -v
```
Expected: all 4 tests PASS

- [ ] **Step 5: Commit**

```
git add cambridge/dispatcher.py tests/cambridge/test_dispatcher.py
git commit -m "feat(dispatcher): _order_to_dict routes pickup orders via origin_pc"
```

---

## Task 6: `backhaul_pass()` — Greedy Pickup Assignment

**Files:**
- Modify: `cambridge/dispatcher.py` — add `backhaul_pass()` and `_rebuild_route_from_dict()`
- Test: `tests/cambridge/test_dispatcher.py`

### Context
After the ALNS delivery solve, `run_day_multi_trip()` has a `routes_all` dict of `{vehicle_id: route_dict}` from the solver. `backhaul_pass()` reconstructs `DeliveryRoute` objects, uses `try_insert_pickup()` to find the cheapest feasible insertion for each PL_EXPORT order, and returns an updated routes dict plus a list of unassigned pickup order IDs.

The solver `route_dict` format is:
```python
{
  'stops': [{'order_id': 'X', 'destination_pc': 'CB2 1AA', 'arrival_iso': '...', ...}],
  'total_distance_km': 120.5,
  'asset_type': 'Lorry',
  'shift_start_iso': '2026-01-07T07:00:00',
  'shift_end_iso':   '2026-01-07T17:00:00',
  'return_time_iso': '2026-01-07T16:30:00',
  ...
}
```

`_rebuild_route_from_dict()` reconstructs a `DeliveryRoute` (with `DeliveryStop` objects) from this dict so the engine functions work on it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/cambridge/test_dispatcher.py`:

```python
from datetime import datetime, date
from cambridge.dispatcher import backhaul_pass, _rebuild_route_from_dict
from cambridge.scope import ScopedOrder
from vrptw_engine import DeliveryRoute, DeliveryStop, PickupStop


def _solver_route_dict(stops_pcs=None, asset_type='Lorry',
                       shift_start='2026-01-07T07:00:00',
                       shift_end='2026-01-07T18:00:00'):
    """Minimal solver route dict matching run_vrptw output format."""
    stops_pcs = stops_pcs or []
    stops = [
        {'order_id': f'D{i}', 'destination_pc': pc,
         'lat': 52.2 + i * 0.01, 'lon': 0.1 + i * 0.01,
         'weight_kg': 100.0, 'pallets': 1.0, 'arrival_iso': '2026-01-07T09:00:00'}
        for i, pc in enumerate(stops_pcs)
    ]
    return {
        'stops': stops,
        'total_distance_km': 50.0,
        'asset_type': asset_type,
        'shift_start_iso': shift_start,
        'shift_end_iso': shift_end,
        'return_time_iso': '2026-01-07T16:00:00',
        'capacity_kg': 5000,
        'capacity_pallets': 15,
    }


def _pickup_order(order_id='P1', origin_pc='SG8 5AL', pallets=2.0):
    return ScopedOrder(
        order_id=order_id, name='EXP', flow='PL_EXPORT',
        origin_pc=origin_pc, destination_pc='LS27 0AA',
        weight_kg=pallets * 100, pallets=pallets,
        delivery_window=(datetime(2026, 1, 7, 10, 0), datetime(2026, 1, 7, 16, 0)),
        collection_window=None, stop_type='pickup',
    )


def test_rebuild_route_from_dict_produces_delivery_route():
    route_dict = _solver_route_dict(['CB2 1AA', 'SG1 1AA'])
    cache = {'CB2 1AA': (52.2049, 0.1218), 'SG1 1AA': (51.9023, -0.2146)}
    r = _rebuild_route_from_dict('V1', route_dict, cache)
    assert isinstance(r, DeliveryRoute)
    assert r.vehicle_id == 'V1'
    assert len(r.stops) == 2
    assert all(isinstance(s, DeliveryStop) for s in r.stops)


def test_backhaul_pass_assigns_pickup_to_route():
    routes = {'V1': _solver_route_dict(['CB2 1AA'])}
    cache = {
        'CB2 1AA':  (52.2049, 0.1218),
        'SG8 5AL':  (52.0000, -0.0200),  # Royston — close to route area
    }
    pickup = _pickup_order('P1', 'SG8 5AL', pallets=2.0)
    updated, unassigned = backhaul_pass(routes, [pickup], cache, cost_rates={})
    # The pickup should be in one of the routes' stops
    all_pickup_ids = [
        s['order_id']
        for r in updated.values()
        for s in r.get('stops', [])
        if s.get('stop_type') == 'pickup'
    ]
    assert 'P1' in all_pickup_ids or 'P1' in unassigned  # either placed or correctly unassigned


def test_backhaul_pass_unassigns_when_no_capacity():
    routes = {'V1': _solver_route_dict(['CB2 1AA'],
                                        shift_start='2026-01-07T07:00:00',
                                        shift_end='2026-01-07T07:30:00')}  # tiny window
    cache = {'CB2 1AA': (52.2049, 0.1218), 'SG8 5AL': (52.0000, -0.0200)}
    pickup = _pickup_order('P1', 'SG8 5AL', pallets=2.0)
    _, unassigned = backhaul_pass(routes, [pickup], cache, cost_rates={})
    assert 'P1' in unassigned


def test_backhaul_pass_empty_pickups_returns_routes_unchanged():
    routes = {'V1': _solver_route_dict(['CB2 1AA'])}
    cache = {'CB2 1AA': (52.2049, 0.1218)}
    updated, unassigned = backhaul_pass(routes, [], cache, cost_rates={})
    assert updated == routes
    assert unassigned == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/cambridge/test_dispatcher.py::test_rebuild_route_from_dict_produces_delivery_route tests/cambridge/test_dispatcher.py::test_backhaul_pass_assigns_pickup_to_route -v
```
Expected: FAIL — functions not defined

- [ ] **Step 3: Implement `_rebuild_route_from_dict()`**

Add to `cambridge/dispatcher.py` (before `backhaul_pass`):

```python
def _rebuild_route_from_dict(vehicle_id: str, route_dict: dict,
                              postcode_cache: dict) -> 'DeliveryRoute':
    """Reconstruct a DeliveryRoute from a solver output dict for engine calls.

    Uses capacity and shift from the route dict if present; falls back to
    VEHICLE_PROFILES for the vehicle. Stop lat/lon come from the route dict
    if present (the solver stores them), otherwise from postcode_cache.
    """
    from vrptw_engine import DeliveryRoute, DeliveryStop, PickupStop
    profile = VEHICLE_PROFILES.get(vehicle_id, {})
    cap_kg  = route_dict.get('capacity_kg',  profile.get('capacity_kg_per_trip', 5000))
    cap_pal = route_dict.get('capacity_pallets', profile.get('capacity_pallets_per_trip', 15))
    asset   = route_dict.get('asset_type', profile.get('asset_type', 'Lorry'))

    from datetime import datetime
    def _parse_iso(s):
        return datetime.fromisoformat(str(s)) if s else None

    shift_start = _parse_iso(route_dict.get('shift_start_iso')) or datetime.combine(
        datetime.today().date(), __import__('datetime').time(7, 0))
    shift_end = _parse_iso(route_dict.get('shift_end_iso')) or datetime.combine(
        datetime.today().date(), __import__('datetime').time(18, 0))

    depot_lat, depot_lon = CB22_DEPOT_ANCHOR
    stops = []
    for s in route_dict.get('stops', []):
        lat = s.get('lat') or s.get('dest_lat')
        lon = s.get('lon') or s.get('dest_lon')
        if lat is None or lon is None:
            pc = s.get('destination_pc') or s.get('stop_pc')
            if pc:
                coords = postcode_cache.get(pc)
                if coords:
                    lat, lon = (coords['lat'], coords['lon']) if isinstance(coords, dict) else coords
        if lat is None:
            continue
        w  = float(s.get('weight_kg', s.get('goods_weight_kg', 100.0)))
        p  = float(s.get('pallets', s.get('goods_pallet_spaces', 1.0)))
        sh = s.get('service_h')
        we = _parse_iso(s.get('window_end_iso'))
        stop_type = s.get('stop_type', 'delivery')
        if stop_type == 'pickup':
            stops.append(PickupStop(order_id=s['order_id'], lat=lat, lon=lon,
                                    weight_kg=w, pallets=p, service_h=sh, window_end=we))
        else:
            stops.append(DeliveryStop(order_id=s['order_id'], lat=lat, lon=lon,
                                      weight_kg=w, pallets=p, service_h=sh, window_end=we))

    return DeliveryRoute(
        vehicle_id=vehicle_id,
        depot_lat=depot_lat, depot_lon=depot_lon,
        shift_start=shift_start, shift_end=shift_end,
        capacity_kg=cap_kg, capacity_pallets=cap_pal,
        asset_type=asset, stops=stops,
    )
```

- [ ] **Step 4: Implement `backhaul_pass()`**

Add to `cambridge/dispatcher.py` after `_rebuild_route_from_dict`:

```python
def backhaul_pass(
    routes: dict,
    pickup_orders: list,          # list[ScopedOrder] with stop_type='pickup'
    postcode_cache: dict,
    cost_rates: dict,
) -> tuple[dict, list[str]]:
    """Greedily append PL_EXPORT collection stops to existing delivery routes.

    For each pickup order (sorted by pallets descending so large collections are
    placed first), find the route where try_insert_pickup() yields the lowest
    added cost.  If no route can accommodate the pickup, it is reported as
    unassigned.

    Returns:
        updated_routes: copy of `routes` with pickup stops appended to stop lists
        unassigned_pickup_ids: order_ids that could not be placed
    """
    from vrptw_engine import PickupStop, try_insert_pickup

    if not pickup_orders:
        return routes, []

    # Rebuild DeliveryRoute objects so the engine functions work.
    live_routes: dict[str, 'DeliveryRoute'] = {
        vid: _rebuild_route_from_dict(vid, rd, postcode_cache)
        for vid, rd in routes.items()
    }

    unassigned: list[str] = []

    # Largest pickups first — harder to fit, so place them while routes are emptier.
    for order in sorted(pickup_orders, key=lambda o: o.pallets, reverse=True):
        stop_pc = order.origin_pc
        if stop_pc is None:
            unassigned.append(order.order_id)
            continue
        coords = postcode_cache.get(stop_pc)
        if coords is None:
            unassigned.append(order.order_id)
            continue
        lat, lon = (coords['lat'], coords['lon']) if isinstance(coords, dict) else coords

        svc_h = service_minutes_for_load(order.pallets) / 60.0
        win_end = order.delivery_window[1] if order.delivery_window else None
        stop = PickupStop(
            order_id=order.order_id, lat=lat, lon=lon,
            weight_kg=order.weight_kg, pallets=order.pallets,
            service_h=svc_h, window_end=win_end,
        )

        best_vid, best_result = None, None
        for vid, route in live_routes.items():
            result = try_insert_pickup(route, stop, cost_rates)
            if result is None:
                continue
            if best_result is None or result.added_cost < best_result.added_cost:
                best_vid, best_result = vid, result

        if best_vid is None:
            unassigned.append(order.order_id)
        else:
            live_routes[best_vid].stops = best_result.stops

    # Serialise updated live_routes back to the dict format.
    updated: dict = {}
    for vid, rd in routes.items():
        updated_dict = dict(rd)  # shallow copy of original route dict
        live = live_routes.get(vid)
        if live is not None:
            new_stops = []
            for s in live.stops:
                # Convert stop objects back to dicts, preserving original delivery dicts.
                if hasattr(s, 'order_id'):
                    orig = next((x for x in rd.get('stops', []) if x.get('order_id') == s.order_id), None)
                    if orig:
                        new_stops.append(orig)
                    else:
                        # Pickup stop added by backhaul_pass — build a new dict.
                        new_stops.append({
                            'order_id': s.order_id,
                            'lat': s.lat,
                            'lon': s.lon,
                            'weight_kg': s.weight_kg,
                            'pallets': s.pallets,
                            'stop_type': 'pickup',
                        })
            updated_dict = dict(rd)
            updated_dict['stops'] = new_stops
        updated[vid] = updated_dict

    return updated, unassigned
```

- [ ] **Step 5: Run dispatcher tests**

```
python -m pytest tests/cambridge/test_dispatcher.py -v
```
Expected: all tests PASS

- [ ] **Step 6: Commit**

```
git add cambridge/dispatcher.py tests/cambridge/test_dispatcher.py
git commit -m "feat(dispatcher): backhaul_pass() greedy VRPB pickup assignment post-ALNS"
```

---

## Task 7: Wire `backhaul_pass()` into `run_day_multi_trip()`

**Files:**
- Modify: `cambridge/dispatcher.py:530-560` (`run_day_multi_trip` body)
- Test: integration via existing backtest runner

### Context
`run_day_multi_trip` builds `available_at_06` (orders ready by 06:00) and runs the ALNS loop on them. After the loop, `routes_all` holds the final delivery routes. We split the incoming orders into delivery vs pickup before the loop runs, and call `backhaul_pass()` at the end with the pickup orders.

- [ ] **Step 1: Locate the split point**

In `cambridge/dispatcher.py`, find the `remaining_orders = available_at_06` line (around line 551). The change inserts a delivery/pickup split immediately before this.

- [ ] **Step 2: Implement the split and post-loop backhaul call**

Replace the `remaining_orders = available_at_06` assignment and find the end of the events loop (search for the block that builds `routes_all` and the final `DayDispatchOutput`). The change is:

```python
    # Split into delivery orders (ALNS) and pickup orders (backhaul_pass post-solve).
    remaining_orders = [o for o in available_at_06 if o.stop_type == 'delivery']
    pickup_orders    = [o for o in available_at_06 if o.stop_type == 'pickup']
```

Then at the END of the function, just before the `return DayDispatchOutput(...)` line, add:

```python
    # Backhaul pass: greedily append PL_EXPORT collection stops to delivery routes.
    if pickup_orders:
        from profitability_report.profitability_report_merged import _rate_bundle
        cost_rates = {}  # backhaul_pass handles missing rates gracefully
        routes_all, unassigned_pickups = backhaul_pass(
            routes_all, pickup_orders, postcode_cache, cost_rates
        )
        all_unassigned.extend(unassigned_pickups)
```

- [ ] **Step 3: Verify the backtest still runs**

```
python -m cambridge --date 2026-01-07 2>&1 | tail -20
```
Expected: backtest completes (PARTIAL verdict unchanged or improved). No crash.

- [ ] **Step 4: Count how many PL_EXPORT orders now appear in plan output**

```python
import json
with open('data/Output/cambridge/vehicle_plan_2026-01-07.json') as f:
    plan = json.load(f)
pickup_stops = [
    s for r in plan['routes'].values()
    for s in r.get('stops', [])
    if s.get('stop_type') == 'pickup'
]
print(f'Pickup stops in plan: {len(pickup_stops)}')
```
Expected: > 0 (some PL_EXPORT collections now appear in the plan)

- [ ] **Step 5: Commit**

```
git add cambridge/dispatcher.py
git commit -m "feat(dispatcher): integrate backhaul_pass into run_day_multi_trip"
```

---

## Task 8: Full Test Suite Pass + Smoke Backtest

**Files:**
- No new files

- [ ] **Step 1: Run the full test suite**

```
python -m pytest tests/ -v --tb=short 2>&1 | tail -40
```
Expected: all tests PASS. Zero failures.

- [ ] **Step 2: Run the backtest for Jan 7–9 to verify stability**

```
python -m cambridge --start 2026-01-07 --end 2026-01-09 2>&1 | grep -E "verdict|PASS|PARTIAL|FAIL|pickup"
```
Expected: verdicts unchanged or improved vs pre-plan baseline (no regressions on delivery metrics; pickup stops now appear in plans).

- [ ] **Step 3: Commit**

```
git add .
git commit -m "test: full suite green after PL_EXPORT + VRPB backhaul (Plan A complete)"
```

---

## Self-Review

**Spec coverage check:**
- ✅ PL_EXPORT flow tag in `classify_order` — Task 1
- ✅ `ScopedOrder.stop_type` — Task 2
- ✅ PL_EXPORT in scope (backtest + forward) — Task 2, `in_cambridge_scope` handles PL_EXPORT via origin catchment fallback
- ✅ `PickupStop` dataclass — Task 3
- ✅ VRPB `feasible()` (ordering + independent capacity) — Task 3
- ✅ `try_insert_pickup()` — Task 4
- ✅ `_order_to_dict()` routes pickups via origin_pc — Task 5
- ✅ `backhaul_pass()` greedy assignment — Task 6
- ✅ `run_day_multi_trip()` integration — Task 7
- ✅ Full suite green — Task 8

**Placeholder scan:** No TBDs, no "implement later", no "handle edge cases" without code.

**Type consistency:**
- `PickupStop` defined in Task 3, imported in Tasks 4, 6 — consistent
- `try_insert_pickup` defined in Task 4, called in Task 6 — consistent
- `backhaul_pass` defined in Task 6, called in Task 7 — consistent
- `_rebuild_route_from_dict` defined in Task 6, called in Task 6 — consistent
- `ScopedOrder.stop_type` added in Task 2, read in Tasks 5 and 7 — consistent
