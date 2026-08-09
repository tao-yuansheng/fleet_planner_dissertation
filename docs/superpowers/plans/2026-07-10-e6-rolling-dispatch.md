# E6 Rolling-Horizon Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans, inline, task-by-task, TDD.
> Standing rules: **NO git commits** (checkpoints instead), all changes restorable, flag-off paths
> bit-identical. Spec: `docs/superpowers/specs/2026-07-10-rolling-horizon-e6-design.md`.

**Goal:** A non-anticipative rolling dispatcher (`run_rolling.py`) on the existing pipeline:
solver sees only orders booked by each epoch, departed trips freeze, slipped orders carry with
aging, tours/shuttle/trunk/handover/emission all keep working.

**Architecture:** Per-epoch restricted solves through an extracted `solve_window()`; frozen trips
accumulate as `SelectedPlanRecord`s + route_totals/route_times; one final emission through the
unchanged `emit` path. Duty honesty via new `RouteVehicle` carry fields threaded like
`avail_overrides`.

**Tech stack:** existing freight_planner modules, pandas, pytest.

**Key measured constants (spec §2):** epochs 03:00/08:00/12:00; δ=60 min; multi-slip with aging;
shuttle bins exempt from visibility.

---

## File map

| file | action |
|---|---|
| `freight_planner/visibility.py` | CREATE — visibility mask + shuttle exemption |
| `freight_planner/epoch_state.py` | CREATE — grid, freeze selection, duty carry, slip pool, ledger |
| `freight_planner/run_rolling.py` | CREATE — CLI, epoch loop, accumulate, merged emission |
| `freight_planner/routing_adapter.py` | MODIFY — `RouteVehicle` gains `drive_since_break0`, `max_drive_minutes_cap`; `evaluate_day`/`evaluate_route` honor them |
| `freight_planner/alns.py` | MODIFY — `_rv_ov` + threading `duty_overrides`; `_priority_key` aging term |
| `freight_planner/route_seed.py` | MODIFY — RouteVehicle build honors duty override; `_priority_key` aging |
| `freight_planner/tour_plan.py` | MODIFY — `run_multiday_seed_plan(external_reserved=, extra_avail_overrides=, duty_overrides=)` |
| `freight_planner/run_alns.py` | MODIFY — extract `build_window_inputs()` / `solve_window()` / `emit_outputs()`; `main()` recomposes them |
| `freight_planner/tests/test_visibility.py` etc. | CREATE tests per task |
| `freight_planner/experiments/E6_rolling/` | CREATE — README, provenance patch, runner |

Execution order: T0 (baseline, background) → T1 → T2 → T3 → T4 (gate) → T5 → T6 (gate) → T7 (launch).

---

### Task 0: Refactor baseline capture (background)

- [ ] Run current `run_alns` at N=300 on the gate window into a scratch dir (background, ~4 min):
  `python -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-17 --handover-in freight_planner/runs/2026-01/2026-01-05_to_2026-01-10/plan/handover.json --iterations 300 --time-budget 100000 --no-improve 100000 --seed 0 --out-dir <scratch>/gate_before`
- [ ] Note: gate artifacts = `selected_plan_alns.csv`, `route_stops.csv` (byte-compare after T4).

### Task 1: `visibility.py`

- [ ] **Failing tests** `tests/test_visibility.py`:
  - collection (`PL_EXPORT`) created 10:00 is invisible at 08:00 epoch, visible at 12:00 epoch
  - `FULL_FLEET` follows the collection rule; `LOCAL_COLLECT` too
  - delivery (`PL_IMPORT`) with `destination_date` D is invisible at D−1 12:00, visible at D−1 18:00 and later
  - unknown/blank flow follows the collection rule (conservative)
  - shuttle-exempt order ids are visible regardless of creation time
  - missing `timestamp_created` ⇒ visible (cannot gate on absent evidence; conservative toward E1 behaviour, counted and logged)
- [ ] **Implement**:

```python
"""E6 order-visibility: what the dispatcher may know at a decision epoch.

Collections (PL_EXPORT / LOCAL_COLLECT / FULL_FLEET and unknown flows) are visible
once their commercial order exists: timestamp_created <= as_of.
Deliveries (PL_IMPORT / LOCAL_DELIVER) are visible from 18:00 the evening before
their service day (freight lands at depot/hub overnight; booking is not our
knowledge point). origin_timestamp is never consulted (51.7% placeholder).
Shuttle-exempt orders (standing scheduled capacity, K1) bypass the gate.
"""
COLLECT_FLOWS = {"PL_EXPORT", "LOCAL_COLLECT", "FULL_FLEET"}
DELIVER_FLOWS = {"PL_IMPORT", "LOCAL_DELIVER"}
DELIVERY_REVEAL_HOUR = 18  # D-1 evening

def visible_order_ids(order_meta: pd.DataFrame, as_of: datetime,
                      exempt_order_ids: set[str] | None = None) -> set[str]:
    """order_meta columns: order_id, flow, created (datetime64), service_day
    (datetime64, delivery day for deliver-flows). Returns visible order_id set."""
```
  Vectorized: deliver-mask ⇒ `as_of >= service_day - 1d @18:00`; everything else ⇒
  `created <= as_of` (NaT created ⇒ True); union exempt.
- [ ] Helper `build_order_meta(qargo_df, demand_df) -> pd.DataFrame` joining
  `timestamp_created` (tz-naive) + `destination_date` onto demand's `order_id`/flow column
  (`corrected_flow` if present else `flow`).
- [ ] Shuttle exemption `shuttle_exempt_order_ids(candidate_df, options, vehicles) -> set[str]`:
  run `shuttle.detect_shuttle_bins` on the full-window candidate frame, map bin `job_ids` →
  candidate `order_id`s. (Standing service: bins that qualify under full knowledge.)
- [ ] Run tests → green. **548-suite spot**: `pytest freight_planner/tests -x -q` still green.

### Task 2: duty carry in `routing_adapter.py`

- [ ] **Failing tests** `tests/test_duty_carry.py`:
  - `RouteVehicle(drive_since_break0=0.0, max_drive_minutes_cap=None)` defaults ⇒ `evaluate_day`
    output equal (all fields) to pre-change golden for a 2-trip synthetic day
  - `drive_since_break0=260` (4.33 h) ⇒ first HGV leg of the day triggers a statutory break where
    the default run has none
  - `max_drive_minutes_cap=90` on a day needing 120 drive-min ⇒ `DRIVING_CAP` infeasible
- [ ] **Implement**: add to `RouteVehicle` (routing_adapter.py:~58):
  `drive_since_break0: float = 0.0` and `max_drive_minutes_cap: float | None = None`.
  In `evaluate_day`: `carry = float(getattr(vehicle, "drive_since_break0", 0.0) or 0.0)`;
  effective cap `min(max_drive_minutes, vehicle.max_drive_minutes_cap)` when set.
  In `evaluate_route` direct path: default `drive_since_break` arg now seeded from the vehicle
  field **only via evaluate_day** (single-trip callers keep the explicit arg; no behaviour change).
- [ ] Tests green; suite green.

### Task 3: threading — `duty_overrides`, `external_reserved`, aging priority

- [ ] **Failing tests** `tests/test_epoch_threading.py`:
  - `improve_route_seed(..., duty_overrides={(vid, day): (260.0, 90.0)})` produces a day evaluation
    whose first trip breaks earlier / rejects on `DRIVING_CAP` vs `duty_overrides=None`
  - `duty_overrides=None` ⇒ result object equal to pre-change run (same seed) — bit-identical guard
  - `run_multiday_seed_plan(..., external_reserved={(vid, day)})` never assigns that vehicle-day
    (daily or tour)
  - candidate frame with `slip_priority=1` on one job ⇒ that job is seeded first
    (`_priority_key` leading term `-slip_priority`); absent column ⇒ ordering unchanged
