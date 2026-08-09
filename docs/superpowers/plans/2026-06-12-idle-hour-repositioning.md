# Idle-Hour Repositioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **STANDING USER CONSTRAINT — DO NOT COMMIT.** All work stays local. Wherever a normal plan would `git commit`, this plan ends a task with a **Checkpoint** (run the tests, confirm green) and nothing else. Do not run `git add`/`git commit`. (The working tree at `e:\BEAT` is not a git repo.)

**Goal:** When a vehicle ends a day away from base, use its remaining drivable hours to advance toward the next objective (a pending delivery, or the home depot) instead of parking at the last stop — fixing 0-km/all-unrouted multi-day tours and wasted late-day capacity, in both Phase 1b and Phase 2.

**Architecture:** One shared helper module (`cambridge/repositioning.py`) provides the geometry + hours math. The Phase 1b tour router (`tour_router.route_tour`) and the Phase 2 day coordinator (`day_coordinator.plan_day`'s REMOTE-overnight block) both call it; only the *target* differs (Phase 1b: nearest pending delivery else depot; Phase 2: depot only).

**Tech Stack:** Python 3, pytest. Reuses `simulation.routing.DEFAULT_AVG_SPEED_KMH` (50 km/h) and `cambridge.config.ROAD_DISTANCE_FACTOR` (1.3) so repositioned points stay solver-consistent.

**Spec:** `docs/superpowers/specs/2026-06-12-tour-router-prepositioning-design.md`

---

## File Structure

- `cambridge/config.py` — add `LEGAL_DAILY_DRIVE_HOURS = 9.0` constant.
- `cambridge/repositioning.py` *(new)* — `advance_toward`, `reachable_haversine_km`, `remaining_drive_hours`, plus a private `_haversine_km`. Single responsibility: idle-hour repositioning math. No I/O, no solver calls — pure and unit-testable.
- `cambridge/week_planner/tour_router.py` — `route_tour`: replace the overnight-position block to call the helper (Phase 1b).
- `cambridge/day_coordinator.py` — `plan_day`: in the REMOTE-overnight block, advance toward depot before writing `REMOTE:` (Phase 2).
- `tests/cambridge/test_repositioning.py` *(new)* — unit tests for the helper + integration tests for both phases.

---

### Task 1: Add the legal daily-driving constant

**Files:**
- Modify: `cambridge/config.py` (next to `RIGID_LEGAL_MAX_SHIFT_HOURS`, ~line 380)

- [ ] **Step 1: Add the constant**

In `cambridge/config.py`, immediately after the `RIGID_LEGAL_MAX_SHIFT_HOURS = 13.0` block, add:

```python
# v2.4: legal daily DRIVING limit (not on-duty span). Used to cap how far a
# vehicle may reposition/return in one day when consuming idle hours. UK HGV
# rule: 9h driving/day (extendable to 10h twice weekly); 9.0 is the safe base.
LEGAL_DAILY_DRIVE_HOURS = 9.0
```

- [ ] **Step 2: Verify it imports**

Run: `python -c "from cambridge.config import LEGAL_DAILY_DRIVE_HOURS; print(LEGAL_DAILY_DRIVE_HOURS)"`
Expected: `9.0`

- [ ] **Step 3: Checkpoint** (no commit — see header)

---

### Task 2: Shared repositioning helper

**Files:**
- Create: `cambridge/repositioning.py`
- Test: `tests/cambridge/test_repositioning.py`

- [ ] **Step 1: Write the failing unit tests**

Create `tests/cambridge/test_repositioning.py`:

```python
from datetime import datetime
import math

from cambridge.repositioning import (
    advance_toward, reachable_haversine_km, remaining_drive_hours, _haversine_km,
)


def test_advance_toward_far_target_moves_full_cap():
    # Target ~100 km north; cap 40 km -> move ~40 km, not reaching target.
    cur = (52.0, 0.0)
    target = (52.9, 0.0)            # ~100 km north
    lat, lon, leg = advance_toward(cur, target, max_km=40.0)
    assert 39.0 < leg < 41.0
    assert 52.0 < lat < target[0]   # moved north, not past target


def test_advance_toward_near_target_lands_exactly_no_overshoot():
    cur = (52.0, 0.0)
    target = (52.1, 0.0)            # ~11 km
    lat, lon, leg = advance_toward(cur, target, max_km=500.0)
    assert abs(lat - target[0]) < 1e-9 and abs(lon - target[1]) < 1e-9
    assert leg < 12.0               # capped at the real distance, no overshoot


def test_advance_toward_zero_cap_stays_put():
    cur = (52.0, 0.0)
    lat, lon, leg = advance_toward(cur, (55.0, -4.0), max_km=0.0)
    assert (lat, lon) == cur and leg == 0.0


def test_reachable_haversine_km_caps_at_legal_day():
    # 9h * 50 km/h / 1.3 ~= 346 km haversine; 20h is capped to the 9h value.
    assert abs(reachable_haversine_km(9.0) - reachable_haversine_km(20.0)) < 1e-6
    assert 340.0 < reachable_haversine_km(9.0) < 350.0
    assert reachable_haversine_km(0.0) == 0.0


def test_remaining_drive_hours_from_return_time_iso():
    shift_end = datetime(2026, 1, 15, 20, 0)
    rd = {'return_time_iso': '2026-01-15T13:00:00'}
    assert abs(remaining_drive_hours(rd, shift_end) - 7.0) < 1e-6


def test_remaining_drive_hours_from_shift_start_plus_on_duty():
    shift_end = datetime(2026, 1, 15, 20, 0)
    rd = {'shift_start_iso': '2026-01-15T07:00:00', 'on_duty_minutes': 360}  # ends 13:00
    assert abs(remaining_drive_hours(rd, shift_end) - 7.0) < 1e-6


def test_remaining_drive_hours_missing_fields_is_zero():
    assert remaining_drive_hours({}, datetime(2026, 1, 15, 20, 0)) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/cambridge/test_repositioning.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cambridge.repositioning'`

- [ ] **Step 3: Create the helper module**

Create `cambridge/repositioning.py`:

```python
"""Idle-hour repositioning math.

When a vehicle ends an operating day away from base, use its remaining drivable
hours to advance toward the next objective (a pending delivery, or the home
depot) instead of parking at its last stop. Pure geometry + time arithmetic;
no solver calls or I/O. Shared by Phase 1b (tour_router) and Phase 2
(day_coordinator).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional, Tuple

from cambridge.config import ROAD_DISTANCE_FACTOR, LEGAL_DAILY_DRIVE_HOURS

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), 'simulation'))
from routing import DEFAULT_AVG_SPEED_KMH  # noqa: E402  (50.0 km/h)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p = math.pi / 180.0
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def reachable_haversine_km(remaining_hours: float) -> float:
    """Max straight-line (haversine) km reachable in `remaining_hours`, capped at
    a legal daily driving day. Road km = hours * speed; convert to haversine by
    dividing out ROAD_DISTANCE_FACTOR so the point stays solver-consistent."""
    hrs = max(0.0, min(remaining_hours, LEGAL_DAILY_DRIVE_HOURS))
    road_km = hrs * DEFAULT_AVG_SPEED_KMH
    return road_km / ROAD_DISTANCE_FACTOR


def remaining_drive_hours(route_dict: dict, shift_end: datetime) -> float:
    """Hours between the route's last action and `shift_end`. 0 when unknown."""
    t: Optional[datetime] = None
    iso = route_dict.get('return_time_iso')
    if iso:
        try:
            t = datetime.fromisoformat(str(iso))
        except (ValueError, TypeError):
            t = None
    if t is None:
        s = route_dict.get('shift_start_iso')
        odm = route_dict.get('on_duty_minutes')
        if s and odm is not None:
            try:
                t = datetime.fromisoformat(str(s)) + timedelta(minutes=float(odm))
            except (ValueError, TypeError):
                t = None
    if t is None:
        return 0.0
    return max(0.0, (shift_end - t).total_seconds() / 3600.0)


def advance_toward(cur: Tuple[float, float], target: Tuple[float, float],
                   max_km: float) -> Tuple[float, float, float]:
    """Move from `cur` toward `target` by up to `max_km` (haversine), never
    overshooting. Returns (new_lat, new_lon, leg_haversine_km). Linear lat/lon
    interpolation — adequate for overnight positioning."""
    dist = _haversine_km(cur[0], cur[1], target[0], target[1])
    if dist <= 0.0 or max_km <= 0.0:
        return cur[0], cur[1], 0.0
    frac = min(max_km, dist) / dist
    new_lat = cur[0] + (target[0] - cur[0]) * frac
    new_lon = cur[1] + (target[1] - cur[1]) * frac
    return new_lat, new_lon, dist * frac
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_repositioning.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Checkpoint** (no commit)

---

### Task 3: Phase 1b integration (`route_tour`)

**Files:**
- Modify: `cambridge/week_planner/tour_router.py:135-150` (the overnight-position block inside the per-day loop)
- Test: `tests/cambridge/test_repositioning.py` (append integration test)

- [ ] **Step 1: Write the failing integration test**

Append to `tests/cambridge/test_repositioning.py`:

```python
from datetime import date


def _make_tour(vehicle_id, order_ids, depart, ret, region='SCOTLAND',
               home_depot='CB22'):
    from cambridge.plan_types import Tour
    return Tour(tour_id='T1', vehicle_id=vehicle_id, region=region,
                home_depot=home_depot, order_ids=list(order_ids),
                depart_date=depart, return_date=ret)


def test_route_tour_prepositions_to_reach_far_delivery():
    """A Scotland delivery unreachable same-day from CB22 should route once the
    tractor pre-positions north on the first tour day."""
    from cambridge.scope import ScopedOrder
    from cambridge.plan_types import OrderClass
    from cambridge.week_planner.tour_router import route_tour

    # One far delivery (Kilmarnock KA1 ~ 55.61, -4.50), open daytime window.
    order = ScopedOrder(
        order_id='o-ka1', name='WT-KA1', flow='FULL_FLEET',
        origin_pc='SG8 5RL', destination_pc='KA1 2NN',
        weight_kg=2000.0, pallets=2.0,
        delivery_window=(datetime(2026, 1, 15, 7, 0), datetime(2026, 1, 15, 18, 0)),
        collection_window=None, stop_type='delivery',
        depot_id='OVERFLOW', order_class=OrderClass.TOUR,
    )
    pc = {'KA1 2NN': (55.6147, -4.5046)}
    # Tour departs the day BEFORE the delivery window: day 1 (Jan 14) positions
    # north, day 2 (Jan 15) delivers within the window, day 3 (Jan 16) returns.
    tour = _make_tour('HX17CUA', ['o-ka1'], date(2026, 1, 14), date(2026, 1, 16))

    plan = route_tour(tour, [order], pc, solver_budget_s=5.0)

    assert plan.total_planned_km > 0.0
    assert 'o-ka1' not in plan.unrouted_order_ids
    # Day 1 is a positioning leg: an overnight REMOTE point north of CB22 (52.09).
    day1 = plan.daily_routes[0]
    assert day1.overnight_lat is not None and day1.overnight_lat > 52.5
```

> NOTE: confirm the `ScopedOrder` and `Tour` constructor argument names against
> `cambridge/scope.py` and `cambridge/plan_types.py` before running; adjust the
> kwargs in this test to match (do not change the production dataclasses).

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/cambridge/test_repositioning.py::test_route_tour_prepositions_to_reach_far_delivery -v`
Expected: FAIL — `total_planned_km == 0.0` and `o-ka1` in `unrouted_order_ids` (current parked-at-depot behavior).

- [ ] **Step 3: Replace the overnight-position block**

In `cambridge/week_planner/tour_router.py`, add to the imports near the top (after the existing `from cambridge.scope import ScopedOrder`):

```python
from cambridge.repositioning import (
    advance_toward, reachable_haversine_km, remaining_drive_hours, _haversine_km,
)
from cambridge.config import ROAD_DISTANCE_FACTOR
```

Then replace this existing block (currently lines ~135-142):

```python
        overnight_lat = overnight_lon = None
        if not is_last_day and route_dict.get('stops'):
            last = route_dict['stops'][-1]
            overnight_lat = last['lat']
            overnight_lon = last['lon']
            current_lat, current_lon = overnight_lat, overnight_lon
        else:
            current_lat, current_lon = depot_anchor
```

with:

```python
        overnight_lat = overnight_lon = None
        if is_last_day:
            current_lat, current_lon = depot_anchor
        else:
            # Where the tractor physically is at the end of today's work.
            if route_dict.get('stops'):
                end_lat = route_dict['stops'][-1]['lat']
                end_lon = route_dict['stops'][-1]['lon']
                rem_h = remaining_drive_hours(route_dict, shift_end)
            else:
                # Empty day: no work consumed, the whole shift is available.
                end_lat, end_lon = current_lat, current_lon
                rem_h = (shift_end - shift_start).total_seconds() / 3600.0

            # Next objective: nearest pending delivery, else head home (depot).
            if remaining:
                target = min(
                    ((o['dest_lat'], o['dest_lon']) for o in remaining),
                    key=lambda t: _haversine_km(end_lat, end_lon, t[0], t[1]),
                )
            else:
                target = depot_anchor

            cap_km = reachable_haversine_km(rem_h)
            new_lat, new_lon, leg_hav = advance_toward(
                (end_lat, end_lon), target, cap_km)
            total_km += leg_hav * ROAD_DISTANCE_FACTOR
            overnight_lat, overnight_lon = new_lat, new_lon
            current_lat, current_lon = new_lat, new_lon
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/cambridge/test_repositioning.py::test_route_tour_prepositions_to_reach_far_delivery -v`
Expected: PASS

- [ ] **Step 5: Run the full helper test file (no regression)**

Run: `python -m pytest tests/cambridge/test_repositioning.py -v`
Expected: PASS (all)

- [ ] **Step 6: Checkpoint** (no commit)

---

### Task 4: Phase 2 integration (`day_coordinator` early return)

**Files:**
- Modify: `cambridge/repositioning.py` (add the composed `early_return_overnight`)
- Modify: `cambridge/day_coordinator.py:753-769` (the REMOTE-overnight computation in `plan_day`)
- Test: `tests/cambridge/test_repositioning.py` (append test for `early_return_overnight`)

- [ ] **Step 1: Write the failing test**

Append to `tests/cambridge/test_repositioning.py`:

```python
def test_early_return_overnight_moves_toward_depot_with_spare_hours():
    """A remote vehicle that finished with spare hours overnights closer to the
    depot than its last stop."""
    from cambridge.repositioning import early_return_overnight, _haversine_km

    depot = (52.0859, 0.1717)                 # CB22
    last_stop = (53.5, -2.5)                  # NW England, far from CB22
    route = {'return_time_iso': '2026-01-14T13:00:00'}   # done 13:00
    shift_end = datetime(2026, 1, 14, 20, 0)             # 7h spare

    new_lat, new_lon = early_return_overnight(route, last_stop, depot, shift_end)

    assert _haversine_km(new_lat, new_lon, *depot) < _haversine_km(*last_stop, *depot)


def test_early_return_overnight_no_spare_hours_stays_put():
    """No spare hours -> the vehicle stays at its last stop."""
    from cambridge.repositioning import early_return_overnight

    depot = (52.0859, 0.1717)
    last_stop = (53.5, -2.5)
    route = {'return_time_iso': '2026-01-14T20:00:00'}   # finished at shift end
    shift_end = datetime(2026, 1, 14, 20, 0)

    new_lat, new_lon = early_return_overnight(route, last_stop, depot, shift_end)
    assert (round(new_lat, 6), round(new_lon, 6)) == (round(last_stop[0], 6),
                                                      round(last_stop[1], 6))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/cambridge/test_repositioning.py -k early_return -v`
Expected: FAIL — `ImportError: cannot import name 'early_return_overnight'`.

- [ ] **Step 3a: Add `early_return_overnight` to the helper**

Append to `cambridge/repositioning.py`:

```python
def early_return_overnight(route_dict: dict,
                           last: Tuple[float, float],
                           depot: Tuple[float, float],
                           shift_end: datetime) -> Tuple[float, float]:
    """Phase-2 composition: from a remote last stop, spend any spare shift hours
    driving back toward the depot. Returns the advanced overnight (lat, lon);
    the original `last` when there are no spare hours."""
    rem_h = remaining_drive_hours(route_dict, shift_end)
    cap_km = reachable_haversine_km(rem_h)
    new_lat, new_lon, _ = advance_toward(last, depot, cap_km)
    return new_lat, new_lon
```

- [ ] **Step 3b: Run the new tests to verify they pass**

Run: `python -m pytest tests/cambridge/test_repositioning.py -k early_return -v`
Expected: PASS (2 passed)

- [ ] **Step 3c: Wire `early_return_overnight` into `plan_day`**

In `cambridge/day_coordinator.py`, add near the top imports:

```python
from cambridge.repositioning import early_return_overnight
```

Then in the REMOTE-overnight loop (currently lines ~753-769), replace:

```python
    from cambridge.config import OVERNIGHT_STAY_MIN_KM as _OVERNIGHT_KM
    for _rid, _route in dispatch_output.routes.items():
        if _route.get('requires_depot_return', True):
            continue
        _olat = _route.get('overnight_lat')
        _olon = _route.get('overnight_lon')
        if _olat is None or _olon is None:
            continue
        _base_vid = _rid.split('_')[0]
        depot = VEHICLE_DEPOT_MAP.get(_base_vid, 'CB22')
        _dlat, _dlon = DEPOT_ANCHORS[depot]
        _p = _math.pi / 180
        _a = (_math.sin((_olat - _dlat) * _p / 2) ** 2
              + _math.cos(_dlat * _p) * _math.cos(_olat * _p)
              * _math.sin((_olon - _dlon) * _p / 2) ** 2)
        if 2 * 6371.0 * _math.asin(_math.sqrt(_a)) >= _OVERNIGHT_KM:
            new_vehicle_locations[_base_vid] = f'REMOTE:{_olat:.5f}:{_olon:.5f}'
```

with:

```python
    from cambridge.config import OVERNIGHT_STAY_MIN_KM as _OVERNIGHT_KM
    for _rid, _route in dispatch_output.routes.items():
        if _route.get('requires_depot_return', True):
            continue
        _olat = _route.get('overnight_lat')
        _olon = _route.get('overnight_lon')
        if _olat is None or _olon is None:
            continue
        _base_vid = _rid.split('_')[0]
        depot = VEHICLE_DEPOT_MAP.get(_base_vid, 'CB22')
        _dlat, _dlon = DEPOT_ANCHORS[depot]

        # Early return: spend any spare shift hours driving back toward the depot
        # instead of parking at the last drop. Shrinks next-day deadhead.
        _se_iso = _route.get('shift_end_iso')
        if _se_iso:
            try:
                _shift_end = datetime.fromisoformat(str(_se_iso))
                _olat, _olon = early_return_overnight(
                    _route, (_olat, _olon), (_dlat, _dlon), _shift_end)
            except (ValueError, TypeError):
                pass

        _p = _math.pi / 180
        _a = (_math.sin((_olat - _dlat) * _p / 2) ** 2
              + _math.cos(_dlat * _p) * _math.cos(_olat * _p)
              * _math.sin((_olon - _dlon) * _p / 2) ** 2)
        if 2 * 6371.0 * _math.asin(_math.sqrt(_a)) >= _OVERNIGHT_KM:
            new_vehicle_locations[_base_vid] = f'REMOTE:{_olat:.5f}:{_olon:.5f}'
        # else: advanced within OVERNIGHT_KM of base -> made it home, stays at depot
```

> `datetime` is already imported at the top of `day_coordinator.py`; confirm
> before adding a duplicate import.

- [ ] **Step 4: Run the Phase-2 test and the full file**

Run: `python -m pytest tests/cambridge/test_repositioning.py -v`
Expected: PASS (all)

- [ ] **Step 5: Run the existing day-coordinator tests (no regression)**

Run: `python -m pytest tests/cambridge/test_day_coordinator.py -v`
Expected: PASS (no failures introduced)

- [ ] **Step 6: Checkpoint** (no commit)

---

### Task 5: Backtest validation on a delivery-inclusive window

**Files:** none (verification only)

- [ ] **Step 1: Clear stale bytecode**

Run: `find . -name "tour_router*.pyc" -delete; find . -name "day_coordinator*.pyc" -delete; find . -name "repositioning*.pyc" -delete`

- [ ] **Step 2: Run the Jan 12–16 backtest (window includes the tour delivery days)**

Run: `python -m cambridge --multiday --start 2026-01-12 --end 2026-01-16 2>&1 | tail -60`

Expected observations:
- Phase 1b lines for the SCOTLAND / YORKSHIRE / SW tours now show **non-zero km** and **0 (or far fewer) UNROUTED** instead of `0 km  N UNROUTED`.
- Validity summary: `TOUR_UNROUTED` lower than the pre-fix Jan 12–16 baseline; FF crossdock-deliver coverage up.
- Planned km rises (the new positioning/return legs are real trunk km) — note the figure for the km-trend record.

- [ ] **Step 3: Spot-check one tour order is now served**

Run:
```bash
python -c "
import pandas as pd
df = pd.read_csv('fleet_replay_exports/plan_manifest_2026-01-12_to_2026-01-16.csv')
sub = df[df.order_name.isin(['WT255267','WT255893','WT255803'])]
print(sub[['order_name','stop_type','stop_pc','plan_status','assigned_vehicle','unassigned_reason']].to_string(index=False))
"
```
Expected: the DELIVERY legs (KA1/ML6/YO61) now show `plan_status=ASSIGNED` with a tour tractor, rather than `TOUR_UNROUTED`.

- [ ] **Step 4: Open the combined HTML and confirm no straight-line artifact**

Open `fleet_replay_exports/plan_replay_2026-01-12_to_2026-01-16.html`; confirm the tour tractor's pre-positioning/return legs follow plausible road geometry (or at least a single clean leg), not a zig-zag/teleport. If a straight-line artifact appears, the map's positioning-leg rendering needs a synthetic waypoint (noted as a risk in the spec).

- [ ] **Step 5: Checkpoint** (no commit) — record the before/after coverage and planned-km figures for the running km-trend story.

---

## Notes for the implementer

- **Do not commit.** Standing user constraint. End tasks at the Checkpoint.
- Reuse `simulation.routing.DEFAULT_AVG_SPEED_KMH` and `config.ROAD_DISTANCE_FACTOR`; do not hard-code a speed.
- The helper is pure — keep solver calls and I/O out of `repositioning.py`.
- `_haversine_km` is intentionally duplicated in the helper (rather than imported from `scope`) to keep `repositioning.py` dependency-light; it is the standard formula.
- If `ScopedOrder`/`Tour` constructor kwargs in the Task-3 test don't match the dataclasses, fix the **test**, never the production dataclass.
