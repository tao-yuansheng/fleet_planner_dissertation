# Tour Realism Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make multi-day tour evaluation honest (road distance, per-stop service time, two-cap day splitting), replace due-date dwell with a hard LATE constraint (early delivery OK), and emit runsheet-grade per-stop dates/times.

**Architecture:** All evaluator changes live in `freight_planner/tours.py::evaluate_tour` (+ `_leg_km`, `TourStop`); the emit fix lives in `freight_planner/tour_plan.py`; two new knobs join the consolidated tour params in `cambridge/config.py`. Tour formation (`build_tours`, cohesion, due-spread gate ≤ `MAX_TOUR_DAYS_HARD`) is untouched — the due-spread gate is what bounds "how early" a stop can be served (≤ ~4 days).

**Tech Stack:** Python 3, pytest, pandas. Spec: `docs/superpowers/specs/2026-07-01-tour-realism-pack-design.md`.

**Session constraints:** NO `git commit` (standing stakeholder instruction — skip all commit steps). Test command prefix (Git Bash):
`cd /e/BEAT/ZECURE-Phase2-main/BackEnd/logistics && PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1 /e/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe -m pytest ...`

**Key existing facts** (verified against code):
- `service_minutes(pallets, vtype)` = 10 + 6·pallets (tractor) / 10 + 3·pallets (rigid), from `freight_planner.route_costs` (already imported in tours.py).
- `road_km` = OSRM when router installed, else haversine × `ROAD_DISTANCE_FACTOR` (1.3). Already imported in tours.py (used by `is_tour_only`).
- `_DAY_DRIVE_CAP_MIN` = 600. `MULTIDAY_AVG_SPEED_KMH` = 80. `MAX_TOUR_DAYS_HARD` = 4.
- Test constants (tests/freight_planner/test_tours.py:130): `GLASGOW=(55.86,-4.25)`, `CARLISLE=(54.89,-2.93)`; `_tractor()` home CB22 (52.086, 0.172). Offline (no router): CB22→GLA ≈ 497 min at 80 km/h over 1.3×haversine; GLA→CAR ≈ 127 min; CAR→CB22 ≈ 351 min.
- The emit (tour_plan.py ~line 300–355) already computes `day_iso = start + stop.day_index` but writes `planned_arrive=f"{day_iso} 12:00:00"` and passes `job=cand` whose `service_date` is the DUE date, not the serve date.

---

### Task 1: Config knobs

**Files:**
- Modify: `cambridge/config.py` (beside `TOUR_COHESION_KM` etc., ~line 301)

- [ ] **Step 1: Add the two knobs**

After the `TOUR_ORIGIN_AT_DEPOT_RADIUS_KM` line add:

```python
TOUR_DAY_ELAPSED_CAP_MIN: float = 13.0 * 60.0  # tour duty day (drive+service); tractor 07:00-20:00
TOUR_DAY_START_HOUR: int = 7                   # clock anchor for emitted tour stop times
```

- [ ] **Step 2: Verify import**

Run: `python -c "from cambridge.config import TOUR_DAY_ELAPSED_CAP_MIN, TOUR_DAY_START_HOUR; print('ok')"` (with the prefix above)
Expected: `ok`

---

### Task 2: Honest tour distance (`road_km`)

**Files:**
- Modify: `freight_planner/tours.py` (`_leg_km` ~line 151; return leg in `evaluate_tour` ~line 224)
- Test: `tests/freight_planner/test_tours.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/freight_planner/test_tours.py`:

```python
# ------------------------------------------------- realism: road distance -----

def test_tour_legs_use_road_distance_not_straight_line():
    # offline road_km = haversine x ROAD_DISTANCE_FACTOR (1.3); a tour's km must
    # reflect road distance, not the straight line
    from cambridge.config import ROAD_DISTANCE_FACTOR
    from freight_planner.route_costs import haversine_km as _hav

    veh = _tractor()
    ev = evaluate_tour(veh, [_job("GLA", *GLASGOW, pallets=4.0)])
    straight = 2.0 * _hav(52.086, 0.172, *GLASGOW)
    assert ev.feasible
    assert ev.total_km > straight * (ROAD_DISTANCE_FACTOR - 0.05)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/freight_planner/test_tours.py::test_tour_legs_use_road_distance_not_straight_line -v`