- [ ] **Implement**:
  - `alns.py`: `_rv_ov(meta, day, avail_overrides, duty_overrides=None)` sets the two new
    RouteVehicle fields; add `duty_overrides` param to the ~8 helpers that already take
    `avail_overrides` (lines ≈616/671/772/1170/1195/1218 + internal calls 972/1032) and to
    `improve_route_seed`/`improve_existing_solution`.
  - `route_seed.py`: RouteVehicle build (:122–138) reads `duty_overrides.get((vid, day))`;
    `_priority_key` gains leading `-slip_priority` (0.0 default via `_g`).
  - `alns.py` `_priority_key` (repair ordering): same leading term.
  - `tour_plan.py` `run_multiday_seed_plan(..., external_reserved=None, extra_avail_overrides=None,
    duty_overrides=None)`: union `external_reserved` into `reserved` before tour selection
    (flows into daily-seed exclusion automatically at :417–421); merge `extra_avail_overrides`
    with trunk's; pass `duty_overrides` through to the daily seed.
  - `run_alns.py` ALNS call passes `duty_overrides=None` explicitly (no behaviour change).
- [ ] Tests green; suite green.

### Task 4: `solve_window()` extraction + bit-identical gate

- [ ] **Shape** (run_alns.py):

```python
@dataclass
class SolveConfig:      # every current CLI arg, plus rolling hooks (all default-off)
    start: date; end: date; qargo: str; postcode_cache: str; out_dir: str
    handover_in: str | None; date_basis: str; responsibility_mode: str
    iterations: int; time_budget: float; no_improve: int; seed: int; restarts: int
    log_every: int; router: str; sa_temp: float; sa_cooling: float
    consolidate_tours: bool; repair_every: int; regret_repair: bool; day_flex: bool
    # rolling hooks (None = exact current behaviour)
    visible_order_ids: set[str] | None = None
    external_reserved: set[tuple[str, str]] | None = None
    extra_avail_overrides: dict[tuple[str, str], str] | None = None
    duty_overrides: dict[tuple[str, str], tuple[float, float]] | None = None
    slip_priority: dict[str, int] | None = None   # order_id -> slip days
    emit: bool = True                              # rolling: solve only, emit later

def build_window_inputs(cfg, runlog) -> WindowInputs   # the current "build inputs" stage
def solve_window(cfg, inputs, runlog) -> SolveResult   # seed + trunk + ALNS stages
def emit_outputs(cfg, inputs, result, dirs, runlog)    # the current "write outputs" stage
def main(argv) -> int                                  # parse -> cfg -> the three, as today
```
  - `WindowInputs`: qargo_df, postcode_cache, demand_df, legs_all_df, legs_df, vehicle_df,
    candidate_df, candidate_all, compatibility_df, freight_states_df, option/hub-drop choices,
    osrm_router+pairs_before, handover, catchment. `SolveResult`: seed, imp, trunk_plan,
    tour_records, tour_km, combined_avail_overrides.
  - Hook application inside `build_window_inputs`: after the handover exclusion, if
    `cfg.visible_order_ids is not None` filter `legs_df`/`demand_df` by order_id (same idiom as
    delivered-exclusion); attach `slip_priority` column to candidate frames (default 0).
  - Hook application inside `solve_window`: pass `external_reserved`/`extra_avail_overrides`/
    `duty_overrides` into `run_multiday_seed_plan`; merge into the ALNS `avail_overrides`/
    `duty_overrides`/`excluded_vehicle_days` exactly where handover/trunk overrides merge today.
  - **Pure code motion otherwise** — no reordering, no renamed locals inside moved blocks.
- [ ] Move-only steps: (1) cut "build inputs" block into `build_window_inputs`; (2) cut
  seed/trunk/ALNS stages into `solve_window`; (3) cut "write outputs" into `emit_outputs`;
  (4) `main()` calls the three; run the suite after each step.
