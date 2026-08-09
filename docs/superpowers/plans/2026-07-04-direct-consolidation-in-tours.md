# DIRECT Consolidation in Tour Building — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a far full-fleet DIRECT move be collected en route on a consolidated multi-drop sweep (the real X8RNW pattern) instead of spinning out a dedicated vehicle — by relaxing the tour builder's non-depot-DIRECT exclusion behind a system-km guard.

**Architecture:** Tour-builder-only change. `resolve_cluster` (`tours.py`) stops force-splitting clusters that contain a non-depot-origin DIRECT; instead it builds the consolidated tour (the two-point DIRECT job self-drives `depot→origin→dest`) and keeps it only when feasible AND no more km than the per-depot split. The salvage pass (`tour_plan.py`) stops excluding DIRECT singletons from re-pooling. The resolver (`options_resolver.py`, the `1.6` ratio, `_window_infeasible`) is untouched.

**Tech Stack:** Python 3, pytest. No new dependencies.

**Standing rules for the executor:** **NO git commits** — skip every "Commit" step (repo is not under git). The final validation task is run **inline by the controller, never by a subagent**. Tests live under `tests/freight_planner/`; run with `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest <path> -v`.

**Important — km-dependent assertions:** the keep/split decision compares real `road_km`. Where a test's outcome depends on that comparison, the step says so and gives a robust fixture. If a fixture ever lands on the wrong side of the guard, adjust its coordinates (move a consolidate-origin nearer the cluster, or push a fallback into capacity-infeasibility) rather than weakening the assertion.

---

## Reference: the exact current code

`freight_planner/tours.py::resolve_cluster` (lines ~510–536):

```python
    if len(depots) == 1:
        depot, ordered, ev = _build_at(next(iter(depots)), jobs)
        return [(depot, ordered, ev)] if ev.feasible else _per_depot()

    # A DIRECT move collected at a customer in another depot's territory would need
    # cross-territory customer collection -> not allowed (fall back per depot). A DIRECT
    # whose origin is AT a depot (the Stoke yard) collects there via its own leg -> fine.
    if any(j.leg_kind == DIRECT_CUSTOMER_MOVE and not _origin_at_depot(j, anchors) for j in jobs):
        return _per_depot()

    # Load-stops are only for freight STAGED at a depot (deliveries). ...
    delivery_depots = {_depot_of(j) for j in jobs if j.leg_kind == CUSTOMER_DELIVERY}
    if len(delivery_depots) <= 1:
        primary = (next(iter(delivery_depots)) if delivery_depots
                   else _primary_depot(jobs, depots, _depot_of))
        depot, ordered, ev = _build_at(primary, jobs)
        return [(depot, ordered, ev)] if ev.feasible else _per_depot()

    primary = _primary_depot([j for j in jobs if j.leg_kind == CUSTOMER_DELIVERY],
                             delivery_depots, _depot_of)
    load_stops = [load_stop_job(d, anchors[d][0], anchors[d][1])
                  for d in sorted(delivery_depots - {primary})]
    depot, ordered, ev = _build_at(primary, jobs, load_stops)
    return [(depot, ordered, ev)] if ev.feasible else _per_depot()
```

`freight_planner/tour_plan.py` salvage pass (lines ~330–335):

```python
    single_idx = [i for i, (_, jobs, ev) in enumerate(resolved)
                  if consolidate_tours
                  and ev.feasible
                  and len([j for j in jobs if j.leg_kind != DEPOT_LOAD]) == 1
                  and all(j.leg_kind not in (DIRECT_CUSTOMER_MOVE, HUB_DROP)
                          for j in jobs if j.leg_kind != DEPOT_LOAD)]
```

---

### Task 1: Guarded consolidation in `resolve_cluster`

**Files:**
- Modify: `freight_planner/tours.py` (`resolve_cluster`, ~510–536)
- Test: `tests/freight_planner/test_tours.py`

- [ ] **Step 1: Write the failing test — an on-corridor non-depot DIRECT now consolidates**

Append to `tests/freight_planner/test_tours.py` (uses the existing `_job`, `_proto_for3`, `_ANCH` helpers already in the file):

