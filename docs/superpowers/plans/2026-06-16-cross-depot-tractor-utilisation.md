# Cross-Depot Tractor Utilisation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the planner leaving capable tractors idle while in-scope orders go undelivered, by (Layer 1) allocating tractor-needing OVERFLOW freight to the depot with spare tractor capacity at plan time, and (Layer 2) letting idle home-depot tractors absorb reachable, field-collectible overflow across depots at dispatch time.

**Architecture:** Layer 1 makes `capacity_allocator._assign_overflow_local` vehicle-type-aware (separate rigid-stop vs tractor headroom pools). Layer 2 extracts pure helpers into a new `cambridge/fleet_sweep.py` and uses them to widen `day_coordinator`'s existing cross-depot "Pass 2" so it feeds idle home-depot tractors (not only REMOTE ones) into the field-collectible sweep, gated by a day-trip reachability cap.

**Tech Stack:** Python 3.12, pytest. Pure-function helpers + targeted edits to two existing modules. No new external deps.

> **STANDING CONSTRAINT — NO GIT.** `e:\BEAT` is not a git repository and the user requires all work to stay local. **Do not run `git` at any step.** Where this plan says "Checkpoint", it means: confirm the file is saved and the listed tests pass — nothing else. Subagents executing this plan must never invoke git.

> **Before running any task:** the codebase caches bytecode that has bitten this project before. Run `find . -name "*.pyc" -delete` from `e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics` before the first test run of a session.

**Reference spec:** `docs/superpowers/specs/2026-06-16-cross-depot-tractor-utilisation-design.md`

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `cambridge/week_planner/capacity_allocator.py` | Phase 1 OVERFLOW LOCAL allocation | Modify: type-aware headroom |
| `cambridge/fleet_sweep.py` | **New.** Pure helpers: idle-tractor detection + reachability test | Create |
| `cambridge/day_coordinator.py` | Phase 2 per-day orchestration + cross-depot Pass 2 | Modify: feed idle tractors into Pass 2 |
| `tests/cambridge/test_overflow_balancing.py` | **New.** Layer 1 unit tests | Create |
| `tests/cambridge/test_fleet_sweep.py` | **New.** Layer 2 helper unit tests | Create |

**Known constant values (verified 2026-06-16):** `CATCHMENT_RADIUS_KM = 100.0`, `DEPOT_ANCHORS = {'CB22': (52.0859, 0.1717), 'BEDFORD': (52.33106, -0.60767), 'ST_IVES': (52.33367, -0.06487)}`, `MIN_VIABLE_TRIP_HOURS_V16 = 3.0`.

---

## Task 1: Layer 1 — vehicle-type-aware OVERFLOW headroom

**Files:**
- Modify: `cambridge/week_planner/capacity_allocator.py` (constant block ~line 22-28; `_assign_overflow_local` ~line 41-89; caller in `allocate_local_capacity` ~line 139-216)
- Test: `tests/cambridge/test_overflow_balancing.py` (create)

**What changes & why:** Today `_assign_overflow_local` sizes one blind pool `vehicles × 7 stops` and assigns each OVERFLOW order to the nearest depot with stop-headroom. It cannot see that the nearer depot's *tractors* are full while the farther depot's tractors are idle. We split headroom into a rigid-stop pool and a tractor pool, classify each OVERFLOW order as tractor-needing (nearest-candidate-depot distance > `CATCHMENT_RADIUS_KM`) or rigid-serviceable, and route tractor-needing orders to the depot with tractor headroom.

- [ ] **Step 1: Write the failing tests**

Create `tests/cambridge/test_overflow_balancing.py`:

```python
"""Layer 1: vehicle-type-aware OVERFLOW LOCAL balancing.

A tractor-needing OVERFLOW order (destination beyond rigid catchment) must go to
the depot that still has TRACTOR headroom, even when a nearer depot has only
rigid-stop headroom left. Rigid-serviceable local OVERFLOW keeps using nearest
depot with rigid headroom (no regression).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from cambridge.week_planner.capacity_allocator import _assign_overflow_local
from cambridge.scope import ScopedOrder


def _order(oid, dest_pc):
    return ScopedOrder(
        order_id=oid, name=oid, flow='PL_IMPORT',
        origin_pc=None, destination_pc=dest_pc,
        weight_kg=100.0, pallets=1,
        delivery_window=None, collection_window=None,
    )


def test_tractor_needing_overflow_goes_to_depot_with_tractor_headroom():
    # Coventry CV1 1AA ~ 120 km from CB22, ~95 km from BEDFORD: nearer = BEDFORD,
    # and >100 km from CB22 so it is tractor-needing. BEDFORD has NO tractor
    # headroom (0 tractors); CB22 has tractor headroom -> assign CB22.
    cache = {'CV1 1AA': (52.4068, -1.5197)}
    orders = [_order('o1', 'CV1 1AA')]
    rigid_vehicles   = {'CB22': 10, 'BEDFORD': 10}
    tractor_vehicles = {'CB22': 5,  'BEDFORD': 0}
    native_rigid_load   = {'CB22': 0, 'BEDFORD': 0}
    native_tractor_load = {'CB22': 0, 'BEDFORD': 0}
    out = _assign_overflow_local(
        orders, rigid_vehicles, tractor_vehicles,
        native_rigid_load, native_tractor_load, cache,
    )
    assert 'o1' in out['CB22']
    assert 'o1' not in out['BEDFORD']


def test_rigid_serviceable_overflow_uses_nearest_with_rigid_headroom():
    # SG8 (Royston) is ~15 km from CB22, well within 100 km rigid catchment ->
    # rigid-serviceable; nearest depot CB22 has rigid headroom -> CB22.
    cache = {'SG8 5HG': (52.0470, -0.0210)}
    orders = [_order('o2', 'SG8 5HG')]
    rigid_vehicles   = {'CB22': 10, 'BEDFORD': 10}
    tractor_vehicles = {'CB22': 5,  'BEDFORD': 5}
    native_rigid_load   = {'CB22': 0, 'BEDFORD': 0}
    native_tractor_load = {'CB22': 0, 'BEDFORD': 0}
    out = _assign_overflow_local(
        orders, rigid_vehicles, tractor_vehicles,
        native_rigid_load, native_tractor_load, cache,
    )
    assert 'o2' in out['CB22']
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_overflow_balancing.py -v`
Expected: FAIL — `_assign_overflow_local() takes ... positional arguments` (signature mismatch / TypeError).

- [ ] **Step 3: Add the tractor stop-target constant**

In `cambridge/week_planner/capacity_allocator.py`, after the `_OVERFLOW_TARGET_STOPS` block (~line 25), add:

```python
# Tractors do fewer, longer stops than rigids; size their OVERFLOW headroom
# against observed tractor median (~3 stops/day on the Jan 2026 backtest).
_OVERFLOW_TARGET_TRACTOR_STOPS: float = 3.0
```

- [ ] **Step 4: Rewrite `_assign_overflow_local` to be type-aware**

Replace the whole function body (currently ~line 41-89) with:

```python
def _assign_overflow_local(
    overflow_orders: list[ScopedOrder],
    rigid_vehicles: dict[str, int],
    tractor_vehicles: dict[str, int],
    native_rigid_load: dict[str, int],
    native_tractor_load: dict[str, int],
    postcode_cache: dict,
    target_stops: float = _OVERFLOW_TARGET_STOPS,
    target_tractor_stops: float = _OVERFLOW_TARGET_TRACTOR_STOPS,
) -> dict[str, list[str]]:
    """Capacity-aware assignment of OVERFLOW LOCAL orders to CB22 or BEDFORD.

    Headroom is tracked in two pools per depot: rigid-stop headroom and tractor
    headroom. An order whose nearest-candidate-depot distance exceeds the rigid
    catchment radius is *tractor-needing* and consumes tractor headroom; closer
    orders are *rigid-serviceable* and consume rigid-stop headroom. Tractor-needing
    orders are steered to the depot with tractor headroom (nearest such depot),
    so the freight is trunked to a depot that has an available tractor.

    Returns dict[depot_id -> list[order_id]] of assignments.
    """
    from cambridge.config import DEPOT_ANCHORS, CATCHMENT_RADIUS_KM

    assignments: dict[str, list[str]] = {dep: [] for dep in _OVERFLOW_CANDIDATES}
    rigid_load   = {dep: native_rigid_load.get(dep, 0) for dep in _OVERFLOW_CANDIDATES}
    tractor_load = {dep: native_tractor_load.get(dep, 0) for dep in _OVERFLOW_CANDIDATES}
    rigid_cap   = {dep: rigid_vehicles.get(dep, 0) * target_stops
                   for dep in _OVERFLOW_CANDIDATES}
    tractor_cap = {dep: tractor_vehicles.get(dep, 0) * target_tractor_stops
                   for dep in _OVERFLOW_CANDIDATES}

    def _dist(order: ScopedOrder, depot_id: str) -> float:
        pc = (order.destination_pc if order.stop_type == 'delivery'
              else (order.origin_pc or ''))
        coords = postcode_cache.get(pc or '') if pc else None
        if not coords:
            return 999.0
        lat = coords['lat'] if isinstance(coords, dict) else coords[0]
        lon = coords['lon'] if isinstance(coords, dict) else coords[1]
        anc = DEPOT_ANCHORS[depot_id]
        return _haversine_km(anc[0], anc[1], lat, lon)

    def _rigid_headroom(dep: str) -> float:
        return max(0.0, rigid_cap[dep] - rigid_load[dep])

    def _tractor_headroom(dep: str) -> float:
        return max(0.0, tractor_cap[dep] - tractor_load[dep])

    # Sort descending by |dist_CB22 - dist_BED|: clear-cut cases first so they
    # lock in before borderline orders consume headroom.
    sorted_orders = sorted(
        overflow_orders,
        key=lambda o: abs(_dist(o, 'CB22') - _dist(o, 'BEDFORD')),
        reverse=True,
    )

    for order in sorted_orders:
        dists = {d: _dist(order, d) for d in _OVERFLOW_CANDIDATES}
        nearest = min(dists, key=dists.__getitem__)
        farther = max(dists, key=dists.__getitem__)
        tractor_needing = dists[nearest] > CATCHMENT_RADIUS_KM

        if tractor_needing:
            target = nearest if _tractor_headroom(nearest) > 0 else farther
            tractor_load[target] += 1
        else:
            target = nearest if _rigid_headroom(nearest) > 0 else farther
            rigid_load[target] += 1
        assignments[target].append(order.order_id)

    return assignments
```

- [ ] **Step 5: Update the caller in `allocate_local_capacity`**

In the first pass (the `for depot_id, (rigids, tractors) in fleet.items():` loop, ~line 144), after `available` is built and before `depot_vehicles[depot_id] = len(available)` (~line 186), add per-type counts. Add these dicts alongside `depot_vehicles`/`native_load` initialisation (~line 140):

```python
        depot_rigid_vehicles: dict[str, int] = {}
        depot_tractor_vehicles: dict[str, int] = {}
        native_rigid_load: dict[str, int] = {}
        native_tractor_load: dict[str, int] = {}
```

Inside the depot loop, **after** the existing `native_load[depot_id] = sum(...)` assignment (~line 188-193) — it must come after `native_load[depot_id]` is set, since the block below subtracts from it — add:

```python
            depot_tractor_vehicles[depot_id] = sum(1 for v in available if v in tractors)
            depot_rigid_vehicles[depot_id]   = len(available) - depot_tractor_vehicles[depot_id]
            native_tractor_load[depot_id] = sum(
                1 for o in week_orders
                if o.delivery_date == d and o.depot_id == depot_id
                and o.order_class != OrderClass.TOUR
                and _order_needs_tractor(o, postcode_cache)
            )
            native_rigid_load[depot_id] = native_load[depot_id] - native_tractor_load[depot_id]
```

Add this module-level helper near `_haversine_km` (~line 39):

```python
def _order_needs_tractor(order: ScopedOrder, postcode_cache: dict | None) -> bool:
    """True when the order's stop is beyond rigid catchment of BOTH candidate depots."""
    from cambridge.config import DEPOT_ANCHORS, CATCHMENT_RADIUS_KM
    if postcode_cache is None:
        return False
    pc = (order.destination_pc if order.stop_type == 'delivery'
          else (order.origin_pc or ''))
    coords = postcode_cache.get(pc or '') if pc else None
    if not coords:
        return False
    lat = coords['lat'] if isinstance(coords, dict) else coords[0]
    lon = coords['lon'] if isinstance(coords, dict) else coords[1]
    return all(
        _haversine_km(DEPOT_ANCHORS[dep][0], DEPOT_ANCHORS[dep][1], lat, lon) > CATCHMENT_RADIUS_KM
        for dep in _OVERFLOW_CANDIDATES
    )
```

Then update the `_assign_overflow_local` call in the second pass (~line 210) from:

```python
                overflow_assignments = _assign_overflow_local(
                    overflow_day, cand_vehicles, cand_load, postcode_cache,
                )
```

to:

```python
                overflow_assignments = _assign_overflow_local(
                    overflow_day,
                    {dep: depot_rigid_vehicles.get(dep, 0) for dep in _OVERFLOW_CANDIDATES},
                    {dep: depot_tractor_vehicles.get(dep, 0) for dep in _OVERFLOW_CANDIDATES},
                    {dep: native_rigid_load.get(dep, 0) for dep in _OVERFLOW_CANDIDATES},
                    {dep: native_tractor_load.get(dep, 0) for dep in _OVERFLOW_CANDIDATES},
                    postcode_cache,
                )
```

The now-unused `cand_vehicles`/`cand_load` locals (~line 204-209) can be deleted.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_overflow_balancing.py -v`
Expected: PASS (2 passed).

- [ ] **Step 7: Run the existing Phase-1 tests to check for regressions**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/ -k "capacity or allocator or phase1 or overflow" -v`
Expected: PASS, or only pre-existing unrelated failures (note any in the report; do not fix unrelated stale tests).

- [ ] **Step 8: Checkpoint (local, no git)** — confirm `capacity_allocator.py` saved and Steps 6-7 green.

---

## Task 2: Layer 2 — pure helpers for idle-tractor cross-depot sweep

**Files:**
- Create: `cambridge/fleet_sweep.py`
- Test: `tests/cambridge/test_fleet_sweep.py` (create)

**What changes & why:** `plan_day`'s Pass 2 logic is embedded in a large function and is hard to test directly. Extract the two decisions it needs — *which tractors are idle* and *is an order within day-trip reach of any sweep tractor* — into pure, unit-testable helpers. Task 3 wires them into `plan_day`.

- [ ] **Step 1: Write the failing tests**

Create `tests/cambridge/test_fleet_sweep.py`:

```python
"""Layer 2 pure helpers: idle-tractor detection and day-trip reachability."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from cambridge.fleet_sweep import idle_home_depot_tractors, point_within_reach


def test_idle_excludes_deployed_tour_and_non_depot_locations():
    depot_tractors = {'A', 'B', 'C', 'D', 'E'}
    deployed       = {'A'}                       # A ran stops
    tour_committed = {'B'}                        # B on a multi-day tour
    locations = {
        'C': 'CB22_DEPOT',                        # idle at depot -> eligible
        'D': 'B37_HUB',                           # on trunk/hub -> not idle-at-depot
        'E': 'REMOTE:52.5:-1.4',                  # overnight away -> handled elsewhere
        # A and B locations irrelevant (excluded by deployed/tour)
    }
    out = idle_home_depot_tractors(depot_tractors, deployed, tour_committed, locations)
    assert out == {'C'}


def test_idle_treats_missing_location_as_home_depot():
    # A tractor with no recorded location defaults to its home depot (idle).
    out = idle_home_depot_tractors({'X'}, set(), set(), {})
    assert out == {'X'}


def test_point_within_reach_true_when_close_to_any_position():
    # CB22 anchor; a point ~15 km away is within a 200 km cap.
    positions = [(52.0859, 0.1717)]
    assert point_within_reach(52.047, -0.021, positions, 200.0) is True


def test_point_within_reach_false_when_beyond_cap():
    # CB22 anchor; Belfast (~530 km) is beyond a 200 km cap.
    positions = [(52.0859, 0.1717)]
    assert point_within_reach(54.5973, -5.9301, positions, 200.0) is False


def test_point_within_reach_true_when_no_positions():
    # No sweep tractors -> no filter (mirrors existing REMOTE behaviour).
    assert point_within_reach(54.0, -5.0, [], 200.0) is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_fleet_sweep.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cambridge.fleet_sweep'`.

- [ ] **Step 3: Create `cambridge/fleet_sweep.py`**

