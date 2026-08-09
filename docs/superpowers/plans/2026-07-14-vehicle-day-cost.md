# Vehicle-Day Activation Cost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-vehicle-day driver activation cost (a paid 8h floor + overtime to 13h) to the dispatcher's objective and micro-insertion ranking, so the solver reuses an already-working vehicle instead of opening a fresh one for a small job — flag-gated, default OFF (byte-identical).

**Architecture:** One shared helper `driver_day_cost(vehicle_type, duty_hours)` in `vehicle_cost.py`, called from all five cost sites in `alns.py` (three whole-solution sites + two insertion-delta sites). Duty hours come from `DayEvaluation.day_start`/`day_end`. Coverage is protected by the existing lexicographic serve-first acceptance, so the cost can only re-rank equal-coverage solutions. The greedy seed is left untouched.

**Tech Stack:** Python, pytest. No git commits (`e:\BEAT` is not a git repo) — use a full-suite run as each task's checkpoint instead of a commit.

**Spec:** `docs/superpowers/specs/2026-07-14-vehicle-day-cost-design.md`

**Standing constraints:** TDD (failing test first, watch it fail, minimal implementation). Flag-off / disabled path must be byte-identical (hard gate). Seed (`route_seed.py:369`) untouched.

---

## File Structure

- **`freight_planner/config.py`** — add two module constants (the enable flag default + guaranteed-shift hours default).
- **`freight_planner/vehicle_cost.py`** — add the driver hourly-rate table and the `driver_day_cost` / `driver_hourly_gbp` / enable / hours helpers (mirrors the existing `fuel_cost_per_km` structure). References the config **module** at call-time so runtime/CLI toggles take effect.
- **`freight_planner/alns.py`** — add `_duty_hours(day_ev)` helper; wire `driver_day_cost` into `route_cost`, `changed_costs`, `route_cost_by_key` init, `_ranked_inserts_for_job`, `_best_insert_for_job`.
- **`freight_planner/run_rolling.py`, `freight_planner/run_alns.py`** — CLI flags that set the config knobs at runtime.
- **Tests:** `tests/freight_planner/test_vehicle_cost.py` (extend — rates/helper), `tests/freight_planner/test_vehicle_day_cost.py` (new — model + integration + coverage invariant).
- **Docs (Task 10):** `README_DYNAMIC.md`, `PIPELINE.md`, `DESIGN_LOG.md` — with number provenance.

---

## Task 1: Config knobs

**Files:**
- Modify: `freight_planner/config.py` (near `OUT_OF_AREA_KM_FACTOR`, line 48)

- [ ] **Step 1: Add the two constants**

Add after the `OUT_OF_AREA_KM_FACTOR` line in `freight_planner/config.py`:

```python
# --- Driver-day activation cost (vehicle-day fixed cost) -----------------------
# The optimizer's objective is otherwise fuel-per-km only, so it has no reason to
# prefer reusing an already-working vehicle over opening a fresh one for a small
# job. This models the marginal cost of activating one more DRIVER for the day.
# Depreciation/standing cost is deliberately excluded (it is sunk: incurred whether
# the vehicle is driven or parked). Default OFF => objective byte-identical.
VEHICLE_DAY_COST_ENABLED: bool = False
# Guaranteed paid minimum shift (hours). Drivers are paid at least this per active
# day regardless of load, so it is the FLOOR of the driver-day cost; hours worked
# beyond it (up to the 13h duty cap) are paid as overtime. See vehicle_cost.py.
GUARANTEED_SHIFT_HOURS: float = 8.0
```

- [ ] **Step 2: Verify import works**

Run: `python -c "from freight_planner import config; print(config.VEHICLE_DAY_COST_ENABLED, config.GUARANTEED_SHIFT_HOURS)"`
Expected: `False 8.0`

- [ ] **Step 3: Checkpoint** — no test yet (pure constants; exercised by Task 2). Proceed.

---

## Task 2: Driver rate table + `driver_day_cost` helper

**Files:**
- Modify: `freight_planner/vehicle_cost.py`
- Test: `tests/freight_planner/test_vehicle_cost.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/freight_planner/test_vehicle_cost.py`:

```python
from freight_planner.vehicle_cost import driver_hourly_gbp, driver_day_cost
from freight_planner import config


def test_driver_hourly_rates_are_the_declared_jigsaw_rates():
    # profitability_report/vehicle_cost_rates.json -> driving_hourly_gbp
    assert driver_hourly_gbp("tractor") == 47.59
    assert driver_hourly_gbp("rigid") == 40.97
    assert driver_hourly_gbp("van") == 40.97


def test_driver_hourly_unknown_type_falls_back_to_rigid():
    assert driver_hourly_gbp("") == driver_hourly_gbp("rigid")
    assert driver_hourly_gbp("articulated") == driver_hourly_gbp("rigid")


def test_driver_day_cost_zero_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", False)
    assert driver_day_cost("tractor", 10.0) == 0.0
    assert driver_day_cost("rigid", 2.0) == 0.0


def test_driver_day_cost_charges_the_guaranteed_floor(monkeypatch):
    # A short day still costs a full guaranteed shift (the activation cost).
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", True)
    monkeypatch.setattr(config, "GUARANTEED_SHIFT_HOURS", 8.0)
    assert driver_day_cost("rigid", 2.0) == 8.0 * 40.97
    assert driver_day_cost("tractor", 0.5) == 8.0 * 47.59


def test_driver_day_cost_charges_overtime_above_the_floor(monkeypatch):
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", True)
    monkeypatch.setattr(config, "GUARANTEED_SHIFT_HOURS", 8.0)
    assert driver_day_cost("rigid", 10.0) == 10.0 * 40.97   # variable overtime


def test_driver_day_cost_zero_for_empty_day(monkeypatch):
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", True)
    assert driver_day_cost("rigid", 0.0) == 0.0


def test_vehicle_day_cost_env_overrides_config(monkeypatch):
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", False)
    monkeypatch.setenv("FREIGHT_VEHICLE_DAY_COST", "1")
    assert driver_day_cost("rigid", 2.0) == 8.0 * 40.97
    monkeypatch.setenv("FREIGHT_VEHICLE_DAY_COST", "0")
    assert driver_day_cost("rigid", 2.0) == 0.0


def test_guaranteed_hours_env_override(monkeypatch):
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", True)
    monkeypatch.setenv("FREIGHT_GUARANTEED_SHIFT_HOURS", "9")
    assert driver_day_cost("rigid", 3.0) == 9.0 * 40.97
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/freight_planner/test_vehicle_cost.py -q`
Expected: FAIL with `ImportError: cannot import name 'driver_hourly_gbp'`.

- [ ] **Step 3: Implement the helper**

In `freight_planner/vehicle_cost.py`, change the config import (line 20) from the value-bound form to a **module** reference so runtime toggles are seen, and add the driver-cost block. Replace:

```python
from freight_planner.config import OUT_OF_AREA_KM_FACTOR
```

with:

```python
from freight_planner import config as _config
from freight_planner.config import OUT_OF_AREA_KM_FACTOR
```

Then append at the end of the module:

```python
# --- Driver-day activation cost ----------------------------------------------
# Declared driver rates from profitability_report/vehicle_cost_rates.json
# (`driving_hourly_gbp`). Tractor drivers are paid more than rigid/van drivers.
# NB: this is the DRIVER cost only. The £70/day standing cost in that file is
# depreciation (incurred parked or driven = sunk) and is deliberately NOT used
# here — putting it in the objective would penalize USING an owned vehicle.
DRIVER_GBP_PER_HOUR: dict[str, float] = {
    "tractor": 47.59,
    "rigid": 40.97,
    "van": 40.97,
}
DEFAULT_DRIVER_GBP_PER_HOUR: float = DRIVER_GBP_PER_HOUR["rigid"]

_TRUE = {"1", "true", "yes"}
_FALSE = {"0", "false", "no"}


def driver_hourly_gbp(vehicle_type: str) -> float:
    """GBP per on-duty hour for a driver of ``vehicle_type`` (case-insensitive,
    rigid fallback)."""
    return DRIVER_GBP_PER_HOUR.get(
        str(vehicle_type or "").strip().lower(), DEFAULT_DRIVER_GBP_PER_HOUR)


def vehicle_day_cost_enabled() -> bool:
    """Is the driver-day activation cost active? Env ``FREIGHT_VEHICLE_DAY_COST``
    overrides the ``config.VEHICLE_DAY_COST_ENABLED`` default (read at call-time
    so CLI/runtime toggles take effect)."""
    env = os.environ.get("FREIGHT_VEHICLE_DAY_COST", "").strip().lower()
    if env in _TRUE:
        return True
    if env in _FALSE:
        return False
    return bool(_config.VEHICLE_DAY_COST_ENABLED)


def guaranteed_shift_hours() -> float:
    env = os.environ.get("FREIGHT_GUARANTEED_SHIFT_HOURS", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return float(_config.GUARANTEED_SHIFT_HOURS)


def driver_day_cost(vehicle_type: str, duty_hours: float) -> float:
    """GBP labour cost of activating one vehicle-day: a paid guaranteed-shift
    floor plus overtime for hours worked beyond it (the 13h duty ceiling is a
    hard feasibility constraint elsewhere, so ``duty_hours`` is already bounded).

        cost = hourly[type] * max(guaranteed_hours, duty_hours)

    Returns 0.0 when disabled (=> objective byte-identical) or for an empty
    vehicle-day (``duty_hours <= 0``: the floor applies only when occupied)."""
    if not vehicle_day_cost_enabled():
        return 0.0
    if duty_hours <= 0.0:
        return 0.0
    return driver_hourly_gbp(vehicle_type) * max(guaranteed_shift_hours(), float(duty_hours))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/freight_planner/test_vehicle_cost.py -q`
