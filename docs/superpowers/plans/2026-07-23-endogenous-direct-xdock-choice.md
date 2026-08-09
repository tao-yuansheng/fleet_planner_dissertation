# Endogenous DIRECT-vs-XDOCK Mode Choice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the static ρ=1.6 DIRECT-vs-XDOCK resolver and let the seed + ALNS choose the mode by real co-loaded insertion cost, for same-day FULL_FLEET option sets.

**Architecture:** `legs.py` already emits both groups (DIR + XC/XD, shared `option_set`/`freight_id`, `option_group ∈ {DIRECT,XDOCK}`). We stop collapsing in `build_window_inputs`, carry both groups into the optimizer (the candidate frame already has the columns), enforce a mutual-exclusion invariant at every insertion site, add an ALNS OptionSwap destroy operator that re-prices the choice against the full solution, and derive the chosen mode from the final selected plan.

**Tech Stack:** Python, pandas, the existing `freight_planner` seed (`tour_plan.run_multiday_seed_plan` + `route_seed`) and ALNS (`alns.py`) machinery; pytest.

**Design doc:** `docs/superpowers/specs/2026-07-23-endogenous-direct-xdock-choice-design.md`

**Critical ordering rule:** Tasks 1–6 build and UNIT-test the mutex machinery with synthetic both-group inputs. The collapse is only removed in Task 7. Until Task 7 the live pipeline still collapses via `resolve_options`, so it stays green throughout. Never remove the collapse before the mutex guards exist, or both modes get served (double-delivery).

**Run environment (all verify commands):**
```
cd /e/BEAT/ZECURE-Phase2-main/BackEnd/logistics
PYTHONPATH=/e/BEAT/ZECURE-Phase2-main/BackEnd/logistics
PY='/e/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe -B'
```
(On PowerShell: `$env:PYTHONPATH='e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics'; & 'E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe' -B -m pytest ...`)

---

## Task 1: Shared mutual-exclusion helper

A single module both the seed and ALNS use, so the invariant is defined once. Key = `option_set`; a leg's group = `option_group`. XDOCK counts as "active" once either XC or XD is placed.

**Files:**
- Create: `freight_planner/option_mutex.py`
- Test: `tests/freight_planner/test_option_mutex.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_option_mutex.py
from freight_planner.option_mutex import OptionMutex


def _cand(option_set, option_group, leg_kind):
    return {"option_set": option_set, "option_group": option_group, "leg_kind": leg_kind}


def test_no_option_set_always_insertable():
    m = OptionMutex()
    assert m.insertable(_cand("", "", "CUSTOMER_DELIVERY")) is True


def test_first_group_becomes_active_and_blocks_rival():
    m = OptionMutex()
    dir_leg = _cand("F1", "DIRECT", "DIRECT_CUSTOMER_MOVE")
    xc = _cand("F1", "XDOCK", "CUSTOMER_PICKUP")
    assert m.insertable(dir_leg) is True
    m.assign(dir_leg)
    assert m.active_group("F1") == "DIRECT"
    assert m.insertable(xc) is False          # rival group blocked


def test_same_group_partner_stays_insertable():
    m = OptionMutex()
    xc = _cand("F1", "XDOCK", "CUSTOMER_PICKUP")
    xd = _cand("F1", "XDOCK", "CUSTOMER_DELIVERY")
    m.assign(xc)
    assert m.active_group("F1") == "XDOCK"
    assert m.insertable(xd) is True           # XDOCK partner NOT blocked


def test_release_clears_group():
    m = OptionMutex()
    xc = _cand("F1", "XDOCK", "CUSTOMER_PICKUP")
    m.assign(xc)
    m.release("F1")
    assert m.active_group("F1") is None
    assert m.insertable(_cand("F1", "DIRECT", "DIRECT_CUSTOMER_MOVE")) is True


def test_rivals_helper_lists_only_other_group():
    m = OptionMutex()
    legs = [_cand("F1", "DIRECT", "DIRECT_CUSTOMER_MOVE"),
            _cand("F1", "XDOCK", "CUSTOMER_PICKUP"),
            _cand("F1", "XDOCK", "CUSTOMER_DELIVERY")]
    # given DIRECT just placed, the rivals among a pending list are the two XDOCK legs
    rivals = m.rival_legs(_cand("F1", "DIRECT", "DIRECT_CUSTOMER_MOVE"), legs)
    assert [l["option_group"] for l in rivals] == ["XDOCK", "XDOCK"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `$PY -m pytest tests/freight_planner/test_option_mutex.py -v`
Expected: FAIL, `ModuleNotFoundError: freight_planner.option_mutex`.

- [ ] **Step 3: Write the implementation**

```python
# freight_planner/option_mutex.py
"""Mutual-exclusion for DIRECT-vs-XDOCK option sets (2026-07-23).

An option set (keyed by `option_set`, == freight_id) offers alternative ways to
serve one freight unit: option_group DIRECT (one leg) or XDOCK (a pickup + a
delivery leg). At most ONE group may be assigned per set. This tracker is the
single source of that invariant, used by the seed and every ALNS insertion site.
"""
from __future__ import annotations


