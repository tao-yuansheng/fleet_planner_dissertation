# Fixed Vehicle-Type Service Dwell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace pallet-dependent customer service time with fixed 15-minute van/rigid and 30-minute tractor visit dwell throughout the freight planner.

**Architecture:** Keep `route_costs.service_minutes(pallets, vehicle_type)` as the single public service-time primitive so route, tour and compatibility consumers remain aligned. Move the authoritative values into shared configuration, retain same-address visit merging and two-visit direct movements, and update methodology text to disclose the empirical fixed-duration assumption.

**Tech Stack:** Python 3.12, pandas-backed calibration evidence, pytest, Markdown documentation, rolling-horizon validation CLI.

## Global Constraints

- Van customer visits are 15 minutes.
- Rigid customer visits are 15 minutes.
- Tractor customer visits are 30 minutes.
- Pallet count remains accepted by the service-time API but does not change dwell.
- Consecutive co-located orders pay one visit dwell.
- Direct customer-to-customer movements pay one dwell at each endpoint.
- Depot reload, trunk-hub dwell and statutory-break logic are unchanged.
- Preserve all unrelated user changes in the dirty worktree.

---

### Task 1: Fixed service-time primitive

**Files:**
- Modify: `tests/freight_planner/test_route_costs.py`
- Modify: `freight_planner/shared/config.py`
- Modify: `freight_planner/route_costs.py`

**Interfaces:**
- Consumes: `service_minutes(pallets: float, vehicle_type: str = "tractor") -> float`
- Produces: `CUSTOMER_SERVICE_MIN_BY_TYPE: dict[str, float]` and fixed service results.

- [ ] **Step 1: Write failing tests**

```python
def test_service_minutes_are_fixed_by_vehicle_type():
    for pallets in (0.0, 1.0, 10.0, 26.0):
        assert service_minutes(pallets, "van") == 15.0
        assert service_minutes(pallets, "rigid") == 15.0
        assert service_minutes(pallets, "tractor") == 30.0


def test_service_minutes_unknown_type_uses_tractor_fallback():
    assert service_minutes(26.0, "unknown") == 30.0
```

- [ ] **Step 2: Run tests and verify the expected red failure**

Run:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest -q tests/freight_planner/test_route_costs.py
```

Expected: the new fixed-value assertions fail against the existing pallet-dependent implementation.

- [ ] **Step 3: Implement the fixed mapping**

Define:

```python
CUSTOMER_SERVICE_MIN_BY_TYPE = {
    "van": 15.0,
    "rigid": 15.0,
    "tractor": 30.0,
}
```

Make `service_minutes` return the normalized vehicle-type value with tractor as
the fallback. Remove obsolete live imports of the per-pallet functions while
leaving unrelated shared legacy constants untouched if other modules still use
them.

- [ ] **Step 4: Run the route-cost tests and verify green**

Run the Task 1 command and require zero failures.

### Task 2: Preserve visit and direct-movement semantics

**Files:**
- Modify: `tests/freight_planner/test_routing_adapter.py`
- Modify: `tests/freight_planner/test_tours.py`
- Modify only if tests expose a defect: `freight_planner/routing_adapter.py`
- Modify only if tests expose a defect: `freight_planner/tours.py`

**Interfaces:**
- Consumes: fixed `service_minutes`.
- Produces: one charge for contiguous co-located jobs and two charges for two-point direct jobs.

- [ ] **Step 1: Add or strengthen behavioural assertions**

Assert that two co-located tractor jobs with different pallet quantities total
30 minutes, while a two-point tractor direct movement totals 60 minutes. Assert
that an overnight direct split places 30 minutes on the collection day and 30
minutes on the delivery day.

- [ ] **Step 2: Run the focused tests**

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest -q tests/freight_planner/test_routing_adapter.py tests/freight_planner/test_tours.py
```

Expected: any stale tests that encode pallet-dependent dwell fail and identify
the exact assertions requiring migration.

- [ ] **Step 3: Make the minimum behavioural adjustment**

Keep the existing same-address subtraction and two-point multiplication if they
already produce the approved results. Change production code only where the
focused tests demonstrate a mismatch.

- [ ] **Step 4: Re-run the focused tests**

Require zero failures.

### Task 3: Documentation alignment

**Files:**
- Modify: `freight_planner/PIPELINE.md`
- Modify: `freight_planner/experiments/METHODOLOGY_FORMULAS.md`
- Modify: `C:/Users/Yuansheng Tao/OneDrive/UCL_BA/0_Dissertation/BEAT-Dissertation/chapter_drafts/05_methodology_draft.md`

**Interfaces:**
- Consumes: approved calibration and fixed configuration values.
- Produces: consistent technical and dissertation descriptions.

- [ ] **Step 1: Replace the obsolete formula**

Replace `10 + rate × pallets` with a fixed vehicle-type visit duration:

```text
s_v = 15 minutes for vans and rigids; 30 minutes for tractors.
```

State that these are rounded Jan-Feb mean observed visit durations, that
co-located orders share one visit duration, and that direct movements pay at
both endpoints.

- [ ] **Step 2: Check documentation consistency**

```powershell
rg -n "6 min/pal|3 min/pal|SERVICE_BASE_MIN|per-pallet|per pallet|load-based service" freight_planner/PIPELINE.md freight_planner/experiments/METHODOLOGY_FORMULAS.md 'C:\Users\Yuansheng Tao\OneDrive\UCL_BA\0_Dissertation\BEAT-Dissertation\chapter_drafts\05_methodology_draft.md'
```

Expected: no live-methodology statement describes customer dwell as
pallet-dependent.

### Task 4: Regression and end-to-end verification

**Files:**
- Create through runner: `freight_planner/result_runs/W0_fixed_dwell_validation_250/`

**Interfaces:**
- Consumes: completed fixed-dwell implementation.
- Produces: test evidence and audited 19-20 February output.

- [ ] **Step 1: Run the maintained suite**

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -m pytest -q tests
```

Require zero failures.

- [ ] **Step 2: Ensure no competing planner process**

Inspect running `freight_planner.run_rolling` processes. Do not start the
validation while another run may write the shared OSRM cache.

- [ ] **Step 3: Run the quick validation**

From `E:\BEAT\ZECURE-Phase2-main\BackEnd\logistics`:

```powershell
& 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -B -m freight_planner.run_rolling `
  --start 2026-02-19 --end 2026-02-20 `
  --out-dir freight_planner/result_runs/W0_fixed_dwell_validation_250 `
  --iterations 250 --seed 0 --delta-r1-min 90 --micro-every-min 30 `
  --converge-pct 0.15 --converge-window 500 --converge-min-iters 1500
```

- [ ] **Step 4: Audit the output**

Require zero capacity, duty, drive, hard-window, non-anticipation, backdating
and option-conflict violations. Confirm WT269897's selected mode, vehicle,
per-day duty, complete kilometres and service-ledger outcome from the emitted
CSVs.

