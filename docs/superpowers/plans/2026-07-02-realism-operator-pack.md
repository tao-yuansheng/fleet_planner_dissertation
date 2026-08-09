# Realism + Operator Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** EU-core driver breaks and a stop-wait cap in both evaluators, a consolidated printable runsheet pack for the whole plan window, and a vehicle-type color view in the trip app.

**Architecture:** Break arithmetic lives once in `route_costs.statutory_breaks` and is applied inside `evaluate_route` (daily, accumulator threaded across trips by `evaluate_day`) and `evaluate_tour` (per tour day). Wait handling changes only `evaluate_route` (first stop → later depot departure; later stops → `EXCESS_WAIT`). `break_minutes_before` flows StopTiming/TourStop → SelectedPlanRecord → route_stops.csv → runsheets/viz. Runsheets is a new standalone module reading only `route_stops.csv`.

**Tech Stack:** Python 3.12, pandas, pytest; Leaflet JS inside the self-contained trip app.

**Spec:** `docs/superpowers/specs/2026-07-02-realism-operator-pack-design.md`

**Standing rules:** NO `git commit` (skip all commit steps — verify and move on). Pipeline outputs → `freight_planner/out`. Viz regen = `viz_app.py` trip app only. Run everything from `BackEnd/logistics` with `PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1` and the venv python `E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe`.

---

### Task 1: Break arithmetic (`statutory_breaks`) + config knobs

**Files:**
- Modify: `cambridge/config.py` (after `STRANDED_REPAIR_ENABLED`)
- Modify: `freight_planner/route_costs.py`
- Test: `tests/freight_planner/test_breaks.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

from freight_planner.route_costs import statutory_breaks


def test_no_break_owed_under_the_limit():
    assert statutory_breaks(0.0, 200.0) == (0.0, 200.0)


def test_one_break_when_a_leg_crosses_270_cumulative():
    # 200 driven + 100 more crosses 270 -> one 45-min break, carry 30
    assert statutory_breaks(200.0, 100.0) == (45.0, 30.0)


def test_long_leg_owes_multiple_breaks():
    # 600 min of driving in one leg -> two breaks (at 270 and 540), carry 60
    assert statutory_breaks(0.0, 600.0) == (90.0, 60.0)


def test_landing_exactly_on_the_limit_owes_the_break_before_the_next_drive():
    assert statutory_breaks(0.0, 270.0) == (0.0, 270.0)
    assert statutory_breaks(270.0, 10.0) == (45.0, 10.0)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/freight_planner/test_breaks.py -q`
Expected: FAIL — `ImportError: cannot import name 'statutory_breaks'`

- [ ] **Step 3: Implement**

`cambridge/config.py` — append to the tour-knob block:

```python
DRIVE_BREAK_AFTER_MIN: float = 270.0  # EU 561/2006: break owed after 4.5h cumulative driving
DRIVE_BREAK_MIN: float = 45.0         # statutory break length (minutes)
MAX_STOP_WAIT_MIN: float = 90.0       # daily routes: max curbside wait at a non-first stop
```

`freight_planner/route_costs.py` — add `DRIVE_BREAK_AFTER_MIN, DRIVE_BREAK_MIN` to the existing `from cambridge.config import ...`, then below `drive_minutes`:

```python
def statutory_breaks(drive_since_break_min: float, drive_min: float) -> tuple[float, float]:
    """Break minutes owed while driving ``drive_min`` more, having driven
    ``drive_since_break_min`` since the last break (EU 561/2006 core rule:
    45 min after 4.5 h cumulative driving). A long leg can owe several — the
    driver stops at services mid-leg. Landing exactly on the limit owes the
    break before the NEXT drive, not this one.
    Returns (break_minutes, new_drive_since_break)."""
    total = float(drive_since_break_min) + float(drive_min)
    n = int(total // DRIVE_BREAK_AFTER_MIN)
    if n and total % DRIVE_BREAK_AFTER_MIN == 0.0:
        n -= 1
    return n * DRIVE_BREAK_MIN, total - n * DRIVE_BREAK_AFTER_MIN
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/freight_planner/test_breaks.py -q` → 4 passed.

### Task 2: Breaks in the daily evaluator (`evaluate_route` / `evaluate_day`)

**Files:**
- Modify: `freight_planner/routing_adapter.py`
- Test: `tests/freight_planner/test_breaks.py`

- [ ] **Step 1: Write the failing tests** (append; a constant-distance fake router makes drive minutes exact)