def _set(cand: dict) -> str:
    return str((cand or {}).get("option_set", "") or "")


def _group(cand: dict) -> str:
    return str((cand or {}).get("option_group", "") or "")


class OptionMutex:
    def __init__(self) -> None:
        self._active: dict[str, str] = {}   # option_set -> active option_group

    def active_group(self, option_set: str) -> str | None:
        return self._active.get(str(option_set)) or None

    def insertable(self, cand: dict) -> bool:
        """True unless the leg's option_set already has a DIFFERENT group active."""
        s, g = _set(cand), _group(cand)
        if not s or not g:
            return True                      # non-optional leg: never constrained
        cur = self._active.get(s)
        return cur is None or cur == g

    def assign(self, cand: dict) -> None:
        s, g = _set(cand), _group(cand)
        if s and g:
            self._active[s] = g

    def release(self, option_set: str) -> None:
        self._active.pop(str(option_set), None)

    def rival_legs(self, cand: dict, legs: list[dict]) -> list[dict]:
        """Legs in `legs` sharing this option_set but a DIFFERENT group."""
        s, g = _set(cand), _group(cand)
        if not s or not g:
            return []
        return [l for l in legs if _set(l) == s and _group(l) not in ("", g)]

    def seed_from_assigned(self, assigned_cands) -> None:
        """Initialise active groups from already-placed/committed candidates
        (rolling-horizon mode-lock: a committed XC locks the set to XDOCK)."""
        for cand in assigned_cands:
            self.assign(cand)
```

- [ ] **Step 4: Run to verify it passes**

Run: `$PY -m pytest tests/freight_planner/test_option_mutex.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add freight_planner/option_mutex.py tests/freight_planner/test_option_mutex.py
git commit -m "feat(freight): OptionMutex helper for DIRECT/XDOCK mutual exclusion"
```
(No git in this workspace — skip the commit command; keep the change on disk. Applies to every task.)

---

## Task 2: Make `insertion_pass` sibling-drop option_group-aware

Today [alns.py:1797-1805](../../../freight_planner/alns.py) drops ALL pending legs sharing `order_id` once one inserts. With both groups present that wrongly drops the XDOCK partner (XC inserts → XD dropped). Refine: drop only RIVAL-group legs; keep the same-group partner; also skip legs a mutex would block.

**Files:**
- Modify: `freight_planner/alns.py` (`insertion_pass`, ~1758–1806)
- Test: `tests/freight_planner/test_option_mutex_alns.py`

- [ ] **Step 1: Write the failing test** — construct a both-group `new_meta` and assert XDOCK inserts BOTH legs (not just XC), and that inserting one group supersedes the rival.

```python
# tests/freight_planner/test_option_mutex_alns.py
# Build a minimal solution + new_meta with a same-day option set (DIR, XC, XD),
# geometry where XDOCK is cheaper, run insertion_pass, assert:
#   * exactly one group ends up inserted,
#   * if XDOCK wins, BOTH XC and XD are in `inserted` (partner not dropped),
#   * the DIRECT leg is NOT inserted.
# Use the same JobMeta/RouteJob/VehicleMeta construction helpers the existing
# alns tests use (see tests/freight_planner/test_alns.py for the fixtures).
```

Model the fixtures on `tests/freight_planner/test_alns.py` (it already builds `JobMeta`, `RouteJob`, `VehicleMeta`, and calls `insertion_pass`). Read that file first for the exact constructors and helper signatures, then assert the three properties above.

- [ ] **Step 2: Run to verify it fails**

Run: `$PY -m pytest tests/freight_planner/test_option_mutex_alns.py -v`
Expected: FAIL — current code drops XD when XC inserts, so XDOCK delivery is missing.

- [ ] **Step 3: Implement** — in `insertion_pass`, replace the order_id sibling-drop with option_group-aware supersede, and add a mutex guard before insertion.

Replace the block at ~1797-1805:
```python
        # OLD:
        oid = str((new_meta[jid].candidate or {}).get("order_id", "") or "")
        if oid:
            for p in [p for p in pending
                      if str((new_meta[p].candidate or {}).get("order_id", "") or "") == oid]:
                pending.remove(p)
