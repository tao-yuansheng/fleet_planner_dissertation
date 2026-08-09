# Plan B: Tractor Trunk Model + Cross-Dock Lifecycle

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Model the nightly Palletline trunk run and its constraints on tractor and rigid scheduling — capping tractor daytime shifts at trunk departure, deriving PL_IMPORT freight availability from trunk return time, enforcing a soft 18:00 deadline for PL_EXPORT collection stops, and extending backtest scope to include tractor long-haul collections (e.g. Stoke ST4 ~190 km).

**Architecture:** A new `TrunkSchedule` dataclass in `collection_planner.py` holds the full trunk timeline (CB22 depart → hub arrive → hub depart → CB22 return → freight ready). `build_freight_availability()` in `dispatcher.py` takes an optional `trunk_schedule` and uses `freight_ready` for PL_IMPORT timing (replacing the hardcoded 06:00). Tractor shift_end is capped before the trunk departure in `build_rigid_for_event()`. PL_EXPORT orders get `delivery_window[1]` = trunk departure (soft deadline via VRPTW window penalty). A new `in_tractor_scope()` function allows backtest attribution of long-haul collections (origin ≤ 300 km) to CB22 tractors.

**Tech Stack:** Python dataclasses, datetime arithmetic, existing `cambridge/` module structure, pytest. No new dependencies.

---

## Domain context (read before implementing)

- **CB22 depot:** Duxford (52.0859, 0.1717).
- **Palletline hub:** B37 7HB, Chelmsley Wood, Birmingham (52.467, -1.787). Our tractors trunk here nightly; inbound sorted PL_IMPORT freight rides back to CB22 in the early hours.
- **Trunk schedule (Option C):** Fixed 18:00 departure from CB22 each evening. Hub dwell ≈ 90 min (sort + load). One-way haversine ≈ 139 km; road factor 1.3 → 181 km; at 50 km/h → ~3.6 h. Trunk returns ~02:44 on the delivery day — before 06:00 in normal operation, so PL_IMPORT freight ready time is clamped to 06:00 unless trunk is delayed.
- **Tractor daytime window:** Tractors run daytime local deliveries/collections from 06:00 to `TRUNK_DEPART_HOUR - TRUNK_PREP_MARGIN_H` = 17:00. They must not still be out on delivery when the trunk departs at 18:00.
- **PL_EXPORT collection deadline:** Rigids collecting from local shippers must return to CB22 before 18:00 so the freight can be loaded on the trunk. This is modelled as a soft `window_end = 18:00` on collection PickupStops (lateness penalty, not hard-reject; feasibility comes from `shift_end`).
- **Tractor long-haul scope:** Stoke ST4 is ~190 km from CB22, beyond the 100 km rigid catchment but within tractor range. `TRACTOR_CATCHMENT_RADIUS_KM = 300` covers this. Only applies in backtest (where we know the vehicle assignment); forward mode keeps the 100 km rigid radius for now.

## File structure

| File | Change |
|---|---|
| `cambridge/config.py` | Add 5 new constants (hub coords, trunk schedule, tractor catchment) |
| `cambridge/collection_planner.py` | Add `TrunkSchedule` dataclass + `build_trunk_schedule()` |
| `cambridge/dispatcher.py` | Update `build_freight_availability()` (trunk_schedule param) and `build_rigid_for_event()` (tractor cap), wire `build_trunk_schedule` into `run_day_multi_trip()` |
| `cambridge/scope.py` | Add `time` import + `TRUNK_DEPART_HOUR` import; update PL_EXPORT `delivery_window` computation; add `in_tractor_scope()`; update backtest branch |
| `tests/cambridge/test_config.py` | 3 new assertions for new constants |
| `tests/cambridge/test_collection_planner.py` | 4 new trunk schedule tests |
| `tests/cambridge/test_dispatcher.py` | 3 new freight_availability tests + 1 tractor cap test |
| `tests/cambridge/test_scope.py` | 1 PL_EXPORT window test + 4 tractor scope tests |

---

## Task 1: Add trunk and tractor constants to config.py

**Files:**
- Modify: `cambridge/config.py` (after `CATCHMENT_RADIUS_KM = 100.0`)
- Test: `tests/cambridge/test_config.py`

- [ ] **Step 1: Write the failing tests**

Open `tests/cambridge/test_config.py` and add at the end:

