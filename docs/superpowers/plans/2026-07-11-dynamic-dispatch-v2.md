# Dynamic Dispatch v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. **Standing rule: NO git commits — replace every "Commit" step with a checkpoint (note progress, keep going).**

**Goal:** Replace the re-seed-every-anchor dynamic architecture with warm-start + insertion + a single bounded noon re-optimization, scored by one unified objective `(served, −(cost + β·disturbance))`.

**Architecture:** One ALNS engine, three swapped inputs (start / destroy-scope / objective). Phase 1 builds the disturbance scoring machinery (inert at β=0). Phase 2 makes anchors warm-start instead of re-seed (fixes the non-anticipation class, harness-TDD). Phase 3 wires a real reference plan + imminence weights + a β CLI dial.

**Tech Stack:** Python, pandas, pytest. Spec: `docs/superpowers/specs/2026-07-11-dynamic-dispatch-v2-design.md`.

**Invariants held throughout:** `β=0` is bit-identical to today's cost-only behaviour (regression gate); full `tests/freight_planner` suite green after every task; static E1 path (`run_alns`) unchanged.

---

## File Structure

- **Create** `freight_planner/disturbance.py` — pure disturbance scoring: `job_positions`, `key_disturbance`, `disturbance`, `imminence_weights`. One responsibility, no I/O, unit-tested in isolation.
- **Modify** `freight_planner/alns.py` — fold `β·Δdisturbance` into `improve_existing_solution`'s `candidate_total`; add `beta`, `reference_routes`, `disturbance_weight` params (default off). Thread the same through `improve_route_seed`.
- **Modify** `freight_planner/run_alns.py` — pass `beta`/`reference_routes`/`disturbance_weight` from `args` into the improve call; add a `reoptimize_window` warm-start solve path (skips seed/tour/trunk, improves the injected live plan).
- **Modify** `freight_planner/run_rolling.py` — the noon anchor calls the warm-start path with the live plan as both start and reference; the 03:00 seed keeps `solve_window`; compute imminence weights; add `--beta` CLI.
- **Create** `tests/freight_planner/test_disturbance.py` — disturbance-module unit tests.
- **Modify** `tests/freight_planner/test_dynamic_e2e.py` — warm-start reproduction (a noon arrival never gets a morning service time) + disturbance-ordering behaviour.

---

## PHASE 1 — Objective machinery (disturbance, inert at β=0)

### Task 1: `job_positions` and `disturbance`

**Files:**
- Create: `freight_planner/disturbance.py`
- Test: `tests/freight_planner/test_disturbance.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/freight_planner/test_disturbance.py
from freight_planner.disturbance import disturbance, job_positions
from freight_planner.routing_adapter import RouteJob


def _j(jid):
    return RouteJob(job_id=jid, leg_kind="CUSTOMER_PICKUP", node=jid,
                    lat=52.1, lon=0.2, pallets=1.0, kg=100.0)


def test_job_positions_flattens_trips():
    plan = {("V1", "D"): [[_j("A"), _j("B")], [_j("C")]]}
    assert job_positions(plan) == {"A": (("V1", "D"), 0), "B": (("V1", "D"), 1),
                                   "C": (("V1", "D"), 2)}


def test_disturbance_zero_when_identical():
    plan = {("V1", "D"): [[_j("A"), _j("B")]]}
    assert disturbance(plan, plan) == 0.0


def test_disturbance_counts_reassignment_full():
    ref = {("V1", "D"): [[_j("A")]], ("V2", "D"): [[_j("B")]]}
    cand = {("V1", "D"): [[_j("A"), _j("B")]], ("V2", "D"): []}
    assert disturbance(cand, ref, gamma=0.5) == 1.0     # B moved V2 -> V1


def test_disturbance_counts_resequence_gamma():
    ref = {("V1", "D"): [[_j("A"), _j("B")]]}
    cand = {("V1", "D"): [[_j("B"), _j("A")]]}
    assert disturbance(cand, ref, gamma=0.5) == 1.0     # A and B each shifted: 2 * 0.5


def test_disturbance_ignores_new_jobs():
    ref = {("V1", "D"): [[_j("A")]]}
    cand = {("V1", "D"): [[_j("A"), _j("NEW")]]}
    assert disturbance(cand, ref) == 0.0                # NEW absent from ref = addition, not move


def test_disturbance_weight_scales_per_job():
    ref = {("V1", "D"): [[_j("A")]], ("V2", "D"): [[_j("B")]]}
    cand = {("V1", "D"): [[_j("A"), _j("B")]], ("V2", "D"): []}
    assert disturbance(cand, ref, weight={"B": 3.0}) == 3.0
```