Expected: FAIL (total_km equals the straight line, below the 1.25× threshold)

- [ ] **Step 3: Implement**

In `freight_planner/tours.py`, `_leg_km`: replace all three `haversine_km(...)` calls with `road_km(...)` (same arguments). In `evaluate_tour`, the return leg: replace `back_km = haversine_km(prev_lat, prev_lon, vehicle.home_lat, vehicle.home_lon)` with `back_km = road_km(prev_lat, prev_lon, vehicle.home_lat, vehicle.home_lon)`. (`road_km` is already imported.) `build_tours`/`_min_gap_km`/`nearest_depot` keep haversine — they are cohesion/geometry heuristics, not cost.

- [ ] **Step 4: Run the whole tours suite**

Run: `pytest tests/freight_planner/test_tours.py -v`
Expected: ALL PASS (existing assertions are `>` thresholds or relational; km only grew ×1.3)

---

### Task 3: Dwell → LATE (early delivery OK)

**Files:**
- Modify: `freight_planner/tours.py` (`evaluate_tour` dwell block, ~line 195)
- Test: `tests/freight_planner/test_tours.py` (rewrite `test_dwell_serves_a_later_due_stop_on_its_due_day_not_early`, add LATE test)

- [ ] **Step 1: Rewrite the dwell test + add the LATE test**

Replace `test_dwell_serves_a_later_due_stop_on_its_due_day_not_early` entirely with:

```python
def test_no_dwell_serves_later_due_stop_as_early_as_the_route_reaches_it():
    # B is due 2 days after A, but early delivery is OK (due = deadline): the
    # vehicle serves B when the route reaches it and never idles in-region.
    # Offline: CB22->GLA ~497min (day 0); GLA->CAR ~127min pushes day_drive
    # past the 600min cap -> B lands on day 1 (not dwelled to day 2).
    jobs = [_job("A", *GLASGOW, pallets=4.0), _job("B", *CARLISLE, pallets=4.0)]
    due = {"A": "2026-01-05", "B": "2026-01-07"}
    tours = build_tours(jobs, _tractor(), cohesion_km=200.0, due_by_job=due)

    assert len(tours) == 1
    day_by_job = {s.job_id: s.day_index for s in tours[0].evaluation.stops}
    assert day_by_job["A"] == 0
    assert day_by_job["B"] == 1          # was 2 under dwell
    assert tours[0].evaluation.days == 2  # was 3 under dwell


INVERNESS = (57.48, -4.22)


def test_stop_reached_after_its_due_day_is_late_infeasible():
    # ordering forces INV onto day 1 (GLA leg + GLA->INV leg exceed the day
    # driving cap), but INV is due on day 0 -> the tour is infeasible LATE.
    veh = _tractor()
    jobs = [_job("GLA", *GLASGOW, pallets=2.0), _job("INV", *INVERNESS, pallets=2.0)]
    ev = evaluate_tour(veh, jobs, due_offsets={"GLA": 0, "INV": 0})
    assert ev.feasible is False
    assert ev.reason == "LATE"
```

- [ ] **Step 2: Run to verify both fail**

Run: `pytest tests/freight_planner/test_tours.py -k "no_dwell or late_infeasible" -v`
Expected: `test_no_dwell...` FAILS (B day == 2, days == 3 under dwell); `test_stop_reached...` FAILS (feasible, no LATE reason exists yet)

- [ ] **Step 3: Implement**

In `evaluate_tour`, delete:

```python
        # dwell: never serve a stop before its due day
        due_day = int(due_offsets.get(job.job_id, 0)) if due_offsets else 0
        if due_day > day_index:
            day_index = due_day
            day_drive = 0.0
```

and insert in its place:

```python
        # deadline: a stop reached after its due day is infeasible (early is OK —
        # stakeholder: dwell is wasted time and resource; due = deadline)
        if due_offsets and job.job_id in due_offsets and day_index > int(due_offsets[job.job_id]):
            return _infeasible_tour("LATE")
```

- [ ] **Step 4: Run the tours + tour_plan suites**

Run: `pytest tests/freight_planner/test_tours.py tests/freight_planner/test_tour_plan.py -v`
Expected: ALL PASS. (`test_splits_when_due_dates_are_too_far_apart` stays green via the `_date_spread_days <= max_span_days` gate in `build_tours`; `test_batches_across_dates_when_within_tour_span` stays green because CAR lands day 1 ≤ its due offset 1.)

---

### Task 4: Service time, two-cap day split, stop timing

**Files:**
- Modify: `freight_planner/tours.py` (`TourStop` ~line 126, `evaluate_tour` full walk, imports)
- Test: `tests/freight_planner/test_tours.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
# --------------------------------------- realism: service time + duty day -----

def test_service_time_counts_and_elapsed_cap_splits_the_day():
    # 3 drops at the same far point: driving fits one day, but with a tiny
    # elapsed cap the service blocks force a day split.
    veh = _tractor()
    jobs = [_job(f"J{i}", *GLASGOW, pallets=4.0) for i in range(3)]
    tight = evaluate_tour(veh, jobs, elapsed_cap_min=530.0)   # 497 drive + 34 service > 530 at stop 2
    roomy = evaluate_tour(veh, jobs)                           # default 780min cap fits all 3
    assert roomy.feasible and tight.feasible
    assert {s.day_index for s in roomy.stops} == {0}
    assert max(s.day_index for s in tight.stops) >= 1
    assert tight.days > roomy.days


def test_tour_stops_carry_monotone_clock_minutes():
    veh = _tractor()
    jobs = [_job(f"J{i}", *GLASGOW, pallets=4.0) for i in range(3)]
    ev = evaluate_tour(veh, jobs)
    assert all(s.arrive_minute >= 0 and s.depart_minute > s.arrive_minute for s in ev.stops)
    same_day = [s for s in ev.stops if s.day_index == 0]
    assert [s.arrive_minute for s in same_day] == sorted(s.arrive_minute for s in same_day)
    # service is load-based: 4 pallets on a tractor = 10 + 6*4 = 34 min
    assert abs(ev.stops[0].depart_minute - ev.stops[0].arrive_minute - 34.0) < 1e-6
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/freight_planner/test_tours.py -k "elapsed_cap or monotone_clock" -v`
Expected: FAIL — `evaluate_tour` has no `elapsed_cap_min` parameter; `TourStop` has no `arrive_minute`.

- [ ] **Step 3: Implement**

1. Add `TOUR_DAY_ELAPSED_CAP_MIN` to the `cambridge.config` import block in `tours.py`.
2. `TourStop`: append two defaulted fields AFTER the existing ones (positional-constructor safety):

```python
@dataclass(frozen=True)
class TourStop:
    job_id: str
    node: str
    day_index: int
    leg_km: float
    load_pallets_after: float
    load_kg_after: float
    # minutes since the tour-day's start (TOUR_DAY_START_HOUR anchors the clock at emit)
    arrive_minute: float = -1.0
    depart_minute: float = -1.0
```

3. Rewrite the `evaluate_tour` walk (keep signature order, add the keyword param):