```python
def test_trunk_depart_hour_is_eighteen():
    from cambridge.config import TRUNK_DEPART_HOUR
    assert TRUNK_DEPART_HOUR == 18


def test_palletline_hub_coords_in_west_midlands():
    from cambridge.config import PALLETLINE_HUB_COORDS
    lat, lon = PALLETLINE_HUB_COORDS
    assert 52.0 < lat < 53.0     # West Midlands latitude band
    assert -2.5 < lon < -1.0    # West Midlands longitude band


def test_tractor_catchment_greater_than_rigid_catchment():
    from cambridge.config import CATCHMENT_RADIUS_KM, TRACTOR_CATCHMENT_RADIUS_KM
    assert TRACTOR_CATCHMENT_RADIUS_KM > CATCHMENT_RADIUS_KM
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/cambridge/test_config.py::test_trunk_depart_hour_is_eighteen tests/cambridge/test_config.py::test_palletline_hub_coords_in_west_midlands tests/cambridge/test_config.py::test_tractor_catchment_greater_than_rigid_catchment -v
```

Expected: FAIL with `ImportError: cannot import name 'TRUNK_DEPART_HOUR'`

- [ ] **Step 3: Add constants to `cambridge/config.py`**

Insert immediately after the `CATCHMENT_RADIUS_KM = 100.0` line (around line 77):

```python
# Palletline national hub (B37 7HB, Chelmsley Wood, Birmingham).
# CB22 tractors trunk here nightly; inbound PL_IMPORT freight is sorted here.
PALLETLINE_HUB_COORDS: tuple[float, float] = (52.467, -1.787)

# Nightly trunk schedule parameters.
TRUNK_DEPART_HOUR: int = 18         # tractor departs CB22 at 18:00 each evening
TRUNK_HUB_DWELL_MIN: int = 90       # sort/load time at Palletline hub (minutes)
TRUNK_PREP_MARGIN_H: float = 1.0    # tractors must be back at CB22 this many hours
                                     # before trunk departs (i.e. by 17:00)

# Maximum origin distance from CB22 for a tractor-assigned PL_EXPORT order.
# Rigid catchment is 100 km; tractors can reach Stoke ST4 (~190 km direct),
# Coventry, Leicester, and similar long-haul shipper locations.
TRACTOR_CATCHMENT_RADIUS_KM: float = 300.0
```

- [ ] **Step 4: Run to verify they pass**

```
pytest tests/cambridge/test_config.py::test_trunk_depart_hour_is_eighteen tests/cambridge/test_config.py::test_palletline_hub_coords_in_west_midlands tests/cambridge/test_config.py::test_tractor_catchment_greater_than_rigid_catchment -v
```

Expected: 3 PASS

- [ ] **Step 5: Run full suite to check no regressions**

```
pytest tests/ -q --tb=short
```

Expected: all previously-passing tests still pass (274+ tests, 0 failures).

---

## Task 2: TrunkSchedule dataclass + build_trunk_schedule() in collection_planner.py

**Files:**
- Modify: `cambridge/collection_planner.py`
- Test: `tests/cambridge/test_collection_planner.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/cambridge/test_collection_planner.py`:

```python
from cambridge.collection_planner import TrunkSchedule, build_trunk_schedule


def test_trunk_schedule_departs_prior_day_at_18():
    ts = build_trunk_schedule(date_type(2026, 1, 7))
    assert ts.depart_cb22.date() == date_type(2026, 1, 6)
    assert ts.depart_cb22.hour == 18
    assert ts.depart_cb22.minute == 0


def test_trunk_schedule_returns_before_06_on_delivery_day():
    # Hub is ~181 km by road, ~3.6 h each way; depart 18:00 → return ~02:44
    ts = build_trunk_schedule(date_type(2026, 1, 7))
    assert ts.arrive_cb22.date() == date_type(2026, 1, 7)
    assert ts.arrive_cb22.hour < 6


def test_trunk_schedule_hub_dwell_applied():
    ts = build_trunk_schedule(date_type(2026, 1, 7))
    from cambridge.config import TRUNK_HUB_DWELL_MIN
    dwell_min = (ts.depart_hub - ts.arrive_hub).total_seconds() / 60
    assert abs(dwell_min - TRUNK_HUB_DWELL_MIN) < 1.0


def test_trunk_schedule_freight_ready_after_arrive_cb22():
    ts = build_trunk_schedule(date_type(2026, 1, 7))
    assert ts.freight_ready > ts.arrive_cb22
    assert ts.day == date_type(2026, 1, 7)
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/cambridge/test_collection_planner.py::test_trunk_schedule_departs_prior_day_at_18 tests/cambridge/test_collection_planner.py::test_trunk_schedule_returns_before_06_on_delivery_day tests/cambridge/test_collection_planner.py::test_trunk_schedule_hub_dwell_applied tests/cambridge/test_collection_planner.py::test_trunk_schedule_freight_ready_after_arrive_cb22 -v
```

Expected: FAIL with `ImportError: cannot import name 'TrunkSchedule'`

- [ ] **Step 3: Add TrunkSchedule and build_trunk_schedule to collection_planner.py**