- [ ] **Step 2: Run to verify fail** — `pytest tests/freight_planner/test_disturbance.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# freight_planner/disturbance.py
"""Plan-disturbance scoring for the dynamic dispatcher (spec 2026-07-11 §5).

disturbance(candidate, reference) = weighted count of jobs that MOVED from the
plan being warm-started: reassigned (changed vehicle-day) counts full, resequenced
(same vehicle-day, different position) counts gamma. New jobs (absent from the
reference) are additions, not disturbances, and do not count. Pure; no I/O.
"""
from __future__ import annotations

from datetime import datetime


def _as_trips(v):
    return [v] if v and hasattr(v[0], "job_id") else list(v or [])


def job_positions(plan: dict) -> dict:
    """job_id -> (key, flat position within its vehicle-day)."""
    out: dict = {}
    for key, trips in plan.items():
        pos = 0
        for t in _as_trips(trips):
            for j in t:
                out[j.job_id] = (key, pos)
                pos += 1
    return out


def key_disturbance(key, trips, ref_positions: dict, *, gamma: float = 0.5,
                    weight: dict | None = None) -> float:
    """Disturbance contribution of ONE vehicle-day vs the reference positions."""
    w = weight or {}
    total = 0.0
    pos = 0
    for t in _as_trips(trips):
        for j in t:
            rp = ref_positions.get(j.job_id)
            if rp is not None:
                jw = float(w.get(j.job_id, 1.0))
                if rp[0] != key:
                    total += jw
                elif rp[1] != pos:
                    total += gamma * jw
            pos += 1
    return total


def disturbance(candidate: dict, reference: dict, *, gamma: float = 0.5,
                weight: dict | None = None) -> float:
    """Whole-plan disturbance of candidate vs reference."""
    ref = job_positions(reference or {})
    return sum(key_disturbance(key, trips, ref, gamma=gamma, weight=weight)
               for key, trips in candidate.items())
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/freight_planner/test_disturbance.py -q` → 6 passed.
- [ ] **Step 5: Checkpoint** — note "Task 1 green (disturbance module)".

### Task 2: imminence weights

**Files:**
- Modify: `freight_planner/disturbance.py`
- Test: `tests/freight_planner/test_disturbance.py`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime
from freight_planner.disturbance import imminence_weights


def test_imminence_full_when_now_decays_to_zero():
    now = datetime(2026, 1, 12, 12, 0)
    disp = {"A": f"2026-01-12 12:00:00", "B": "2026-01-12 18:00:00",
            "C": "2026-01-13 00:00:00"}          # 0h, 6h, 12h ahead
    w = imminence_weights(disp, now, horizon_min=720.0)
    assert abs(w["A"] - 1.0) < 1e-9
    assert abs(w["B"] - 0.5) < 1e-9
    assert abs(w["C"] - 0.0) < 1e-9


def test_imminence_past_is_full_weight():
    now = datetime(2026, 1, 12, 12, 0)
    w = imminence_weights({"A": "2026-01-12 09:00:00"}, now)
    assert abs(w["A"] - 1.0) < 1e-9                # already due = maximally imminent


def test_imminence_missing_time_defaults_full():
    now = datetime(2026, 1, 12, 12, 0)
    assert imminence_weights({"A": ""}, now)["A"] == 1.0
```

- [ ] **Step 2: Run to verify fail** — FAIL (imminence_weights missing).

- [ ] **Step 3: Implement** (append to `disturbance.py`)

```python
def imminence_weights(dispatch_iso: dict, now: datetime,
                      horizon_min: float = 720.0) -> dict:
    """job_id -> imminence weight in [0, 1]: 1.0 for a job dispatching now (or in
    the past), decaying linearly to 0 at ``horizon_min`` minutes ahead. Missing
    times weight 1.0 (conservative: treat unknown as imminent)."""
    out: dict = {}
    for jid, iso in dispatch_iso.items():
        try:
            dt = datetime.fromisoformat(str(iso)) if iso else None
        except ValueError:
            dt = None
        if dt is None:
            out[jid] = 1.0
            continue
        lead = max(0.0, (dt - now).total_seconds() / 60.0)
        out[jid] = max(0.0, 1.0 - lead / horizon_min)
    return out