```python
"""Pure helpers for the cross-depot idle-tractor sweep (Layer 2).

These functions hold no I/O and no solver state so they can be unit-tested in
isolation. `day_coordinator` uses them to widen its existing cross-depot Pass 2:
idle home-depot tractors (not only REMOTE overnight tractors) are offered the
field-collectible orders still unassigned after per-depot dispatch, capped to a
feasible day-trip reach.
"""
from __future__ import annotations

import math


def idle_home_depot_tractors(
    depot_tractors: set[str],
    deployed: set[str],
    tour_committed: set[str],
    vehicle_locations: dict[str, str],
) -> set[str]:
    """Tractors that are at a depot and given no work today.

    A tractor is idle-at-depot when it is in the depot tractor roster, was not
    deployed (ran no stops in any per-depot dispatch), is not committed to a
    multi-day tour, and its current location is a depot (not a hub or REMOTE).
    A missing location defaults to its home depot (treated as at-depot).
    """
    out: set[str] = set()
    for vid in depot_tractors:
        if vid in deployed or vid in tour_committed:
            continue
        loc = str(vehicle_locations.get(vid, '_DEPOT'))
        if loc.endswith('_DEPOT'):
            out.add(vid)
    return out


def point_within_reach(
    lat: float,
    lon: float,
    positions: list[tuple[float, float]],
    max_km: float,
) -> bool:
    """True when (lat, lon) is within max_km of ANY position.

    With no positions the filter is open (returns True), mirroring the existing
    REMOTE-reach behaviour where an empty position list means 'do not filter'.
    """
    if not positions:
        return True
    p = math.pi / 180
    for plat, plon in positions:
        dlat = (lat - plat) * p
        dlon = (lon - plon) * p
        a = (math.sin(dlat / 2) ** 2
             + math.cos(plat * p) * math.cos(lat * p) * math.sin(dlon / 2) ** 2)
        if 2 * 6371.0 * math.asin(math.sqrt(a)) <= max_km:
            return True
    return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_fleet_sweep.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Checkpoint (local, no git)** — confirm `fleet_sweep.py` saved and Step 4 green.

---

## Task 3: Layer 2 — wire idle tractors into `plan_day` Pass 2

**Files:**
- Modify: `cambridge/day_coordinator.py` (per-depot loop ~line 510-566; Pass 2 ~line 581-656)

**What changes & why:** Currently Pass 2 runs only `if remote_vids:` and feeds only REMOTE tractors. We (a) collect the tractors actually deployed and the tractors committed to tours during the per-depot loop, (b) compute idle home-depot tractors with the Task-2 helper, (c) generalise Pass 2 to run when there are *either* REMOTE *or* idle tractors, feeding both, with each sweep tractor's start position added to the reachability positions and a day-trip cap.

- [ ] **Step 1: Add the import and module constant**

At the top of `cambridge/day_coordinator.py`, with the other `cambridge.*` imports, add:

```python
from cambridge.fleet_sweep import idle_home_depot_tractors, point_within_reach
```

Near the top-level constants of the module (after the imports), add:

```python
# Day-trip reach cap for the cross-depot idle-tractor sweep: an idle tractor is
# only offered overflow it can plausibly serve and return from in one shift.
# Long-haul singletons beyond this stay unassigned (a Phase 1 tour-coverage gap).
_MAX_SWEEP_REACH_KM = 200.0
```

- [ ] **Step 2: Track deployed and tour-committed tractors in the per-depot loop**

Immediately before the per-depot loop `for _depot_id in ('CB22', 'BEDFORD', 'ST_IVES'):` (~line 510), add:

```python
    _deployed_tractors: set[str] = set()
    _tour_committed_tractors: set[str] = set()
    _all_depot_tractors: set[str] = set()
```

Inside the loop, after `_tractor_set = DEPOT_TRACTOR_SETS[_depot_id] - remote_vids` (~line 513), add:

```python
        _all_depot_tractors |= (DEPOT_TRACTOR_SETS[_depot_id] - remote_vids)
```

Inside the `if _avail_set:` block where tour tractors are computed (~line 527-529), after `_tour_tractors = _tractor_set - _avail_set`, add:

```python
                    _tour_committed_tractors |= _tour_tractors
```

After `_out.routes = collapse_routes(_out.routes)` (~line 553), record which of this depot's tractors actually ran stops:

```python
        for _rk, _rv in _out.routes.items():
            if isinstance(_rv, dict) and _rv.get('stops'):
                _base_vid = str(_rk).split('_FB')[0]
                if _base_vid in DEPOT_TRACTOR_SETS[_depot_id]:
                    _deployed_tractors.add(_base_vid)
