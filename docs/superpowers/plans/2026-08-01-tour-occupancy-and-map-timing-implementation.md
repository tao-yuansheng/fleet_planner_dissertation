# Tour Occupancy and Map Timing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent ordinary work from overlapping fixed tours, preserve consumed tour-day state when rebuilding tails, reject invalid emitted tour days, and make map timing match the board.

**Architecture:** Tours remain outside the ordinary ALNS solution. Their occupied vehicle-days are threaded into every solver path, physical tour stops are matched by identity rather than list position, and emitted tour records receive an independent cap check. Timeline data carries separate timed resume and park anchors so geometry no longer invents movement times.

**Tech Stack:** Python 3.12, pandas, pytest, Node.js built-in test runner, existing freight-planner dataclasses and timeline JavaScript.

## Global Constraints

- Preserve the separate multi-day tour architecture.
- Driving is capped at 600 minutes per vehicle-day and duty at 780 minutes per continuous shift.
- The committed physical prefix must remain unchanged.
- Geometry determines map paths only; emitted plan records determine movement time.
- Do not modify or discard unrelated dirty-worktree changes.

---

### Task 1: Carry tour reservations into noon warm ALNS

**Files:**
- Modify: `freight_planner/run_alns.py:734-790`
- Test: `tests/freight_planner/test_warmstart.py`

**Interfaces:**
- Consumes: `args.external_reserved: set[tuple[str, str]]`
- Produces: `improve_existing_solution(..., excluded_vehicle_days=reserved)`

- [ ] **Step 1: Write the failing regression test**

Add a second vehicle to the warm-start fixture, reserve `("V1", DAY)`, leave a feasible unassigned order, call `reoptimize_window`, and assert the order is not placed on V1. Also monkeypatch `freight_planner.run_alns.improve_existing_solution` with a recording wrapper and assert it receives `{("V1", DAY)}` as `excluded_vehicle_days`.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/freight_planner/test_warmstart.py::test_reoptimize_passes_external_tour_reservations_to_alns -q`

Expected: FAIL because `excluded_vehicle_days` is absent or `None`.

- [ ] **Step 3: Implement the minimal threading change**

In `reoptimize_window`, normalize the supplied reservation and pass it through:

```python
reserved = set(getattr(args, "external_reserved", None) or set())
...
imp = improve_existing_solution(
    ...,
    excluded_vehicle_days=reserved,
    ...,
)
```

- [ ] **Step 4: Run warm-start and micro reservation tests**

Run: `python -m pytest tests/freight_planner/test_warmstart.py tests/freight_planner/test_micro_pass.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the isolated solver fix**

```powershell
git add -- freight_planner/run_alns.py tests/freight_planner/test_warmstart.py
git commit -m "fix: reserve tour vehicle-days during warm reoptimization"
```

### Task 2: Resume tour tails from the matching physical stop

**Files:**
- Modify: `freight_planner/tours.py:871-906`
- Test: `tests/freight_planner/test_tours.py`

**Interfaces:**
- Consumes: `TourEvaluation.stops: tuple[TourStop, ...]` containing customer and synthetic events
- Produces: `tour_tail_from(...)` cursor seeded from the `TourStop` whose `job_id` matches the last committed `RouteJob`

- [ ] **Step 1: Write the failing synthetic-stop regression test**

Construct an evaluation whose stop sequence is `customer A`, synthetic overnight, `customer B`, return. Call `tour_tail_from(..., committed_count=2)` and assert the cursor location is B and its `day_drive`, duty and drive-since-break equal B's stop state rather than the overnight stop state.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/freight_planner/test_tours.py::test_tour_tail_from_matches_committed_job_across_synthetic_stops -q`

Expected: FAIL because the current code selects `evaluation.stops[committed_count - 1]`.

- [ ] **Step 3: Match by stable job identity**

Replace positional lookup with a reverse identity lookup and fail explicitly if the committed job has no physical stop:

```python
last_stop = next(
    (stop for stop in reversed(tour_eval.stops)
     if str(stop.job_id) == str(last_job.job_id)),
    None,
)
if last_stop is None:
    raise ValueError(f"committed tour job has no evaluated stop: {last_job.job_id}")
```

Keep the existing load and vehicle-position calculations based on `last_job` and the matched `last_stop`.

- [ ] **Step 4: Verify tail and return-split behavior**

Run: `python -m pytest tests/freight_planner/test_tours.py tests/freight_planner/test_tour_attach.py -q`

Expected: PASS, including existing 600-minute return-split tests.

- [ ] **Step 5: Commit the state-alignment fix**

```powershell
git add -- freight_planner/tours.py tests/freight_planner/test_tours.py
git commit -m "fix: preserve tour state across synthetic stops"
```

### Task 3: Reject mixed occupancy and over-cap emitted tours

**Files:**
- Modify: `freight_planner/run_rolling.py`
- Modify: `freight_planner/feasibility_audit.py`
- Test: `tests/freight_planner/test_run_rolling_units.py`
- Test: `tests/freight_planner/test_feasibility_audit.py`

**Interfaces:**
- Produces: `_assert_tour_record_day_caps(records)` raising `ValueError` when summed `planned_drive_minutes` exceeds 600 for a tour vehicle-day
- Produces: `_assert_no_mixed_tour_daily_keys(daily_solution, tour_records)` raising `ValueError` for an occupied key collision
- Produces: audit field `mixed_tour_daily` derived from `route_stops`

- [ ] **Step 1: Write failing invariant tests**