Expected: PASS (all, including the pre-existing fuel tests).

- [ ] **Step 5: Checkpoint** — run `python -m pytest tests/freight_planner/test_vehicle_cost.py -q`; all green.

---

## Task 3: `_duty_hours` helper in `alns.py`

**Files:**
- Modify: `freight_planner/alns.py`
- Test: `tests/freight_planner/test_vehicle_day_cost.py` (new)

- [ ] **Step 1: Write failing test**

Create `tests/freight_planner/test_vehicle_day_cost.py`:

```python
from __future__ import annotations

from freight_planner.alns import _duty_hours


class _Ev:
    def __init__(self, start, end):
        self.day_start = start
        self.day_end = end


def test_duty_hours_from_day_span():
    assert _duty_hours(_Ev("2026-01-12 06:00:00", "2026-01-12 16:00:00")) == 10.0


def test_duty_hours_zero_for_empty_or_degenerate():
    assert _duty_hours(_Ev("", "")) == 0.0
    assert _duty_hours(_Ev("2026-01-12 06:00:00", "2026-01-12 06:00:00")) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/freight_planner/test_vehicle_day_cost.py -q`
Expected: FAIL with `ImportError: cannot import name '_duty_hours'`.

- [ ] **Step 3: Implement `_duty_hours`**

In `freight_planner/alns.py`, add `driver_day_cost` to the vehicle_cost import (line 51):

```python
from freight_planner.vehicle_cost import driver_day_cost, fuel_cost_per_km, out_of_area_penalty_km
```

Add this helper just above `route_km` (before line 672):

```python
def _duty_hours(day_ev) -> float:
    """On-duty span (hours) of an evaluated vehicle-day, from its start/end
    stamps. 0.0 for an empty or degenerate day. This is the paid shift length
    fed to ``driver_day_cost``."""
    start = getattr(day_ev, "day_start", "") or ""
    end = getattr(day_ev, "day_end", "") or ""
    if not start or not end:
        return 0.0
    try:
        a = datetime.fromisoformat(str(start))
        b = datetime.fromisoformat(str(end))
    except ValueError:
        return 0.0
    return max(0.0, (b - a).total_seconds() / 3600.0)
```