```python
from cambridge.config import AVG_SPEED_KMH
from freight_planner.route_costs import reset_router, set_router
from freight_planner.routing_adapter import RouteJob, RouteVehicle, evaluate_day, evaluate_route


class _FixedKmRouter:
    def __init__(self, km): self.km = km
    def distance_km(self, a_lat, a_lon, b_lat, b_lon): return self.km


def _km_for_minutes(minutes: float) -> float:
    return AVG_SPEED_KMH * minutes / 60.0


def _veh(vtype="rigid", shift_end="2026-01-05 23:00:00") -> RouteVehicle:
    return RouteVehicle(
        vehicle_id="V1", start_node="CB22", start_lat=52.0, start_lon=0.0,
        start_time="2026-01-05 06:00:00", capacity_pallets=15.0, capacity_kg=8000.0,
        vehicle_type=vtype, home_depot="CB22", home_lat=52.0, home_lon=0.0,
        return_to_depot=False, shift_end=shift_end,
    )


def _stop(jid, lat=52.5, lon=0.5) -> RouteJob:
    return RouteJob(job_id=jid, leg_kind="CUSTOMER_DELIVERY", node=jid,
                    lat=lat, lon=lon, pallets=1.0, kg=50.0)


def test_route_inserts_a_break_when_cumulative_driving_crosses_270():
    set_router(_FixedKmRouter(_km_for_minutes(150.0)))  # every leg = 150 driving minutes
    try:
        ev = evaluate_route(_veh(), [_stop("A"), _stop("B", 53.0, 1.0)])
        assert ev.feasible
        assert ev.stops[0].break_minutes_before == 0.0     # 150 min driven
        assert ev.stops[1].break_minutes_before == 45.0    # 300 min crosses 270
        assert ev.end_drive_since_break == 30.0            # 300 - 270 carried
    finally:
        reset_router()


def test_vans_are_exempt_from_breaks():
    set_router(_FixedKmRouter(_km_for_minutes(150.0)))
    try:
        ev = evaluate_route(_veh(vtype="van"), [_stop("A"), _stop("B", 53.0, 1.0)])
        assert all(s.break_minutes_before == 0.0 for s in ev.stops)
    finally:
        reset_router()


def test_evaluate_day_carries_the_accumulator_across_trips():
    set_router(_FixedKmRouter(_km_for_minutes(150.0)))
    try:
        dv = evaluate_day(_veh(), [[_stop("A")], [_stop("B", 53.0, 1.0)]])
        assert dv.feasible
        # trip 1 drove 150; trip 2's leg crosses 270 -> break on trip 2's first stop
        assert dv.trip_evaluations[1].stops[0].break_minutes_before == 45.0
    finally:
        reset_router()


def test_break_time_counts_toward_shift_end():
    set_router(_FixedKmRouter(_km_for_minutes(150.0)))
    try:
        # 2 legs = 300 drive + 45 break + service; a shift that fits the driving but
        # not the break must fail SHIFT (was feasible wall-to-wall before breaks)
        tight = _veh(shift_end="2026-01-05 11:20:00")  # 320 min after 06:00
        ev = evaluate_route(tight, [_stop("A"), _stop("B", 53.0, 1.0)])
        assert not ev.feasible and ev.failure_reason == "SHIFT"
    finally:
        reset_router()
```

- [ ] **Step 2: Run to verify failure** — `AttributeError: ... no attribute 'break_minutes_before'` / `end_drive_since_break`.

- [ ] **Step 3: Implement** in `freight_planner/routing_adapter.py`:

1. Import: `from freight_planner.route_costs import drive_minutes, road_km, service_minutes, statutory_breaks`.
2. `StopTiming`: add field `break_minutes_before: float = 0.0`.
3. `RouteEvaluation`: add field `end_drive_since_break: float = 0.0`.
4. `evaluate_route` signature gains `drive_since_break: float = 0.0`; before the loop: `hgv = str(vehicle.vehicle_type).lower() != "van"`.
5. In the loop, replace `dm = drive_minutes(leg_km)` / `arrive = clock + timedelta(minutes=dm)` with:

```python
        dm = drive_minutes(leg_km)
        break_min = 0.0
        if hgv:
            break_min, drive_since_break = statutory_breaks(drive_since_break, dm)
        arrive = clock + timedelta(minutes=dm + break_min)
```

6. `stops.append(StopTiming(..., break_minutes_before=break_min))`.
7. Return leg — replace the `return_to_depot` block body:

```python
    if vehicle.return_to_depot and ordered_jobs:
        back_km = road_km(prev_lat, prev_lon, vehicle.home_lat, vehicle.home_lon)
        back_dm = drive_minutes(back_km)
        back_break = 0.0
        if hgv:
            back_break, drive_since_break = statutory_breaks(drive_since_break, back_dm)
        total_km += back_km
        total_drive += back_dm
        clock = clock + timedelta(minutes=back_dm + back_break)
```

8. Final `RouteEvaluation(..., end_drive_since_break=drive_since_break)`.
9. `evaluate_day`: before the trip loop `carry = 0.0`; call `evaluate_route(trip_vehicle, trip, detail=detail, drive_since_break=carry)` and after a feasible trip `carry = ev.end_drive_since_break`. (The 30-min reload is < 45 min, so it does not reset the accumulator.)

