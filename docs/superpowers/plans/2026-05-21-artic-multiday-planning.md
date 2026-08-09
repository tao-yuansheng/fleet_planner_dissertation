# Artic Multi-Day Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let articulated trucks (`AssetType == 'Tractor Unit'`) run multi-day routes that stay out overnight and return to their home depot at job end, while rigid trucks keep their current same-day return behaviour — with orders still prioritised by deadline across a multi-day horizon.

**Architecture:** Add a class-agnostic time-scheduling layer (`schedule_route`) to the cost engine `pdp_route.py`. It walks a route in time and inserts an overnight rest whenever the daily driving cap (9 h) is exhausted (the truck parks where it stopped — no travel during rest). Deadline feasibility and reported arrival times both run through it. The rigid-vs-artic split is enforced purely as a feasibility rule ("a rigid route may not require any overnight"), so jobs that can't be done same-day naturally fall to artics. Cost gains a per-overnight allowance so the optimiser prefers same-day completion.

**Tech Stack:** Python 3, pytest, pandas (data loader). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-21-artic-multiday-planning-design.md`

**Project conventions for this plan:**
- **Python interpreter:** `E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe`
- **Run tests from:** `E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics`
- **NO GIT COMMITS.** This directory is not a git repo and the user has chosen a no-commit workflow. Each task ends by running the relevant tests green instead of committing. Mark the task complete in your tracker.

**Refinement vs spec:** The spec proposed adding a `multi_day` parameter to *both* `feasible_deadlines` and `arrival_times`. This plan keeps `schedule_route` class-agnostic and only `feasible_deadlines` takes `multi_day` (it decides whether overnights are *allowed*). `arrival_times` gets a body change only — no signature change — so its four call sites (alns/lns/greedy/mcts output) need no edits and automatically reflect overnight gaps. This is smaller and equivalent.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `pdp_route.py` | Cost & time engine (single source of truth) | Add scheduling constants + `_is_multi_day`, `_next_shift_start`, `schedule_route`, `_full_cost`; rewrite `arrival_times`, `feasible_deadlines`, `route_cost`; fix `try_insert` cost consistency |
| `mcts_dispatcher.py` | MCTS dispatcher | One-line update to `polished_route_cost`'s `feasible_deadlines` call |
| `simulation/data_loader.py` | Build vehicle records | Add `multi_day` flag to each vehicle dict |
| `tests/test_pdp_route.py` | Engine tests | Add scheduling / deadline / cost tests |
| `tests/test_data_loader.py` | Data loader tests | New file: `multi_day` flag test |

`arrival_times` call sites in `simulation/alns.py:298`, `simulation/lns.py:174`, `simulation/greedy.py:56`, `mcts_dispatcher.py:366` are **unchanged** (signature preserved).

---

## Task 1: Scheduling primitives in the engine

**Files:**
- Modify: `pdp_route.py` (add constants after line 19; add helpers + `schedule_route` after `arrival_times`, before `_UNSET` at line 115)
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pdp_route.py`:

```python
from datetime import datetime, timezone
from pdp_route import schedule_route, _is_multi_day, _next_shift_start


def test_is_multi_day_only_for_tractor_unit():
    assert _is_multi_day('Tractor Unit') is True
    assert _is_multi_day('Lorry') is False
    assert _is_multi_day('Service Van') is False


def test_next_shift_start_is_six_am_next_day():
    clock = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    nxt = _next_shift_start(clock)
    assert nxt == datetime(2026, 1, 6, 6, 0, tzinfo=timezone.utc)


def test_schedule_route_no_overnight_for_short_route():
    # start (52.0,0.0) -> stop (53.0,0.0) ~111km -> ~2.2h, well under 9h
    start = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)
    stops = [Stop('O1', 53.0, 0.0, 'delivery')]
    arrivals, overnights = schedule_route(52.0, 0.0, stops, start)
    assert overnights == 0
    assert arrivals[0].date() == start.date()
    delta_h = (arrivals[0] - start).total_seconds() / 3600
    assert 2.0 < delta_h < 2.5


def test_schedule_route_inserts_overnight_when_cap_exceeded():
    # start (52.0,0.0) -> stop (56.5,0.0) ~500km -> ~10h driving > 9h cap
    start = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)
    stops = [Stop('O1', 56.5, 0.0, 'delivery')]
    arrivals, overnights = schedule_route(52.0, 0.0, stops, start)
    assert overnights == 1
    # arrival is the morning AFTER start, plus the ~1h leftover leg
    assert arrivals[0].date() == datetime(2026, 1, 6).date()


def test_schedule_route_multiple_overnights_for_very_long_leg():
    # ~2000km single leg -> ~40h driving -> 4 overnights (9h shifts)
    start = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)
    stops = [Stop('O1', 70.0, 0.0, 'delivery')]
    _, overnights = schedule_route(52.0, 0.0, stops, start)
    assert overnights >= 4
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py -k "schedule or shift or multi_day" -v`
Expected: FAIL with `ImportError: cannot import name 'schedule_route'`

- [ ] **Step 3: Add constants**

In `pdp_route.py`, after line 19 (`AVG_SPEED_KMH = 50.0 ...`), add:

```python
MAX_DRIVING_HOURS_PER_DAY = 9.0   # daily driving cap before an overnight rest
SHIFT_START_HOUR = 6              # 06:00 local — when a rested truck resumes
NIGHT_OUT_COST_GBP = 30.0         # per-overnight allowance; tune to real subsistence
HORIZON_DAYS = 7                  # backstop planning horizon for multi-day routes
ARTIC_ASSET_TYPE = 'Tractor Unit' # only this class may stay out overnight
```

- [ ] **Step 4: Add the helpers and `schedule_route`**

In `pdp_route.py`, insert immediately after `arrival_times` (after line 112) and before `_UNSET = object()`:

```python
def _is_multi_day(asset_type: str) -> bool:
    """Artics (tractor units) may run multi-day routes; all others are same-day."""
    return asset_type == ARTIC_ASSET_TYPE


def _next_shift_start(clock: datetime) -> datetime:
    """The next driving shift begins at SHIFT_START_HOUR on the following day."""
    return (clock + timedelta(days=1)).replace(
        hour=SHIFT_START_HOUR, minute=0, second=0, microsecond=0)


def schedule_route(start_lat: float, start_lon: float, stops: list,
                   start_time: datetime) -> tuple:
    """Walk the route in time, inserting an overnight rest whenever the daily
    driving cap is exhausted, and return (arrivals, overnights).

    arrivals[i] is the clock datetime the vehicle reaches stops[i]. During a
    rest the truck parks where it stopped (no travel), so only the clock
    advances — to SHIFT_START_HOUR the next morning. A single leg longer than a
    full shift inserts as many overnights as needed. This function is
    class-agnostic: callers decide whether overnights are permitted (rigids are
    rejected later in feasible_deadlines if overnights > 0)."""
    drive_left = MAX_DRIVING_HOURS_PER_DAY
    clock = start_time
    prev_lat, prev_lon = start_lat, start_lon
    overnights = 0
    arrivals = []
    for stop in stops:
        leg_h = _haversine_km(prev_lat, prev_lon, stop.lat, stop.lon) / AVG_SPEED_KMH
        while leg_h > drive_left:
            clock = _next_shift_start(clock)
            overnights += 1
            leg_h -= drive_left
            drive_left = MAX_DRIVING_HOURS_PER_DAY
        clock = clock + timedelta(hours=leg_h)
        drive_left -= leg_h
        arrivals.append(clock)
        prev_lat, prev_lon = stop.lat, stop.lon
    return arrivals, overnights
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py -k "schedule or shift or multi_day" -v`
Expected: PASS (5 tests)

---

## Task 2: Route arrival times include overnight gaps

