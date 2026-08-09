# Collection Window Anchoring Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.
>
> **STANDING RULES:** NO git commands ever. Run tests from
> `e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics` with `python -m pytest`.

**Goal:** Collections never comply with historical *actual* execution times — an
actual timestamp contributes only its DATE (planning-day placement); the time of
day always expands to the operating day. Requested times stay binding. Deliveries
unchanged (already per stakeholder rule).

**Approved design (stakeholder, 2026-07-04):** the day-reschedule override in
`cambridge/scope.py::_pickup_anchor_timestamp` (and the requested-missing fallback)
currently adopt the full actual timestamp — 1,221 collections/month get hindsight-
hardened windows, 205 outside 06:00-16:00 are unservable by any modeled shift
(e.g. order 8548cf16, ST4 8JB, window 18:37→06:37, NO_FEASIBLE_ROUTE at the Stoke
depot door). Fix: return a midnight marker on the actual's date so the existing
`_is_pickup_placeholder_time` machinery expands it to the operating day in both
`_collection_window` and `_pl_export_window`.

**Context for the implementer:**
- `cambridge/scope.py` is shared infra consumed by `freight_planner/legs.py`
  (imports `_collection_window`, `_pl_export_window`, `_delivery_window_policy`).
- `_pickup_anchor_timestamp` is at scope.py:662-684; `_is_pickup_placeholder_time`
  at :651-659 (True for 00:00 or exactly OPERATING_DAY_START); `_collection_window`
  at :767-782; `_pl_export_window` at :785-802. `time` is already imported.
- Existing tests: `tests/cambridge/test_scope.py` — note
  `test_pl_export_window_uses_actual_origin_timestamp` (line ~718) pins the OLD
  behavior and must be REWRITTEN to the new contract;
  `test_pl_export_window_ignores_date_only_origin_actual_late_timestamp` (~757) and
  `test_collection_window_treats_day_start_requested_time_as_placeholder` (~796)
  should still pass unchanged.
- KNOWN PRE-EXISTING FAILURE in tests/cambridge/test_scope.py (a fleet-confirmed
  scope-bypass test, unrelated drift) — identify it by running the file BEFORE your
  change, and do not chase it; your change must not add failures beyond it.
- `tests/freight_planner` (383 tests) must stay fully green.

---

### Task 1: Date-only actual anchors (TDD)

**Files:**
- Modify: `cambridge/scope.py` (`_pickup_anchor_timestamp` only)
- Modify: `tests/cambridge/test_scope.py`

- [ ] **Step 1.0: Baseline.** Run `python -m pytest tests/cambridge/test_scope.py -q`
  and record which test(s) already fail (pre-existing). Run
  `python -m pytest tests/freight_planner -q` and confirm 383 passed.

- [ ] **Step 1.1: Write the failing tests** (append to tests/cambridge/test_scope.py,
  following its existing row-fixture style — read neighbors ~700-810 first):

```python
def test_pickup_anchor_reschedule_contributes_date_only():
    # Mirror of real order 8548cf16: requested 2026-01-20 00:00 (date marker),
    # actually collected 2026-01-21 18:37 (evening). The actual moves the
    # planning DAY to the 21st but must NOT harden 18:37 into the window.
    row = pd.Series({
        "origin_requested_start_timestamp_local": "2026-01-20 00:00:00",
        "origin_timestamp_local": "2026-01-21 18:37:54",
    })
    anchor = _pickup_anchor_timestamp(row)
    assert anchor == datetime(2026, 1, 21, 0, 0)
    ws, we = _collection_window(row)
    assert ws == datetime.combine(date(2026, 1, 21), OPERATING_DAY_START)
    assert we == datetime.combine(date(2026, 1, 21), OPERATING_DAY_END)


def test_pickup_anchor_actual_only_contributes_date_only():
    row = pd.Series({
        "origin_requested_start_timestamp_local": None,
        "origin_timestamp_local": "2026-01-21 18:37:54",
    })
    anchor = _pickup_anchor_timestamp(row)
    assert anchor == datetime(2026, 1, 21, 0, 0)


def test_pickup_anchor_requested_specific_time_still_respected():
    # A specific REQUESTED time is forward-planning data (freight ready from
    # 14:00) and stays binding; same-day actual does not override it.
    row = pd.Series({
        "origin_requested_start_timestamp_local": "2026-01-21 14:00:00",
        "origin_timestamp_local": "2026-01-21 18:37:54",
    })
    anchor = _pickup_anchor_timestamp(row)
    assert anchor == datetime(2026, 1, 21, 14, 0)
```

(Adapt imports to what the test file already imports — `_pickup_anchor_timestamp`,
`_collection_window`, `OPERATING_DAY_START/END`, `datetime`, `date`, `pd`.)

- [ ] **Step 1.2: Run, verify the first two FAIL** (anchor == 18:37 today):
  `python -m pytest tests/cambridge/test_scope.py -q -k pickup_anchor`

- [ ] **Step 1.3: Implement.** In `_pickup_anchor_timestamp`, change the two
  actual-fallback branches (keep the docstring, extend it with the new rule):

```python
    if not pd.isna(requested):
        if not pd.isna(actual) and actual.date() != requested.date():
            # Rescheduled onto a different day: the actual contributes only the
            # planning DAY. Its time-of-day is execution hindsight (when the real
            # driver happened to arrive — e.g. evening runs at 18:37) and must not
            # become a route constraint: collections never comply with historical
            # actual times (stakeholder rule, 2026-07-04). A midnight marker lets
            # _is_pickup_placeholder_time expand it to the operating day.
            return datetime.combine(actual.date(), time(0, 0))
        return requested.to_pydatetime()

    if not pd.isna(actual):
        return datetime.combine(actual.date(), time(0, 0))
```

- [ ] **Step 1.4: Rewrite `test_pl_export_window_uses_actual_origin_timestamp`**
  (~line 718) to the new contract: the actual timestamp selects the DAY; the
  window is operating-day start → trunk deadline on that day. Rename it
  `test_pl_export_window_uses_actual_origin_DATE_only` (keep its fixture,
  change the assertions). Read it fully first; preserve its scenario intent.

- [ ] **Step 1.5: Run the scope file**:
  `python -m pytest tests/cambridge/test_scope.py -q`
  Expected: all pass EXCEPT the exact pre-existing failure(s) recorded in 1.0.

- [ ] **Step 1.6: Full freight_planner suite**:
  `python -m pytest tests/freight_planner -q` → 383 passed, 0 failed.
  If any freight_planner test fails, it is a REAL regression of this change —
  investigate (likely a fixture that relied on hardened actual times) and report
  it; do not paper over.

- [ ] **Step 1.7: Report** status, TDD evidence, exact counts, the recorded
  pre-existing failure names, files changed.

### Task 2: Validation runs (controller executes inline — NOT the subagent)

- [ ] wk1 + wk2 `run_alns` (planning_window / forward_structural / OSRM), one run
  each, no tuning; compare coverage + km to the K1 baselines 92,743 / 100,082;
  verify order 8548cf16's pickup is now assigned; check the unassigned tail and
  the shuttle/merge-sweep log lines still behave.