Add these imports at the top of `cambridge/collection_planner.py` (the `timedelta` import already exists; add `date` if not already imported):

```python
from cambridge.config import (
    CB22_DEPOT_ANCHOR,
    COLLECTION_PROFILES,
    DEFAULT_COLLECTION_PROFILE,
    AVG_SPEED_KMH,
    ROAD_DISTANCE_FACTOR,
    CROSS_DOCK_BUFFER_MIN,
    PALLETLINE_HUB_COORDS,
    TRUNK_DEPART_HOUR,
    TRUNK_HUB_DWELL_MIN,
)
```

Then add the `TrunkSchedule` dataclass and `build_trunk_schedule()` function right after the existing `CollectionTrip` dataclass (around line 53):

```python
@dataclass
class TrunkSchedule:
    """Timeline for the nightly CB22 → Palletline hub → CB22 trunk run.

    Computed for a given delivery day: the trunk departs CB22 on the prior
    evening, delivers/picks up at the hub, and returns with inbound
    PL_IMPORT freight in the early hours of the delivery day.
    """
    day: date_type
    depart_cb22: datetime    # prior day at TRUNK_DEPART_HOUR
    arrive_hub: datetime     # arrival at Palletline hub
    depart_hub: datetime     # after TRUNK_HUB_DWELL_MIN sort/load
    arrive_cb22: datetime    # return to CB22 with PL_IMPORT freight
    freight_ready: datetime  # arrive_cb22 + CROSS_DOCK_BUFFER_MIN


def build_trunk_schedule(day: date_type) -> TrunkSchedule:
    """Build the nightly trunk timeline for a given delivery day.

    Uses haversine distance CB22 → Palletline hub, road factor, and average
    speed from config. Trunk departs CB22 at TRUNK_DEPART_HOUR on the prior
    day and returns in the early hours of `day`.
    """
    hub_lat, hub_lon = PALLETLINE_HUB_COORDS
    dep_lat, dep_lon = CB22_DEPOT_ANCHOR
    one_way_km = _hav(dep_lat, dep_lon, hub_lat, hub_lon) * ROAD_DISTANCE_FACTOR
    one_way_h = one_way_km / AVG_SPEED_KMH

    prior_day = day - timedelta(days=1)
    depart_cb22 = datetime(prior_day.year, prior_day.month, prior_day.day,
                           TRUNK_DEPART_HOUR, 0, 0)
    arrive_hub = depart_cb22 + timedelta(hours=one_way_h)
    depart_hub = arrive_hub + timedelta(minutes=TRUNK_HUB_DWELL_MIN)
    arrive_cb22 = depart_hub + timedelta(hours=one_way_h)
    freight_ready = arrive_cb22 + timedelta(minutes=CROSS_DOCK_BUFFER_MIN)

    return TrunkSchedule(
        day=day,
        depart_cb22=depart_cb22,
        arrive_hub=arrive_hub,
        depart_hub=depart_hub,
        arrive_cb22=arrive_cb22,
        freight_ready=freight_ready,
    )
```

- [ ] **Step 4: Run to verify the new tests pass**

```
pytest tests/cambridge/test_collection_planner.py -v
```

Expected: all collection_planner tests pass (previously-passing + 4 new).

- [ ] **Step 5: Run full suite**

```
pytest tests/ -q --tb=short
```

Expected: all tests pass.

---

## Task 3: Update build_freight_availability() to use trunk schedule for PL_IMPORT

**Files:**
- Modify: `cambridge/dispatcher.py` (`build_freight_availability` function, lines ~40–64)
- Test: `tests/cambridge/test_dispatcher.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/cambridge/test_dispatcher.py`:

```python
from cambridge.collection_planner import TrunkSchedule


def _trunk_with_freight_ready(freight_ready_iso: str) -> TrunkSchedule:
    from datetime import timedelta
    fr = datetime.fromisoformat(freight_ready_iso)
    return TrunkSchedule(
        day=fr.date(),
        depart_cb22=fr - timedelta(hours=9),
        arrive_hub=fr - timedelta(hours=5),
        depart_hub=fr - timedelta(hours=3, minutes=30),
        arrive_cb22=fr - timedelta(minutes=30),
        freight_ready=fr,
    )


def test_pl_import_freight_ready_clamped_to_day_start_when_trunk_returns_early():
    # Normal case: trunk returns 03:14, freight_ready clamped to 06:00
    trunk = _trunk_with_freight_ready('2026-01-07 03:14:00')
    orders = [_pl('a')]
    ready = build_freight_availability(orders, trips=[], day=date_type(2026, 1, 7),
                                       trunk_schedule=trunk)
    assert ready['a'] == datetime.fromisoformat('2026-01-07 06:00:00')


def test_pl_import_freight_delayed_trunk_pushes_ready_past_06():
    # Delayed trunk: returns 07:30 → rigids cannot start delivering until 07:30
    trunk = _trunk_with_freight_ready('2026-01-07 07:30:00')
    orders = [_pl('a')]
    ready = build_freight_availability(orders, trips=[], day=date_type(2026, 1, 7),
                                       trunk_schedule=trunk)
    assert ready['a'] == datetime.fromisoformat('2026-01-07 07:30:00')


def test_pl_import_freight_ready_unchanged_when_no_trunk_schedule():
    # Backward-compatible: no trunk_schedule → same as before (day_start = 06:00)
    orders = [_pl('a')]
    ready = build_freight_availability(orders, trips=[], day=date_type(2026, 1, 7))
    assert ready['a'] == datetime.fromisoformat('2026-01-07 06:00:00')
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/cambridge/test_dispatcher.py::test_pl_import_freight_ready_clamped_to_day_start_when_trunk_returns_early tests/cambridge/test_dispatcher.py::test_pl_import_freight_delayed_trunk_pushes_ready_past_06 tests/cambridge/test_dispatcher.py::test_pl_import_freight_ready_unchanged_when_no_trunk_schedule -v
```

Expected: FAIL (TypeError: unexpected keyword argument 'trunk_schedule')

- [ ] **Step 3: Update build_freight_availability() in dispatcher.py**

Replace the existing function signature and PL_IMPORT branch (lines ~40–64):

```python
def build_freight_availability(orders: list[ScopedOrder],
                               trips: list[CollectionTrip],
                               day: date_type,
                               trunk_schedule=None) -> dict[str, datetime]:
    """Return order_id → first datetime the order is ready at CB22 to dispatch.

    PL_IMPORT  → max(trunk_schedule.freight_ready, day_start) when a trunk
                 schedule is provided; falls back to day_start (06:00) otherwise.
                 A delayed trunk (e.g. winter weather) automatically defers freight.
    FULL_FLEET → max(trip.freight_ready_at_depot, day_start). Falls back to
                 day_start if no matching collection trip was planned.
    PL_EXPORT  → day_start (collection window enforced via delivery_window[1]).
    """
    day_start = datetime.combine(day, time(DEFAULT_PRE_STAGED_HOUR, 0))
    pl_import_ready = (max(trunk_schedule.freight_ready, day_start)
                       if trunk_schedule is not None else day_start)

    # Map order_id → trip.freight_ready_at_depot
    trip_by_order: dict[str, datetime] = {}
    for trip in trips:
        for order_id in trip.orders:
            trip_by_order[order_id] = trip.freight_ready_at_depot

    out: dict[str, datetime] = {}
    for order in orders:
        if order.flow == 'PL_IMPORT':
            out[order.order_id] = pl_import_ready
        else:
            trip_ready = trip_by_order.get(order.order_id)
            out[order.order_id] = (max(trip_ready, day_start)
                                   if trip_ready else day_start)
    return out
```

- [ ] **Step 4: Run to verify the new tests pass**

```
pytest tests/cambridge/test_dispatcher.py -v
```

Expected: all dispatcher tests pass.

- [ ] **Step 5: Run full suite**

```
pytest tests/ -q --tb=short
```

Expected: all tests pass.

---

## Task 4: Cap tractor shift_end at trunk departure in build_rigid_for_event()

**Files:**
- Modify: `cambridge/dispatcher.py` (`build_rigid_for_event` function, lines ~556–607)
- Test: `tests/cambridge/test_dispatcher.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/cambridge/test_dispatcher.py`:

```python
from cambridge.dispatcher import build_rigid_for_event
from cambridge.config import CB22_TRACTORS, CB22_RIGIDS, TRUNK_DEPART_HOUR, TRUNK_PREP_MARGIN_H


def test_tractor_shift_end_capped_at_trunk_deadline_forward_mode():
    """Tractors must return to depot before the nightly trunk departs."""
    tractor_id = next(iter(CB22_TRACTORS))
    route = build_rigid_for_event(tractor_id, 'A', date_type(2026, 1, 7), 'forward')
    assert route is not None
    expected_cutoff = datetime.combine(
        date_type(2026, 1, 7),
        datetime.min.time().replace(hour=int(TRUNK_DEPART_HOUR - TRUNK_PREP_MARGIN_H)),
    )
    assert route.shift_end == expected_cutoff
```

- [ ] **Step 2: Run to verify it fails**

```
pytest tests/cambridge/test_dispatcher.py::test_tractor_shift_end_capped_at_trunk_deadline_forward_mode -v
```

Expected: FAIL (shift_end is 18:00 + overrun, not 17:00)