```

- [ ] **Step 4: Run to verify pass** — 3 passed.
- [ ] **Step 5: Checkpoint** — "Task 2 green (imminence)".

### Task 3: fold β·Δdisturbance into `improve_existing_solution` (β=0 identity)

**Files:**
- Modify: `freight_planner/alns.py` (params + the objective at `candidate_total = total + delta`)
- Test: `tests/freight_planner/test_disturbance.py`

- [ ] **Step 1: Write the failing test** (β=0 identity + β>0 changes acceptance)

```python
# tests/freight_planner/test_disturbance.py  (add)
import pandas as pd
from freight_planner.alns import improve_existing_solution
from freight_planner.routing_adapter import RouteJob


def _sol_frames():
    def _rj(jid, loc):
        return RouteJob(job_id=jid, leg_kind="CUSTOMER_DELIVERY", node=jid,
                        lat=loc[0], lon=loc[1], pallets=1.0, kg=100.0)
    jobs = {"JA": (52.16, -0.45), "JB": (52.15, -0.44), "JF": (52.10, 0.20)}
    vehicles = pd.DataFrame([
        {"vehicle_id": v, "home_depot": "X", "current_lat": 52.07, "current_lon": 0.17,
         "available_from": "2026-01-12 06:00:00", "shift_end": "2026-01-12 18:00:00",
         "capacity_kg": 8000.0, "capacity_pallets": 15.0, "vehicle_type": "rigid"}
        for v in ("V1", "V2")])
    cand = pd.DataFrame([
        {"leg_id": j, "job_id": j, "order_id": j, "order_name": j,
         "leg_kind": "CUSTOMER_DELIVERY", "flow": "PL_IMPORT", "service_date": "2026-01-12",
         "service_pc": "X", "source_depot": "X", "target_depot": "X", "pallets": 1.0,
         "weight_kg": 100.0, "dependency_type": "PRESTAGED_DELIVERY", "predecessor_leg_id": "",
         "earliest_start": "", "latest_finish": "", "hard_blocker": "",
         "preferred_start_node": "DEPOT", "preferred_end_node": "CUSTOMER",
         "option_set": "", "option_group": "", "origin_lat": None, "origin_lon": None}
        for j in jobs])
    compat = pd.DataFrame([
        {"leg_id": j, "job_id": j, "vehicle_id": v, "same_depot": True, "cross_depot": False,
         "service_lat": loc[0], "service_lon": loc[1], "compatibility_status": "OK"}
        for j, loc in jobs.items() for v in ("V1", "V2")])
    src = {("V1", "2026-01-12"): [[_rj("JA", jobs["JA"]), _rj("JB", jobs["JB"])]],
           ("V2", "2026-01-12"): [[_rj("JF", jobs["JF"])]]}
    return src, cand, vehicles, compat


def test_beta_zero_is_identity():
    src, cand, veh, compat = _sol_frames()
    a = improve_existing_solution(src, cand, veh, compat, iterations=150, rng_seed=1)
    b = improve_existing_solution(src, cand, veh, compat, iterations=150, rng_seed=1,
                                  beta=0.0, reference_routes=src)
    ka = {k: [[j.job_id for j in t] for t in v] for k, v in a.solution.items()}
    kb = {k: [[j.job_id for j in t] for t in v] for k, v in b.solution.items()}
    assert ka == kb                                   # β=0 changes nothing
```

- [ ] **Step 2: Run to verify fail** — FAIL (`improve_existing_solution() got an unexpected keyword argument 'beta'`).

- [ ] **Step 3: Implement** — add params to `improve_existing_solution` signature (near the other dynamic hooks):

```python
    beta: float = 0.0,
    reference_routes: dict | None = None,
    disturbance_weight: dict | None = None,
    disturbance_gamma: float = 0.5,
```

Precompute the reference positions once, just before the main loop (after `job_ids = ...`):

```python
    from freight_planner.disturbance import job_positions, key_disturbance
    _ref_pos = job_positions(reference_routes) if (beta > 0.0 and reference_routes) else None
```

Change the objective at `candidate_total = total + delta` (the `best_plan` selection block) so the score includes disturbance:

```python
            candidate_total = total + delta
            if _ref_pos is not None:
                d_before = sum(key_disturbance(k, routes.get(k, []), _ref_pos,
                                               gamma=disturbance_gamma, weight=disturbance_weight)
                               for k in changed)
                d_after = sum(key_disturbance(k, work.get(k, []), _ref_pos,
                                              gamma=disturbance_gamma, weight=disturbance_weight)
                              for k in changed)
                candidate_total = total + delta + beta * (d_after - d_before)
            score = (served_after, -candidate_total)