- [ ] **Step 4: Run** `python -m pytest tests/freight_planner/test_breaks.py -q` → pass, then the touched suites:
`python -m pytest tests/freight_planner -q` — fix any timing-sensitive fixtures honestly (routes that now legitimately need a break).

### Task 3: Breaks in the tour evaluator (`evaluate_tour`)

**Files:**
- Modify: `freight_planner/tours.py`
- Test: `tests/freight_planner/test_breaks.py`

- [ ] **Step 1: Write the failing tests** (tours use `MULTIDAY_AVG_SPEED_KMH` = 80; a 400 km leg = 300 drive minutes)

```python
from cambridge.config import MULTIDAY_AVG_SPEED_KMH
from freight_planner.tours import evaluate_tour


def _tkm(minutes: float) -> float:
    return MULTIDAY_AVG_SPEED_KMH * minutes / 60.0


def test_tour_leg_crossing_270_gets_a_break_and_day_reset_clears_the_accumulator():
    set_router(_FixedKmRouter(_tkm(300.0)))  # every tour leg = 300 driving minutes
    try:
        ev = evaluate_tour(_veh(), [_stop("A"), _stop("B", 55.0, -2.0)])
        assert ev.feasible
        assert ev.stops[0].break_minutes_before == 45.0    # 300 crosses 270 within the leg
        # leg B forces a new day (600 > 600 drive cap boundary / duty cap), so the
        # accumulator resets with the daily rest and B owes its own within-leg break
        assert ev.stops[1].day_index == 1
        assert ev.stops[1].break_minutes_before == 45.0
    finally:
        reset_router()


def test_tour_break_counts_toward_the_duty_day_cap():
    # 240-min legs: no single leg owes a break, but two legs on one day cross 270
    set_router(_FixedKmRouter(_tkm(240.0)))
    try:
        ev = evaluate_tour(_veh(), [_stop("A"), _stop("B", 55.0, -2.0)])
        assert ev.feasible
        assert ev.stops[1].break_minutes_before == 45.0
        # arrive_minute includes the 45-min break in the day's elapsed clock
        assert ev.stops[1].arrive_minute >= ev.stops[0].depart_minute + 240.0 + 45.0
    finally:
        reset_router()
```

- [ ] **Step 2: Run to verify failure** — `TourStop` has no `break_minutes_before` / values are 0.

- [ ] **Step 3: Implement** in `freight_planner/tours.py`:

1. Import `statutory_breaks` in the existing `from freight_planner.route_costs import ...`.
2. `TourStop`: add field `break_minutes_before: float = 0.0`.
3. `evaluate_tour`: after `day_index = 0` add `drive_since_break = 0.0` and `hgv = str(vehicle.vehicle_type).lower() != "van"`. Replace the day-split block:

```python
        bmin, new_since = (statutory_breaks(drive_since_break, dm) if hgv else (0.0, 0.0))
        if day_elapsed > 0 and (day_drive + dm > _DAY_DRIVE_CAP_MIN
                                or day_elapsed + dm + bmin + sm > elapsed_cap_min):
            day_index += 1
            day_drive = 0.0
            day_elapsed = 0.0
            drive_since_break = 0.0  # the overnight daily rest clears the accumulator
            bmin, new_since = (statutory_breaks(0.0, dm) if hgv else (0.0, 0.0))
```

4. `arrive_min = day_elapsed + dm + bmin`; after computing it: `if hgv: drive_since_break = new_since`.
5. `stops.append(TourStop(..., break_minutes_before=bmin))` (keep existing args).
6. Return leg: compute `bmin_back, _ = (statutory_breaks(drive_since_break, back_dm) if hgv else (0.0, 0.0))` and use `day_elapsed + back_dm + bmin_back > elapsed_cap_min` in the final day-split check.