- [ ] **Gate**: rerun T0 command with post-refactor code into `<scratch>/gate_after`;
  `python -c` byte-compare `selected_plan_alns.csv` + `route_stops.csv` (allow only the
  `created_at` manifest line to differ). MUST be identical; stop and fix on any diff.
- [ ] Full suite green.

### Task 5: `epoch_state.py`

- [ ] **Failing tests** `tests/test_epoch_state.py` (synthetic 2-vehicle, 2-day fixtures):
  - `epoch_grid(win_start, win_end)` yields 3/day (03:00, 08:00, 12:00) × operating days, ordered
  - `commit_band(epoch)` = `[epoch+δ, next_epoch+δ)`; last epoch of day D ⇒ band ends next day's
    first epoch + δ; Saturday 12:00 ⇒ band end = +∞ (window close)
  - `select_frozen(imp.solution, imp.selected, route_times, band)` freezes exactly the trips with
    depot depart inside the band: returns frozen records, frozen `(vid, day)` full-day set (all
    trips frozen), partial-vehicle info (some trips free)
  - partial vehicle: `duty_after_freeze(vehicle_meta, frozen_trips)` re-evaluates the frozen
    prefix via `evaluate_day` and returns `(available_from=day_end, drive_since_break0=
    end_drive_since_break, drive_minutes_left=cap−total_drive)`; a vehicle returning after
    `shift_end` ⇒ excluded for the day
  - tours: a `TourAssignment` whose first depot depart is in-band freezes whole; its span
    `(vid, d)` pairs are reserved
  - slip pool: `roll_day_end(visible_targets_today, served_ids)` moves unserved to
    `pool[oid] += 1` with reason from the epoch's rejected/unassigned map (`NO_EPOCH` when the
    order was never visible today, i.e. booked after the day's last epoch); Saturday ⇒ `UNSERVED`
  - ledger rows: ON_TIME (service_date == origin_date), SLIPPED(n) with n = day gap, UNSERVED;
    `SHUTTLE_STANDING` tag carried for exempt orders
- [ ] **Implement** `epoch_state.py`:

```python
EPOCH_TIMES = (time(3, 0), time(8, 0), time(12, 0))
DELTA_MIN = 60

@dataclass
class EpochPlan:            # one epoch's accepted commitments
    epoch: datetime
    frozen_records: list    # SelectedPlanRecord (daily trips + whole tours)
    frozen_route_totals: dict
    frozen_route_times: dict
    served_order_ids: set[str]
    reserved: set[tuple[str, str]]          # full-day freezes incl. tour spans
    avail_overrides: dict[tuple[str, str], str]   # partial vehicles + trunk next-day
    duty_overrides: dict[tuple[str, str], tuple[float, float]]
    trunk_nights: list      # tonight's trunk when this is the day's last epoch

class RollingState:         # accumulates EpochPlans; owns slip pool + ledger
    def visible_for(self, epoch, order_meta, exempt) -> set[str]   # visibility minus served
    def apply(self, epoch_plan) -> None
    def roll_day_end(self, day, visible_today, served_today, reasons) -> None
    def final_combined_selected(self) -> list
    def ledger_frame(self) -> pd.DataFrame
    def manifest_rows(self) -> list[dict]
```
  `select_frozen` keys `imp.selected` records by `(vehicle_id, service_date, trip_index)`;
  trip depart from `route_times[f"{route_id}#T{k}"]` (fallback `route_times[route_id]` for
  single-trip days). `duty_after_freeze` calls `routing_adapter.evaluate_day` on the frozen
  trip lists with the epoch's vehicle meta (exact same evaluator ⇒ no drift).
- [ ] Tests green; suite green.

### Task 6: `run_rolling.py`

- [ ] **Failing test** `tests/test_run_rolling_toy.py`: monkeypatched mini `solve_window` (two
  scripted epochs) ⇒ a post-cut-off order slips exactly one day tagged `NO_EPOCH`; a frozen
  trip's records appear exactly once in the merged plan; SLIPPED(2) after two misses; slipped
  order carries `slip_priority` next day; churn metric counts one reassignment across snapshots.