(`datetime` is already imported in `alns.py`, used by `_time_of`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/freight_planner/test_vehicle_day_cost.py -q`
Expected: PASS.

- [ ] **Step 5: Checkpoint** — tests green.

---

## Task 4: Wire `route_cost` / `solution_cost`

**Files:**
- Modify: `freight_planner/alns.py` (`route_cost`, line 701)
- Test: `tests/freight_planner/test_vehicle_day_cost.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/freight_planner/test_vehicle_day_cost.py`:

```python
import pytest
from freight_planner import config
from freight_planner.alns import route_cost, VehicleMeta, RouteJob


def _vm(vehicle_type="rigid"):
    return VehicleMeta(
        vehicle_id="V1", home_depot="D", lat=52.2, lon=0.1,
        capacity_pallets=26, capacity_kg=26000, vehicle_type=vehicle_type,
        available_from="", shift_end="", catchment_km=0.0,
        median_trips_per_day=1, multi_trip_share=0.0,
    )


def _job(job_id="J1", lat=52.3, lon=0.2):
    # A single delivery ~11 km from the depot -> short duty, well under 8h.
    return RouteJob(
        job_id=job_id, kind="delivery", node="N", lat=lat, lon=lon,
        pallets=2, weight_kg=1000, service_minutes=15,
        earliest="", latest="", order_id=job_id, leg_id=job_id,
    )


def test_route_cost_byte_identical_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", False)
    trips = [[_job()]]
    before = route_cost(trips, _vm(), "2026-01-12")
    # Same call, flag still off -> deterministic and unchanged.
    assert route_cost(trips, _vm(), "2026-01-12") == before
    # Fuel-only: equals fuel_rate * km (no driver term).
    from freight_planner.alns import route_km, _trips_penalty_km
    from freight_planner.vehicle_cost import fuel_cost_per_km
    vm = _vm()
    assert before == fuel_cost_per_km(vm.vehicle_type) * (
        route_km(trips, vm, "2026-01-12") + _trips_penalty_km(trips, vm))


def test_route_cost_adds_one_driver_floor_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", True)
    monkeypatch.setattr(config, "GUARANTEED_SHIFT_HOURS", 8.0)
    vm = _vm()
    trips = [[_job()]]
    from freight_planner.alns import route_km, _trips_penalty_km, _duty_hours
    from freight_planner.vehicle_cost import fuel_cost_per_km, driver_day_cost
    from freight_planner.routing_adapter import evaluate_day
    from freight_planner.alns import _route_vehicle
    ev = evaluate_day(_route_vehicle(vm, "2026-01-12"), trips)
    expected = fuel_cost_per_km(vm.vehicle_type) * (
        route_km(trips, vm, "2026-01-12") + _trips_penalty_km(trips, vm)
    ) + driver_day_cost(vm.vehicle_type, _duty_hours(ev))
    assert route_cost(trips, vm, "2026-01-12") == pytest.approx(expected)
    # And the driver term is exactly the 8h floor for this short day.
    assert driver_day_cost(vm.vehicle_type, _duty_hours(ev)) == 8.0 * 40.97


def test_route_cost_empty_is_zero(monkeypatch):
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", True)
    assert route_cost([], _vm(), "2026-01-12") == 0.0
```

> NOTE for the implementer: confirm the exact `VehicleMeta` / `RouteJob` constructor field names against `alns.py` (near lines 380–470 and the `_build_vehicle_meta` mapping at 571+) before running — adjust the `_vm` / `_job` factories to match. The behavioral assertions are the contract; the factory kwargs are plumbing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/freight_planner/test_vehicle_day_cost.py -q -k route_cost`
Expected: FAIL — `test_route_cost_adds_one_driver_floor_when_enabled` fails (no driver term yet); the disabled test passes.

- [ ] **Step 3: Implement**

Replace `route_cost` (`freight_planner/alns.py:701-708`):

```python
def route_cost(trips, vm: VehicleMeta, day: str) -> float:
    """Per-type generalized cost (GBP) of a route = fuel_cost_per_km(type) x
    (road-km + out-of-area phantom km), plus the driver-day activation cost when
    enabled (a guaranteed-shift floor + overtime; 0.0 when disabled).

    This is the optimizer's objective unit. Reported plan distance stays physical
    km (see ``_route_totals_from_solution``); only the search ranks on cost."""
    base = fuel_cost_per_km(vm.vehicle_type) * (
        route_km(trips, vm, day) + _trips_penalty_km(trips, vm))
    normalised = _as_trips(trips)
    if not normalised:
        return base
    day_ev = evaluate_day(_route_vehicle(vm, day), normalised)
    return base + driver_day_cost(vm.vehicle_type, _duty_hours(day_ev))
```

(When disabled, `driver_day_cost` returns 0.0 → `base` unchanged → byte-identical. `solution_cost` sums `route_cost` and inherits the term automatically.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/freight_planner/test_vehicle_day_cost.py -q -k route_cost`
Expected: PASS.

- [ ] **Step 5: Checkpoint** — run `python -m pytest tests/freight_planner/test_vehicle_day_cost.py tests/freight_planner/test_route_costs.py -q`; green.

---

## Task 5: Wire the ALNS improve loop (`changed_costs` + `route_cost_by_key`)

**Files:**
- Modify: `freight_planner/alns.py` (`changed_costs` ~989; `route_cost_by_key` init ~1012)
- Test: `tests/freight_planner/test_vehicle_day_cost.py`

- [ ] **Step 1: Write failing test (behavioral: consolidation, coverage held)**

Append a test that runs `improve_existing_solution` (or the seed→improve path used by `test_alns.py`) on a small two-vehicle universe where one vehicle already has a trip and a second small job can either extend it (staying <8h) or open the idle vehicle. Assert: with the flag ON the job lands on the already-used vehicle (fewer occupied vehicle-days) and the served set is unchanged vs OFF.

```python
def test_improve_consolidates_without_losing_coverage(monkeypatch):
    # Build the smallest solution that exposes fresh-vs-reuse, reusing the
    # fixtures in test_alns.py (import its builders or replicate a 2-vehicle,
    # 2-job case). Assert served set identical ON vs OFF, and occupied
    # vehicle-days ON <= OFF.
    ...
```

> IMPLEMENTER: model this test on the existing `tests/freight_planner/test_alns.py` construction helpers (seed + `improve_existing_solution`/`improve_route_seed`). The two invariants to assert: (a) `set(served ON) == set(served OFF)`; (b) count of occupied `(vid, day)` keys `ON <= OFF`. Keep it deterministic (fixed `rng_seed`, small `iterations`).

- [ ] **Step 2: Run to verify it fails** (job goes to fresh vehicle with flag on, or the term isn't applied). Run: `python -m pytest tests/freight_planner/test_vehicle_day_cost.py -q -k consolidat` — Expected: FAIL.

- [ ] **Step 3: Implement — `changed_costs`**

In `changed_costs` (`alns.py:1004-1006`), add the driver term. Replace:

```python
            vm = vehicle_meta[key[0]]
            out[key] = fuel_cost_per_km(vm.vehicle_type) * (
                ev.total_km + _trips_penalty_km(tt, vm))
```

with:

```python
            vm = vehicle_meta[key[0]]
            out[key] = fuel_cost_per_km(vm.vehicle_type) * (
                ev.total_km + _trips_penalty_km(tt, vm)
            ) + driver_day_cost(vm.vehicle_type, _duty_hours(ev))
```

- [ ] **Step 4: Implement — `route_cost_by_key` init**

In the init loop (`alns.py:1012-1019`), add the driver term for occupied keys. Replace:

```python
    for (vid, day), trips in routes.items():
        if vid in vehicle_meta:
            k_phys = km(trips, vid, day)
            phys_km_before += k_phys
            route_cost_by_key[(vid, day)] = fuel_cost_per_km(vehicle_meta[vid].vehicle_type) * (
                k_phys + _trips_penalty_km(trips, vehicle_meta[vid]))
        else:
            route_cost_by_key[(vid, day)] = 0.0
```

with:

```python
    for (vid, day), trips in routes.items():
        if vid in vehicle_meta:
            k_phys = km(trips, vid, day)
            phys_km_before += k_phys
            cost = fuel_cost_per_km(vehicle_meta[vid].vehicle_type) * (
                k_phys + _trips_penalty_km(trips, vehicle_meta[vid]))
            tt = _as_trips(trips)
            if tt and vehicle_day_cost_enabled():
                ev0 = evaluate_day(rv(vid, day), tt, detail=False)
                cost += driver_day_cost(vehicle_meta[vid].vehicle_type, _duty_hours(ev0))
            route_cost_by_key[(vid, day)] = cost
        else:
            route_cost_by_key[(vid, day)] = 0.0
```

Add `vehicle_day_cost_enabled` to the import at `alns.py:51`:

```python
from freight_planner.vehicle_cost import (
    driver_day_cost, fuel_cost_per_km, out_of_area_penalty_km, vehicle_day_cost_enabled,
)
```

(The `enabled` guard here avoids an extra `evaluate_day` per key when disabled, keeping the disabled path both byte-identical AND zero-overhead. `rv` is the closure already defined in `improve_existing_solution`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/freight_planner/test_vehicle_day_cost.py tests/freight_planner/test_alns.py -q`
Expected: PASS (consolidation test green; existing alns tests unaffected with flag default off).

- [ ] **Step 6: Checkpoint** — green.

---

## Task 6: Wire the micro-insertion delta sites

**Files:**
- Modify: `freight_planner/alns.py` (`_ranked_inserts_for_job` 809/814; `_best_insert_for_job` 876/884 + eject-fallback branch below 890)
- Test: `tests/freight_planner/test_vehicle_day_cost.py`

- [ ] **Step 1: Write failing test (micro-pass reuse)**

Append a test that calls `insertion_pass` directly with a current solution containing a returning vehicle (idle at depot, prior trip done) and a fresh vehicle at the same depot, plus one new small job. Assert: flag ON → the job is inserted onto the **returning** vehicle's key; flag OFF → current behavior (record the OFF placement, assert ON differs toward reuse). Also assert no job is dropped (`failed == []`) in both.

```python
def test_micro_insertion_prefers_reuse_when_enabled(monkeypatch):
    from freight_planner.alns import insertion_pass
    # ... build solution {(V_used, day): [prior_trip]} and vehicle_meta for
    # V_used and V_fresh (same depot). new_meta = {J: meta(small job)}.
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", False)
    off_sol, off_ins, off_fail = insertion_pass(sol, new_meta, vmeta)
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", True)
    on_sol, on_ins, on_fail = insertion_pass(sol, new_meta, vmeta)
    assert off_fail == [] and on_fail == []            # coverage held both ways
    # With the cost on, the job attaches to the already-used vehicle-day.
    assert any(k[0] == "V_used" for k in on_sol if on_sol[k] != sol.get(k))
```

> IMPLEMENTER: reuse `JobMeta`/`VehicleMeta` builders from `test_alns.py`. Keep the two vehicles at the same depot so km ties and the driver floor is the deciding term.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/freight_planner/test_vehicle_day_cost.py -q -k micro`
Expected: FAIL (ON picks the fresh vehicle, same as OFF — no driver term in the delta yet).

- [ ] **Step 3: Implement — `_ranked_inserts_for_job`**

Add a duty-capturing base and thread the driver delta. In `_ranked_inserts_for_job`, the base is computed at `alns.py:791` (`base_km = day_km(vid, day, current_trips)`). Immediately after it, add:

```python
            base_duty = _duty_hours(evaluate_day(rv(vid, day), current_trips, detail=False)) if current_trips else 0.0
            vt = vehicle_meta[vid].vehicle_type
```

Then at the two `cands.append(...)` deltas (`alns.py:809` and `:814`), add the driver delta. Replace each:

```python
                cands.append((rate * (day_ev.total_km - base_km + pen), key, candidate_trips))
```

with:

```python
                cands.append((rate * (day_ev.total_km - base_km + pen)
                              + driver_day_cost(vt, _duty_hours(day_ev))
                              - driver_day_cost(vt, base_duty), key, candidate_trips))
```

- [ ] **Step 4: Implement — `_best_insert_for_job`**

Same shape. After `base_km = day_km(vid, day, current_trips)` (`alns.py:857`) add:

```python
            base_duty = _duty_hours(evaluate_day(rv(vid, day), current_trips, detail=False)) if current_trips else 0.0
            vt = vehicle_meta[vid].vehicle_type
```

At the three delta computations — `:876`, `:884`, and the eject-fallback branch below line 890 (find the identical `delta = rate * (day_ev.total_km - base_km + pen)` lines) — replace each:

```python
                delta = rate * (day_ev.total_km - base_km + pen)
```

with:

```python
                delta = (rate * (day_ev.total_km - base_km + pen)
                         + driver_day_cost(vt, _duty_hours(day_ev))
                         - driver_day_cost(vt, base_duty))
```

> The eject-fallback loop (starts ~890) recomputes `base_km` for its own `current_trips`; add the matching `base_duty`/`vt` capture there too, right after its `base_km` assignment, before reusing the replacement. When disabled, both `driver_day_cost` calls return 0.0 → delta arithmetic is `+0.0-0.0` → byte-identical ranking.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/freight_planner/test_vehicle_day_cost.py -q -k micro`
Expected: PASS.

- [ ] **Step 6: Checkpoint** — run `python -m pytest tests/freight_planner/test_vehicle_day_cost.py tests/freight_planner/test_alns.py tests/freight_planner/test_watermark_alns.py -q`; green.

---

## Task 7: CLI wiring

**Files:**
- Modify: `freight_planner/run_rolling.py` (arg parser ~1427; apply before solve)
- Modify: `freight_planner/run_alns.py` (arg parser ~756; apply before solve)

- [ ] **Step 1: Write failing test**

Append to `tests/freight_planner/test_vehicle_day_cost.py`:

```python
def test_run_rolling_cli_sets_config(monkeypatch):
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", False)
    from freight_planner.run_rolling import _parse_rolling_args, _apply_vehicle_day_cost_flags
    args = _parse_rolling_args([
        "--start", "2026-01-12", "--end", "2026-01-13",
        "--vehicle-day-cost", "--guaranteed-shift-hours", "9",
    ])
    _apply_vehicle_day_cost_flags(args)
    assert config.VEHICLE_DAY_COST_ENABLED is True
    assert config.GUARANTEED_SHIFT_HOURS == 9.0
```

> IMPLEMENTER: confirm the parser entrypoint name (`_parse_rolling_args` per the module) and reuse it; if the parser is inline in `main`, extract the two lines into `_apply_vehicle_day_cost_flags(args)` so it is unit-testable.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/freight_planner/test_vehicle_day_cost.py -q -k cli`
Expected: FAIL (`ImportError` / unknown arg).

- [ ] **Step 3: Implement**

In both `run_rolling.py` and `run_alns.py` arg parsers, add:

```python
    parser.add_argument("--vehicle-day-cost", action="store_true",
                        help="add the per-vehicle-day driver activation cost to the objective "
                             "(guaranteed-shift floor + overtime); default off")
    parser.add_argument("--guaranteed-shift-hours", type=float, default=None,
                        help="paid minimum shift hours (floor of the driver-day cost); default 8.0")
```

Add a shared applier (in `run_rolling.py`, and call the same pattern in `run_alns.py`):

```python
def _apply_vehicle_day_cost_flags(args) -> None:
    if getattr(args, "vehicle_day_cost", False):
        _fp_cfg.VEHICLE_DAY_COST_ENABLED = True
    if getattr(args, "guaranteed_shift_hours", None) is not None:
        _fp_cfg.GUARANTEED_SHIFT_HOURS = float(args.guaranteed_shift_hours)
```

Call `_apply_vehicle_day_cost_flags(args)` in each `main()` immediately after arg parsing, before building/solving. (`run_rolling.py` already imports `config as _fp_cfg`; `run_alns.py` imports `config` — use its existing alias.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/freight_planner/test_vehicle_day_cost.py -q -k cli`
Expected: PASS.

- [ ] **Step 5: Checkpoint** — green.

---

## Task 8: Coverage-invariant + full regression

**Files:**
- Test: `tests/freight_planner/test_vehicle_day_cost.py`

- [ ] **Step 1: Write the coverage-invariant test**

```python
def test_coverage_unchanged_when_only_fresh_vehicle_is_feasible(monkeypatch):
    # A job whose ONLY eligible/feasible placement is a fresh vehicle must still
    # be served with the cost ON (lexicographic serve-first dominates cost).
    monkeypatch.setattr(config, "VEHICLE_DAY_COST_ENABLED", True)
    # build solution where the sole reuse candidate is duty-infeasible ->
    # insertion_pass must open the fresh vehicle and return failed == [].
    ...
    assert on_fail == []
```

- [ ] **Step 2: Run to verify it fails (until built as designed) then passes.** Run `-k coverage`.

- [ ] **Step 3: Run the FULL suite with the flag at its default (OFF)**

Run: `python -m pytest tests/ -q`
Expected: PASS with the **same** counts as before this branch (flag default off ⇒ byte-identical). Investigate any diff before proceeding.

- [ ] **Step 4: Manual byte-identity spot-check**

Run a tiny rolling window twice with the flag off (once on `main`-equivalent behavior, once on the branch) and diff `plan_full.csv`:

Run: `python -m freight_planner.run_rolling --start 2026-01-12 --end 2026-01-13 --iterations 200` (flag omitted = off)
Expected: output identical to a pre-change run of the same command (spot-check `plan_full.csv` bytes / a KPI line).

- [ ] **Step 5: Checkpoint** — full suite green; flag-off identity confirmed.

---

## Task 9: Behavioral validation run (informs default, not a gate)

**Files:** none (analysis)

- [ ] **Step 1: Run a real window OFF vs ON**

```bash
python -m freight_planner.run_rolling --start 2026-01-12 --end 2026-01-18            # OFF
python -m freight_planner.run_rolling --start 2026-01-12 --end 2026-01-18 --vehicle-day-cost   # ON
```

- [ ] **Step 2: Compare** occupied vehicle-days (expect ↓), unassignment rate (expect **no rise**), total km & generalized cost (expect fewer/fuller vehicles). Record the numbers for the docs (Task 10) and for the eventual default-on decision. Keep the flag **default OFF** in code regardless.

---

## Task 10: Documentation with number provenance

**Files:**
- Modify: `freight_planner/README_DYNAMIC.md` (objective/mechanisms section)
- Modify: `freight_planner/PIPELINE.md` (cost-model / config knobs)
- Modify: `freight_planner/DESIGN_LOG.md` (decision record)

- [ ] **Step 1: README_DYNAMIC.md** — add a "Vehicle-day activation cost" subsection under the objective/cost discussion. Cover: the problem (fuel-only objective opens fresh vehicles); the model `hourly[type] × max(8h, duty)`; why coverage is safe (lexicographic serve-first); the flag (`--vehicle-day-cost`, `FREIGHT_VEHICLE_DAY_COST`, default off) and `--guaranteed-shift-hours`. **Provenance block (required):**

```
Where the numbers come from:
- Driver hourly £47.59 tractor / £40.97 rigid & van  = profitability_report/
  vehicle_cost_rates.json -> `driving_hourly_gbp` (declared rates v2.1).
- Guaranteed shift 8h  = standard paid minimum day; configurable via
  --guaranteed-shift-hours / FREIGHT_GUARANTEED_SHIFT_HOURS (confirmed with ops 2026-07-14).
- 13h ceiling  = existing duty (`SHIFT`) feasibility cap in routing_adapter, not a price.
- The £70/day standing cost in the same file is DEPRECIATION (sunk: incurred whether
  the vehicle is driven or parked) and is DELIBERATELY EXCLUDED from the objective —
  putting it in would penalize using an owned vehicle.
- Fuel £0.319/£0.216/£0.150 per km  = measured Jan-2026 Jigsaw tank-to-tank (unchanged).
```
Include the OFF-vs-ON validation numbers from Task 9.

- [ ] **Step 2: PIPELINE.md** — in the cost-model / config-knob area, add rows for `VEHICLE_DAY_COST_ENABLED` (default False), `GUARANTEED_SHIFT_HOURS` (default 8.0), the env vars, and the CLI flags; note the five wired sites (objective + micro-insertion) and that the seed's `+10000` spread heuristic is intentionally separate.

- [ ] **Step 3: DESIGN_LOG.md** — one dated entry: the decision (per-vehicle-day driver activation cost, floor+overtime), the historical-failure reconciliation (old scalar penalty vs current lexicographic), depreciation-excluded rationale, and the seed-left-untouched rationale.

- [ ] **Step 4: Verify** the docs reference real symbols (grep the flag names / file paths cited actually exist) and the provenance block matches `vehicle_cost_rates.json`.

- [ ] **Step 5: Checkpoint** — docs updated; `python -m pytest tests/ -q` still green.

---

## Self-Review Notes

- **Spec coverage:** model (Tasks 2–3), all five sites (4–6), flag/config (1,2,7), coverage invariant (5,6,8), seed untouched (no task changes `route_seed.py`), validation (9), docs+provenance (10). ✓
- **Types:** `driver_day_cost(vehicle_type, duty_hours) -> float`, `_duty_hours(day_ev) -> float`, `vehicle_day_cost_enabled() -> bool` used consistently across tasks. ✓
- **Byte-identity:** disabled path proven at unit level (Task 4/6 disabled tests), suite level (Task 8.3), and run level (Task 8.4). ✓
- **Placeholder scan:** the two `...` test bodies (Tasks 5, 6, 8) are explicitly delegated with the invariants to assert spelled out, because they must reuse `test_alns.py` fixtures the implementer has in front of them; every production-code step has complete code. ✓