```
with (using an `OptionMutex` created at the top of `insertion_pass` — `mutex = OptionMutex()`, seeded from already-placed jobs in `solution` if their candidates are available; otherwise start empty):
```python
        cand_ins = new_meta[jid].candidate or {}
        mutex.assign(cand_ins)
        # Supersede only the RIVAL option_group's pending legs (a served order's
        # alternative). The same-group partner (XDOCK's XC<->XD) stays: it must
        # still be inserted, ordered by REQUIRES_PRIOR_PICKUP. Legacy order-id
        # branch-drop is preserved for non-optional legs (option_set == "").
        pending_cands = [new_meta[p].candidate or {} for p in pending]
        rival_ids = {id(c) for c in mutex.rival_legs(cand_ins, pending_cands)}
        if str(cand_ins.get("option_set", "") or ""):
            pending = [p for p in pending
                       if id(new_meta[p].candidate or {}) not in rival_ids]
        else:
            oid = str(cand_ins.get("order_id", "") or "")
            if oid:
                pending = [p for p in pending
                           if str((new_meta[p].candidate or {}).get("order_id", "") or "") != oid]
```
Also, in the pick loops (both the regret branch ~1761 and greedy branch ~1781), skip any `jid` for which `not mutex.insertable(new_meta[jid].candidate or {})` — a rival-group leg must never be chosen once its set is active. Add `from freight_planner.option_mutex import OptionMutex` to the imports.

- [ ] **Step 4: Run to verify it passes**

Run: `$PY -m pytest tests/freight_planner/test_option_mutex_alns.py tests/freight_planner/test_alns.py -v`
Expected: PASS (new test + no regression in existing alns tests).

- [ ] **Step 5: Commit** — `feat(freight): option_group-aware supersede in insertion_pass`

---

## Task 3: Mutex guard in the main ALNS repair loop

`improve_existing_solution` sequences XC→XD via `REQUIRES_PRIOR_PICKUP` but has NO DIRECT-vs-XDOCK mutex. With both groups present it would insert DIR AND the XDOCK bundle. Add a guard so a rival-group leg is never inserted when its set is already active in the working solution.

**Files:**
- Modify: `freight_planner/alns.py` (`improve_existing_solution` repair, the `insert_queue` loop ~1345–1365 and the regret branch ~1289–1324)
- Test: extend `tests/freight_planner/test_option_mutex_alns.py`

- [ ] **Step 1: Write the failing test** — seed a solution containing a DIRECT leg for set `F1`, put the rival XC/XD in the unassigned pool, run `improve_existing_solution` for a few iterations, assert the solution never simultaneously contains DIRECT and XDOCK legs for `F1` (job-conservation + one-group invariant). Read `improve_existing_solution`'s signature (~1583) and the existing `test_alns.py` improvement-test fixtures first.

- [ ] **Step 2: Run to verify it fails**

Run: `$PY -m pytest tests/freight_planner/test_option_mutex_alns.py -v`
Expected: FAIL — both groups can coexist.

- [ ] **Step 3: Implement** — build an `OptionMutex` from the CURRENT working solution at the start of each repair attempt (seed it from the candidates of jobs already in `work`), and in the `insert_queue` loop (~1345) and the regret-repair loop (~1293) skip a `jid` when `not mutex.insertable(meta.candidate)`. When a leg is placed, `mutex.assign(meta.candidate)`. This mirrors the dependency skip already at ~1352. Helper to collect a solution's assigned candidates:
```python
def _assigned_candidates(work, job_meta):
    out = []
    for trips in work.values():
        for t in _as_trips(trips):
            for j in t:
                m = job_meta.get(j.job_id)
                if m is not None:
                    out.append(m.candidate or {})
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `$PY -m pytest tests/freight_planner/test_option_mutex_alns.py tests/freight_planner/test_alns.py -v`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(freight): DIRECT/XDOCK mutex guard in ALNS repair loop`

---

## Task 4: OptionSwap destroy operator

A fourth destroy operator that flips a set's mode by removing its active group and letting repair re-pick under the mutex against the full solution.

**Files:**
- Modify: `freight_planner/alns.py` (`_DESTROY_OPS` ~100, `_active_destroy_ops` validation ~117, the destroy dispatch ~1207, and add `_option_swap_removal`)
- Test: extend `tests/freight_planner/test_option_mutex_alns.py`

- [ ] **Step 1: Write the failing test** — start from a solution where set `F1` is served DIRECT but XDOCK is strictly cheaper given the rest of the routes; force the destroy operator to `option_swap` (via `FP_ALNS_DESTROY_OPS=option_swap`), run enough iterations, assert `F1` ends up served XDOCK (both XC and XD placed) and total cost dropped. Read `_worst_removal`/`_shaw_removal` (~272/~312) and the dispatch (~1207) for the operator signature to mirror.

- [ ] **Step 2: Run to verify it fails**

Run: `FP_ALNS_DESTROY_OPS=option_swap $PY -m pytest tests/freight_planner/test_option_mutex_alns.py -v`
Expected: FAIL — `option_swap` unknown op (raises in `_active_destroy_ops`).

- [ ] **Step 3: Implement**
  - Add `"option_swap"` to `_DESTROY_OPS`.
  - `_option_swap_removal(routes, job_meta, rng, pinned, count)`: pick up to `count` option-set orders that (a) are currently assigned, (b) have NO pinned/committed leg (mode-lock: never swap a departed set), (c) whose sibling group exists in `job_meta`. Return the job_ids of the active group's legs to remove (both XC and XD for an XDOCK set; the DIR leg for a DIRECT set). The existing repair (Task 3, now mutex-cleared for that set once its legs are removed) re-inserts the best group.
  - Wire it into the destroy dispatch (~1207) like the other ops, and into `_AdaptiveOps` weighting (it joins the adaptive pool automatically via `_active_destroy_ops()`).
  - Guard: if a chosen set has any leg in `pinned`, skip it (do not remove) — this is the rolling-horizon mode-lock.

- [ ] **Step 4: Run to verify it passes**

Run:
```
FP_ALNS_DESTROY_OPS=option_swap $PY -m pytest tests/freight_planner/test_option_mutex_alns.py -v
$PY -m pytest tests/freight_planner/test_alns.py -v
```
Expected: PASS both (default op set still works; swap works when forced).

- [ ] **Step 5: Commit** — `feat(freight): OptionSwap ALNS destroy operator (endogenous mode re-pricing)`

---

## Task 5: Seed interleaved choice + mutex

The seed (`run_multiday_seed_plan` → daily `route_seed` greedy) must (a) never place both groups, and (b) prefer the cheaper group by trial insertion against the partial solution. Mutex-safety is required; trial-both is a quality nicety (ALNS refines regardless).

**Files:**
- Modify: `freight_planner/route_seed.py` (the daily greedy insertion loop) and/or `freight_planner/tour_plan.py` (`run_multiday_seed_plan`, the daily hand-off ~304)
- Test: `tests/freight_planner/test_seed_option_choice.py`

- [ ] **Step 1: Write the failing test** — a both-group candidate frame (one same-day set, geometry making XDOCK cheaper) through `run_multiday_seed_plan`; assert the seed's selected plan contains exactly one group for the set, and that a DIRECT-cheaper geometry yields DIRECT. Read `route_seed`'s greedy constructor (the function that iterates daily candidates and calls `try_insert_job`) to find the exact insertion site before writing the assertion helpers.

- [ ] **Step 2: Run to verify it fails**

Run: `$PY -m pytest tests/freight_planner/test_seed_option_choice.py -v`
Expected: FAIL — seed places both groups (double-serve).

- [ ] **Step 3: Implement** — construct an `OptionMutex` at the start of the daily greedy; before committing a leg insertion, check `mutex.insertable(cand)` and skip rival-group legs; on commit, `mutex.assign(cand)`. For trial-both: when the greedy first reaches a set, evaluate the best-insertion delta for the DIRECT leg and for the XDOCK bundle (XC then XD) against the current partial solution and commit the cheaper group; mask the rival via the mutex. Seed the mutex from any already-committed/frozen legs passed into the window (mode-lock). Follow the existing `try_insert_job` usage in `route_seed` for the trial-insertion API.

- [ ] **Step 4: Run to verify it passes**

Run: `$PY -m pytest tests/freight_planner/test_seed_option_choice.py tests/freight_planner/test_route_seed.py -v`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(freight): endogenous DIRECT/XDOCK choice in the seed`