Add one test with a tour day containing 153.76 + 525 drive minutes and assert `_assert_tour_record_day_caps` raises. Add another with a daily solution key matching a tour record key and assert `_assert_no_mixed_tour_daily_keys` raises.

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/freight_planner/test_run_rolling_units.py -k "tour_record_day_caps or mixed_tour_daily" -q`

Expected: FAIL because the invariant functions do not exist.

- [ ] **Step 3: Implement and call the invariants**

Aggregate tour records by `(vehicle_id, service_date)` and sum `planned_drive_minutes`. Aggregate tour keys independently of route ID. Invoke both checks after each anchor/micro tour mutation and before final emission. Use a 1e-6 numerical tolerance.

- [ ] **Step 4: Add the mixed-occupancy audit regression**

Build route-stop rows containing `ROUTE:V1:DAY` and `TOUR:V1:DAY` for the same key. Assert `build_feasibility_audit(...)["mixed_tour_daily"] == 1`, and include it in the hard total and Markdown report.

- [ ] **Step 5: Run invariant and audit tests**

Run: `python -m pytest tests/freight_planner/test_run_rolling_units.py tests/freight_planner/test_feasibility_audit.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the fail-closed validation**

```powershell
git add -- freight_planner/run_rolling.py freight_planner/feasibility_audit.py tests/freight_planner/test_run_rolling_units.py tests/freight_planner/test_feasibility_audit.py
git commit -m "fix: reject invalid tour vehicle-days"
```

### Task 4: Give the map separate authoritative resume and park events

**Files:**
- Modify: `freight_planner/viz_timeline_build.py:220-335`
- Modify: `freight_planner/viz_timeline_maplogic.cjs:206-275`
- Test: `tests/freight_planner/test_viz_timeline_build.py`
- Test: `tests/freight_planner/maplogic.test.cjs`

**Interfaces:**
- Produces tour-day fields: `resume`, `resumeT`, `park`, `parkT`, `startT`, `endT`
- Consumes those fields in `tourDayNodes(geom, td)` without synthesizing anchor times from geometry

- [ ] **Step 1: Write the failing builder test**

Create a day with a 05:00 overnight resume and an 18:00 overnight park. Assert `_tour_day_routes` retains both coordinates and times rather than overwriting the first with the second.

- [ ] **Step 2: Verify builder RED**

Run: `python -m pytest tests/freight_planner/test_viz_timeline_build.py::test_tour_day_routes_keeps_same_day_resume_and_park_separate -q`

Expected: FAIL because `overnight_by_day[day]` retains only the last record.

- [ ] **Step 3: Preserve ordered overnight events and endpoint times**

Store overnight rows per day as ordered `{coord, arr, dep}` events. For each day use the earliest event as resume and the latest distinct event as park. Retain depot departure and return timestamps. Calculate the movement-start anchor from the first emitted leg's `planned_arrive - drive_minutes - break_minutes_before`; use the emitted overnight/depot-return arrival as the movement-end anchor.

- [ ] **Step 4: Write the failing JavaScript timing tests**

Pass `resumeT`, `parkT`, `startT` and `endT` values deliberately different from geometry-derived estimates. Assert the first and final `tourDayNodes` use the supplied values exactly, including a pure-return day.

- [ ] **Step 5: Verify JavaScript RED**

Run: `node --test tests/freight_planner/maplogic.test.cjs`

Expected: FAIL because `tourDayNodes` currently calls `driveMin` for anchor timing.

- [ ] **Step 6: Use authoritative anchor times**

Change `tourDayNodes` so anchor coordinates still define geometry, while anchor `arr`/`dep` come from `startT` and `endT`. Return no timed movement node when required timing is missing; do not clamp negative values to zero.

- [ ] **Step 7: Run all timeline tests**

Run: `python -m pytest tests/freight_planner/test_viz_timeline_build.py -q`

Run: `node --test tests/freight_planner/maplogic.test.cjs`

Expected: PASS.

- [ ] **Step 8: Commit the map correction**

```powershell
git add -- freight_planner/viz_timeline_build.py freight_planner/viz_timeline_maplogic.cjs tests/freight_planner/test_viz_timeline_build.py tests/freight_planner/maplogic.test.cjs
git commit -m "fix: use authoritative tour timing on timeline map"
```

### Task 5: Integrated verification

**Files:**
- Verify only; update code/tests only if a failure is caused by this change.

**Interfaces:**
- Consumes all preceding task outputs.
- Produces fresh evidence that the affected subsystems and full suite remain green.

- [ ] **Step 1: Run the focused regression set**

Run:

```powershell
python -m pytest tests/freight_planner/test_warmstart.py tests/freight_planner/test_tours.py tests/freight_planner/test_tour_attach.py tests/freight_planner/test_run_rolling_units.py tests/freight_planner/test_feasibility_audit.py tests/freight_planner/test_viz_timeline_build.py -q
node --test tests/freight_planner/maplogic.test.cjs
```

- [ ] **Step 2: Run the complete Python suite**

Run: `python -m pytest -q`

Expected: zero failures.

- [ ] **Step 3: Inspect the final diff and repository history**

Run: `git diff --check` and `git log -5 --oneline`.

Confirm only intended paths were staged in each commit and unrelated user changes remain untouched.

- [ ] **Step 4: Run a targeted validation window**

Use the repository's existing rolling wrapper for the affected dates after all tests are green. Confirm zero mixed tour/daily keys, zero tour drive/duty violations, and map/board agreement. Do not launch the full C0 run unless separately requested.
