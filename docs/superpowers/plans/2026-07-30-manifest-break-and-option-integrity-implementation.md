# Manifest Break and Option Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent duplicate statutory breaks in emitted multi-trip HGV days and enforce one DIRECT/XDOCK mode across fixed tours and daily routes.

**Architecture:** Manifest replay will maintain drive-since-break state across every trip of a vehicle-day, including generated depot returns. The multi-day coordinator will own an `OptionMutex`, claim modes only for accepted tours, and pass those claims into the daily seed so same-group feeder legs remain eligible while rival modes are rejected.

**Tech Stack:** Python 3, pandas, pytest, existing `OptionMutex`, route/tour evaluators, and selected-plan manifest builders.

## Global Constraints

- Preserve the current forward-mode rule: different-date FULL_FLEET orders remain XDOCK-only.
- Preserve fixed-tour-before-daily construction; this change does not create a joint tour/daily optimiser.
- Preserve geometry-derived return kilometres and travel times.
- Final option cleanup remains a defensive backstop, not the normal resolver.
- Do not modify or discard unrelated dirty-worktree changes.

---

### Task 1: Replay statutory breaks across the whole vehicle-day

**Files:**
- Modify: `tests/freight_planner/test_manifest_kpi.py`
- Modify: `freight_planner/manifest.py:417-590`

**Interfaces:**
- Consumes: `build_route_stops(selected_df, candidate_df, compatibility_df, vehicle_df, route_times=...)`.
- Produces: emitted `route_stops` whose generated return row contains only a genuinely newly owed break.

- [ ] **Step 1: Write the failing multi-trip regression**

Add a test named
`test_route_stops_does_not_duplicate_break_already_taken_on_second_trip`.
Construct one rigid vehicle-day with two trips:

```python
selected = pd.DataFrame([
    {
        "route_id": "ROUTE:R1:2026-02-16", "trip_index": 1, "sequence": 1,
        "vehicle_id": "R1", "vehicle_home_depot": "CB22",
        "service_date": "2026-02-16", "job_id": "JOB:A:C",
        "leg_id": "A:C", "order_id": "A", "leg_kind": "CUSTOMER_PICKUP",
        "planned_arrive": "2026-02-16 08:30:00",
        "planned_depart": "2026-02-16 08:45:00",
        "planned_km": 100.0, "planned_drive_minutes": 150.0,
        "load_pallets_after": 1.0, "load_kg_after": 100.0,
        "break_minutes_before": 0.0,
    },
    {
        "route_id": "ROUTE:R1:2026-02-16", "trip_index": 2, "sequence": 1,
        "vehicle_id": "R1", "vehicle_home_depot": "CB22",
        "service_date": "2026-02-16", "job_id": "JOB:B:C",
        "leg_id": "B:C", "order_id": "B", "leg_kind": "CUSTOMER_PICKUP",
        "planned_arrive": "2026-02-16 17:15:00",
        "planned_depart": "2026-02-16 17:30:00",
        "planned_km": 100.0, "planned_drive_minutes": 150.0,
        "load_pallets_after": 1.0, "load_kg_after": 100.0,
        "break_minutes_before": 45.0,
    },
])
```

Give the two stops controlled coordinates and an HGV vehicle. Patch only
`freight_planner.manifest.road_minutes` and `road_km` so each generated return
is exactly 20 minutes and 10 km; these are the external geometry boundary, not
the behavior under test. Supply literal route starts/ends for both trip keys.

Assert:

```python
returns = stops[stops.stop_type == "depot_return"].sort_values("trip_index")
assert returns["break_minutes_before"].tolist() == [0.0, 0.0]
assert pd.Timestamp(returns.iloc[1]["planned_arrive"]) == pd.Timestamp("2026-02-16 17:50:00")
```

Production mutation caught: resetting `dsb` inside each trip or failing to
advance it through the already-recorded second-trip break makes the second
return acquire another 45 minutes.

- [ ] **Step 2: Run the regression and verify RED**

Run:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest `
  tests/freight_planner/test_manifest_kpi.py::test_route_stops_does_not_duplicate_break_already_taken_on_second_trip -q
```

Expected: FAIL because the second generated return has
`break_minutes_before == 45.0` and arrives at 18:35.

- [ ] **Step 3: Implement vehicle-day break-state replay**

In `build_route_stops`:

1. Create `dsb_by_vehicle_day: dict[tuple[str, str], float] = {}` before the
   sorted route/trip group loop.
2. Replace the per-group `dsb = 0.0` with:

```python
dsb_key = (vid, sdate)
dsb = float(dsb_by_vehicle_day.get(dsb_key, 0.0))
```

3. Continue advancing `dsb` through each selected stop using
   `statutory_breaks(dsb, planned_drive_minutes)`. The returned break is replay
   information only because the selected stop already carries the evaluator's
   `break_minutes_before`.