- [ ] **Implement** epoch loop:

```python
def main(argv):  # --start --end --qargo --handover-in --iterations --seed --out-dir
                 # --epochs 03:00,08:00,12:00 --delta-min 60 --epoch-iterations (default =
                 # --iterations) --trace-dir
    base_cfg = SolveConfig(...)                     # emit=False
    inputs0 = build_window_inputs(base_cfg, runlog) # ONCE: qargo, geocode, catchment, OSRM warm
    exempt = shuttle_exempt_order_ids(inputs0.candidate_all, ...)
    order_meta = build_order_meta(inputs0.qargo_df, inputs0.demand_df)
    state = RollingState(...)
    for epoch in epoch_grid(start, end):
        os.environ["FP_ALNS_TRACE"] = str(trace_dir / f"trace_epoch_{epoch:%m%d_%H%M}.csv")
        vis = state.visible_for(epoch, order_meta, exempt)
        cfg = replace(base_cfg, visible_order_ids=vis,
                      external_reserved=state.reserved_upto(epoch),
                      extra_avail_overrides=state.avail_overrides_for(epoch),
                      duty_overrides=state.duty_overrides_for(epoch),
                      slip_priority=state.slip_priority_map())
        inputs = refilter_inputs(inputs0, cfg)      # cheap: order-id filters + veh patch only
        result = solve_window(cfg, inputs, runlog)
        state.apply(freeze(result, commit_band(epoch), vehicle_meta))
        state.snapshot_for_churn(result)            # stability metric
        if epoch is last_of_day: state.roll_day_end(...)
        manifest.append(epoch row: visible, frozen trips, committed, wall_s, alns iters)
    final = merge(state)                            # combined_selected, totals, times, trunk
    emit_outputs(final_cfg, inputs0, final, dirs, runlog)   # UNMODIFIED emission path
    write rolling_manifest.json, service_ledger.csv, churn.csv
```
  `refilter_inputs` re-derives `legs_df/demand_df/candidate_df/compatibility_df/freight_states`
  from the cached full frames by order-id mask + vehicle patches (NOT a full rebuild; OSRM/geocode
  /catchment reused). Final `emit_outputs` gets full-window demand/legs (coverage judged against
  the whole universe) and rejected = last-epoch remaining + UNSERVED ledger entries.
- [ ] Tests green; suite green.
- [ ] **Integration smoke** (~20 min): window 2026-01-12, seed 0, `--epoch-iterations 200`.
  Assert: completes; `temporal_violations == 0`; every frozen order appears once; ledger sums to
  collection universe; 18 trace files exist; `viz_app --validate` and `month_summary` read the
  plan dir without error.

### Task 7: E6 experiment + full run

- [ ] `freight_planner/experiments/E6_rolling/README.md`: purpose, THIRD provenance epoch, grid/δ,
  pre-registered predictions (682 orders ≈ 6.9% first-attempt slip; NO_EPOCH-dominated; Δkm>0),
  run inventory.
- [ ] Provenance: `git diff 21564100 -- BackEnd/logistics > freight_planner/experiments/code_snapshot_rolling.patch`
  (read-only git); parent SHA + date into `experiments/PROVENANCE.md`.
- [ ] Launch (background, orphan-safe single script, sequential epochs — they are causally chained):
  window **2026-01-12..17** (E1 comparable: 10k × s0/s1/s2 exist), seed 0, 10k per epoch, traces on.
  Expected ~7–10 h. Monitor first two epochs then report.
- [ ] Deliverable to user: run started + how convergence will be read (per-epoch traces).

## Verification checklist
- 548-suite green after every task; bit-identical gate after T4 (byte-diff)
- toy rolling semantics tested before any real solve
- no git commits anywhere; all new/modified files listed in E6 README for restorability