```

And at the acceptance `candidate_total = total + delta` (after `best_plan` is chosen), recompute consistently:

```python
        _score, work, placements, new_cost, delta, served_gain, inserted_jids, _attempted = best_plan
        candidate_total = -_score[1]      # reuse the disturbance-adjusted total from selection
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/freight_planner/test_disturbance.py -q` → all green; then `pytest tests/freight_planner -q` → full suite green (β=0 identity holds).
- [ ] **Step 5: Checkpoint** — "Task 3 green (β-fold, β=0 identity, suite green)".

### Task 4: thread β through `improve_route_seed` and `solve_window`

**Files:**
- Modify: `freight_planner/alns.py` (`improve_route_seed` params + pass-through)
- Modify: `freight_planner/run_alns.py` (`solve_window` reads `args.beta` etc.)
- Test: `tests/freight_planner/test_disturbance.py`

- [ ] **Step 1: Write the failing test** — `improve_route_seed` accepts and forwards β (β=0 identity via the seed wrapper). Use the `Seed`/`extra_routes` shape from `test_micro_pass.py::test_injection_prepends_inflight_trips` as the fixture, asserting a `beta=0.0, reference_routes=<source>` call equals the no-arg call.

- [ ] **Step 2: Run to verify fail** — FAIL (unexpected kwarg).

- [ ] **Step 3: Implement** — add `beta/reference_routes/disturbance_weight/disturbance_gamma` to `improve_route_seed` signature and forward them into the `improve_existing_solution(...)` call. In `solve_window` add `beta=getattr(args, "beta", 0.0)`, `reference_routes=getattr(args, "reference_routes", None)`, `disturbance_weight=getattr(args, "disturbance_weight", None)` to the `improve_route_seed(...)` call.

- [ ] **Step 4: Run to verify pass** — test green; `pytest tests/freight_planner -q` green.
- [ ] **Step 5: Checkpoint** — "Phase 1 complete: disturbance machinery wired, inert at β=0, suite green."

---

## PHASE 2 — Warm-start anchor (drop re-seed; fixes non-anticipation)

### Task 5: harness reproduction — a noon arrival must not get a morning service time (RED)

**Files:**
- Modify: `tests/freight_planner/test_dynamic_e2e.py`

- [ ] **Step 1: Write the failing test.** Using the `_toy_ctx`/`_dyn_ctx` scripted-solver harness: a vehicle runs a tiny morning trip committed at 03:00 (departs 06:30); an order booked 10:40 arrives; drive `run_dynamic_loop`. Assert the 10:40 order, if served on D1, has a `collected_day`/route position whose evaluated arrival is **≥ its booking time** — i.e. it never rides the committed morning trip. With a scripted solver, assert it lands on a *new* key or a floored position, never inside the pre-committed morning trip's stops. (This is RED against the current re-seed anchor if the scripted solver mimics a re-seed that back-fills; design the fake solver to expose the seam.)

- [ ] **Step 2: Run to verify fail** — the assertion fails under the current anchor path.

### Task 6: `reoptimize_window` — warm-start solve that skips the seed

**Files:**
- Modify: `freight_planner/run_alns.py` (new `reoptimize_window(args, start, inputs, runlog)`)
- Test: `tests/freight_planner/test_warmstart.py` (create)

- [ ] **Step 1: Write the failing test** — `reoptimize_window` given `inject_routes` (a live plan) + a small candidate frame with one new visible order returns a `SolveResult` whose `imp.solution` contains the injected trips (unchanged prefix) plus the new order placed after the floor; it does **not** call the seed/tour/trunk stages (assert via a recorder that `run_multiday_seed_plan` is not invoked — monkeypatch it to raise).

- [ ] **Step 2: Run to verify fail** — FAIL (function missing).

- [ ] **Step 3: Implement** — `reoptimize_window` builds `vehicle_meta`/`candidate` frames from `inputs` (reuse `build_window_inputs` output), takes `inject_routes` as `source_routes`, inserts newly-visible order metas via `insertion_pass`, then calls `improve_existing_solution` with `watermarks/commit_floor/locked_keys/beta/reference_routes` — **no seed, no tour formation, no trunk**. Return a `SolveResult` shaped like `solve_window`'s (empty `seed.tours`, carried `tour_records`).

- [ ] **Step 4: Run to verify pass** — test green.
- [ ] **Step 5: Checkpoint** — "Task 6 green (reoptimize_window; seed not called)."

### Task 7: noon anchor uses `reoptimize_window`; 03:00 keeps `solve_window`

**Files:**
- Modify: `freight_planner/run_rolling.py` (anchor branch: choose solve path by epoch index)
- Modify: `freight_planner/run_rolling.py::LoopCtx` (add `reopt_fn=reoptimize_window`)
- Test: `tests/freight_planner/test_dynamic_e2e.py` (Task 5 test now GREEN)

- [ ] **Step 1: Implement** — in `run_dynamic_loop`, the FIRST anchor of the window calls `ctx.solve_fn` (full seed). Every later anchor calls `ctx.reopt_fn` with `inject_routes = current_sol`, `reference_routes = current_sol`, the visible new-order metas, and the same watermark/floor/lock context. Tours/shuttle/trunk carry from `merged_tours`/`merged_tour_records` (already preserved).

- [ ] **Step 2: Run Task 5 test** — now GREEN (noon arrival never gets a morning time).
- [ ] **Step 3: Run** `pytest tests/freight_planner -q` — full suite green (all prior harness reproductions still pass).
- [ ] **Step 4: Checkpoint** — "Phase 2 complete: warm-start anchor; non-anticipation reproduction green; suite green."

---

## PHASE 3 — Wire real reference + imminence + β dial

### Task 8: compute imminence weights for the live plan and pass to the re-opt

**Files:**
- Modify: `freight_planner/run_rolling.py` (build `disturbance_weight` from `timings`/`stop_timings` dispatch times)
- Test: `tests/freight_planner/test_dynamic_e2e.py`

- [ ] **Step 1: Write the failing test** — at a later anchor, `ns.disturbance_weight` is non-empty and a soon-dispatching in-flight job has a higher weight than a far-future one (via `imminence_weights` over the plan's per-trip departure times).

- [ ] **Step 2: Run to verify fail** — FAIL (`disturbance_weight` absent on the reopt `ns`).

- [ ] **Step 3: Implement** — before the reopt call, build a `dispatch_iso` map (job_id -> its trip's depot-departure from `imp.route_times`/`timings`), compute `imminence_weights(dispatch_iso, now)`, and pass as `disturbance_weight` on the reopt `ns`.

- [ ] **Step 4: Run to verify pass** — test green.
- [ ] **Step 5: Checkpoint** — "Task 8 green (imminence wired)."

### Task 9: `--beta` CLI dial and disturbance-ordering behaviour test

**Files:**
- Modify: `freight_planner/run_rolling.py::main` (`--beta` arg → `LoopCfg.beta` → reopt `ns.beta`)
- Modify: `freight_planner/run_rolling.py::LoopCfg` (add `beta: float = 0.0`)
- Test: `tests/freight_planner/test_dynamic_e2e.py`

- [ ] **Step 1: Write the failing test** — scripted harness: with a live plan and one new order that could sit on either a soon-trip or a far-trip at equal km, `β=0` picks the km-tie arbitrarily but `β` high picks the *far* (low-imminence) trip. Assert the placement differs with β and that high β keeps the soon-trip's stops unchanged.

- [ ] **Step 2: Run to verify fail** — FAIL (`--beta`/`LoopCfg.beta` missing; behaviour identical).

- [ ] **Step 3: Implement** — add `--beta` (default 0.0) to `main`, thread `LoopCfg.beta` into every reopt `ns.beta`. Seed stays β=0 (empty reference).

- [ ] **Step 4: Run to verify pass** — test green; `pytest tests/freight_planner -q` green.
- [ ] **Step 5: Checkpoint** — "Phase 3 complete: β dial live; disturbance steers placement; suite green. Ready for a β=0 smoke (must match a cost-only warm-start run) then a β>0 ablation."

---

## Self-review notes

- **Spec coverage:** §5 objective → Tasks 1–4; §4 warm-start → Tasks 5–7; §3 frontier (expire/watermark/lock) → carried unchanged, exercised by Task 5/7; §6 insertion → reused (`insertion_pass`) in Task 6; §11 dials (β) → Task 9, imminence → Tasks 2/8; §12 β=0 gate → Task 3, harness → Tasks 5/9.
- **β=0 identity** is asserted in Task 3 and re-checked by the full suite after every task.
- **micro-δ** CLI and the 1-vs-2 re-opt ablation are spec §11 experiments, NOT implementation tasks — deferred to run-time, out of this plan's scope.
- **Deferred/unchanged:** tours/shuttle/trunk formation stay at the 03:00 seed (Task 7 preserves them); `merge_frozen_routing` left as-is.