```python
def evaluate_tour(vehicle: RouteVehicle, ordered_jobs: list[RouteJob],
                  due_offsets: dict | None = None,
                  elapsed_cap_min: float = TOUR_DAY_ELAPSED_CAP_MIN) -> TourEvaluation:
    """Walk a multi-day tour: depot -> stops -> depot, splitting into days by the
    driving cap AND the elapsed duty cap (drive + load-based service), over road
    distance at the long-haul speed. Catches capacity, the day caps, and lateness.

    ``due_offsets`` (job_id -> days from tour start) is a *deadline*: a stop
    reached after its due day is infeasible (LATE). Early service is fine — the
    vehicle never dwells (dwell is wasted time and resource)."""
    if not ordered_jobs:
        return _infeasible_tour("EMPTY")

    cap_p, cap_kg = float(vehicle.capacity_pallets), float(vehicle.capacity_kg)
    running_p = sum(float(j.pallets) for j in ordered_jobs if j.leg_kind == CUSTOMER_DELIVERY)
    running_kg = sum(float(j.kg) for j in ordered_jobs if j.leg_kind == CUSTOMER_DELIVERY)
    if running_p > cap_p + _EPS or running_kg > cap_kg + _EPS:
        return _infeasible_tour("CAPACITY")
    peak_p, peak_kg = running_p, running_kg   # load leaving the depot (all deliveries aboard)

    day_index = 0
    day_drive = 0.0
    day_elapsed = 0.0
    total_km = total_drive = 0.0
    prev_lat, prev_lon = vehicle.start_lat, vehicle.start_lon
    stops: list[TourStop] = []

    for job in ordered_jobs:
        leg_km = _leg_km(prev_lat, prev_lon, job)
        dm = longhaul_drive_minutes(leg_km)
        sm = service_minutes(job.pallets, vehicle.vehicle_type)
        if (job.leg_kind in _TWO_POINT_KINDS
                and job.origin_lat is not None and job.origin_lon is not None):
            sm *= 2.0  # handling at the collection point AND the destination/hub
        if day_elapsed > 0 and (day_drive + dm > _DAY_DRIVE_CAP_MIN
                                or day_elapsed + dm + sm > elapsed_cap_min):
            day_index += 1
            day_drive = 0.0
            day_elapsed = 0.0
        # deadline: a stop reached after its due day is infeasible (early is OK —
        # stakeholder: dwell is wasted time and resource; due = deadline)
        if due_offsets and job.job_id in due_offsets and day_index > int(due_offsets[job.job_id]):
            return _infeasible_tour("LATE")

        arrive_min = day_elapsed + dm
        depart_min = arrive_min + sm

        if job.leg_kind == CUSTOMER_DELIVERY:
            running_p -= float(job.pallets)
            running_kg -= float(job.kg)
            on_p, on_kg = running_p, running_kg
        elif job.leg_kind in (CUSTOMER_PICKUP, HUB_DROP) or job.leg_kind in _TWO_POINT_KINDS:
            on_p = running_p + float(job.pallets)
            on_kg = running_kg + float(job.kg)
            if job.leg_kind == CUSTOMER_PICKUP:
                running_p, running_kg = on_p, on_kg
        else:
            on_p, on_kg = running_p, running_kg

        peak_p = max(peak_p, on_p)
        peak_kg = max(peak_kg, on_kg)
        if on_p > cap_p + _EPS or on_kg > cap_kg + _EPS:
            return _infeasible_tour("CAPACITY")

        total_km += leg_km
        total_drive += dm
        stops.append(TourStop(job.job_id, job.node, day_index, leg_km, running_p, running_kg,
                              arrive_minute=arrive_min, depart_minute=depart_min))
        day_drive += dm
        day_elapsed = depart_min
        prev_lat, prev_lon = job.lat, job.lon

    back_km = road_km(prev_lat, prev_lon, vehicle.home_lat, vehicle.home_lon)
    back_dm = longhaul_drive_minutes(back_km)
    if day_elapsed > 0 and (day_drive + back_dm > _DAY_DRIVE_CAP_MIN
                            or day_elapsed + back_dm > elapsed_cap_min):
        day_index += 1
    total_km += back_km
    total_drive += back_dm

    days = day_index + 1
    if days > MAX_TOUR_DAYS_HARD:
        return _infeasible_tour("TOUR_TOO_LONG")

    return TourEvaluation(True, "", total_km, total_drive, days, tuple(stops),
                          peak_pallets=peak_p, peak_kg=peak_kg)
```