---

## Task 6: Endogenous-choice reporting record

Replace the `resolve_options`-produced `option_choices` with a record derived from the FINAL selected plan.

**Files:**
- Create: `freight_planner/option_report.py` (`endogenous_option_choices(selected_plan_df) -> list[OptionChoice]`, `option_choices_md`)
- Modify: `freight_planner/run_alns.py` (`build_window_inputs` / `emit_outputs`) to build the record after the solve instead of at resolve time
- Test: `tests/freight_planner/test_option_report.py`

- [ ] **Step 1: Write the failing test** — a selected-plan frame with one set served XDOCK (XC+XD rows) and one served DIRECT (DIR row); assert the record reports `{set1: XDOCK, set2: DIRECT}`, the DIRECT/XDOCK counts, and (where marginal costs are available) the per-order cost ratio field is populated.

- [ ] **Step 2: Run to verify it fails**

Run: `$PY -m pytest tests/freight_planner/test_option_report.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement** `endogenous_option_choices`: group selected legs by `option_set`, read the surviving `option_group`, emit one record per set (chosen/rejected/counts, plus realized marginal cost per mode where the plan carries it). Reuse the existing `option_choices_md` output shape so reports render unchanged. Wire the call in `emit_outputs` (or wherever `option_choices` is consumed for reports) to use the selected plan.

- [ ] **Step 4: Run to verify it passes**

Run: `$PY -m pytest tests/freight_planner/test_option_report.py -v`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(freight): endogenous option-choice report from selected plan`