- [ ] **Step 3: Update build_rigid_for_event() in dispatcher.py**

Add the import at the top of the function (or at module level with other config imports):

```python
from cambridge.config import (
    ...
    TRUNK_DEPART_HOUR,
    TRUNK_PREP_MARGIN_H,
    ...
)
```

Then in `build_rigid_for_event()`, replace the shift overrun block (currently `end = end + timedelta(hours=SHIFT_OVERRUN_HOURS)`) with:

```python
    # Tractors: hard cap at trunk departure minus prep margin; no overtime slack.
    # Rigids: extend by overrun budget to allow overtime deliveries.
    if vehicle_id in CB22_TRACTORS:
        trunk_cutoff = datetime.combine(
            day, time(int(TRUNK_DEPART_HOUR - TRUNK_PREP_MARGIN_H), 0))
        end = min(end, trunk_cutoff)
    else:
        end = end + timedelta(hours=SHIFT_OVERRUN_HOURS)
```

Note: `time` is already imported in `dispatcher.py` via `from datetime import ..., time, ...`.

- [ ] **Step 4: Run to verify the test passes**

```
pytest tests/cambridge/test_dispatcher.py::test_tractor_shift_end_capped_at_trunk_deadline_forward_mode -v
```

Expected: PASS — tractor shift_end == 17:00.

- [ ] **Step 5: Run full suite**

```
pytest tests/ -q --tb=short
```

Expected: all tests pass.

---

## Task 5: PL_EXPORT delivery_window[1] = trunk departure (soft collection deadline)

**Files:**
- Modify: `cambridge/scope.py` (`build_scoped_orders` function)
- Test: `tests/cambridge/test_scope.py`

**Background:** Rigids collecting PL_EXPORT freight must return to CB22 before the trunk departs at 18:00 so freight can be loaded. The delivery_window end for these pickup stops was previously `_collection_window(row)[1]` (origin_timestamp + 24h). We replace it with the fixed trunk departure time on the same calendar day. This creates a soft constraint: `PickupStop.window_end = 18:00` triggers a lateness penalty in the VRPTW objective if a collection arrives after 18:00.

- [ ] **Step 1: Write the failing test**

Add to `tests/cambridge/test_scope.py`:

```python
def test_pl_export_delivery_window_end_is_trunk_depart_hour(sample_postcode_cache):
    """PL_EXPORT collection window closes at nightly trunk departure (18:00)."""
    import pandas as pd
    from cambridge.scope import build_scoped_orders
    from cambridge.config import TRUNK_DEPART_HOUR

    df = pd.DataFrame([{
        'order_id': 'exp-win-1',
        'name': 'WT-EXP',
        'order_import_integration_type': 'MANUAL',
        'resource_subcontractor': 'Palletline (export to API)',
        'resource_tractor': None,
        'resource_rigid': None,
        'origin_postal_code': 'CB9 8QP',
        'destination_postal_code': 'ME10 3FP',
        'destination_requested_start_timestamp_local': '2026-01-07 12:00:00',
        'origin_requested_start_timestamp_local': '2026-01-07 09:00:00',
        'goods_weight': 500.0,
        'goods_pallet_spaces': 1.0,
        'service_level_name': 'Next day',
        'transport_service': '1. Non Hazardous Shipment',
    }])
    orders = build_scoped_orders(df, sample_postcode_cache)
    assert len(orders) == 1
    o = orders[0]
    assert o.flow == 'PL_EXPORT'
    assert o.delivery_window[0].hour == 9            # from origin timestamp
    assert o.delivery_window[1].hour == TRUNK_DEPART_HOUR  # 18:00 hard deadline
    assert o.delivery_window[1].minute == 0
    assert o.delivery_window[1].date() == o.delivery_window[0].date()
```

Also extend the existing `test_pl_export_scoped_order_has_pickup_stop_type` test by adding one assertion at the end (find the test and add):

```python
    # After Plan B Task 5: window closes at trunk departure (18:00), not origin+24h
    from cambridge.config import TRUNK_DEPART_HOUR
    assert o.delivery_window[1].hour == TRUNK_DEPART_HOUR
```

- [ ] **Step 2: Run to verify the new test fails**

```
pytest tests/cambridge/test_scope.py::test_pl_export_delivery_window_end_is_trunk_depart_hour -v
```

Expected: FAIL (delivery_window[1].hour != 18)

- [ ] **Step 3: Update build_scoped_orders() in scope.py**

Add to the imports at the top of `cambridge/scope.py`:

```python
from datetime import datetime, time, timedelta   # add `time` here
```

And add to the config imports:

```python
from cambridge.config import (
    ...
    TRUNK_DEPART_HOUR,
)
```

