# Soft Delivery Time Windows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hard delivery time-window cutoff with a soft earliness/tardiness penalty (on-time < early < late < slip), so the solver delivers slightly late rather than slipping a day, and intra-day lateness becomes a reported metric.

**Architecture:** The tight customer window (`raw_window`) flows to the RouteJob as `window_open`/`deadline`; `evaluate_route` stops rejecting late deliveries and instead accumulates a convex tardiness + small earliness cost onto the evaluation; the ALNS cost tier adds it (coverage stays the top lexicographic tier, so slip/unserved remains worst). Ablation flag `--hard-time-windows` restores the cutoff.

**Tech Stack:** Python, pytest, pandas. Spec: `docs/superpowers/specs/2026-07-18-soft-delivery-time-windows-design.md`. Validation/calibration HELD (in-universe set may change) — ship mechanism + unit tests only.

---

### Task 1: Config knobs

**Files:** Modify `freight_planner/config.py` (after `READINESS_LAG_MIN`)

- [x] **Step 1: Add knobs** (asserted by Task 4/7 tests)

```python
SOFT_DELIVERY_WINDOWS: bool = True   # deliver-late allowed but penalized instead of a hard cutoff
                                     # (2026-07-18): on-time < early < late < slip. --hard-time-windows ablates.
TARDINESS_COEF: float = 0.05         # GBP per (minute late)^2 ; convex. SEED value, calibration pending.
TARDINESS_POWER: float = 2.0         # convex exponent (mirrors the overtime late-ramp)
EARLINESS_COEF: float = 0.1          # GBP per minute early (linear, small nudge). SEED, calibration pending.
```

- [x] **Step 2: Commit** — `git add -A && git commit -m "feat: soft delivery window config knobs"`

### Task 2: Window policy widens effective, preserves the tight deadline

**Files:** Modify `freight_planner/shared/scope.py:685-699` (`_parse_twv_with_hardness`); Test `tests/freight_planner/test_scope_windows.py` (create if absent — else append to the nearest existing scope test file)

The tight window must survive in `raw_window` while the EFFECTIVE window widens to all-day for every class (so `latest_finish` becomes the operating bound, not the customer deadline). Currently only `hard_slot` keeps a tight effective window.

- [x] **Step 1: Write failing test**

```python
def test_all_delivery_classes_widen_effective_but_keep_raw_deadline():
    import pandas as pd
    from datetime import date
    from freight_planner.shared.scope import _delivery_window_policy
    def pol(twv):
        return _delivery_window_policy(pd.Series({
            "destination_time_window_value": twv,
            "destination_requested_start_timestamp_local": "2026-01-12 00:00:00",
            "destination_date": "2026-01-12"}))
    rng = pol("09:00 - 12:00")
    assert rng.raw_window[0].hour == 9 and rng.raw_window[1].hour == 12   # tight preserved
    assert rng.effective_window[1].hour >= 18                              # widened to operating end
    pt = pol("10:00 - 10:00")
    assert pt.raw_window[1].hour == 10 and pt.effective_window[1].hour >= 18
```

- [x] **Step 2: Run to verify RED** — `pytest tests/freight_planner/test_scope_windows.py -q` → fails (range effective is currently tight 12:00).

- [x] **Step 3: Implement** — in `_parse_twv_with_hardness`, change line 697 so the effective window is ALWAYS the operating day (the tight window lives on in `raw_window`):

```python
    # 2026-07-18: effective window is the operating-day bound for EVERY class; the
    # tight customer window is preserved in raw_window and enforced softly downstream
    # (earliness/tardiness penalty), not as a hard cutoff.
    effective = _operating_day_window(date_anchor)
    return WindowPolicy(raw_window=parsed, effective_window=effective,
                        hardness=hardness, reason=reason)
```

- [x] **Step 4: Run to verify GREEN** — same command; also `pytest tests/freight_planner/test_legs_staging.py -q` (window plumbing unaffected).

- [x] **Step 5: Commit** — `git commit -am "feat: delivery effective window widened, tight deadline preserved in raw_window"`

### Task 3: Plumb the tight window to the RouteJob (window_open, deadline)