---

## Task 7: Flip the switch — remove the collapse and delete the ρ resolver

Now that the mutex machinery is in place and unit-tested, stop collapsing and delete the static resolver.

**Files:**
- Modify: `freight_planner/run_alns.py` (remove the `resolve_options` DIRECT/XDOCK call ~321; keep `resolve_hub_drop`; set `option_choices` to the endogenous record from Task 6, or `[]` at build time filled post-solve)
- Modify: `freight_planner/options_resolver.py` (delete `resolve_options`, `_cost_choice`, `_window_infeasible`, `_proto_vehicle`, `_route_km`, `DEFAULT_XDOCK_RATIO`, `OptionChoice` if unused elsewhere, `option_choices_md` if superseded by Task 6; KEEP `resolve_hub_drop`, `HubDropChoice`, `hub_drop_choices_md`, `DEFAULT_HUBDROP_RATIO`)
- Modify/delete tests: remove the DIRECT/XDOCK cases in `tests/freight_planner/test_options_resolver.py` and the resolver half of `tests/freight_planner/test_same_day_xdock.py` (KEEP the `staged_delivery_start` staging tests and all hub-drop tests)
- Test: `tests/freight_planner/test_endogenous_xdock_integration.py`

- [ ] **Step 1: Write the failing integration test** — build a window whose candidate frame contains a same-day option set (both groups), run `solve_window` (seed + ALNS), assert: exactly one group per set in the selected plan; no double-delivery (`plan_validation` clean); job-conservation holds. Model the harness on the smallest existing `solve_window`/seed integration test.

- [ ] **Step 2: Run to verify it fails**

Run: `$PY -m pytest tests/freight_planner/test_endogenous_xdock_integration.py -v`
Expected: FAIL — with the collapse still present, only one group is in the frame, so the "both groups offered, one chosen" assertion fails (or the resolver still decides). Then perform the deletion.

- [ ] **Step 3: Remove the collapse + delete the resolver** per the Files list. After deletion, grep to confirm no live references remain:

Run: `$PY -c "import freight_planner.run_alns, freight_planner.run_rolling, freight_planner.run_route_seed"` (must import clean)
Run: `grep -rn "resolve_options\|DEFAULT_XDOCK_RATIO" freight_planner/ | grep -v hub` (expected: no matches)