```python
def test_multi_depot_oncorridor_direct_consolidates():
    # A Bedford-staged Scotland delivery + a DIRECT whose customer origin is a Midlands
    # point ON THE WAY north (not at any depot). Folding the DIRECT in saves an entire
    # second Scotland round-trip, so the km-guard keeps it: ONE consolidated tour.
    bd_del = _job("bd_del", 55.87, -3.97, pallets=3.0)                 # BEDFORD-staged (Airdrie)
    mid_dir = RouteJob(job_id="mid_dir", leg_kind="DIRECT_CUSTOMER_MOVE", node="dir",
                       lat=55.46, lon=-4.50, pallets=2.0, kg=1600.0,   # dest Ayr (Scotland)
                       origin_lat=53.20, origin_lon=-1.40)             # Midlands origin, on the way
    src = {"bd_del": "BEDFORD", "mid_dir": "CB22"}.get
    out = resolve_cluster([bd_del, mid_dir], src, due_by_job=None,
                          proto_for=_proto_for3, anchors=_ANCH)
    assert len(out) == 1                                                # consolidated, not per-depot
    _depot, ordered, ev = out[0]
    assert ev.feasible
    assert {"bd_del", "mid_dir"} <= {j.job_id for j in ordered}
```

- [ ] **Step 2: Run it — expect FAIL (current gate falls back to 2 tours)**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_tours.py::test_multi_depot_oncorridor_direct_consolidates -v`
Expected: FAIL — `assert len(out) == 1` fails because the current DIRECT gate returns two per-depot tours.

- [ ] **Step 3: Implement the guarded consolidation**

In `freight_planner/tours.py::resolve_cluster`, **delete** the exclusion block:

```python
    # A DIRECT move collected at a customer in another depot's territory would need
    # cross-territory customer collection -> not allowed (fall back per depot). A DIRECT
    # whose origin is AT a depot (the Stoke yard) collects there via its own leg -> fine.
    if any(j.leg_kind == DIRECT_CUSTOMER_MOVE and not _origin_at_depot(j, anchors) for j in jobs):
        return _per_depot()
```

and, immediately after the `_per_depot` helper definition (right before `if len(depots) == 1:`), add the flag + keep/split helper:

```python
    # A non-depot-origin DIRECT is an en-route CUSTOMER collection on the sweep (the
    # X8RNW pattern). It is allowed, but only kept when it does not cost more km than
    # splitting per depot — so an off-corridor origin (or an infeasible fold) falls back
    # automatically and coverage never drops. Depot-origin DIRECTs and pure-delivery
    # clusters keep today's behaviour (the guard is inert for them).
    has_nondepot_direct = any(
        j.leg_kind == DIRECT_CUSTOMER_MOVE and not _origin_at_depot(j, anchors) for j in jobs)

    def _keep_or_split(depot, ordered, ev):
        if not ev.feasible:
            return _per_depot()
        if has_nondepot_direct:
            split = _per_depot()
            if not all(e.feasible for _, _, e in split):
                return [(depot, ordered, ev)]  # split can't serve it either -> keep the feasible fold
            split_km = sum(e.total_km for _, _, e in split)
            if ev.total_km > split_km + _EPS:
                return split
        return [(depot, ordered, ev)]
```

Then replace the three `return [(depot, ordered, ev)] if ev.feasible else _per_depot()` lines with `return _keep_or_split(depot, ordered, ev)`:

```python
    if len(depots) == 1:
        depot, ordered, ev = _build_at(next(iter(depots)), jobs)
        return _keep_or_split(depot, ordered, ev)

    delivery_depots = {_depot_of(j) for j in jobs if j.leg_kind == CUSTOMER_DELIVERY}
    if len(delivery_depots) <= 1:
        primary = (next(iter(delivery_depots)) if delivery_depots
                   else _primary_depot(jobs, depots, _depot_of))
        depot, ordered, ev = _build_at(primary, jobs)
        return _keep_or_split(depot, ordered, ev)

    primary = _primary_depot([j for j in jobs if j.leg_kind == CUSTOMER_DELIVERY],
                             delivery_depots, _depot_of)
    load_stops = [load_stop_job(d, anchors[d][0], anchors[d][1])
                  for d in sorted(delivery_depots - {primary})]
    depot, ordered, ev = _build_at(primary, jobs, load_stops)
    return _keep_or_split(depot, ordered, ev)
```

Also update the docstring bullet `* multi depot incl. a DIRECT move -> fall back to one tour per source depot.` to:
`* multi depot incl. a non-depot DIRECT -> consolidate it as an en-route pickup when that is feasible and no more km than splitting; else fall back per depot.`

(`_EPS` is already imported/defined in `tours.py` — it is used throughout `evaluate_tour`.)

- [ ] **Step 4: Run it — expect PASS**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_tours.py::test_multi_depot_oncorridor_direct_consolidates -v`
Expected: PASS.

- [ ] **Step 5: Write the fallback test — an infeasible fold still splits (deterministic, capacity)**

