# R1+R2 Dynamic Dispatcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans, inline,
> task-by-task, TDD. Standing rules: NO git commits; flag-off bit-identical; **NO RUNS
> (including smoke solves) until the stakeholder clears them** — build to green unit
> tests, then hold.
> Spec: `docs/superpowers/specs/2026-07-10-rolling-horizon-e6-design.md` §4.7a.

**Goal:** the modern dynamic dispatcher: commitment watermarks (stop-level freeze
periods) + insertion into in-flight suffixes + insertion-only micro-passes, with
strict whole-trip freezing surviving as a degenerate config.

**Two load-bearing simplifications (spec §4.7a):**
1. Frozen prefix = a *constraint on mutation positions*, evaluated by re-running the
   whole trip from its original depot departure — zero evaluator changes.
2. Suffix stops pinned to their vehicle (v1) — no cross-vehicle reassignment, no
   onboard-freight teleporting.

---

## File map

| file | action |
|---|---|
| `freight_planner/epoch_state.py` | MODIFY — `Watermarks` model: per-(vid, day, trip) committed index + floor time; advance rule; strict config |
| `freight_planner/alns.py` | MODIFY — watermark-aware destroy (skip committed stops) + repair (position > watermark, arrive ≥ floor) + optional stability penalty |
| `freight_planner/run_rolling.py` | MODIFY — inject in-flight trips into each epoch's initial solution; stop-level record accumulation (watermark deltas); micro-pass loop; retire daily DutyOverride pool path |
| `tests/freight_planner/test_watermarks.py` etc. | CREATE per task |

Execution order: T1 → T2 → T3 → T4 → T5 → T6 (hold before any solve).

### T1 — `Watermarks` model (epoch_state)
- [ ] Failing tests: watermark advance for a trip given stop `planned_depart`s and
  `now + δ_R1` (begun stop counts; untouched trip = index 0); strict config pins to
  trip end at commit; floor time exposed; per-(vid, day, trip) keying; monotone
  (never retreats).
- [ ] Implement `Watermarks` dataclass + `advance(records/route_times, now, delta_r1)`
  + `strict_from_band(...)` (reproduces whole-trip freezing).
- [ ] Suite green.

### T2 — watermark-aware ALNS operators
- [ ] Failing tests (small synthetic solution): destroy never removes a committed
  stop; insertion offers only positions after the watermark; an insertion whose
  arrival lands before the floor is rejected; whole-trip re-evaluation keeps the
  prefix stops' timings identical; stability penalty (when set) makes a marginal
  suffix change unattractive; watermarks=None ⇒ behaviour identical to today
  (bit-identical guard at operator level).
- [ ] Implement: thread `watermarks` (dict) through `improve_existing_solution` →
  removal ops + `try_insert_job` call sites, mirroring how `pinned_job_ids` and
  `avail_overrides` already thread. Position floor via index; time floor via the
  candidate evaluation's arrive at the insertion point.
- [ ] Suite green.

### T3 — run_rolling: injection + stop-level accumulation
- [ ] Failing tests (toy): an in-flight trip enters the next epoch's initial solution
  with its watermark; newly committed stop records accumulate append-only exactly
  once (watermark delta), never duplicated across epochs; a suffix insertion at
  epoch k appears in the merged plan attributed to its vehicle's existing trip;
  ledger/collected-day stamps derive from watermark commits.
- [ ] Implement: replace whole-trip `select_frozen`/exclusion for daily work with
  watermark bookkeeping; keep tours (whole-freeze) and trunk (day-close) and slip
  machinery (promise-fixed) unchanged; retire the daily dispatch-floor/DutyOverride
  path (in-flight trips carry their own duty via whole-trip evaluation; trunk
  next-day + handover holds keep DutyOverride).
- [ ] Suite green.

### T4 — strict-config equivalence gate (unit-level, no real solve)
- [ ] Test: on a synthetic 2-epoch window, running the new machinery with the strict
  config (watermark = trip end at commit) reproduces the old whole-trip freeze
  decisions (same committed records, same served set).

### T5 — R2 micro-passes
- [ ] Failing tests: between anchors, a newly revealed order is inserted into an open
  suffix by a repair-only pass (no destroy ops invoked); cadence configurable;
  micro-pass respects watermarks + δ_R1; ledger marks it served same-day.
- [ ] Implement `micro_pass(...)` in run_rolling: visibility at simulated time t_m,
  repair-only call (`iterations=0`-style pure insertion path or `repair_every=1`
  with destroy disabled), watermark advance before each pass.
- [ ] Suite green.

### T6 — HOLD
- [ ] All units green; no smoke, no window solves. Report to stakeholder for run
  clearance; on clearance: smoke @200/epoch, verify tells (Mon day-close trunk ≈
  E1's 8 trips; post-noon order served same afternoon via micro-pass), re-cut
  provenance patch, then the full run.