```

- [ ] **Step 3: Compute idle tractors and generalise the Pass 2 guard**

Replace the Pass 2 header and guard. Change (~line 581-585):

```python
    # Pass 2: REMOTE tractors handle field-collectible orders still unassigned
    all_dispatch_orders: list[ScopedOrder] = [
        o for _orders in orders_by_depot.values() for o in _orders
    ]
    if remote_vids:
        import math as _math2
        _unassigned_set = set(_merged_unassigned)
```

to:

```python
    # Pass 2: cross-depot field-collectible sweep. Both REMOTE overnight tractors
    # AND idle home-depot tractors are offered the field-collectible orders still
    # unassigned after per-depot dispatch, capped to a feasible day-trip reach.
    all_dispatch_orders: list[ScopedOrder] = [
        o for _orders in orders_by_depot.values() for o in _orders
    ]
    _idle_tractors = idle_home_depot_tractors(
        _all_depot_tractors, _deployed_tractors, _tour_committed_tractors, all_veh_locs,
    )
    _sweep_vids = frozenset(remote_vids) | frozenset(_idle_tractors)
    if _idle_tractors:
        print(f'  [SWEEP] {len(_idle_tractors)} idle home-depot tractor(s) join the'
              f' cross-depot field-collectible sweep')
    if _sweep_vids:
        import math as _math2
        _unassigned_set = set(_merged_unassigned)
```

- [ ] **Step 4: Add idle-tractor depot anchors to the reachability positions**

In the position-collection block (~line 592-602), after the loop that appends REMOTE positions and before `_MAX_REMOTE_REACH_KM = 200.0`, add idle-tractor depot anchors and switch the cap to the sweep constant. Replace:

```python
        _remote_positions: list[tuple[float, float]] = []
        for _vid in remote_vids:
            _loc = str(all_veh_locs.get(_vid, ''))
            if _loc.startswith('REMOTE:'):
                _parts = _loc.split(':')
                try:
                    _remote_positions.append((float(_parts[1]), float(_parts[2])))
                except (IndexError, ValueError):
                    pass

        _MAX_REMOTE_REACH_KM = 200.0

        def _within_remote_reach(order: 'ScopedOrder') -> bool:
            if not _remote_positions:
                return True
            pc = order.origin_pc if order.stop_type == 'pickup' else order.destination_pc
            coords = postcode_cache.get(pc) if pc else None
            if coords is None:
                return True
            olat, olon = ((coords['lat'], coords['lon'])
                          if isinstance(coords, dict) else coords)
            p = _math2.pi / 180
            for rlat, rlon in _remote_positions:
                dlat = (olat - rlat) * p
                dlon = (olon - rlon) * p
                a = (_math2.sin(dlat / 2) ** 2
                     + _math2.cos(rlat * p) * _math2.cos(olat * p)
                     * _math2.sin(dlon / 2) ** 2)
                if 2 * 6371.0 * _math2.asin(_math2.sqrt(a)) <= _MAX_REMOTE_REACH_KM:
                    return True
            return False
```

with:

```python
        _sweep_positions: list[tuple[float, float]] = []
        for _vid in remote_vids:
            _loc = str(all_veh_locs.get(_vid, ''))
            if _loc.startswith('REMOTE:'):
                _parts = _loc.split(':')
                try:
                    _sweep_positions.append((float(_parts[1]), float(_parts[2])))
                except (IndexError, ValueError):
                    pass
        for _vid in _idle_tractors:
            _loc = str(all_veh_locs.get(_vid, '_DEPOT'))
            for _dep, _anc in DEPOT_ANCHORS.items():
                if _loc.startswith(_dep):
                    _sweep_positions.append((_anc[0], _anc[1]))
                    break

        def _within_remote_reach(order: 'ScopedOrder') -> bool:
            pc = order.origin_pc if order.stop_type == 'pickup' else order.destination_pc
            coords = postcode_cache.get(pc) if pc else None
            if coords is None:
                return True
            olat, olon = ((coords['lat'], coords['lon'])
                          if isinstance(coords, dict) else coords)
            return point_within_reach(olat, olon, _sweep_positions, _MAX_SWEEP_REACH_KM)