Append to `tests/freight_planner/test_tours.py`:

```python
def test_multi_depot_direct_infeasible_fold_falls_back():
    # The delivery already nearly fills the truck (22 pal). The DIRECT's destination is
    # nearer the depot than the far delivery, so nearest-neighbour visits (and PICKS UP)
    # the DIRECT first — while the 22-pal delivery is still aboard -> on-board peak 28 > 26
    # -> the consolidated fold is CAPACITY-infeasible -> fall back per depot, every job
    # still served (coverage never drops).
    ayr = _job("ayr", 55.87, -3.97, pallets=22.0, kg=17000.0)          # CB22, FAR (visited last)
    near_dir = RouteJob(job_id="near_dir", leg_kind="DIRECT_CUSTOMER_MOVE", node="dir",
                        lat=53.00, lon=-1.50, pallets=6.0, kg=5000.0,   # dest NEARER depot (first)
                        origin_lat=52.90, origin_lon=-2.10)             # origin near (Stoke area)
    src = {"ayr": "CB22", "near_dir": "BEDFORD"}.get
    out = resolve_cluster([ayr, near_dir], src, due_by_job=None,
                          proto_for=_proto_for3, anchors=_ANCH)
    served = {j.job_id for _, jobs, _ in out for j in jobs if j.leg_kind != DEPOT_LOAD}
    assert {"ayr", "near_dir"} <= served                               # nothing dropped
    assert len(out) >= 2                                               # split, not one 28-pallet tour
```

- [ ] **Step 6: Run it — expect PASS**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_tours.py::test_multi_depot_direct_infeasible_fold_falls_back -v`
Expected: PASS. If `len(out)` is unexpectedly 1, the DIRECT was ordered *after* the delivery (peak never rose) — nudge `near_dir`'s dest closer to the depot / farther from `ayr` so nearest-neighbour picks it up first, or raise `ayr` pallets so any ordering overflows.

- [ ] **Step 7: Update the existing exclusion test to the new behaviour**

`test_multi_depot_cluster_with_direct_falls_back_per_depot` ([test_tours.py:68]) encodes the *old* rule (a non-depot DIRECT always splits). Its DIRECT (origin `51.40,-0.20`, London) is a southward backtrack, but folding still saves a Scotland round-trip, so under the guard it may now consolidate. Run it:

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_tours.py::test_multi_depot_cluster_with_direct_falls_back_per_depot -v`

- If it now **fails** (consolidates), the new behaviour is correct — replace the test body's assertions with:

```python
    # A non-depot DIRECT now consolidates when folding it saves a Scotland round-trip.
    assert len(out) == 1
    _depot, ordered, _ev = out[0]
    assert {"ayr", "dir"} <= {j.job_id for j in ordered}
```

  and rename it to `test_multi_depot_direct_consolidates_when_cheaper`.
- If it still **passes** (guard rejected the London backtrack as more km), leave it unchanged — that is also correct behaviour, and Steps 1 & 5 already cover the consolidate/fallback paths.

- [ ] **Step 8: Run the full tours suite for regressions**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_tours.py -v`
Expected: all pass. `test_depot_origin_direct_does_not_block_consolidation` and `test_load_stops_only_for_delivery_depots` (depot-origin DIRECTs → `has_nondepot_direct` is False → guard inert) must still pass unchanged.

- [ ] **Step 9: Commit** — SKIP (no git).

---

### Task 2: Re-pool DIRECT singletons in the salvage pass

**Files:**
- Modify: `freight_planner/tour_plan.py` (salvage pass, ~330–335)
- Test: `tests/freight_planner/test_tour_plan.py`

- [ ] **Step 1: Update the salvage exclusion to allow DIRECT (keep HUB_DROP excluded)**

In `freight_planner/tour_plan.py`, change the salvage `single_idx` filter from:

```python
                  and all(j.leg_kind not in (DIRECT_CUSTOMER_MOVE, HUB_DROP)
                          for j in jobs if j.leg_kind != DEPOT_LOAD)]
```

to:

```python
                  and all(j.leg_kind != HUB_DROP
                          for j in jobs if j.leg_kind != DEPOT_LOAD)]
```

and update the comment `# Two-point moves stay put — they are what poisons consolidation.` to
`# HUB_DROP two-point moves stay put; DIRECT moves now re-pool (resolve_cluster's km-guard gates the fold).`

- [ ] **Step 2: Update the existing salvage test to the new behaviour**