4. Advance `dsb` through the generated return, emit the returned `ret_break`,
   then store the post-return value:

```python
if _is_hgv:
    ret_break, dsb = statutory_breaks(dsb, ret_min)
    dsb_by_vehicle_day[dsb_key] = dsb
```

5. For vans, leave break replay disabled and store no state.

- [ ] **Step 4: Run the focused manifest tests and verify GREEN**

Run:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest `
  tests/freight_planner/test_manifest_kpi.py `
  tests/freight_planner/test_utilization_duty.py -q
```

Expected: all tests pass, including the existing genuine-long-return-break
test and the new no-duplicate-break test.

- [ ] **Step 5: Review the Task 1 diff**

Run:

```powershell
git diff --check -- freight_planner/manifest.py tests/freight_planner/test_manifest_kpi.py
git diff -- freight_planner/manifest.py tests/freight_planner/test_manifest_kpi.py
```

Confirm that no geometry, distance, duty-cap, or route-selection behavior was
changed.

---

### Task 2: Share DIRECT/XDOCK claims between tours and daily seed

**Files:**
- Modify: `tests/freight_planner/test_tour_plan.py`
- Modify: `freight_planner/route_seed.py:367-550`
- Modify: `freight_planner/tour_plan.py:244-730`

**Interfaces:**
- Extends: `run_route_seed_plan(..., option_mutex: OptionMutex | None = None) -> RouteSeedResult`.
- Produces: one coordinator-owned `OptionMutex` whose tour claims seed the daily planner.
- Consumes: tour `job_meta[job_id]` fields `option_set` and `option_group`.

- [ ] **Step 1: Write the failing DIRECT-tour/XDOCK-daily regression**

Add
`test_far_direct_tour_claim_blocks_rival_daily_xdock_group` using two tractors,
one option set `F1`, and three candidates:

```python
direct = _cand(
    leg_id="F1:DIR", order_id="F1", flow="FULL_FLEET",
    leg_kind="DIRECT_CUSTOMER_MOVE", dependency_type="NONE_DIRECT",
    option_set="F1", option_group="DIRECT",
    source_depot="CB22", target_depot="CB22",
    origin_lat=52.10, origin_lon=0.20,
)
pickup = _cand(
    leg_id="F1:XC", order_id="F1", flow="FULL_FLEET",
    leg_kind="CUSTOMER_PICKUP", dependency_type="PRODUCES_DEPOT_FREIGHT",
    option_set="F1", option_group="XDOCK",
    source_depot="CB22", target_depot="CB22",
)
delivery = _cand(
    leg_id="F1:XD", order_id="F1", flow="FULL_FLEET",
    leg_kind="CUSTOMER_DELIVERY", dependency_type="REQUIRES_PRIOR_PICKUP",
    predecessor_leg_id="F1:XC", option_set="F1", option_group="XDOCK",
    source_depot="CB22", target_depot="CB22",
)
```

Use real compatibility rows and patch only `tour_plan.is_tour_only` to return
true for `F1:DIR`; this isolates the already-proven partition boundary without
depending on OSRM. Initialise `F1` at the customer origin.

Assert:

```python
selected = {r.leg_id for r in result.selected if r.leg_id}
assert "F1:DIR" in selected
assert not ({"F1:XC", "F1:XD"} & selected)
```

Production mutation caught: replacing the shared mutex with a fresh local
mutex in `run_route_seed_plan` allows XDOCK to claim `F1` independently and
causes the DIRECT tour commit to be superseded.

- [ ] **Step 2: Write the failing XDOCK-tour feeder regression**

Add `test_xdock_tour_claim_keeps_same_group_daily_pickup_eligible`. Use the
same candidate shape, but patch tour classification so only `F1:XD` is
tour-only. Assert:

```python
selected = {r.leg_id for r in result.selected if r.leg_id}
assert {"F1:XC", "F1:XD"} <= selected
assert "F1:DIR" not in selected
```

This proves the coordinator filters only rival groups rather than removing the
whole option set.

- [ ] **Step 3: Run both regressions and verify RED**

Run:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest `
  tests/freight_planner/test_tour_plan.py::test_far_direct_tour_claim_blocks_rival_daily_xdock_group `
  tests/freight_planner/test_tour_plan.py::test_xdock_tour_claim_keeps_same_group_daily_pickup_eligible -q
```

Expected: at least the DIRECT-tour case fails because the daily and tour
planners currently create independent mode decisions.

- [ ] **Step 4: Allow the daily seed to consume a coordinator-owned mutex**

Import `OptionMutex` as today, extend the signature:

```python
def run_route_seed_plan(
    candidates: pd.DataFrame,
    vehicles: pd.DataFrame,
    compatibility: pd.DataFrame,
    freight_states: pd.DataFrame,
    plan_id: str = "ROUTESEED",
    ledger: FreightLedger | None = None,
    excluded_vehicle_days: set[tuple[str, str]] | None = None,
    avail_overrides: dict[tuple[str, str], str] | None = None,
    option_mutex: OptionMutex | None = None,
) -> RouteSeedResult:
```

Replace the unconditional local constructor with:

```python
mutex = option_mutex if option_mutex is not None else OptionMutex()
```

All existing callers remain source-compatible and retain isolated seed
behavior unless they explicitly supply the shared tracker.

- [ ] **Step 5: Claim modes only for accepted tour assignments**

In `run_multiday_seed_plan`:

1. Import and instantiate `OptionMutex` before assigning resolved tours.
2. Add a local lookup:

```python
def _option_candidate(job) -> dict:
    meta = job_meta.get(str(job.job_id), {})
    return {
        "option_set": str(meta.get("option_set", "") or ""),
        "option_group": str(meta.get("option_group", "") or ""),
    }
```

3. At the start of `_assign_one`, collect the real-job option candidates.
   Reject an assignment containing both groups for one option set, or a group
   rival to an earlier accepted tour claim. Record `OPTION_SUPERSEDED` for the
   conflicting real jobs and do not reserve a vehicle.
4. After feasibility and vehicle selection succeed, call
   `tour_option_mutex.assign(cand)` for each real-job option candidate.
5. Pass `option_mutex=tour_option_mutex` to the final
   `run_route_seed_plan(daily_candidates, ...)` call. Do not pass it to the
   preliminary busyness-only prepass.

- [ ] **Step 6: Run focused tour and option tests and verify GREEN**

Run:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest `
  tests/freight_planner/test_tour_plan.py `
  tests/freight_planner/test_seed_option_choice.py `
  tests/freight_planner/test_drop_superseded_options.py `
  tests/freight_planner/test_route_seed.py -q
```

Expected: all pass. The new DIRECT-tour case selects only DIRECT; the XDOCK
tour case selects its pickup and delivery but not DIRECT.

- [ ] **Step 7: Review the Task 2 diff**

Run:

```powershell
git diff --check -- freight_planner/route_seed.py freight_planner/tour_plan.py `
  tests/freight_planner/test_tour_plan.py
git diff -- freight_planner/route_seed.py freight_planner/tour_plan.py `
  tests/freight_planner/test_tour_plan.py
```

Confirm that different-date option generation, tour clustering, vehicle
ranking, tour attachment and ALNS operators are unchanged.

---

### Task 3: Integration verification and documentation alignment

**Files:**
- Modify only if behavior wording is stale:
  - `freight_planner/PIPELINE.md`
  - `freight_planner/README_DYNAMIC.md`
  - `freight_planner/RULES.md`

**Interfaces:**
- Verifies: emitted break timing, shared mode ownership, final option audit,
  handover behavior and rolling commitment behavior.
- Produces: test evidence suitable for deciding whether to launch a fresh W0
  validation run.

- [ ] **Step 1: Run the affected integration modules**

Run:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest `
  tests/freight_planner/test_manifest_kpi.py `
  tests/freight_planner/test_utilization.py `
  tests/freight_planner/test_utilization_duty.py `
  tests/freight_planner/test_tour_plan.py `
  tests/freight_planner/test_seed_option_choice.py `
  tests/freight_planner/test_drop_superseded_options.py `
  tests/freight_planner/test_dynamic_e2e.py `
  tests/freight_planner/test_run_rolling_units.py -q
```

Expected: zero failures.

- [ ] **Step 2: Update only directly stale documentation**

If the listed documents say that DIRECT/XDOCK exclusion is local to the daily
seed or that final cleanup is the normal cross-component resolver, replace
that wording with:

```markdown
Fixed tour assignments and the daily seed share one option claim. A tour may
claim DIRECT or XDOCK; the daily seed can add legs from that same group but
cannot select its rival. Final cleanup remains an integrity backstop.
```

Do not edit unrelated methodology or campaign material.

- [ ] **Step 3: Run the complete freight-planner suite**

Run:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest `
  tests/freight_planner -q
```

Expected: zero failures.

- [ ] **Step 4: Inspect final scope**

Run:

```powershell
git status --short
git diff --check
git diff --stat
```

Separate the files changed by this implementation from the user's existing
dirty-worktree changes. Do not stage, revert or commit unrelated files.

- [ ] **Step 5: Report evidence and request validation-run authority**

Report the red/green regression results, full-suite count, files changed, and
the expected W0 effects:

- X888RNW's emitted return no longer receives the duplicated 45-minute break;
- the tour/daily seed cannot select both modes for `70645961...`;
- same-group XDOCK pickup-to-tour-delivery chains remain possible.

Do not launch W0 automatically; wait for an explicit validation-run request.