- [ ] **Step 4: Run** `python -m pytest tests/freight_planner/test_breaks.py tests/freight_planner/test_tours.py tests/freight_planner/test_tour_plan.py -q` — fix honestly-affected fixtures (a tour that now needs an extra day is correct behavior; adjust the test's expectation only when the new arithmetic says so).

### Task 4: Stop-wait cap + first-stop late departure + `EXCESS_WAIT`

**Files:**
- Modify: `freight_planner/routing_adapter.py`
- Modify: `freight_planner/alns.py` (`_REPAIRABLE_REASONS`)
- Test: `tests/freight_planner/test_breaks.py` (same fixtures)

- [ ] **Step 1: Write the failing tests**

```python
def test_first_stop_wait_becomes_a_later_depot_departure():
    set_router(_FixedKmRouter(_km_for_minutes(60.0)))
    try:
        j = RouteJob(job_id="A", leg_kind="CUSTOMER_DELIVERY", node="A", lat=52.5, lon=0.5,
                     pallets=1.0, kg=50.0, earliest_start="2026-01-05 10:00:00")
        ev = evaluate_route(_veh(), [j])
        assert ev.feasible
        assert ev.stops[0].wait_minutes == 0.0            # no curbside idle
        assert ev.stops[0].arrive == "2026-01-05 10:00:00"
        assert ev.route_start == "2026-01-05 09:00:00"    # departs just-in-time, not 06:00
    finally:
        reset_router()


def test_wait_over_the_cap_at_a_later_stop_is_excess_wait():
    set_router(_FixedKmRouter(_km_for_minutes(60.0)))
    try:
        a = _stop("A")
        b = RouteJob(job_id="B", leg_kind="CUSTOMER_DELIVERY", node="B", lat=53.0, lon=1.0,
                     pallets=1.0, kg=50.0, earliest_start="2026-01-05 14:00:00")
        ev = evaluate_route(_veh(), [a, b])   # arrive B ~08:10; wait ~350 min > 90
        assert not ev.feasible and ev.failure_reason == "EXCESS_WAIT"
    finally:
        reset_router()


def test_wait_under_the_cap_stays_feasible():
    set_router(_FixedKmRouter(_km_for_minutes(60.0)))
    try:
        a = _stop("A")
        b = RouteJob(job_id="B", leg_kind="CUSTOMER_DELIVERY", node="B", lat=53.0, lon=1.0,
                     pallets=1.0, kg=50.0, earliest_start="2026-01-05 09:00:00")
        ev = evaluate_route(_veh(), [a, b])
        assert ev.feasible and 0.0 < ev.stops[1].wait_minutes <= 90.0
    finally:
        reset_router()


def test_excess_wait_is_a_repairable_reason_for_alns():
    from freight_planner.alns import _REPAIRABLE_REASONS
    assert "EXCESS_WAIT" in _REPAIRABLE_REASONS
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** in `evaluate_route` (`routing_adapter.py`):

1. Import `MAX_STOP_WAIT_MIN` from `cambridge.config` (extend the existing import line).
2. Before the loop: `route_start_shift = 0.0` and `first_stop = True`.
3. Replace the wait block:

```python
        es = _parse(job.earliest_start)
        wait = max(0.0, (es - arrive).total_seconds() / 60.0) if es else 0.0
        if wait > 0.0 and first_stop:
            # leave the depot later instead of idling at the first customer
            route_start_shift = wait
            arrive = arrive + timedelta(minutes=wait)
            wait = 0.0
        elif wait > MAX_STOP_WAIT_MIN:
            return _infeasible("EXCESS_WAIT", start_iso)
        first_stop = False
        service_start = arrive + timedelta(minutes=wait)
```

4. Final `RouteEvaluation(..., route_start=_iso(start_dt + timedelta(minutes=route_start_shift)), ...)`.
5. `freight_planner/alns.py`: `_REPAIRABLE_REASONS = {"SHIFT", "DRIVING_CAP", "TIME_WINDOW", "NO_FEASIBLE_ROUTE", "EXCESS_WAIT"}`.

- [ ] **Step 4: Run** the new tests, then the whole `tests/freight_planner` suite — fixtures that relied on long first-stop waits now see shifted `route_start`; update expectations only where the new semantics are the honest answer.

### Task 5: `break_minutes_before` through the plan-record schema

**Files:**
- Modify: `freight_planner/plan_schema.py`, `freight_planner/plan_records.py`, `freight_planner/tour_plan.py` (both `builder.assign` sites)
- Test: `tests/freight_planner/test_plan_records.py`

- [ ] **Step 1: Failing test** (append to `test_plan_records.py`; reuse its `_vehicle`/`_job`/`_cand` helpers)

```python
def test_records_carry_break_minutes_before():
    from freight_planner.route_costs import reset_router, set_router
    from cambridge.config import AVG_SPEED_KMH

    class _R:
        def distance_km(self, *a): return AVG_SPEED_KMH * 150.0 / 60.0  # 150-min legs

    set_router(_R())
    try:
        a, b = _job("JA", 52.5, 0.5), _job("JB", 53.0, 1.0)
        routes = {("V1", "2026-01-05"): [[a, b]]}
        recs = build_plan_records(routes, {"JA": _cand("JA"), "JB": _cand("JB")},
                                  lambda vid, day: _vehicle(), lambda vid: "CB22",
                                  lambda c, h: "SEED", "P")
        by_id = {r.job_id: r for r in recs}
        assert by_id["JB"].break_minutes_before == 45.0
        assert by_id["JA"].break_minutes_before == 0.0
    finally:
        reset_router()
```

- [ ] **Step 2: Run to verify failure** — `SelectedPlanRecord` has no `break_minutes_before`.

- [ ] **Step 3: Implement**

1. `plan_schema.SelectedPlanRecord`: append field `break_minutes_before: float = 0.0` (last field; `SELECTED_PLAN_COLUMNS` extends automatically).
2. `SelectedPlanBuilder.assign`: add kwarg `break_minutes_before: float = 0.0`, pass into the record.
3. `plan_records.build_plan_records`: in `builder.assign(...)` add `break_minutes_before=float(getattr(stop, "break_minutes_before", 0.0) or 0.0)`.
4. `tour_plan.py`: both `builder.assign` sites (DEPOT_LOAD ~line 564 and customer ~line 597) add `break_minutes_before=(stop.break_minutes_before if stop else 0.0)`.

- [ ] **Step 4: Run** `python -m pytest tests/freight_planner/test_plan_records.py tests/freight_planner/test_manifest_kpi.py -q`.

### Task 6: route_stops gains `vehicle_type` + `break_minutes_before`; tour `depot_return` date fix

**Files:**
- Modify: `freight_planner/manifest.py` (`ROUTE_STOP_COLUMNS`, `build_route_stops`), `freight_planner/reports.py`
- Test: `tests/freight_planner/test_manifest_kpi.py`

- [ ] **Step 1: Failing tests** (append; follow the file's existing fixture style for `selected_df`/`vehicle_df`)

```python
def test_route_stops_carry_vehicle_type_and_breaks_and_tour_return_date():
    selected = pd.DataFrame([{
        "route_id": "TOUR:V9:2026-01-14", "trip_index": 0, "sequence": 1,
        "vehicle_id": "V9", "vehicle_home_depot": "CB22",
        "service_date": "2026-01-14", "leg_id": "L1", "order_id": "O1",
        "leg_kind": "CUSTOMER_DELIVERY", "planned_arrive": "2026-01-14 09:00:00",
        "planned_depart": "2026-01-14 09:20:00", "planned_km": 10.0,
        "load_pallets_after": 1.0, "load_kg_after": 100.0,
        "break_minutes_before": 45.0,
    }])
    vehicles = pd.DataFrame([{"vehicle_id": "V9", "current_lat": 52.0, "current_lon": 0.1,
                              "vehicle_type": "tractor"}])
    stops = build_route_stops(selected, pd.DataFrame(), pd.DataFrame(), vehicles,
                              tour_return_dates={"TOUR:V9:2026-01-14": "2026-01-16"})
    assert set(stops["vehicle_type"]) == {"tractor"}
    stop_row = stops[stops["stop_type"] == "customer_delivery"].iloc[0]
    assert stop_row["break_minutes_before"] == 45.0
    ret = stops[stops["stop_type"] == "depot_return"].iloc[0]
    assert ret["service_date"] == "2026-01-16"   # final tour day, not the start date
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement**

1. `ROUTE_STOP_COLUMNS`: insert `"vehicle_type"` after `"vehicle_home_depot"` and `"break_minutes_before"` after `"planned_depart"`.
2. `build_route_stops` signature gains `tour_return_dates: dict[str, str] | None = None`. Build `vtype = {vid: str(getattr(r, "vehicle_type", "") or "")}` alongside the existing `veh` coords dict. `_row` base gains `vehicle_type=vtype.get(vid, "")` and `break_minutes_before=0.0`; the per-stop `_row(...)` passes `break_minutes_before=float(getattr(s, "break_minutes_before", 0.0) or 0.0)`.
3. `depot_return` row: `service_date=((tour_return_dates or {}).get(str(route_id)) or (str(g["service_date"].astype(str).max()) if is_tour else sdate))`.
4. `reports.write_reports`: import `date, timedelta` from `datetime`; before the `build_route_stops` call:

```python
    tour_return_dates = {}
    for ta in tours or []:
        rid = f"TOUR:{ta.vehicle_id}:{ta.start_date}"
        end_d = date.fromisoformat(str(ta.start_date)) + timedelta(days=max(0, int(ta.days) - 1))
        tour_return_dates[rid] = end_d.isoformat()
```

and pass `tour_return_dates=tour_return_dates` into `build_route_stops(...)`.

- [ ] **Step 4: Run** `python -m pytest tests/freight_planner/test_manifest_kpi.py -q`.

### Task 7: `runsheets.py` — consolidated printable HTML pack

**Files:**
- Create: `freight_planner/runsheets.py`
- Modify: `freight_planner/run_alns.py` (write-outputs stage)
- Test: `tests/freight_planner/test_runsheets.py` (new)

- [ ] **Step 1: Failing tests**

```python
from __future__ import annotations

import pandas as pd

from freight_planner.runsheets import build_runsheets_html


def _stops_df():
    rows = [
        dict(route_id="ROUTE:V1:2026-01-19", vehicle_id="V1", vehicle_home_depot="CB22",
             vehicle_type="rigid", is_tour=False, service_date="2026-01-19", trip_index=1,
             sequence=0, stop_type="depot_start", order_id="", node="CB22", service_pc="",
             planned_arrive="", planned_depart="", leg_km=0.0, break_minutes_before=0.0,
             load_pallets_after=0.0, load_kg_after=0.0),
        dict(route_id="ROUTE:V1:2026-01-19", vehicle_id="V1", vehicle_home_depot="CB22",
             vehicle_type="rigid", is_tour=False, service_date="2026-01-19", trip_index=1,
             sequence=1, stop_type="customer_delivery", order_id="O1", node="CB1", service_pc="CB1 1AA",
             planned_arrive="2026-01-19 09:00:00", planned_depart="2026-01-19 09:15:00",
             leg_km=20.0, break_minutes_before=45.0, load_pallets_after=0.0, load_kg_after=0.0),
        dict(route_id="ROUTE:V1:2026-01-19", vehicle_id="V1", vehicle_home_depot="CB22",
             vehicle_type="rigid", is_tour=False, service_date="2026-01-19", trip_index=1,
             sequence=2, stop_type="depot_return", order_id="", node="CB22", service_pc="",
             planned_arrive="", planned_depart="", leg_km=18.0, break_minutes_before=0.0,
             load_pallets_after=0.0, load_kg_after=0.0),
    ]
    return pd.DataFrame(rows)


def test_runsheets_html_has_vehicle_section_stop_rows_and_break_row():
    html_text = build_runsheets_html(_stops_df(), title="Runsheets 2026-01-19..2026-01-24")
    assert "V1" in html_text and "rigid" in html_text and "CB22" in html_text
    assert "CB1 1AA" in html_text and "09:00" in html_text
    assert "45-min statutory break" in html_text
    assert "page-break-before" in html_text        # printable: one vehicle per sheet
    assert "Runsheets 2026-01-19..2026-01-24" in html_text


def test_runsheets_orders_days_within_a_vehicle():
    df = _stops_df()
    d2 = df.copy(); d2["service_date"] = "2026-01-20"; d2["route_id"] = "ROUTE:V1:2026-01-20"
    html_text = build_runsheets_html(pd.concat([d2, df], ignore_index=True))
    assert html_text.index("2026-01-19") < html_text.index("2026-01-20")
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `freight_planner/runsheets.py`:

```python
"""Consolidated printable runsheets for a whole plan window.

Reads only ``plan/route_stops.csv`` (the runsheet-grade per-stop table shared
with the trip app), so it stays decoupled from planner internals. One
self-contained HTML: per-vehicle sections (page-break per vehicle for browser
printing), one table per service day with depot start/return, ordered stops,
statutory-break rows and trip (reload) boundaries.
"""
from __future__ import annotations

import argparse
import html as _html
from pathlib import Path

import pandas as pd

_CSS = """
body{font-family:system-ui,-apple-system,sans-serif;font-size:12px;color:#111;margin:24px}
h1{font-size:18px;margin:0 0 2px} .sub{color:#666;font-size:11px;margin-bottom:18px}
section.vehicle{page-break-before:always;margin-top:28px}
section.vehicle:first-of-type{page-break-before:auto;margin-top:0}
h2{font-size:15px;border-bottom:2px solid #333;padding-bottom:3px;margin:0 0 2px}
.vmeta{color:#555;font-size:11px;margin-bottom:8px}
h3{font-size:12px;margin:12px 0 4px}
table{border-collapse:collapse;width:100%} th,td{border:1px solid #bbb;padding:3px 6px;text-align:left;font-size:11px}
th{background:#eee} td.num{text-align:right;font-variant-numeric:tabular-nums}
tr.break td{background:#fff6dd;color:#7a5c00;font-style:italic}
tr.depot td{background:#f2f2f2;color:#444}
tr.reload td{background:#eef4ff;color:#33507a;font-style:italic}
@media print{body{margin:8mm} a{color:inherit}}
"""


def _fmt_time(ts: str) -> str:
    s = str(ts or "")
    return s[11:16] if len(s) >= 16 else ""


def _stop_label(stop_type: str) -> str:
    return str(stop_type or "").replace("_", " ")


def build_runsheets_html(route_stops: pd.DataFrame, title: str = "Runsheets") -> str:
    df = route_stops.copy()
    if df.empty:
        return f"<html><body><h1>{_html.escape(title)}</h1><p>No routes.</p></body></html>"
    for col, default in (("vehicle_type", ""), ("break_minutes_before", 0.0), ("is_tour", False)):
        if col not in df.columns:
            df[col] = default

    out: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{_html.escape(title)}</title><style>{_CSS}</style></head><body>",
        f"<h1>{_html.escape(title)}</h1>",
    ]
    n_veh = df["vehicle_id"].nunique()
    total_km = float(df["leg_km"].fillna(0).sum())
    out.append(f"<div class=sub>{n_veh} vehicles · {total_km:,.0f} km planned · "
               f"{int((~df['stop_type'].isin(['depot_start','depot_return'])).sum())} stops</div>")

    df["_home"] = df["vehicle_home_depot"].astype(str)
    for (home, vid), vg in df.groupby(["_home", "vehicle_id"], sort=True):
        vtype = str(vg["vehicle_type"].iloc[0] or "")
        days = sorted(vg["service_date"].astype(str).unique())
        vkm = float(vg["leg_km"].fillna(0).sum())
        out.append("<section class=vehicle>")
        out.append(f"<h2>{_html.escape(str(vid))}</h2>")
        out.append(f"<div class=vmeta>{_html.escape(vtype)} · home {_html.escape(home)} · "
                   f"{len(days)} active day(s) · {vkm:,.0f} km</div>")
        for day in days:
            dg = vg[vg["service_date"].astype(str) == day]
            out.append(f"<h3>{_html.escape(day)}</h3>")
            out.append("<table><tr><th>#</th><th>stop</th><th>order</th><th>postcode</th>"
                       "<th>arrive</th><th>depart</th><th>leg km</th><th>on board (pal/kg)</th></tr>")
            prev_trip = None
            for r in dg.sort_values(["route_id", "trip_index", "sequence"]).itertuples(index=False):
                trip = (str(r.route_id), int(getattr(r, "trip_index", 0) or 0))
                if prev_trip is not None and trip != prev_trip:
                    out.append("<tr class=reload><td colspan=8>reload / turnaround at depot</td></tr>")
                prev_trip = trip
                brk = float(getattr(r, "break_minutes_before", 0.0) or 0.0)
                if brk > 0:
                    out.append(f"<tr class=break><td colspan=8>{brk:.0f}-min statutory break "
                               f"(45-min statutory break rule: 4.5 h driving)</td></tr>")
                stype = str(r.stop_type)
                cls = " class=depot" if stype in ("depot_start", "depot_return") else ""
                out.append(
                    f"<tr{cls}><td>{int(getattr(r, 'sequence', 0) or 0)}</td>"
                    f"<td>{_html.escape(_stop_label(stype))}</td>"
                    f"<td>{_html.escape(str(getattr(r, 'order_id', '') or ''))}</td>"
                    f"<td>{_html.escape(str(getattr(r, 'service_pc', '') or getattr(r, 'node', '') or ''))}</td>"
                    f"<td>{_fmt_time(getattr(r, 'planned_arrive', ''))}</td>"
                    f"<td>{_fmt_time(getattr(r, 'planned_depart', ''))}</td>"
                    f"<td class=num>{float(getattr(r, 'leg_km', 0.0) or 0.0):.1f}</td>"
                    f"<td class=num>{float(getattr(r, 'load_pallets_after', 0.0) or 0.0):.0f} / "
                    f"{float(getattr(r, 'load_kg_after', 0.0) or 0.0):,.0f}</td></tr>")
            out.append("</table>")
        out.append("</section>")
    out.append("</body></html>")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan-dir", required=True, help="a run's plan/ folder (has route_stops.csv)")
    ap.add_argument("--out", default="", help="output HTML (default: <plan>/../reports/runsheets.html)")
    ap.add_argument("--title", default="Runsheets")
    args = ap.parse_args()
    plan = Path(args.plan_dir)
    stops = pd.read_csv(plan / "route_stops.csv")
    out = Path(args.out) if args.out else plan.parent / "reports" / "runsheets.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_runsheets_html(stops, title=args.title), encoding="utf-8")
    print(f"runsheets: {stops['vehicle_id'].nunique()} vehicles -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Then wire into `run_alns.py` write-outputs stage, right after `write_reports(...)` returns:

```python
        from freight_planner.runsheets import build_runsheets_html  # top-level import preferred
        rs_df = pd.read_csv(plan_dir / "route_stops.csv")
        (reports_dir / "runsheets.html").write_text(
            build_runsheets_html(rs_df, title=f"Runsheets {start.isoformat()}..{end.isoformat()}"),
            encoding="utf-8")
        runlog.log(f"runsheets: {rs_df['vehicle_id'].nunique()} vehicles")
```

(place the import with the other `freight_planner.*` imports at the top of the file).

- [ ] **Step 4: Run** `python -m pytest tests/freight_planner/test_runsheets.py -q` → pass.

### Task 8: Viz vehicle-type color view

**Files:**
- Modify: `freight_planner/viz_app.py` (payload ~line 271; JS: chips UI, `colorOf`, `applyColorMode`; trip-list dot ~line 628; card dot ~line 544)

No pytest coverage exists for the app; verification is regenerate-and-grep (Step 3).

- [ ] **Step 1: Payload** — in the trip dict add:

```python
            "vtype": (str(g["vehicle_type"].iloc[0]).lower()
                      if "vehicle_type" in g.columns else ""),
```

- [ ] **Step 2: JS** —

1. Near the top of the script (by `tripLayers`):

```js
const TYPE_COLORS={tractor:'#e74c3c',rigid:'#4a9eff',van:'#2ecc71'};
let colorMode='vehicle';
function colorOf(t){ return colorMode==='type' ? (TYPE_COLORS[t.vtype]||'#8892a8') : t.color; }
function applyColorMode(){
  for(const Lr of Object.values(tripLayers)){
    const c=colorOf(Lr.t);
    Lr.line.setStyle({color:c});
    Lr.markers.forEach(m=>{ m._color=c; });
  }
  document.getElementById('legend-type').style.display = colorMode==='type'?'flex':'none';
  document.getElementById('cm-vehicle').classList.toggle('on',colorMode==='vehicle');
  document.getElementById('cm-type').classList.toggle('on',colorMode==='type');
  applySelection();   // re-icons markers via _normalMarkers/_selectMarkers with the new _color
  renderList();       // refresh the sidebar dots (renderList rebuilds rows)
}
```

2. Sidebar HTML (next to the existing filter chips):

```html
<div class=lbl>Colour by</div>
<div class=chips>
  <span class="chip on" id=cm-vehicle onclick="colorMode='vehicle';applyColorMode()">Vehicle</span>
  <span class=chip id=cm-type onclick="colorMode='type';applyColorMode()">Type</span>
</div>
<div class=chips id=legend-type style="display:none">
  <span class=chip style="border-color:#e74c3c;color:#e74c3c">tractor</span>
  <span class=chip style="border-color:#4a9eff;color:#4a9eff">rigid</span>
  <span class=chip style="border-color:#2ecc71;color:#2ecc71">van</span>
</div>
```

3. Replace the two hardcoded dot colors with `colorOf(t)`: the trip-list row template (`background:${t.color}` → `background:${colorOf(t)}`) and the trip-card row (`background:${t.color}` → `background:${colorOf(t)}`). If the list is built once at startup under a different function name, recolor whatever that builder is — the requirement is: toggling recolors lines, markers, list dots and the card.

- [ ] **Step 3: Verify** — regenerate one app and grep the payload:

```
python -m freight_planner.viz_app --plan-dir freight_planner/out/forward_structural/planning_window/2026-01-19_to_2026-01-24/plan --out <same>/reports/trip_app.html
grep -c '"vtype"' <same>/reports/trip_app.html   # > 0
grep -c 'TYPE_COLORS' <same>/reports/trip_app.html  # > 0
```

Then open-check manually (user does this; report done).

### Task 9: Full suite + validation runs + docs

- [ ] **Step 1:** `python -m pytest tests -q` — everything green except the 3 known pre-existing environmental failures (`test_postcode_resolver`, `test_routing` OSRM-fallback, `test_window_start`).
- [ ] **Step 2:** One run per week, conservation armed, official out dir:

```
FP_ALNS_CONSERVE=1 python -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-17 --time-budget 90 --out-dir freight_planner/out
FP_ALNS_CONSERVE=1 python -m freight_planner.run_alns --start 2026-01-19 --end 2026-01-24 --time-budget 90 --out-dir freight_planner/out
```

- [ ] **Step 3:** Regenerate both trip apps (`viz_app.py`, same paths as today). Confirm `reports/runsheets.html` exists for both runs.
- [ ] **Step 4:** Report coverage/km/vehicle-day deltas vs baselines (wk1 99.7% / 89.6k; wk2 99.8% / 99.1k) — breaks and the wait cap may honestly cost coverage/km; report, do NOT tune (standing rule: no iteration loops).
- [ ] **Step 5:** QUEST_LOG session entry + memory update (tour-realism-pack roadmap items 1–2 done, operator outputs done).

## Self-review notes

- Spec coverage: Part 1 → Tasks 1–3; Part 2 → Task 4; Part 3 → Tasks 5–7 (schema flow + module + hook + depot_return fix); Part 4 → Task 8; validation → Task 9. Van exemption tested (Task 2), duty-cap interaction tested (Task 3), first-stop shift + cap + repairable reason tested (Task 4).
- Type consistency: `statutory_breaks(since, dm) -> (break_min, new_since)` used identically in Tasks 2–3; field name `break_minutes_before` everywhere; `end_drive_since_break` only on `RouteEvaluation`; `tour_return_dates` param name matches Task 6 test and reports call.
- Known judgment calls for the executor: existing tests that legitimately change behavior (first-stop waits, tight shifts) get updated expectations with a comment, not workarounds; `try_insert_job` keeps a zero accumulator (pre-filter only — the authoritative check is whole-day `evaluate_day`, which threads the carry).