Then in `build_scoped_orders()`, replace the `ScopedOrder(...)` construction's `delivery_window` line:

**Before:**
```python
            delivery_window=(_collection_window(row) if flow == 'PL_EXPORT'
                             else _delivery_window(row)),
```

**After:**
```python
            delivery_window=(_pl_export_window(row) if flow == 'PL_EXPORT'
                             else _delivery_window(row)),
```

And add this helper function above `build_scoped_orders()`:

```python
def _pl_export_window(row: pd.Series) -> Tuple[datetime, datetime]:
    """Derive the PL_EXPORT collection window.

    Start = origin collection timestamp (when shipper hands over freight).
    End   = trunk departure time on the same calendar day (18:00) — the hard
            deadline for rigids to return with collected freight to CB22.
    """
    coll_start = _collection_window(row)[0]
    trunk_deadline = datetime.combine(coll_start.date(), time(TRUNK_DEPART_HOUR, 0))
    return coll_start, trunk_deadline
```

- [ ] **Step 4: Run to verify the new test passes**

```
pytest tests/cambridge/test_scope.py::test_pl_export_delivery_window_end_is_trunk_depart_hour -v
```

Expected: PASS

- [ ] **Step 5: Run full scope test suite**

```
pytest tests/cambridge/test_scope.py -v
```

Expected: all scope tests pass (the extended `test_pl_export_scoped_order_has_pickup_stop_type` passes with the new assertion).

- [ ] **Step 6: Run full suite**

```
pytest tests/ -q --tb=short
```

Expected: all tests pass.

---

## Task 6: in_tractor_scope() + long-haul PL_EXPORT backtest attribution

**Files:**
- Modify: `cambridge/scope.py` (new function + backtest branch update)
- Test: `tests/cambridge/test_scope.py`

**Background:** Stoke ST4 is ~190 km from CB22 — inside `TRACTOR_CATCHMENT_RADIUS_KM` (300 km) but outside `CATCHMENT_RADIUS_KM` (100 km). In backtest mode, if a CB22 tractor is assigned to a PL_EXPORT order with a Stoke origin, we should include it. Forward mode keeps the 100 km rigid radius (tractor-constrained routing requires per-order vehicle type constraints, which are a Plan C feature).

- [ ] **Step 1: Write the failing tests**

Add to `tests/cambridge/test_scope.py`:

```python
def test_in_tractor_scope_stoke_st4_is_in_scope():
    from cambridge.scope import in_tractor_scope
    import pandas as pd
    # ST4 (Stoke-on-Trent) ≈ 190 km from CB22 — within TRACTOR_CATCHMENT_RADIUS_KM=300
    row = pd.Series({'origin_postal_code': 'ST4 4RJ'})
    cache = {'ST4 4RJ': (53.006, -2.185)}
    assert in_tractor_scope(row, 'PL_EXPORT', cache) is True


def test_in_tractor_scope_stoke_st4_not_in_cambridge_scope():
    from cambridge.scope import in_cambridge_scope
    import pandas as pd
    row = pd.Series({'origin_postal_code': 'ST4 4RJ'})
    cache = {'ST4 4RJ': (53.006, -2.185)}
    assert in_cambridge_scope(row, 'PL_EXPORT', cache) is False


def test_in_tractor_scope_returns_false_for_non_pl_export():
    from cambridge.scope import in_tractor_scope
    import pandas as pd
    row = pd.Series({'origin_postal_code': 'ST4 4RJ'})
    cache = {'ST4 4RJ': (53.006, -2.185)}
    assert in_tractor_scope(row, 'FULL_FLEET', cache) is False


def test_in_tractor_scope_excludes_origins_beyond_300km():
    from cambridge.scope import in_tractor_scope
    import pandas as pd
    # PH1 (Perth, Scotland) ≈ 530 km from CB22
    row = pd.Series({'origin_postal_code': 'PH1 1AA'})
    cache = {'PH1 1AA': (56.396, -3.437)}
    assert in_tractor_scope(row, 'PL_EXPORT', cache) is False


def test_backtest_pl_export_tractor_long_haul_stoke_in_scope():
    """Backtest: CB22 tractor doing Stoke ST4 PL_EXPORT collection → in scope."""
    import pandas as pd
    from cambridge.scope import build_scoped_orders
    from cambridge.config import CB22_TRACTORS
    tractor_id = next(iter(CB22_TRACTORS))
    df = pd.DataFrame([{
        'order_id': 'exp-stoke-1',
        'name': 'WT-STOKE',
        'order_import_integration_type': 'MANUAL',
        'resource_subcontractor': 'Palletline (export to API)',
        'resource_tractor': tractor_id,
        'resource_rigid': None,
        'origin_postal_code': 'ST4 4RJ',
        'destination_postal_code': 'B37 7HB',
        'destination_requested_start_timestamp_local': '2026-01-07 12:00:00',
        'origin_requested_start_timestamp_local': '2026-01-07 09:00:00',
        'goods_weight': 5000.0,
        'goods_pallet_spaces': 10.0,
        'service_level_name': 'Next day',
        'transport_service': '1. Non Hazardous Shipment',
        'shipment_names': None,
    }])
    cache = {'ST4 4RJ': (53.006, -2.185), 'B37 7HB': (52.467, -1.787)}
    orders = build_scoped_orders(df, cache, cb22_fleet_only=True)
    assert len(orders) == 1
    assert orders[0].flow == 'PL_EXPORT'
    assert orders[0].stop_type == 'pickup'
    assert orders[0].origin_pc == 'ST4 4RJ'
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/cambridge/test_scope.py::test_in_tractor_scope_stoke_st4_is_in_scope tests/cambridge/test_scope.py::test_in_tractor_scope_stoke_st4_not_in_cambridge_scope tests/cambridge/test_scope.py::test_in_tractor_scope_returns_false_for_non_pl_export tests/cambridge/test_scope.py::test_in_tractor_scope_excludes_origins_beyond_300km tests/cambridge/test_scope.py::test_backtest_pl_export_tractor_long_haul_stoke_in_scope -v
```