```

- [ ] **Step 5: Feed the sweep tractors (REMOTE + idle) into the dispatch call**

In the eligible-order list and the dispatch call (~line 624-648), the comprehension already filters `not _needs_depot_load(o)` and `_within_remote_reach(o)` — keep it. Change the vehicle wiring from REMOTE-only to the sweep set. Replace:

```python
            _remote_veh_locs = {v: l for v, l in all_veh_locs.items() if v in remote_vids}
            _out2 = run_day_multi_trip(
                day=day, orders=_remote_eligible, trips=[],
                postcode_cache=postcode_cache, mode=mode, telem_df=telem_df,
                solver_budget_s=solver_budget_s, pre_staged_ids=pre_staged_ids,
                vehicle_locations=_remote_veh_locs,
                vehicle_available_from=None,
                rigid_set=frozenset(),
                tractor_set=frozenset(remote_vids),
                depot_anchor=CB22_DEPOT_ANCHOR,  # start coords come from REMOTE: locs anyway
            )
```

with:

```python
            _sweep_veh_locs = {v: l for v, l in all_veh_locs.items() if v in _sweep_vids}
            _out2 = run_day_multi_trip(
                day=day, orders=_remote_eligible, trips=[],
                postcode_cache=postcode_cache, mode=mode, telem_df=telem_df,
                solver_budget_s=solver_budget_s, pre_staged_ids=pre_staged_ids,
                vehicle_locations=_sweep_veh_locs,
                vehicle_available_from=None,
                rigid_set=frozenset(),
                tractor_set=frozenset(_sweep_vids),
                depot_anchor=CB22_DEPOT_ANCHOR,  # idle-tractor start coords come from depot locs
            )
```

> Note: idle home-depot tractors have a `*_DEPOT` location in `all_veh_locs` (or none). `run_day_multi_trip` resolves a `*_DEPOT` location to the depot anchor for the route start, so no REMOTE coordinate is needed for them.

- [ ] **Step 6: Run the helper + coordinator import sanity check**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -c "import cambridge.day_coordinator"`
Expected: no ImportError / SyntaxError.

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/test_fleet_sweep.py -v`
Expected: PASS (5 passed).

- [ ] **Step 7: Run the day_coordinator-related test suite for regressions**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/cambridge/ -k "coordinator or day or dispatch or remote" -v`
Expected: PASS, or only pre-existing unrelated failures (record any; do not fix stale unrelated tests).

- [ ] **Step 8: Checkpoint (local, no git)** — confirm `day_coordinator.py` saved and Steps 6-7 green.

---

## Task 4: Integration backtest + documentation

**Files:**
- Modify: `cambridge/README.md` (Recent changes table; replace any subcontract-valve / KM-gap framing as needed)
- Modify (memory): `C:\Users\Yuansheng Tao\.claude\projects\e--BEAT\memory\misses-not-capacity-two-mechanisms.md`, `validity-in-universe-coverage.md`, `MEMORY.md`
- Create (memory): a new note recording the cross-depot fix + the "no subcontract valve / long-haul = Phase 1" decision

- [ ] **Step 1: Clear stale bytecode and run the integration backtest**

Run:
```bash
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && find . -name "*.pyc" -delete && python -m cambridge --multiday --start 2026-01-12 --end 2026-01-16
```
Expected: completes; console shows `[SWEEP] N idle home-depot tractor(s) join...` lines on peak days; a fresh `fleet_replay_exports/plan_manifest_2026-01-12_to_2026-01-16.csv` is written.

- [ ] **Step 2: Measure the before/after fleet utilisation**

Run this analysis against the new manifest (the same method used to diagnose the problem):

```bash
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -c "
import sys; sys.path.insert(0,'.')
import pandas as pd, collections
from cambridge.config import CB22_RIGIDS,CB22_TRACTORS,BEDFORD_RIGIDS,BEDFORD_TRACTORS,ST_IVES_RIGIDS
ROSTER=set(CB22_RIGIDS)|set(CB22_TRACTORS)|set(BEDFORD_RIGIDS)|set(BEDFORD_TRACTORS)|set(ST_IVES_RIGIDS)
man=pd.read_csv('fleet_replay_exports/plan_manifest_2026-01-12_to_2026-01-16.csv')
asg=man[man.plan_status=='ASSIGNED']
for day in sorted(asg.planned_day.dropna().unique()):
    vs=set(asg[asg.planned_day==day].assigned_vehicle.astype(str))
    print(day,'deployed',len(vs),'idle',65-len(vs))
un=man[man.plan_status=='UNASSIGNED']
print('CAPACITY_OVERFLOW:',(un.unassigned_reason=='CAPACITY_OVERFLOW').sum())
served=(man.plan_status=='ASSIGNED').sum()
OUT={'CANCELLED','NO_RESOURCES','UNKNOWN_FLOW'}
inuniv=served + ((~un.unassigned_reason.isin(OUT)).sum())
print('in-universe coverage: %.1f%%'%(100*served/inuniv))
"
```
Expected vs the 2026-06-16 baseline (idle 8-17/day; Jan-15 idle 12 incl. 6 CB22 tractors; CAPACITY_OVERFLOW 100; coverage 95.3%): **idle tractor count and CAPACITY_OVERFLOW both fall; coverage rises.** Record the actual numbers in the next step. If CAPACITY_OVERFLOW does *not* fall, stop and report — the wiring is not taking effect.