- [ ] **Step 4: Run to verify it passes**

Run:
```
$PY -m pytest tests/freight_planner/test_endogenous_xdock_integration.py -v
$PY -m pytest tests/freight_planner/ -q
```
Expected: integration test PASS; full suite green (fix any test that referenced the deleted resolver by removing/adapting it per the Files list).

- [ ] **Step 5: Commit** — `feat(freight): remove ρ resolver, endogenous DIRECT/XDOCK choice live`

---

## Task 8: Verify freight initial-state location under both groups

`build_initial_freight_states` dedups by `freight_id` (one state per unit), but with the richer leg set (DIR + XC + XD) confirm the initial LOCATION still resolves to "at origin customer", not XD's `AT_DEPOT` mid-state.

**Files:**
- Test: `tests/freight_planner/test_freight_state_both_groups.py`
- Modify (only if the test fails): `freight_planner/state.py` (`build_initial_freight_states`, ~40–132)

- [ ] **Step 1: Write the test** — legs frame with DIR + XC + XD for one `freight_id`; assert the built initial state's location/ready is the origin customer (matching a DIRECT-only or XC-only frame). Read `state.py:40-132` for the exact state fields to assert.

- [ ] **Step 2: Run**

Run: `$PY -m pytest tests/freight_planner/test_freight_state_both_groups.py -v`
Expected: PASS if already correct (dedup by freight_id); if FAIL, the initial-location derivation is picking up XD's mid-state.

- [ ] **Step 3: Fix only if needed** — make the initial-state location derive from the freight's ORIGIN (pickup/direct origin), ignoring delivery-side `AT_DEPOT` legs. Keep the change minimal.

- [ ] **Step 4: Re-run** the test + `tests/freight_planner/test_state*.py`. Expected: PASS.

- [ ] **Step 5: Commit** — `test(freight): freight initial-state correct with both option groups` (+ fix if applied)

---

## Task 9: Validation run

**Files:**
- Create: `experiments/validate_endogenous_xdock.py` (instrumented run + report)

- [ ] **Step 1: Full suite green**

Run: `$PY -m pytest tests/freight_planner/ -q`
Expected: all pass (≥ prior 1018).

- [ ] **Step 2: Endogenous validation run — Feb 2–8, combined parquet**

Run:
```
$PY -m freight_planner.run_rolling --start 2026-02-02 --end 2026-02-08 \
    --out-dir experiments/out/endogenous_xdock_feb02_08
```
(Combined parquet is the hard-set default; no `--qargo`.)

- [ ] **Step 3: Same-day XDOCK end-to-end instrumentation** — from the run's service/plan ledger, count same-day option sets served XDOCK where BOTH XC and XD committed and the delivery landed on time. Emit to a report. Requirement: zero silent strands (a set whose XC committed but XD never delivered). If any strand, STOP and open a debugging task — do not proceed to interpret the numbers.

- [ ] **Step 4: Report the split + cost-ratio distribution** — DIRECT vs XDOCK counts on the window, served-count, total routed km, and the empirical per-order marginal-cost-ratio distribution (the evidence that a single ρ could not have been right). Sanity-check served-count against recent runs (no coverage regression) and km against the last comparable run.

- [ ] **Step 5: Commit** — `feat(experiments): endogenous XDOCK validation run + report`

---

## Notes for the implementer

- **Read before editing.** Tasks 2–5 modify intricate functions in a ~1900-line `alns.py` and the seed. Each step names the exact function and the nearby line range — read the whole function and its existing tests (`tests/freight_planner/test_alns.py`, `test_route_seed.py`) before changing it, and follow the surrounding patterns (`try_insert_job`, `_best_insert_for_job`, `_ranked_inserts_for_job`, the `pinned`/`_conserve_check` conventions).
- **Conservation.** Run the conservation-checked variants where available: `FP_ALNS_CONSERVE=1` must stay clean across OptionSwap moves.
- **Mode-lock is free but must be seeded.** Committed legs are already `pinned` (no destroy op removes them); the only addition is seeding the per-window `OptionMutex` from committed legs so the rival group is never offered.
- **Multi-day sets are out of scope** — they keep the existing path; the mutex must only engage same-day option sets (both legs same window). Confirm multi-day option sets are not accidentally superseded (they carry `option_set` too — verify the seed/ALNS only trial-swaps sets whose both legs are in the current window).