Expected: FAIL with `ImportError: cannot import name 'in_tractor_scope'`

- [ ] **Step 3: Add in_tractor_scope() to scope.py**

Add this function after `in_cambridge_scope()` (around line 161):

```python
def in_tractor_scope(row: pd.Series, flow: FlowTag,
                     postcode_cache: dict) -> bool:
    """Scope check for tractor-assigned PL_EXPORT long-haul collections.

    Allows origins up to TRACTOR_CATCHMENT_RADIUS_KM from CB22 — significantly
    larger than the rigid CATCHMENT_RADIUS_KM. Only applies to PL_EXPORT.
    In backtest mode, used when the order has a CB22 tractor assigned.

    Returns False for all non-PL_EXPORT flows so callers can use it
    unconditionally without a flow guard.
    """
    if flow != 'PL_EXPORT':
        return False
    from cambridge.config import TRACTOR_CATCHMENT_RADIUS_KM
    origin_pc = str(row.get('origin_postal_code', '')).strip().upper()
    origin_coords = postcode_cache.get(origin_pc)
    if origin_coords is None:
        return False
    if isinstance(origin_coords, dict):
        olat, olon = origin_coords['lat'], origin_coords['lon']
    else:
        olat, olon = origin_coords
    dlat, dlon = CB22_DEPOT_ANCHOR
    return _haversine_km(olat, olon, dlat, dlon) <= TRACTOR_CATCHMENT_RADIUS_KM
```

- [ ] **Step 4: Update the backtest branch in build_scoped_orders()**

Find the backtest branch in `build_scoped_orders()` (currently around line 328):

```python
            elif tractor_set & CB22_TRACTORS:
                if flow in ('FULL_FLEET', 'PL_EXPORT'):
                    # Apply origin 100 km filter: local rigid/tractor collections only;
                    # excludes long-haul tractor runs (Stoke ST4, ~240 km).
                    if not in_cambridge_scope(row, flow, postcode_cache):
                        continue
                # PL_IMPORT tractor-only: always in scope (hub-delivered freight).
```

Replace with:

```python
            elif tractor_set & CB22_TRACTORS:
                if flow in ('FULL_FLEET', 'PL_EXPORT'):
                    # PL_EXPORT tractor long-haul: try tractor radius (300 km) first,
                    # then fall back to rigid radius (100 km). FULL_FLEET uses rigid
                    # radius only (tractor collections already in plan_collections).
                    if flow == 'PL_EXPORT' and in_tractor_scope(row, flow, postcode_cache):
                        pass  # e.g. Stoke ST4 ~190 km — in scope for tractor
                    elif not in_cambridge_scope(row, flow, postcode_cache):
                        continue
                # PL_IMPORT tractor-only: always in scope (hub-delivered freight).
```

- [ ] **Step 5: Run to verify all new tests pass**

```
pytest tests/cambridge/test_scope.py -v
```

Expected: all scope tests pass.

- [ ] **Step 6: Run full suite**

```
pytest tests/ -q --tb=short
```

Expected: all tests pass.

---

## Task 7: Wire build_trunk_schedule() into run_day_multi_trip() and expose in backtest report

**Files:**
- Modify: `cambridge/dispatcher.py` (`run_day_multi_trip` function)
- Modify: `cambridge/backtest.py` (`run_day_backtest` function)
- Test: `tests/cambridge/test_dispatcher.py`

