# Overtime + fairness cost — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline).
> Spec: docs/superpowers/specs/2026-07-16-overtime-fairness-cost-design.md

**Goal:** price late/overtime work convexly (ramp past 19:00 + payroll OT beyond the 9h
floor) so the solver spreads evening work across active vehicles, and make the 13h duty
cap chain-aware (split-shift) so held evening trips on morning vehicles are legal.

**Architecture:** all cost math in `vehicle_cost.py` (new `driver_day_cost_ev(vtype,
day_ev)` computing chains/working-hours/late-ramp from `day_ev.trip_evaluations`
route_start/route_end — present even at detail=False); feasibility in
`routing_adapter.evaluate_day` (per-chain 13h duty cap, NEW — the old bound was the
deleted shift wall); alns objective sites swap `driver_day_cost(vt, _duty_hours(ev))` →
`driver_day_cost_ev(vt, ev)`.

**Tech stack:** pure Python, pytest, existing config-flag autouse reset fixture.

---

### Task 1: config knobs
**Files:** Modify `freight_planner/config.py`; test `tests/freight_planner/test_vehicle_day_cost.py`
- [ ] RED: test knobs exist w/ defaults (`OVERTIME_COST_ENABLED True`,
      `OT_DUTY_MULTIPLIER 1.5`, `LATE_PREMIUM_START_HOUR 19.0`, `LATE_PREMIUM_BASE 0.5`,
      `LATE_RAMP_PER_HOUR 0.25`, `SPLIT_SHIFT_GAP_H 3.0`)
- [ ] GREEN: add block under the driver-day-cost section with rationale comments.

### Task 2: chain + working-hours + late-ramp math (vehicle_cost.py)
**Files:** Modify `freight_planner/vehicle_cost.py`; test `test_vehicle_day_cost.py`
- [ ] RED tests against a stub day_ev (SimpleNamespace with trip_evaluations of
      route_start/route_end, day_start/day_end):
      * `_chain_spans`: 30-min gap = one chain; 4h gap = two chains; empty = [].
      * `working_hours`: sum of chain spans (gap excluded); equals span when one chain.
      * `late_premium_gbp`: trip 18:00→20:00 → hourly×(0.5·1+0.125·1²)=0.625·hourly;
        19:00→21:00 → 1.5·hourly; entirely before 19:00 → 0; two vehicles 1h each
        (19-20) sum 1.125·hourly < one vehicle 2h (19-21) 1.5·hourly (the user probe).
      * `driver_day_cost_ev`: OVERTIME off → equals old driver_day_cost(span); on →
        base(max(9,wh)) + 0.5·hourly·max(0,wh−9) + late premium; disabled
        vehicle-day-cost → 0.0.
- [ ] GREEN: implement `overtime_cost_enabled()` (env `FREIGHT_OVERTIME_COST`),
      `span_hours(day_ev)` (old _duty_hours logic), `_chain_spans`, `working_hours`,
      `late_premium_gbp`, `driver_day_cost_ev`. Closed-form ramp integral per trip:
      `t0=max(0,(start−19:00)h)`, `t1=max(0,(end−19:00)h)`,
      `hourly·(BASE·(t1−t0) + RAMP/2·(t1²−t0²))`.

### Task 3: per-chain 13h duty cap (routing_adapter.evaluate_day)
**Files:** Modify `freight_planner/routing_adapter.py`; test `tests/freight_planner/test_routing_adapter.py`
- [ ] RED: a single 14h-span chain (windowed stops forcing long waits… simplest: two
      stops with earliest_start far apart in ONE trip) → infeasible `DUTY_CAP`; two
      chains (morning trip + ≥3h gap via depart_floor + evening trip) each ≤13h,
      whole-day span 14h → FEASIBLE; driving cap still whole-day.
- [ ] GREEN: track per-chain start (first trip's ev.route_start); on inter-trip gap
      ≥ `SPLIT_SHIFT_GAP_H` close chain and check span ≤ `MAX_DUTY_H_PER_DAY`; check
      final chain after loop. Failure reason `DUTY_CAP`.

### Task 4: wire the objective sites (alns.py)
**Files:** Modify `freight_planner/alns.py`; tests `test_micro_pass.py` behavioral
- [ ] RED: insertion-level user probe — two floored evening jobs, two vehicles both
      already active past floor, equal geometry → each vehicle gets one (not both on
      one); reuse-over-fresh test still green.
- [ ] GREEN: `route_cost` + the 3 enumerators (ranked/best/third) + `changed_costs`
      pricing sites use `driver_day_cost_ev(vt, day_ev)`; base side keeps the base
      evaluation object (`base_ev`) instead of `base_duty` float. Remove `_duty_hours`
      or delegate to `vehicle_cost.span_hours`.

### Task 5: ablation flag plumbing + full suite
- [ ] `--no-overtime-cost` CLI on run_alns + run_rolling (mirroring
      `--no-vehicle-day-cost` → env), help text.
- [ ] Full suite; fix fallout (tests asserting exact objective values may need the
      flag off or updated expectations — judge each: behavior tests keep semantics,
      arithmetic tests update).

### Task 6: docs + memory
- [ ] RULES.md C5 (per-chain duty + overtime cost paragraph), PIPELINE.md (objective
      formula + C4 row + config table), DESIGN_LOG.md dated entry, config reference.
- [ ] Memory: new `overtime-fairness-cost.md` + MEMORY.md index line; link
      [[operating-window-dewire]].