Notes: the Task 3 LATE check and Task 2 `road_km` return leg are inside this final body (this rewrite supersedes their intermediate versions). `DEPOT_LOAD` stops flow through the `else` branch and cost `service_minutes(0)` = 10 min base — loading staged freight is not free.

- [ ] **Step 4: Run the tours + tour_plan suites**

Run: `pytest tests/freight_planner/test_tours.py tests/freight_planner/test_tour_plan.py -v`
Expected: ALL PASS. Watch specifically: `test_single_far_delivery_spans_multiple_days` (still ≥2 days), `test_tour_rejects_when_too_many_days` (still TOUR_TOO_LONG), the Scotland/peak regression tests. If a formation test flips because honest time grows a tour past a cap, STOP and re-examine (that is a real behavior change to surface, not to paper over).

---

### Task 5: Runsheet-grade emit

**Files:**
- Modify: `freight_planner/tour_plan.py` (imports; both emit branches ~line 300–355)
- Test: `tests/freight_planner/test_tour_plan.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/freight_planner/test_tour_plan.py` (reuse the module's existing `_vehicles/_cand/_compat/_prestaged` fixtures — model on `test_far_order_becomes_a_tour_near_stays_daily`'s setup):

```python
def test_tour_emit_writes_serve_dates_and_real_clock_times():
    # the emitted tour stops must carry the day actually driven (start + day_index)
    # as service_date, and real clock times — not the flat "start date 12:00:00".
    vehicles = _vehicles([("T1", "tractor", "CB22", 26, 24000)])
    cands = pd.DataFrame([
        _cand(job_id="far1:D", leg_id="far1:D", order_id="far1", leg_kind="CUSTOMER_DELIVERY",
              service_pc="G1 1AA", service_date="2026-01-12", source_depot="CB22",
              target_depot="CB22", pallets=4.0, kg=1000.0),
    ])
    compat = pd.DataFrame([_compat("far1:D", "T1", (55.86, -4.25))])
    result = run_multiday_seed_plan(
        cands, vehicles, compat, {"far1": _prestaged("far1")},
        start=date(2026, 1, 12), end=date(2026, 1, 17),
    )
    tour_recs = [r for r in result.selected if str(r.route_id).startswith("TOUR:")]
    assert tour_recs
    for r in tour_recs:
        assert r.planned_arrive[:10] == r.service_date       # serve date = day driven
        assert not r.planned_arrive.endswith(" 12:00:00")    # real clock, not placeholder
        hh = int(r.planned_arrive[11:13])
        assert 7 <= hh <= 20                                  # inside the tour duty day
```

(If the fixture signatures differ from this sketch, mirror the exact call pattern of `test_far_order_becomes_a_tour_near_stays_daily` — the assertions are the contract.)

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/freight_planner/test_tour_plan.py::test_tour_emit_writes_serve_dates_and_real_clock_times -v`
Expected: FAIL on `endswith(" 12:00:00")` (today's placeholder) and/or the service_date prefix (today: due date, arrive date may differ).

- [ ] **Step 3: Implement**

In `freight_planner/tour_plan.py`:

1. Import the knob: add `TOUR_DAY_START_HOUR` to the existing `cambridge.config` import.
2. Add a module-level helper near the other private helpers:

```python
def _tour_clock(day_iso: str, minute: float) -> str:
    """Anchor a day-relative stop minute to the tour day's calendar clock."""
    if minute is None or minute < 0:
        return f"{day_iso} 12:00:00"
    total = TOUR_DAY_START_HOUR * 60 + int(round(float(minute)))
    return f"{day_iso} {total // 60:02d}:{total % 60:02d}:00"
```

3. DEPOT_LOAD branch: replace

```python
                    planned_arrive=f"{day_iso} 12:00:00", planned_depart=f"{day_iso} 12:00:00",
```

with

```python
                    planned_arrive=_tour_clock(day_iso, stop.arrive_minute if stop else -1.0),
                    planned_depart=_tour_clock(day_iso, stop.depart_minute if stop else -1.0),
