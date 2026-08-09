# Intraday Tour Attachment Implementation Plan

> **For agentic workers:** TDD, bite-sized. Standing rules: **NO git commits** (checkpoint =
> run tests). Flags default OFF ⇒ byte-identical (regression gate). `resume=None` and
> `TOUR_ATTACH_ENABLED=False` must both leave existing behaviour bit-for-bit unchanged.

**Goal:** free-ride far orders booked after the 03:00 seed onto an in-flight tour's modifiable
tail — today or a later tour-day, never adding a tour day, never redirecting a driver mid-leg.

**Architecture:** Phase A adds pure tour-side primitives in `freight_planner/tours.py`
(resume-aware `evaluate_tour`, guarded `try_insert_tour_job`, `best_tour_attachment`, tail
split reusing `epoch_state.committed_stop_count`). Phase B wires a flag-gated attachment step
into `freight_planner/run_rolling.py`. **Checkpoint with the user between phases.**

**Spec:** `docs/superpowers/specs/2026-07-12-multiday-tour-insertion-design.md`.

---

## Phase A — tour-side primitives (no rolling wiring)

### Task A1: `evaluate_tour(resume=...)` — evaluate a tail from real mid-day duty

**Files:** Modify `freight_planner/tours.py`; Test `tests/freight_planner/test_tours.py`

- [ ] **Step 1 — failing tests**

```python
def test_evaluate_tour_resume_none_byte_identical():
    veh, jobs = _tractor(), _far_scotland_tour()
    assert evaluate_tour(veh, jobs) == evaluate_tour(veh, jobs, resume=None)

def test_evaluate_tour_resume_seeds_first_day_duty():
    from freight_planner.tours import _DayCursor
    veh = _tractor()
    jobs = [_job("a", 55.0, -3.0, pallets=3, kg=2000)]
    fresh = evaluate_tour(veh, jobs)
    # resume with 9h already driven today: the single stop can't also fit today -> +1 day
    tired = evaluate_tour(veh, jobs, resume=_DayCursor(0, 540.0, 620.0, 180.0))
    assert tired.days > fresh.days
```

- [ ] **Step 2 — run, expect FAIL** (`resume` kwarg unknown).
- [ ] **Step 3 — implement:** add `resume: "_DayCursor | None" = None` to `evaluate_tour`.
  Where the day accumulators init (`day_index=0; day_drive=0.0; ...`), seed from resume:

```python
    if resume is not None:
        day_drive = float(resume.day_drive)
        day_elapsed = float(resume.day_elapsed)
        drive_since_break = float(resume.drive_since_break)
```

  (Position already comes from `vehicle.start_lat/lon`; `day_index` stays 0 — the tail's
  day 0 is the resume day.) Everything else unchanged; `resume=None` ⇒ identical.

- [ ] **Step 4 — run, expect PASS.** Checkpoint.

### Task A2: `try_insert_tour_job` threads `resume`; `best_tour_attachment` guard

**Files:** Modify `freight_planner/tours.py`; Test `tests/freight_planner/test_tours.py`

- [ ] **Step 1 — failing tests**

```python
def test_try_insert_threads_resume():
    veh = _tractor()
    base = [_job("a", 55.0, -3.0, pallets=3, kg=2000)]
    cand = _job("b", 55.2, -3.1, pallets=2, kg=1200)
    from freight_planner.tours import _DayCursor, try_insert_tour_job
    got = try_insert_tour_job(veh, base, cand, resume=_DayCursor(0, 60.0, 80.0, 60.0))
    assert got is not None and any(j.job_id == "Jb" for j in got[0])

def test_best_tour_attachment_rejects_new_day_but_takes_free_ride():
    # a candidate that fits the tail's existing days is taken; one that only fits by
    # extending is rejected under max_extra_days=0
    ...  # see Step 3 helper; asserts served vs None on the two candidates
```

- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement:** add `resume=None` to `try_insert_tour_job` and pass it into each
  `evaluate_tour(...)` call. Add:

```python
def best_tour_attachment(vehicle, tail_jobs, candidate, *, resume=None,
                         due_offsets=None, floor_offsets=None,
                         standalone_km=float("inf"), max_extra_days=0):
    """Best insertion of `candidate` into `tail_jobs`, or None. Rejects any that add a
    tour day (days_new > days_base + max_extra_days) or cost more than a standalone run."""
    base = evaluate_tour(vehicle, tail_jobs, due_offsets, floor_offsets=floor_offsets,
                         resume=resume)
    if not base.feasible:
        return None
    got = try_insert_tour_job(vehicle, tail_jobs, candidate, due_offsets=due_offsets,
                              floor_offsets=floor_offsets, resume=resume)
    if got is None:
        return None
    new_jobs, ev = got
    if ev.days > base.days + max_extra_days:
        return None
    if ev.total_km - base.total_km > standalone_km:
        return None
    return new_jobs, ev, ev.total_km - base.total_km
```

- [ ] **Step 4 — run, expect PASS.** Checkpoint.

### Task A3: tour tail split from the commit frontier (reuse `committed_stop_count`)

**Files:** Modify `freight_planner/tours.py` (or a small `tour_runtime.py`); Test same.

- [ ] **Step 1 — failing test**

```python
def test_split_tour_at_frontier_locks_rolling_stop():
    # stops: s1 done, s2 being rolled toward (prev departed), s3/s4 future
    # head = [s1, s2]; tail = [s3, s4]; resume carries s2's day/position/freight
    ...  # asserts head stop ids, tail stop ids, resume.day_elapsed > 0
```

- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** `split_tour_at_frontier(stops, depot_depart_iso, now_plus_delta)`
  → `(head, tail, resume)` calling `epoch_state.committed_stop_count` for the split index and
  deriving `resume` (`_DayCursor` + start position + carried freight) from the last head stop.

- [ ] **Step 4 — run, expect PASS.** Full `tests/freight_planner/` green. **Checkpoint + stop
  for user review before Phase B.**

---

## Phase B — rolling integration (flag-gated) — AFTER user checkpoint

### Task B1: `TOUR_ATTACH_ENABLED` flag (default False)

- [ ] TDD: assert `config.TOUR_ATTACH_ENABLED is False`; add the constant to `config.py`.

### Task B2: intraday attachment step in `run_rolling`

**Files:** Modify `freight_planner/run_rolling.py`; Test `tests/freight_planner/test_dynamic_loop.py`

- [ ] **Step 1 — failing test:** a far order booked at 10:30, unassigned after the re-solve,
  attaches to an in-flight tour whose tail passes it; assert order served, tour `days`
  unchanged, head records identical, a driver-notify/emit record produced.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** an `attach_intraday(...)` step, called at each re-opt epoch when
  `TOUR_ATTACH_ENABLED`: build candidates (unassigned today far orders), split each in-flight
  tour via `split_tour_at_frontier`, call `best_tour_attachment(max_extra_days=0)`, splice the
  winner into that tour's `merged_tour_records` tail, mark served (feed
  `collection_orders_in_plan`), emit via the micro-insert notify path. Additive-only.
- [ ] **Step 4 — run, expect PASS.**

### Task B3: flag-off identity + regression

- [ ] Test: a full rolling window with `TOUR_ATTACH_ENABLED=False` is byte-identical
  (served/rejected/km/records) to before.
- [ ] Run `python -m pytest tests/freight_planner/ -q` — all green.
- [ ] A short flag-ON rolling run: report orders newly served, tours touched, km added,
  and assert 0 head-stop mutations / 0 tours with grown `days`. Checkpoint.

---

## Self-review notes

- **Spec coverage:** resume-eval (A1), guarded attachment (A2), frontier tail split + no
  mid-leg detour via `committed_stop_count` (A3), rolling step (B2), identity gates (A1/B1/B3).
- **Safety:** the no-mid-leg-detour guarantee is `committed_stop_count`'s `rolling` clause,
  reused verbatim — head stops are never in the insertable tail.
- **Identity:** `resume=None` and `TOUR_ATTACH_ENABLED=False` are both no-ops.