**Files:**
- Modify: `pdp_route.py:101-112` (`arrival_times` body)
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pdp_route.py`:

```python
def test_arrival_times_reflect_overnight_for_long_route():
    # ~500km single delivery -> one overnight -> arrival is the next day
    start = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)
    stops = [Stop('O1', 56.5, 0.0, 'delivery')]
    arrivals = arrival_times(52.0, 0.0, stops, start)
    assert arrivals[0].date() == datetime(2026, 1, 6).date()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py::test_arrival_times_reflect_overnight_for_long_route -v`
Expected: FAIL — current `arrival_times` ignores the cap, so arrival is still 2026-01-05.

- [ ] **Step 3: Rewrite `arrival_times` to delegate to `schedule_route`**

Replace `pdp_route.py:101-112` with:

```python
def arrival_times(start_lat: float, start_lon: float,
                  stops: list, start_time: datetime) -> list:
    """Estimated arrival datetime at each stop, including overnight rests when
    the daily driving cap is exceeded (see schedule_route). Signature unchanged
    so existing callers transparently get multi-day-aware arrival times."""
    arrivals, _ = schedule_route(start_lat, start_lon, stops, start_time)
    return arrivals
```

> Note: `schedule_route` is defined later in the file but `arrival_times` only calls it at runtime, so definition order is fine (matches how `feasible_load` calls `load_profile`).

- [ ] **Step 4: Run the test plus the existing arrival test**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py -k arrival -v`
Expected: PASS — both `test_arrival_times_advance_by_distance_over_speed` (short route, unchanged) and the new overnight test.

---

## Task 3: Deadline feasibility spans days; rigids may not stay out

**Files:**
- Modify: `pdp_route.py:132-151` (`feasible_deadlines`)
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pdp_route.py`:

```python
def _far_stops():
    # pickup at origin, delivery ~500km away (needs an overnight for an artic)
    return [Stop('O1', 52.0, 0.0, 'pickup'), Stop('O1', 56.5, 0.0, 'delivery')]


def test_feasible_deadlines_rigid_cannot_stay_out_overnight():
    start = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)
    # generous deadline 3 days out — still infeasible because a rigid can't overnight
    orders = {'O1': {'time_window_end': '2026-01-08T18:00:00+00:00'}}
    assert feasible_deadlines(52.0, 0.0, _far_stops(), start, orders, multi_day=False) is False


def test_feasible_deadlines_artic_ok_when_deadline_allows_overnight():
    start = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)
    orders = {'O1': {'time_window_end': '2026-01-08T18:00:00+00:00'}}
    assert feasible_deadlines(52.0, 0.0, _far_stops(), start, orders, multi_day=True) is True


def test_feasible_deadlines_artic_rejects_too_tight_deadline():
    start = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)
    # delivery needs an overnight, but deadline is same-day morning -> infeasible
    orders = {'O1': {'time_window_end': '2026-01-05T09:00:00+00:00'}}
    assert feasible_deadlines(52.0, 0.0, _far_stops(), start, orders, multi_day=True) is False