- [ ] **Step 1: Write the smoke test**

Add to `tests/cambridge/test_dispatcher.py`:

```python
def test_run_day_multi_trip_builds_trunk_schedule_internally():
    """run_day_multi_trip calls build_trunk_schedule and passes it to
    build_freight_availability — verify no crash and PL_IMPORT orders are
    available at day_start when trunk returns before 06:00."""
    from cambridge.dispatcher import run_day_multi_trip

    pl_order = _pl('smoke-pl')
    result = run_day_multi_trip(
        date_type(2026, 1, 7),
        orders=[pl_order],
        trips=[],
        postcode_cache={'CB2 1AA': (52.2049, 0.1218)},
        mode='forward',
        solver_budget_s=2.0,
    )
    # Smoke: function completes, order is either assigned or in unassigned (not crashed).
    assert isinstance(result.metrics, dict)
    assert result.metrics['orders_total'] == 1
```

- [ ] **Step 2: Run to verify it passes (it should — smoke test verifies no regression)**

```
pytest tests/cambridge/test_dispatcher.py::test_run_day_multi_trip_builds_trunk_schedule_internally -v
```

Expected: PASS (the call completes without error even before the wiring is done, because `build_freight_availability` defaults to day_start when no trunk_schedule is provided).

- [ ] **Step 3: Add build_trunk_schedule import and call in run_day_multi_trip()**

At the top of `run_day_multi_trip()` in `dispatcher.py`, after the existing imports inside the function, add:

```python
    from cambridge.collection_planner import build_trunk_schedule
    trunk_schedule = build_trunk_schedule(day)
```

Then update the `build_freight_availability` call later in the function:

**Before:**
```python
    freight_ready = build_freight_availability(orders, trips, day)
```

**After:**
```python
    freight_ready = build_freight_availability(orders, trips, day,
                                               trunk_schedule=trunk_schedule)
```

- [ ] **Step 4: Add trunk schedule to the backtest report JSON**

In `run_day_backtest()` in `backtest.py`, after building `day_out` (around line 312), add:

```python
    from cambridge.collection_planner import build_trunk_schedule
    trunk = build_trunk_schedule(day)
```

Then in the `report` dict construction (around line 424), add a `trunk_schedule` key:

```python
    report = {'day': day.isoformat(), 'planned': planned, 'actual': actual,
              'level0': l0, 'level1': l1,
              'unassigned': day_out.unassigned,
              'trunk_schedule': {
                  'depart_cb22':  trunk.depart_cb22.isoformat(),
                  'arrive_hub':   trunk.arrive_hub.isoformat(),
                  'depart_hub':   trunk.depart_hub.isoformat(),
                  'arrive_cb22':  trunk.arrive_cb22.isoformat(),
                  'freight_ready': trunk.freight_ready.isoformat(),
              }}
```

- [ ] **Step 5: Run the full test suite**

```
pytest tests/ -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 6: Re-run the Jan 7 backtest and verify the report includes trunk_schedule**

```
python -m cambridge --date 2026-01-07
```

Then check the output JSON:

```
python -c "
import json
r = json.load(open('data/Output/cambridge/day_compare_2026-01-07.json'))
print(json.dumps(r['trunk_schedule'], indent=2))
"
```

Expected output (approximate times):
```json
{
  "depart_cb22": "2026-01-06T18:00:00",
  "arrive_hub": "2026-01-06T21:3x:xx",
  "depart_hub": "2026-01-06T23:0x:xx",
  "arrive_cb22": "2026-01-07T02:4x:xx",
  "freight_ready": "2026-01-07T03:1x:xx"
}
```

freight_ready will be before 06:00, confirming normal PL_IMPORT freight is clamped to 06:00 (no behavioral change on non-delayed days).

---

## Self-Review

**Spec coverage:**
- [x] TrunkSchedule data model — Task 2
- [x] PL_IMPORT freight ready from trunk return — Task 3
- [x] Tractor shift_end capped at trunk departure — Task 4
- [x] PL_EXPORT window_end = trunk departure — Task 5
- [x] in_tractor_scope() for long-haul backtest — Task 6
- [x] Wire into run_day_multi_trip() and backtest report — Task 7
- [x] Config constants — Task 1

**Type consistency:**
- `TrunkSchedule` is defined in Task 2, imported in Tasks 3 and 7 consistently.
- `build_trunk_schedule(day: date_type) -> TrunkSchedule` — signature used consistently across Tasks 2 and 7.
- `build_freight_availability(..., trunk_schedule=None)` — Task 3 adds the param; Task 7 passes it.
- `in_tractor_scope(row, flow, postcode_cache) -> bool` — defined in Task 6, used in same task.

**No placeholders:** All test code is concrete; all implementation code is shown in full.