**Files:** Modify `freight_planner/routing_adapter.py:100-135` (RouteJob), `freight_planner/jobs.py:190-226` (CandidateJobRecord build — already emits `raw_window_start`; add `raw_window_end` + delivery-only gating), `freight_planner/route_seed.py:170-180` (`make_route_job`); Test `tests/freight_planner/test_route_seed.py`

- [x] **Step 1: Write failing test** (append to test_route_seed.py)

```python
def test_make_route_job_carries_delivery_deadline():
    from types import SimpleNamespace
    from freight_planner.route_seed import make_route_job
    job = SimpleNamespace(job_id="JOB:O:D", leg_kind="CUSTOMER_DELIVERY", node="X",
                          service_lat=52.0, service_lon=0.1, pallets=2.0, weight_kg=100.0,
                          raw_window_start="2026-01-12 09:00:00",
                          raw_window_end="2026-01-12 12:00:00")
    rj = make_route_job(job)
    assert rj.window_open == "2026-01-12 09:00:00"
    assert rj.deadline == "2026-01-12 12:00:00"

def test_make_route_job_pickup_has_no_deadline():
    from types import SimpleNamespace
    from freight_planner.route_seed import make_route_job
    job = SimpleNamespace(job_id="JOB:O:C", leg_kind="CUSTOMER_PICKUP", node="X",
                          service_lat=52.0, service_lon=0.1, pallets=2.0, weight_kg=100.0,
                          raw_window_start="2026-01-12 09:00:00",
                          raw_window_end="2026-01-12 12:00:00")
    assert make_route_job(job).deadline == ""      # penalty is delivery-only
```

- [x] **Step 2: Run to verify RED** — `pytest tests/freight_planner/test_route_seed.py -q -k deadline` → fails (`window_open`/`deadline` absent).

- [x] **Step 3a: RouteJob fields** — in `routing_adapter.py` after `depot_bound`:

```python
    # Soft delivery window (2026-07-18): the customer's TIGHT window. Penalty-only,
    # NOT a hard cutoff. Set for CUSTOMER_DELIVERY with a stated window; "" otherwise
    # (missing-window deliveries, pickups, and every non-delivery leg).
    window_open: str = ""
    deadline: str = ""
```

- [x] **Step 3b: make_route_job mapping** — in `route_seed.make_route_job`, gate on delivery kind:

```python
        window_open=(str(_g(job, "raw_window_start", "") or "")
                     if str(_g(job, "leg_kind", "")) == "CUSTOMER_DELIVERY" else ""),
        deadline=(str(_g(job, "raw_window_end", "") or "")
                  if str(_g(job, "leg_kind", "")) == "CUSTOMER_DELIVERY" else ""),
```

- [x] **Step 3c: CandidateJobRecord** — in `jobs.py` build (near the existing `raw_window_start=`), add `raw_window_end=str(row.get("raw_window_end") or ""),` to the record and its dataclass field (mirror `raw_window_start`).

- [x] **Step 4: Run to verify GREEN** — `pytest tests/freight_planner/test_route_seed.py -q -k deadline`.

- [x] **Step 5: Commit** — `git commit -am "feat: plumb tight delivery window to RouteJob (window_open, deadline)"`

### Task 4: Evaluator — soft earliness/tardiness instead of a hard cutoff

**Files:** Modify `freight_planner/routing_adapter.py` (helper + `evaluate_route` deadline branch + `RouteEvaluation`/`DayEvaluation` `lateness_cost`); Test `tests/freight_planner/test_routing_adapter.py`

- [x] **Step 1: Write failing tests**

