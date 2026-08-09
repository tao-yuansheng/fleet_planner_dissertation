# Experiment Instrumentation Batch (E0 prerequisites) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans, inline, NO git
> commits. Restore mechanism: `freight_planner/experiments/code_snapshots/` (pre-copies +
> RESTORE.md + post diff). Every toggle ENV-gated, DEFAULT-OFF; all-defaults must stay
> bit-identical (checkpoint fingerprint 16,908@200 / 14,583@1000, accepted 167/707).

**Goal:** the Ch.5 experiment plan's instrumentation: anytime-trace CSV + four E3 ablation
toggles + env provenance in the manifest — all experiment-only, tracked, restorable.

**Files:** modify `freight_planner/alns.py`, `freight_planner/run_alns.py`;
create `tests/freight_planner/test_alns_toggles.py`. Snapshots already taken.

### Task 1: env helpers + removal band (`FP_ALNS_REMOVAL_MIN/MAX`, default 2/5)
- `_removal_band()` module fn; loop uses `rng.randint(k_lo, min(k_hi, len(job_ids)))`
  with (k_lo, k_hi) read ONCE at improve_solution start. Defaults reproduce the exact
  original expression (same rng consumption).
- Tests: default (2,5); env (3,8); min>max -> ValueError.

### Task 2: destroy-op restriction (`FP_ALNS_DESTROY_OPS`, comma list, default all)
- `_active_destroy_ops()` -> tuple; validates subset of {random,worst,shaw}, ValueError
  otherwise; `ops = _AdaptiveOps(_active_destroy_ops(), rng)`.
- Tests: default = ("random","worst","shaw"); "worst" -> ("worst",); junk -> ValueError.

### Task 3: uniform weights (`FP_ALNS_UNIFORM_WEIGHTS=1`)
- `_AdaptiveOps.__init__` reads env -> `self.adaptive`; `update_weights()` resets scores
  but leaves weights untouched when not adaptive.
- Tests: with env, weights stay 1.0 after rewards+update; without, they move.

### Task 4: RRT acceptance (`FP_ALNS_ACCEPT=rrt`, `FP_ALNS_RRT_DEVIATION` default 0.02)
- Acceptance site restructured with IDENTICAL rng consumption in default sa mode:
  `if not accept and delta > 0: (rrt -> threshold on best_total*(1+dev)) elif temp>0 -> rng`.
- Tests: `_read_accept_env()` parsing; rrt smoke via tiny improve_solution run (accepts
  a worse-but-within-deviation move; runs clean).

### Task 5: anytime trace (`FP_ALNS_TRACE=<csv path>`)
- At the on_progress checkpoint site (same log_every cadence): append
  `elapsed_s,iteration,accepted,cost,best_cost,served`. Header once. File opened lazily,
  closed at loop end. Zero writes when env unset.
- Tests: tiny improve_solution with env tmp path + log_every=1 -> file has header + rows,
  cost column numeric.

### Task 6: manifest env provenance (run_alns.py)
- `_env_toggles()` -> {k: v for FP_ALNS* + FREIGHT_FUEL_UNIFORM}; added to
  write_run_manifest dict as "env_toggles".
- Test: direct call with monkeypatched env.

### Task 7: verification
- Full suite green.
- Fingerprint: all-defaults 1000-iter run reproduces 16,908@200 / 14,583@1000,
  accepted 167/707 (bit-identical proof).
- Write `experiments/code_snapshots/instrumentation.diff` (pre vs post).