def test_feasible_deadlines_rejects_beyond_horizon():
    start = datetime(2026, 1, 5, 6, 0, tzinfo=timezone.utc)
    # ~5000km -> many overnights -> arrival beyond the 7-day horizon backstop
    stops = [Stop('O1', 52.0, 0.0, 'pickup'), Stop('O1', 95.0, 0.0, 'delivery')]
    orders = {'O1': {'time_window_end': '2026-12-31T00:00:00+00:00'}}  # loose deadline
    assert feasible_deadlines(52.0, 0.0, stops, start, orders, multi_day=True) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py -k feasible_deadlines -v`
Expected: FAIL — `feasible_deadlines` does not accept a `multi_day` keyword yet (`TypeError`).

- [ ] **Step 3: Rewrite `feasible_deadlines`**

Replace `pdp_route.py:132-151` with:

```python
def feasible_deadlines(start_lat: float, start_lon: float, stops: list,
                       start_time: datetime, orders: dict,
                       multi_day: bool = False) -> bool:
    """True if the route is time-feasible for its vehicle class.

    Schedules the route (inserting overnight rests on the daily driving cap) and
    requires: (1) a rigid (multi_day=False) needs zero overnights — it must
    finish within one shift; (2) no stop is reached beyond the planning horizon;
    (3) every delivery's scheduled arrival is at or before its deadline.

    multi_day defaults to False so existing callers keep same-day semantics."""
    arrivals, overnights = schedule_route(start_lat, start_lon, stops, start_time)
    if overnights > 0 and not multi_day:
        return False
    horizon_end = start_time + timedelta(days=HORIZON_DAYS)
    for stop, arrival in zip(stops, arrivals):
        if arrival > horizon_end:
            return False
        if stop.stop_type != 'delivery':
            continue
        deadline = _order_deadline(orders[stop.order_id])
        if deadline is None:
            continue
        if arrival > deadline:
            return False
    return True
```

- [ ] **Step 4: Run the new tests plus the existing deadline tests**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py -k "deadline" -v`
Expected: PASS — new tests plus `test_feasible_deadlines_rejects_late_delivery` and `test_feasible_deadlines_accepts_on_time_delivery` (both short routes, `multi_day` defaults to False, behaviour unchanged).

---

## Task 4: Night-out allowance in route cost

**Files:**
- Modify: `pdp_route.py:59-70` (add `_full_cost`, rewrite `route_cost`)
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pdp_route.py`:

```python
from pdp_route import _stops_cost, NIGHT_OUT_COST_GBP


def test_route_cost_adds_night_out_allowance_for_artic_overnight():
    cost_rates = _load_cost_rates(RATES_PATH)
    # artic with a ~500km delivery -> exactly one overnight
    route = Route('A1', 52.0, 0.0, 26000.0, 26, 'Tractor Unit',
                  datetime(2026, 1, 5, 6, tzinfo=timezone.utc),
                  stops=[Stop('O1', 52.0, 0.0, 'pickup'),
                         Stop('O1', 56.5, 0.0, 'delivery')])
    mileage = _stops_cost(route.start_lat, route.start_lon, route.stops,
                          route.asset_type, cost_rates)
    cost = route_cost(route, cost_rates)
    assert cost == mileage + NIGHT_OUT_COST_GBP


def test_route_cost_no_night_out_for_rigid():
    cost_rates = _load_cost_rates(RATES_PATH)
    # rigid, short route, no overnight -> cost is pure mileage
    route = Route('R1', 51.5, 0.0, 5000.0, 12, 'Lorry',
                  datetime(2026, 1, 5, 9, tzinfo=timezone.utc),
                  stops=[Stop('O1', 51.5, 0.0, 'pickup'), Stop('O1', 51.5, 1.0, 'delivery')])
    mileage = _stops_cost(route.start_lat, route.start_lon, route.stops,
                          route.asset_type, cost_rates)
    assert route_cost(route, cost_rates) == mileage
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py -k "night_out or no_night_out" -v`
Expected: FAIL — `test_route_cost_adds_night_out_allowance_for_artic_overnight` fails because current `route_cost` returns mileage only (no allowance).

- [ ] **Step 3: Add `_full_cost` and rewrite `route_cost`**

Replace `pdp_route.py:68-70` (the current `route_cost`) with:

```python
def _full_cost(start_lat: float, start_lon: float, stops: list, asset_type: str,
               start_time: datetime, cost_rates: dict) -> float:
    """Mileage cost (closed loop) plus, for artics, a per-overnight allowance.
    This is the objective the search and the output both optimise."""
    mileage = _stops_cost(start_lat, start_lon, stops, asset_type, cost_rates)
    if _is_multi_day(asset_type) and stops:
        _, overnights = schedule_route(start_lat, start_lon, stops, start_time)
        return mileage + overnights * NIGHT_OUT_COST_GBP
    return mileage


