# Forward Mode Eligibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restrict ordinary direct service to same-date full-fleet orders and remove the post-seed stranded-to-direct conversion.

**Architecture:** Apply the date rule where physical legs are generated, so invalid direct alternatives never enter the seed or ALNS. Remove the post-seed repair from the tour orchestrator so rejected cross-dock pickups retain their original repairable rejection reason. Preserve the existing cross-dock, handover, ALNS and multi-day tour machinery.

**Tech Stack:** Python, pandas, pytest, the freight-planner rolling runner and OSRM route cache.

## Global Constraints

- Same collection and delivery date: generate ordinary `DIRECT` and `XDOCK`.
- Different collection and delivery date: generate `XDOCK` only.
- Do not redesign multi-day tour classification, evaluation, assignment, day splitting or emission.
- Never convert failed cross-dock work to direct after the seed.
- Preserve unrelated working-tree changes.
- Write each regression test before its production change and observe the expected failure.

---

### Task 1: Enforce forward mode eligibility in leg generation

**Files:**
- Modify: `tests/freight_planner/test_options_legs.py`
- Modify: `freight_planner/legs.py:436-506`

**Interfaces:**
- Consumes: `DemandRecord.collect_date`, `DemandRecord.deliver_date`.
- Produces: `build_movement_leg_records(...) -> list[MovementLegRecord]` with no `DIRECT_CUSTOMER_MOVE` row when the dates differ.

- [ ] **Step 1: Change the multi-day option test to require XDOCK only**

```python
def test_different_date_full_fleet_emits_xdock_only():
    qargo = pd.DataFrame([_row()])
    legs = build_movement_leg_records(qargo, [_ff_demand()], _cache())

    assert {leg.option_group for leg in legs} == {"XDOCK"}
    assert {leg.leg_kind for leg in legs} == {
        "CUSTOMER_PICKUP", "CUSTOMER_DELIVERY",
    }
    assert all(not leg.leg_id.endswith(":DIR") for leg in legs)
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```powershell
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/freight_planner/test_options_legs.py::test_different_date_full_fleet_emits_xdock_only -q
```

Expected: failure because the current branch emits `DIRECT`, `XDOCK` pickup and `XDOCK` delivery.

- [ ] **Step 3: Remove ordinary DIRECT emission from the different-date branch**

Keep the combined staging-depot calculation and emit only the existing collection and delivery legs. Preserve their `option_set` and `option_group="XDOCK"` metadata so split freight and ledger dependencies continue to work.

- [ ] **Step 4: Run focused same-date and different-date tests**

Run:

```powershell
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/freight_planner/test_options_legs.py -q
```

Expected: all tests pass, including the existing same-day test that requires `DIR`, `XC` and `XD`.

### Task 2: Remove post-seed stranded-to-direct conversion

**Files:**
- Modify: `tests/freight_planner/test_tour_plan.py:425-524`
- Modify: `tests/freight_planner/test_config_defaults.py:66`
- Modify: `freight_planner/tour_plan.py:20-30, 112-122, 630-822, 920-930`
- Modify: `freight_planner/config.py:88`
- Modify: `freight_planner/run_alns.py:779-787`
- Modify: `freight_planner/run_rolling.py:2460`

**Interfaces:**
- Consumes: ordinary `RejectedJob` values from `run_route_seed_plan`.
- Produces: `TourSeedResult` without synthetic repair candidates or repair-order IDs; rejected pickups retain `NO_FEASIBLE_ROUTE`, `SHIFT`, `DRIVING_CAP`, or their actual seed reason.

- [ ] **Step 1: Replace repair-success expectations with a no-conversion regression**

```python
def test_stranded_xdock_pair_is_not_converted_to_direct():
    vehicles = _vehicles([
        {"vehicle_id": "ST1", "home_depot": "STOKE", "vtype": "tractor"},
        {"vehicle_id": "CB1", "home_depot": "CB22", "vtype": "tractor", "cap_p": 10.0},
    ])
    far = [_cand(
        leg_id="FAR:D", order_id="FAR", service_pc="G1", pallets=20.0,
        source_depot="STOKE", target_depot="STOKE",
    )]
    c1, k1, f1 = _stranded_pair("S1", "ST4", STOKE_YARD, "B7", (52.49, -1.87))
    candidates = pd.DataFrame(far + c1)
    compatibility = pd.DataFrame([_compat("FAR:D", "ST1", GLASGOW)] + k1)
    freight = pd.DataFrame([_prestaged("FAR", "STOKE"), f1])

    result = run_multiday_seed_plan(
        candidates, vehicles, compatibility, freight, date(2026, 1, 5)
    )

    assert not any(
        str(job.job_id).startswith("RD:")
        for tour in result.tours
        for job in tour.jobs
    )
    reasons = {rejected.job_id: rejected.reason for rejected in result.rejected}
    assert reasons["JOB:S1:C"] != "REPAIRED_DIRECT"