- [ ] **Step 3: Lateness regression check**

Run:
```bash
cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/test_window_start.py tests/cambridge/test_overflow_balancing.py tests/cambridge/test_fleet_sweep.py -v
```
Expected: all PASS. Confirm the backtest console reported no increase in planned lateness vs baseline (≤2 vehicles late, per the prior memory note). If lateness rose materially, report it.

- [ ] **Step 4: Update the README**

In `cambridge/README.md`, add a row to the "Recent changes" table:

```markdown
| **Cross-depot tractor utilisation** | Layer 1: `capacity_allocator._assign_overflow_local` now sizes OVERFLOW headroom by vehicle type (rigid-stop vs tractor pools) and steers tractor-needing overflow to the depot with tractor headroom. Layer 2: `day_coordinator` Pass 2 now feeds **idle home-depot tractors** (not only REMOTE ones) into the cross-depot field-collectible sweep, capped to a `_MAX_SWEEP_REACH_KM = 200 km` day-trip via `cambridge/fleet_sweep.py`. | The plan deployed only ~50 of 65 vehicles while dropping in-territory orders: on Jan 15, 6 CB22 tractors sat idle *at depot* while 34 orders overflowed onto a saturated Bedford. Tractors ran at ~60% pallet fill vs rigids at 180%. The fix co-locates tractor-needing freight with available tractors and lets idle tractors absorb reachable overflow. |
```

Also add `cambridge/fleet_sweep.py` to the file map table, and remove/soften any remaining "subcontract valve" wording (search the file for "subcontract"): long-haul singletons the sweep cannot reach are a **Phase 1 tour-coverage** gap, not a subcontract target.

- [ ] **Step 5: Update memory notes**

- In `misses-not-capacity-two-mechanisms.md` and `validity-in-universe-coverage.md`: replace the "subcontract valve" / "price misses as subcontract cost" framing with: misses split into (a) reachable overflow now absorbed by idle tractors (cross-depot fix), and (b) long-haul singletons = a Phase 1 tour-coverage gap (no subcontracting).
- Create a new memory note `cross-depot-tractor-utilisation.md` (type: project) summarising: the idle-tractor finding (65 roster, ~50 deployed, 6 idle CB22 tractors on Jan-15 peak), the two-layer fix, files touched, and the recorded decision that there is **no subcontract valve**. Link `[[misses-not-capacity-two-mechanisms]]` and `[[validity-in-universe-coverage]]`.
- Add the new note's one-line pointer to `MEMORY.md`.

- [ ] **Step 6: Checkpoint (local, no git)** — confirm README + memory saved and the backtest numbers recorded in the new memory note.

---

## Self-review notes (author)

- **Spec coverage:** Layer 1 → Task 1; Layer 2 helpers → Task 2; Layer 2 wiring → Task 3; freight-location correctness preserved by keeping `_needs_depot_load` filter (Task 3 Step 5); day-trip cap → `_MAX_SWEEP_REACH_KM` (Task 3); long-haul-singleton/no-subcontract decision recorded → Task 4 Steps 4-5; testing + success criteria → Tasks 1-2 unit tests + Task 4 integration.
- **No-git:** every "Commit" replaced by "Checkpoint (local, no git)" per standing constraint.
- **Type consistency:** `_assign_overflow_local` new signature `(orders, rigid_vehicles, tractor_vehicles, native_rigid_load, native_tractor_load, postcode_cache, ...)` used identically in Task 1 test, function def, and caller. `idle_home_depot_tractors(depot_tractors, deployed, tour_committed, vehicle_locations)` and `point_within_reach(lat, lon, positions, max_km)` match between `fleet_sweep.py`, its tests, and the `day_coordinator` call sites.
- **Partial-tractor exclusion** is intentional (idle-only), per spec.