def route_cost(route: Route, cost_rates: dict) -> float:
    return _full_cost(route.start_lat, route.start_lon, route.stops,
                      route.asset_type, route.start_time, cost_rates)
```

> `_full_cost` references `_is_multi_day` and `schedule_route`, defined later in the file; this is fine since they are only called at runtime.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py -k "night_out or no_night_out or route_cost" -v`
Expected: PASS — new tests plus the existing `test_route_cost_uses_asset_rate` (rigid, unchanged).

---

## Task 5: Consistent insertion cost + class-aware deadline checks

The objective must be identical during insertion and at output. `try_insert` currently mixes `route_cost` (base) with `_stops_cost` (candidate); once `route_cost` includes the night-out allowance, the insertion delta would be wrong. Fix `try_insert` to use `_full_cost` for the candidate, and make both `try_insert` and `polished_route_cost` pass the vehicle class into `feasible_deadlines`.

**Files:**
- Modify: `pdp_route.py:183-188` (`try_insert` deadline call + candidate cost)
- Modify: `mcts_dispatcher.py:13-14` (import) and `mcts_dispatcher.py:102-103` (`polished_route_cost` deadline call)
- Test: `tests/test_pdp_route.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pdp_route.py`:

```python
def test_cheapest_insertion_routes_overnight_job_to_artic_not_rigid():
    cost_rates = _load_cost_rates(RATES_PATH)
    start = datetime(2026, 1, 5, 6, tzinfo=timezone.utc)
    routes = {
        'RIGID': Route('RIGID', 52.0, 0.0, 26000.0, 26, 'Lorry', start, stops=[]),
        'ARTIC': Route('ARTIC', 52.0, 0.0, 26000.0, 26, 'Tractor Unit', start, stops=[]),
    }
    # delivery ~500km away (needs an overnight); deadline 3 days out
    orders = {'O1': {'origin_lat': 52.0, 'origin_lon': 0.0,
                     'dest_lat': 56.5, 'dest_lon': 0.0,
                     'goods_weight_kg': 1000.0, 'goods_pallet_spaces': 2,
                     'time_window_end': '2026-01-08T18:00:00+00:00'}}
    vid, result = cheapest_insertion('O1', orders, routes, cost_rates)
    assert vid == 'ARTIC'
    assert result is not None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py::test_cheapest_insertion_routes_overnight_job_to_artic_not_rigid -v`
Expected: FAIL — `try_insert` calls `feasible_deadlines` without `multi_day`, so the rigid is wrongly considered feasible and may win on cost.

- [ ] **Step 3: Update `try_insert`**

In `pdp_route.py`, replace lines 183-188 (the `feasible_deadlines` call and the `cand_cost` assignment) with:

```python
            if not feasible_deadlines(route.start_lat, route.start_lon, candidate,
                                      route.start_time, orders,
                                      _is_multi_day(route.asset_type)):
                continue

            cand_cost = _full_cost(route.start_lat, route.start_lon, candidate,
                                   route.asset_type, route.start_time, cost_rates)
```

(The `base_cost = route_cost(route, cost_rates)` line at 171 already yields full cost — leave it. `added = cand_cost - base_cost` at 189 now compares like with like.)

- [ ] **Step 4: Update `polished_route_cost` in the MCTS dispatcher**

In `mcts_dispatcher.py`, the `pdp_route` import is at lines 13-14:

```python
from pdp_route import (Route, Stop, route_cost, try_insert, cheapest_insertion,
                       feasible_load, feasible_deadlines)
```

Add `_is_multi_day`:

```python
from pdp_route import (Route, Stop, route_cost, try_insert, cheapest_insertion,
                       feasible_load, feasible_deadlines, _is_multi_day)
```

Then replace lines 102-103 (the `feasible_deadlines` call inside `polished_route_cost`):