```

- [ ] **Step 2: Run the regression and verify it fails**

Run:

```powershell
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/freight_planner/test_tour_plan.py::test_stranded_xdock_pair_is_not_converted_to_direct -q
```

Expected: failure because the current orchestrator creates an `RD:S1` movement and relabels the rejection.

- [ ] **Step 3: Delete the stranded repair planning path**

Remove:

- `STRANDED_REPAIR_ENABLED`;
- the post-seed synthetic `RD:` construction and tour attachment/batching block;
- `TourSeedResult.repaired_order_ids`;
- `TourSeedResult.repaired_candidates`;
- output concatenation and log handling for synthetic repair candidates;
- corresponding empty stub fields in the rolling finalizer.

Do not remove general tour insertion helpers that are used by ordinary tour planning or dynamic tour attachment.

- [ ] **Step 4: Run tour and ALNS tests**

Run:

```powershell
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/freight_planner/test_tour_plan.py tests/freight_planner/test_alns.py tests/freight_planner/test_config_defaults.py -q
```

Expected: all tests pass; no test requires synthetic `RD:` planning.

### Task 3: Preserve horizon-boundary collection and reporting semantics

**Files:**
- Modify: `tests/freight_planner/test_date_basis.py`
- Modify: `tests/freight_planner/test_manifest_kpi.py`
- Modify: `freight_planner/plan_full.py:79`

**Interfaces:**
- Consumes: an XDOCK pickup inside the window and delivery after the window.
- Produces: an in-window collection leg available for routing and a later delivery represented as handover/deferred work, without `REPAIRED_DIRECT`.

- [ ] **Step 1: Extend the existing beyond-window fixture**

Assert that the filtered movement set retains the collection leg, excludes no valid in-window pickup, and contains no ordinary direct leg for the different-date order.

- [ ] **Step 2: Run the date-basis and KPI tests**

Run:

```powershell
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/freight_planner/test_date_basis.py tests/freight_planner/test_manifest_kpi.py -q
```

Expected before any needed production adjustment: the new no-direct assertion fails on the old candidate set; after Task 1 it passes. Existing legacy `REPAIRED_DIRECT` report-reader tests may remain for backward compatibility, but current planning tests must not produce the value.

- [ ] **Step 3: Update the plan dictionary**

Remove `REPAIRED_DIRECT` as an example of a current planner-generated reason. Keep the dictionary accurate for `DUE_BEYOND_WINDOW`, `NO_FEASIBLE_ROUTE`, accounting statuses and hard blockers.

### Task 4: Verify the implementation and run 20–21 February

**Files:**
- Create via runner: `freight_planner/result_runs/W0_forward_mode_fix_2day/2026-02/2026-02-20_to_2026-02-21/`

**Interfaces:**
- Consumes: the approved production code and standard W0 settings.
- Produces: a two-day validation run with reports, CSVs, timeline, manifest and audited logs.

- [ ] **Step 1: Run focused regression tests**

```powershell
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/freight_planner/test_options_legs.py tests/freight_planner/test_tour_plan.py tests/freight_planner/test_date_basis.py tests/freight_planner/test_manifest_kpi.py -q
```

- [ ] **Step 2: Run the full freight-planner suite**

```powershell
E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe -m pytest tests/freight_planner -q
```

- [ ] **Step 3: Launch the validation run from the repository folder**

Use the same rolling-run command and W0 settings recorded in the baseline manifest, changing only:

- start: `2026-02-20`;
- end: `2026-02-21`;
- output: `freight_planner/result_runs/W0_forward_mode_fix_2day`;
- convergence: `0.15%`, window `500`, minimum iterations `1500`;
- seed: `0`;
- travel slack: `1.0`.

- [ ] **Step 4: Audit the completed run**

Confirm:

- process exit code is zero;
- no traceback, exception, assertion failure or error marker appears in console/error/ALNS logs;
- feasibility and non-anticipation audits report zero violations;
- order `7fff06d1-8fdd-4451-a372-0853c6ffd06f` has no `DIRECT` or `RD:` plan row;
- its 20 February cross-dock collection is routed or remains honestly unserved with its real feasibility reason;
- no current planning output contains `REPAIRED_DIRECT`.