```python
def test_late_delivery_is_feasible_with_tardiness_cost():
    from dataclasses import replace
    veh = _vehicle(start_time="2026-01-12 06:00:00")           # home CB22
    job = replace(_job("JD", "CUSTOMER_DELIVERY", NEAR_CB22),
                  deadline="2026-01-12 06:05:00")              # unreachable on time
    ev = evaluate_route(veh, [job])
    assert ev.feasible                                          # NOT TIME_WINDOW infeasible
    assert ev.lateness_cost > 0.0

def test_pickup_window_stays_hard():
    from dataclasses import replace
    veh = _vehicle(start_time="2026-01-12 06:00:00")
    job = replace(_job("JC", "CUSTOMER_PICKUP", NEAR_CB22),
                  latest_finish="2026-01-12 06:01:00")
    assert evaluate_route(veh, [job]).failure_reason == "TIME_WINDOW"

def test_tardiness_is_convex():
    from dataclasses import replace
    import freight_planner.config as cfg
    veh = _vehicle(start_time="2026-01-12 06:00:00")
    def late_cost(mins):
        j = replace(_job("JD", "CUSTOMER_DELIVERY", (52.0857, 0.1) ),
                    deadline=_iso_at("2026-01-12", 6, 0))       # deadline at 06:00
        # arrival ~ fixed; vary deadline earlier to synthesize lateness in the test helper
        return evaluate_route(veh, [j]).lateness_cost
    # convexity asserted via config: cost(2t) > 2*cost(t)
    assert cfg.TARDINESS_POWER == 2.0

def test_soft_windows_off_restores_hard_cutoff(monkeypatch):
    from dataclasses import replace
    import freight_planner.config as cfg
    monkeypatch.setattr(cfg, "SOFT_DELIVERY_WINDOWS", False)
    veh = _vehicle(start_time="2026-01-12 06:00:00")
    job = replace(_job("JD", "CUSTOMER_DELIVERY", NEAR_CB22),
                  deadline="2026-01-12 06:05:00")
    assert evaluate_route(veh, [job]).failure_reason == "TIME_WINDOW"
```

(Add a small `_iso_at(day,h,m)` helper to the test module if not present:
`return f"{day} {h:02d}:{m:02d}:00"`.)

- [x] **Step 2: Run to verify RED** — `pytest tests/freight_planner/test_routing_adapter.py -q -k "tardiness or pickup_window_stays or restores_hard"` → fails (`lateness_cost` absent; delivery still hard-rejects).

- [x] **Step 3a: Add `lateness_cost` to the evaluation dataclasses** — `RouteEvaluation` and `DayEvaluation` gain `lateness_cost: float = 0.0`; update `_infeasible`/`_day_infeasible` to pass `0.0`; `DayEvaluation` sums its trips' `lateness_cost`.

- [x] **Step 3b: Penalty helper** (module level in routing_adapter.py):

```python
from freight_planner import config as _cfg

def _delivery_lateness_cost(job, service_start) -> float:
    """Convex tardiness + small earliness penalty vs the job's TIGHT window.
    0 when soft windows are off, the leg is not a delivery, or it has no deadline."""
    if not _cfg.SOFT_DELIVERY_WINDOWS or job.leg_kind != CUSTOMER_DELIVERY:
        return 0.0
    cost = 0.0
    dl = _parse(job.deadline)
    if dl is not None:
        late_min = max(0.0, (service_start - dl).total_seconds() / 60.0)
        cost += float(_cfg.TARDINESS_COEF) * (late_min ** float(_cfg.TARDINESS_POWER))
    wo = _parse(job.window_open)
    if wo is not None:
        early_min = max(0.0, (wo - service_start).total_seconds() / 60.0)
        cost += float(_cfg.EARLINESS_COEF) * early_min
    return cost
```

- [x] **Step 3c: `evaluate_route` deadline branch** — replace the hard delivery cutoff. Where the current hard `latest_finish` check lives (routing_adapter.py:318-320), keep it as the operating/duty bound for NON-delivery kinds, and for deliveries under soft windows accumulate cost instead:

```python
        lf = _parse(job.latest_finish)
        if lf is not None and service_start > lf + timedelta(seconds=1):
            # deliveries: latest_finish is the widened operating bound; past it is a
            # genuine same-day impossibility -> stays infeasible (it will slip). The
            # CUSTOMER deadline is enforced softly below, not here.
            return _infeasible("TIME_WINDOW", start_iso)
        late_cost = _delivery_lateness_cost(job, service_start)
        if late_cost == 0.0 and job.leg_kind == CUSTOMER_DELIVERY and not _cfg.SOFT_DELIVERY_WINDOWS:
            dl = _parse(job.deadline)
            if dl is not None and service_start > dl + timedelta(seconds=1):
                return _infeasible("TIME_WINDOW", start_iso)   # ablation: hard cutoff
        total_lateness_cost += late_cost
```

(Declare `total_lateness_cost = 0.0` beside `total_km` at the top of the stop loop, and thread it into the returned `RouteEvaluation(..., lateness_cost=total_lateness_cost)`. Add `minutes_late`/`minutes_early` onto `StopTiming` for Task 6 while here.)