```

4. Real-job branch: replace

```python
            arrive = f"{day_iso} 12:00:00"
```

with

```python
            arrive = _tour_clock(day_iso, stop.arrive_minute if stop else -1.0)
            depart = _tour_clock(day_iso, stop.depart_minute if stop else -1.0)
```

and change the assign call: `job=cand` → `job={**cand, "service_date": day_iso}` (serve date, not due date — `cand` is a plain dict from `row._asdict()`), and `planned_arrive=arrive, planned_depart=arrive` → `planned_arrive=arrive, planned_depart=depart`.

- [ ] **Step 4: Run the tour_plan suite**

Run: `pytest tests/freight_planner/test_tour_plan.py -v`
Expected: ALL PASS.

---

### Task 6: Docstrings + full suite

**Files:**
- Modify: `freight_planner/tours.py` (module docstring bullet 2, which still says "straight-line speed"), `tests/freight_planner/test_tours.py` (stale comment in `test_single_far_delivery_spans_multiple_days`: "straight-line")

- [ ] **Step 1: Update the tours.py module docstring**

Replace the bullet

```
  * tour legs themselves use the long-haul straight-line speed
    (``MULTIDAY_AVG_SPEED_KMH``), because the local 50 km/h x 1.3 road model badly
    overestimates motorway trunking (Scotland reads ~13h one-way vs the real ~6h)
    and would force spurious overnights;
```

with

```
  * tour legs cost real road distance (``road_km``: OSRM when installed) at the
    long-haul motorway speed (``MULTIDAY_AVG_SPEED_KMH``), plus load-based service
    time per stop; a tour day is capped by driving (``MAX_DRIVING_H_PER_DAY``)
    AND elapsed duty time (``TOUR_DAY_ELAPSED_CAP_MIN``);
  * a due date is a deadline: stops may be served early (never dwell — wasted
    time and resource), but a stop reached after its due day is infeasible;
```

- [ ] **Step 2: Run the full test suite**

Run: `pytest tests/ -q`
Expected: ALL PASS (baseline was 632 tests; now +5 new, 1 rewritten).

---

### Task 7: Full-run validation (both weeks) + logs

- [ ] **Step 1: Run both weeks** (background, sequential; OSRM must be up on localhost:5000)

```bash
cd /e/BEAT/ZECURE-Phase2-main/BackEnd/logistics && \
python -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-17 --time-budget 90 --out-dir freight_planner/out && \
python -m freight_planner.run_alns --start 2026-01-19 --end 2026-01-24 --time-budget 90 --out-dir freight_planner/out
```

- [ ] **Step 2: Compare against baseline**

Baseline (2026-07-01, pre-pack): wk1 99.4% (2462/2476), 101,736 km, 76 tour vehicle-days, 33 tours; wk2 99.3% (2455/2472), 98,872 km, 71 tour vehicle-days. Check `kpi_summary.md` both weeks:
- coverage ≥ baseline − 0.1pp; `NO_FEASIBLE_TOUR` = 0; phantom = 0; 0 temporal/ledger violations (`validation_metrics.json`).
- Expected movements: planned km UP a few % (honest tour road km); tour vehicle-days may rise (service time); any `TOUR_TOO_LONG`/`LATE` rejections are surfaced findings to report, not silent failures.
- Verify per-stop emit: in `route_stops.csv`, tour stops now show varying `service_date` within multi-day tours and non-12:00 `planned_arrive` times between 07:00–20:00.

- [ ] **Step 3: Regenerate viz (trip_app ONLY, per standing instruction)**

```bash
python -m freight_planner.viz_app --plan-dir freight_planner/out/forward_structural/planning_window/2026-01-12_to_2026-01-17/plan
python -m freight_planner.viz_app --plan-dir freight_planner/out/forward_structural/planning_window/2026-01-19_to_2026-01-24/plan
```

- [ ] **Step 4: Log**

Add a QUEST_LOG.md session entry (what changed, measured before/after) and update the memory index. NO git commit.