```python
            and feasible_deadlines(route.start_lat, route.start_lon, polished,
                                   route.start_time, orders,
                                   _is_multi_day(route.asset_type))):
```

- [ ] **Step 5: Run the new test plus the full engine + dispatcher suites**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_pdp_route.py tests/test_mcts_dispatcher.py -v`
Expected: PASS — including the new split test and all existing insertion tests (`test_try_insert_*`, `test_cheapest_insertion_*`), which use rigids on short routes and are unaffected.

---

## Task 6: Expose the vehicle class from the data loader

**Files:**
- Modify: `simulation/data_loader.py` — `build_vehicles()` result dict (around lines 159-169)
- Test: `tests/test_data_loader.py` (new file)

- [ ] **Step 1: Write the failing test**

Create `tests/test_data_loader.py`:

```python
import pandas as pd
from simulation.data_loader import build_vehicles


def test_build_vehicles_flags_artic_as_multi_day():
    df = pd.DataFrame([
        {'AssetName': 'ART1', 'CircuitName': 'Duxford - Artic',
         'AssetType': 'Tractor Unit', 'typical_tonnes': 30},
        {'AssetName': 'RIG1', 'CircuitName': 'Duxford - Rigid',
         'AssetType': 'Lorry', 'typical_tonnes': 18},
    ])
    vehicles = build_vehicles(df, {})
    by_id = {v['vehicle_id']: v for v in vehicles}
    assert by_id['ART1']['multi_day'] is True
    assert by_id['RIG1']['multi_day'] is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_data_loader.py -v`
Expected: FAIL with `KeyError: 'multi_day'`.

- [ ] **Step 3: Add the `multi_day` flag**

In `simulation/data_loader.py`, inside the `result.append({...})` dict in `build_vehicles()` (currently ending at line 168 with `'depot_lon': depot_lon,`), add one entry:

```python
            'multi_day':        asset_type == 'Tractor Unit',
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/test_data_loader.py -v`
Expected: PASS

---

## Task 7: Full regression

**Files:** none (verification only)

- [ ] **Step 1: Run the entire suite**

Run: `"E:/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe" -m pytest tests/ -q`
Expected: PASS — the original 41 tests plus the new tests added in Tasks 1–6 (≈55 total), zero failures.

- [ ] **Step 2: Smoke-test a real batch (optional but recommended)**

If `run_daily_batch.py` has a runnable entry for a sample day, run it for one day with the artic fleet present and confirm: (a) no exceptions, (b) at least one artic route reports an `arrival_time` on a later calendar day than `start_time` when a long-deadline distant order is present, and (c) `estimated_cost_gbp` for such a route exceeds its pure mileage cost by a multiple of £30. If no convenient entry point exists, skip — the unit tests cover the engine behaviour.

---

## Notes for the implementer

- **Definition order in `pdp_route.py`:** `arrival_times` (Task 2), `_full_cost`/`route_cost` (Task 4), and `try_insert` (Task 5) all reference `schedule_route`/`_is_multi_day`, which Task 1 places *after* `arrival_times`. Python resolves these at call time, so the forward references are fine — mirroring how `feasible_load` already calls `load_profile` defined above it. Do not reorder the file to "fix" this.
- **Why the rigid rule lives in `feasible_deadlines`, not `schedule_route`:** keeping `schedule_route` class-agnostic means `arrival_times` and `_full_cost` can share it without duplicating the class branch, and reported arrival times are always correct regardless of how the route was classified.
- **`NIGHT_OUT_COST_GBP = 30.0` is a tunable placeholder.** It biases the optimiser toward same-day completion but is not yet a measured rate. Leave a clear constant (done) so it can be set to the real subsistence figure later.
- **Out of scope (do not build):** full EU/UK driver-hours rules, per-stop service time, real road distances, inter-depot repositioning, and the Phase 3 rolling day-by-day commit layer. The rolling layer is a separate plan.