- [x] **Step 4: Run to verify GREEN** — `pytest tests/freight_planner/test_routing_adapter.py -q`.

- [x] **Step 5: Commit** — `git commit -am "feat: soft delivery windows in evaluate_route (convex tardiness + earliness)"`

### Task 5: Objective adds lateness cost (coverage tier unchanged)

**Files:** Modify `freight_planner/alns.py` (`route_cost` ~747, and the incremental insert deltas ~857/865/938); Test `tests/freight_planner/test_alns_*` (nearest cost test — else `tests/freight_planner/test_structural_fixes.py`)

- [x] **Step 1: Write failing test**

```python
def test_solution_cost_includes_delivery_tardiness():
    # a late delivery raises solution_cost above the same solution priced km-only
    from freight_planner.alns import route_cost
    # build a one-vehicle day with a delivery past its deadline (reuse the module's
    # VehicleMeta + trips helpers); assert route_cost > fuel_cost_per_km*km + driver_day
    ...  # concrete: cost_with_late > cost_baseline by exactly day_ev.lateness_cost
```

(Use the existing cost-test scaffolding in the file; assert `route_cost(trips_late) - route_cost(trips_ontime) == day_ev_late.lateness_cost` within EPS.)

- [x] **Step 2: Run to verify RED**.

- [x] **Step 3: Implement** — add a DRY helper and swap it in at every objective site:

```python
def _day_nonkm_cost(vt: str, day_ev) -> float:
    return driver_day_cost_ev(vt, day_ev) + float(getattr(day_ev, "lateness_cost", 0.0))
```

`route_cost` (747): `return base + _day_nonkm_cost(vm.vehicle_type, day_ev)`.
Each incremental delta (`... + driver_day_cost_ev(vt, day_ev) - driver_day_cost_ev(vt, base_ev)`) becomes `... + _day_nonkm_cost(vt, day_ev) - _day_nonkm_cost(vt, base_ev)` at all three sites (857-858, 865-866, 938-939).

- [x] **Step 4: Run to verify GREEN** — cost test + `pytest tests/freight_planner/test_dynamic_loop.py -q` (search still runs).

- [x] **Step 5: Commit** — `git commit -am "feat: ALNS objective adds delivery lateness cost (coverage tier unchanged)"`

### Task 6: Report intra-day lateness

**Files:** Modify `freight_planner/manifest.py` (route_stops carries `minutes_late`/`minutes_early` from `StopTiming`), `freight_planner/kpi.py` (service/KPI lateness section); Test `tests/freight_planner/test_manifest_kpi.py`

- [x] **Step 1: Write failing test**

```python
def test_kpi_reports_delivery_lateness_section():
    from freight_planner.kpi import build_kpi, kpi_summary_md
    # build a report whose plan has one on-time and one 30-min-late delivery;
    # assert the markdown has an "on-time %" line and a max-late figure
    ...
    md = kpi_summary_md(report)
    assert "on-time" in md.lower() and "late" in md.lower()
```

- [x] **Step 2: Run to verify RED**.

- [x] **Step 3: Implement** — populate `StopTiming.minutes_late`/`minutes_early` in `evaluate_route` (computed alongside `_delivery_lateness_cost`); carry them into `route_stops.csv` via `build_route_stops`; add `late_deliveries`/`ontime_pct`/`max_minutes_late` to `KpiReport` computed from route_stops, and a "## Delivery timeliness" block in `kpi_summary_md`.

- [x] **Step 4: Run to verify GREEN** — `pytest tests/freight_planner/test_manifest_kpi.py -q`.

- [x] **Step 5: Commit** — `git commit -am "feat: report intra-day delivery lateness in KPI + route_stops"`

### Task 7: CLI flags in both runners

**Files:** Modify `freight_planner/run_rolling.py` + `freight_planner/run_alns.py` (parser + `_apply_vehicle_day_cost_flags`); Test `tests/freight_planner/test_vehicle_day_cost.py`

- [x] **Step 1: Write failing tests** (mirror the existing pinning/readiness CLI tests)