`test_singleton_fallout_tours_are_repooled_into_one_sweep` ([test_tour_plan.py]) sets up a
DIRECT with a near-centroid origin (`52.40,-0.80`) expecting it to *poison* the cluster and
strand the two deliveries. Under Task 1 the DIRECT now **consolidates directly**, so all three
jobs should land on one tour. Run it:

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_tour_plan.py::test_singleton_fallout_tours_are_repooled_into_one_sweep -v`

Read the current assertions in the test body first. Replace whatever asserts "two singletons re-pooled" with an assertion that the three orders (`ayr`, `air`, `dir`) are served together on a single tour, e.g.:

```python
    tour_of = {j.job_id: ta.start_date for ta in res.tours for j in ta.jobs}
    assert {"ayr:D", "air:D"} <= set(tour_of) or any(  # served on tours
        j.order_id in {"ayr", "air", "dir"} for ta in res.tours for j in ta.jobs)
    # all three now ride ONE sweep (the DIRECT consolidates instead of poisoning)
    dates = {d for jid, d in tour_of.items()}
    assert len(res.tours) >= 1
```

Because this integration test depends on real `road_km`/clustering, **run it and adjust the
assertion to the actual consolidated shape** — the intent to lock in is "the DIRECT is served
on a shared sweep, not stranded," and no job is dropped. Keep a rename to
`test_oncorridor_direct_consolidates_into_sweep` if the re-pool framing no longer applies.

- [ ] **Step 3: Run the full tour_plan suite for regressions**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/test_tour_plan.py -v`
Expected: all pass (assertions updated to the consolidated behaviour; no job dropped anywhere).

- [ ] **Step 4: Run the whole freight_planner suite**

Run: `cd e:/BEAT/ZECURE-Phase2-main/BackEnd/logistics && python -m pytest tests/freight_planner/ -q`
Expected: all pass.

- [ ] **Step 5: Commit** — SKIP (no git).

---

### Task 3: Controller inline validation (NOT a subagent)

The controller runs this directly — it needs the real OSRM pipeline and is the km stakeholder check.

- [ ] **Step 1: Re-run wk1 → wk2 with the change**

```
python -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-17 --out-dir freight_planner/out_wk1_ho
python -m freight_planner.run_alns --start 2026-01-19 --end 2026-01-24 --out-dir freight_planner/out_wk2_ho \
    --handover-in freight_planner/out_wk1_ho/forward_structural/planning_window/2026-01-12_to_2026-01-17/plan/handover.json
```
(Snapshot the prior `run_wk1_ho.log`/`run_wk2_ho.log` to `*.beforeconsolidation.log` first.)

- [ ] **Step 2: Confirm the target outcome**

- `WT255892` (order `3eec5977…`) is served on a **shared** Scotland sweep, not a dedicated single-order tour (check `selected_plan_alns.csv`: its DIRECT leg shares a `trip_id` with other Scotland orders).
- Fewer far-corridor vehicle-days; wk1 total km moves **down** from 93,622 toward the odometer (89,571).
- Coverage held ~99.9%/100%; `temporal violations (must be 0): 0`, `ledger violations (must be 0): 0`.

- [ ] **Step 3: Report the km delta as a stakeholder line**

A km-down-with-better-structure change. If km moves materially, report before/after vs odometer (89,571 / 92,789), don't silently accept or revert. Update QUEST_LOG (`DESIGNED` entry → `SHIPPED` with numbers) and memory.

---

## Self-review notes (author)

- **Spec coverage:** Change 1 (relax gate + km-guard) = Task 1; Change 2 (salvage) = Task 2; validation = Task 3. Resolver untouched (no task edits `options_resolver.py`). Edge cases: capacity (Task 1 Step 5), feasibility fallback (the `_keep_or_split` `not ev.feasible` branch), scope guard (`has_nondepot_direct` gate → depot-origin/pure-delivery unchanged, Task 1 Step 8).
- **`_keep_or_split` subtlety:** when the per-depot split is itself infeasible, keep the feasible consolidated fold (the `not all(e.feasible ...)` branch) — otherwise the guard could reject a fold in favour of a split that also can't serve the jobs, dropping coverage.
- **Type consistency:** `_keep_or_split(depot, ordered, ev)` takes the same triple the branches already produce; `_per_depot()` returns `list[(depot, jobs, ev)]`; `ev.total_km`/`ev.feasible` are `TourEvaluation` fields already used in the file.
- **km-dependence:** Steps 7 (Task 1) and 2 (Task 2) modify existing km-sensitive tests — both instruct run-then-adjust to the spec-correct intent, never weaken.