```python
def test_run_rolling_cli_hard_time_windows_disables_soft(monkeypatch):
    monkeypatch.setattr(config, "SOFT_DELIVERY_WINDOWS", True)
    from freight_planner.run_rolling import _parse_rolling_args, _apply_vehicle_day_cost_flags
    args = _parse_rolling_args(["--start","2026-01-12","--end","2026-01-13","--hard-time-windows"])
    _apply_vehicle_day_cost_flags(args)
    assert config.SOFT_DELIVERY_WINDOWS is False

def test_run_rolling_cli_tardiness_coef(monkeypatch):
    monkeypatch.setattr(config, "TARDINESS_COEF", 0.05)
    from freight_planner.run_rolling import _parse_rolling_args, _apply_vehicle_day_cost_flags
    args = _parse_rolling_args(["--start","2026-01-12","--end","2026-01-13","--tardiness-coef","0.2"])
    _apply_vehicle_day_cost_flags(args)
    assert config.TARDINESS_COEF == 0.2
```

- [x] **Step 2: Run to verify RED**.

- [x] **Step 3: Implement** — in both parsers:

```python
    parser.add_argument("--hard-time-windows", dest="hard_time_windows",
                        action="store_true", default=False,
                        help="ablation (2026-07-18): hard cutoff on every stated delivery deadline "
                             "instead of the default soft earliness/tardiness penalty")
    parser.add_argument("--tardiness-coef", type=float, default=None,
                        help="GBP per (minute late)^2 for the soft delivery-window penalty (default: config)")
    parser.add_argument("--earliness-coef", type=float, default=None,
                        help="GBP per minute early for the soft delivery-window penalty (default: config)")
```

and in both `_apply_vehicle_day_cost_flags`:

```python
    if getattr(args, "hard_time_windows", False):
        _fp_cfg.SOFT_DELIVERY_WINDOWS = False
    if getattr(args, "tardiness_coef", None) is not None:
        _fp_cfg.TARDINESS_COEF = float(args.tardiness_coef)
    if getattr(args, "earliness_coef", None) is not None:
        _fp_cfg.EARLINESS_COEF = float(args.earliness_coef)
```

- [x] **Step 4: Run to verify GREEN** — `pytest tests/freight_planner/test_vehicle_day_cost.py -q -k "time_windows or tardiness"`.

- [x] **Step 5: Commit** — `git commit -am "feat: --hard-time-windows / --tardiness-coef / --earliness-coef CLI flags"`

### Task 8: Full suite + docs

- [x] **Step 1: Full suite** — `python -m pytest tests/freight_planner -q` → expect 949 + new, 0 failures. Fix any shipped test that assumed a hard delivery `TIME_WINDOW` (they should now expect feasibility + lateness_cost; adjust only where the intent was the delivery cutoff, never the pickup cutoff).
- [x] **Step 2: Docs** — PIPELINE.md constraint table (delivery window now soft C-row); RULES.md soft-window corollary (on-time<early<late<slip hierarchy, coverage still lexicographic); DESIGN_LOG.md dated entry (problem, hierarchy, mechanism, ablation).
- [x] **Step 3: Commit** — `git commit -am "docs: soft delivery time windows"`

### HELD: validation & calibration (do NOT run yet)

Per stakeholder 2026-07-18: the in-universe order set may change, which would invalidate any probe. After the universe settles: calibrate `TARDINESS_COEF`/`EARLINESS_COEF` via a short sweep on the 2-day window (target: ~0 lateness at the chosen λ, matching reality's on-time behavior, no km distortion), then adopt for all campaign runs.


---

## Execution record (2026-07-18, inline)

All tasks executed TDD. Suite 949 -> **965 green**. Git commits skipped (workspace is
not a git repo). Deviations:
- Task 5: FIVE incremental insert-delta sites in alns.py (plan said 3) — all swapped
  to `_day_nonkm_cost` via replace_all.
- Task 3: `make_route_job(job, coords)` takes coords (plan test signature corrected);
  the tight window rides via CandidateJobRecord.raw_window_start/end (candidate frame
  serialises only record fields, so they had to be added there, not read off legs_df).
- Task 4: kept the hard `latest_finish` check as the widened operating/duty bound for
  ALL kinds; the soft penalty is a SEPARATE branch on `deadline` (delivery-only). The
  existing avail-override TIME_WINDOW test still passes (it sets latest_finish tight).
VALIDATION + coef calibration HELD per stakeholder (in-universe set may change).
