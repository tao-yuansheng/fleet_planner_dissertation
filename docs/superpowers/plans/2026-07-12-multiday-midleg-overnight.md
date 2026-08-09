# Multi-day Mid-leg Overnight (Approach A) Implementation Plan

> **For agentic workers:** TDD, bite-sized. Standing rules: **NO git commits** — each
> task ends at a checkpoint (run tests) instead of a commit. `MULTIDAY_MIDLEG_OVERNIGHT`
> default OFF must keep `evaluate_tour` byte-identical (regression gate).

**Goal:** End a multi-day tour-day part-way along the current leg (not back at the last
stop), carrying an interpolated overnight location + freight aboard + a fresh daily
budget into the next day, behind a default-OFF flag.

**Architecture:** All changes in `freight_planner/tours.py` + one flag in
`freight_planner/config.py`. Two pure helpers (`_drive_fits`, `_interp_latlon`), a
`DayStart` record on `TourEvaluation`, and a flag-gated mid-leg split branch inside
`evaluate_tour` (single-point legs + the return leg; two-point legs unchanged).

**Tech stack:** Python, pytest. Spec: `docs/superpowers/specs/2026-07-12-multiday-midleg-overnight-design.md`.

---

### Task 1: Config flag

**Files:** Modify `freight_planner/config.py`; Test `tests/freight_planner/test_tours.py`

- [ ] **Step 1 — failing test**

```python
def test_midleg_flag_defaults_off():
    from freight_planner import config
    assert config.MULTIDAY_MIDLEG_OVERNIGHT is False
```

- [ ] **Step 2 — run, expect FAIL** (`AttributeError`).
- [ ] **Step 3 — implement:** add under the Tour-formation block in `config.py`:

```python
MULTIDAY_MIDLEG_OVERNIGHT: bool = False  # A: end a tour-day part-way along the leg (carry overnight coord); OFF = park at last stop (byte-identical)
```

- [ ] **Step 4 — run, expect PASS.** Checkpoint.

---

### Task 2: `_drive_fits` helper

Max drive-minutes that fit today under **both** the drive-cap room and the duty-cap
room (duty absorbs the statutory break owed for driving that much).

**Files:** Modify `freight_planner/tours.py`; Test `tests/freight_planner/test_tours.py`

- [ ] **Step 1 — failing tests**

```python
from freight_planner.tours import _drive_fits

def test_drive_fits_van_is_min_of_rooms():
    assert _drive_fits(0.0, 180.0, 300.0, hgv=False) == 180.0   # drive-bound
    assert _drive_fits(0.0, 400.0, 250.0, hgv=False) == 250.0   # duty-bound

def test_drive_fits_hgv_drive_bound_when_duty_slack():
    # 180 drive from a fresh accumulator owes no break (<270); duty has room -> 180
    assert _drive_fits(0.0, 180.0, 780.0, hgv=True) == 180.0

def test_drive_fits_hgv_duty_bound_absorbs_break():
    # duty room 300; driving x plus its break must fit. Near the 270 boundary a 45-min
    # break is owed, so x is pushed below the naive 300.
    x = _drive_fits(0.0, 600.0, 300.0, hgv=True)
    assert x < 300.0
    from freight_planner.route_costs import statutory_breaks
    assert x + statutory_breaks(0.0, x)[0] <= 300.0 + 1e-6
```

- [ ] **Step 2 — run, expect FAIL** (`ImportError`).
- [ ] **Step 3 — implement** (near the other primitives in `tours.py`):

```python
def _drive_fits(drive_since_break: float, drive_room: float, duty_room: float,
                hgv: bool) -> float:
    """Max drive-minutes today under both the drive-cap room and the duty-cap room.
    Duty must also absorb the statutory break owed while driving that much."""
    hi = min(float(drive_room), float(duty_room))
    if hi <= 0.0:
        return 0.0
    if not hgv:
        return hi
    lo = 0.0
    for _ in range(40):                       # bisect the monotone x + breaks(since, x)
        mid = 0.5 * (lo + hi)
        if mid + statutory_breaks(drive_since_break, mid)[0] <= duty_room + _EPS:
            lo = mid
        else:
            hi = mid
    return lo
```

- [ ] **Step 4 — run, expect PASS.** Checkpoint.

---

### Task 3: `_interp_latlon` helper

**Files:** Modify `freight_planner/tours.py`; Test `tests/freight_planner/test_tours.py`

- [ ] **Step 1 — failing tests**

```python
from freight_planner.tours import _interp_latlon

def test_interp_latlon_endpoints_and_midpoint():
    assert _interp_latlon(52.0, 0.0, 54.0, -2.0, 0.0) == (52.0, 0.0)
    assert _interp_latlon(52.0, 0.0, 54.0, -2.0, 1.0) == (54.0, -2.0)
    assert _interp_latlon(52.0, 0.0, 54.0, -2.0, 0.5) == (53.0, -1.0)
```

- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement:**

```python
def _interp_latlon(a_lat: float, a_lon: float, b_lat: float, b_lon: float,
                   f: float) -> tuple[float, float]:
    """Linear interpolation at fraction f in [0,1] (drive-time fraction = km
    fraction; longhaul_drive_minutes is linear in km)."""
    g = min(1.0, max(0.0, float(f)))
    return (a_lat + (b_lat - a_lat) * g, a_lon + (b_lon - a_lon) * g)
```

- [ ] **Step 4 — run, expect PASS.** Checkpoint.

---

### Task 4: `DayStart` record + `TourEvaluation.day_starts`

**Files:** Modify `freight_planner/tours.py`; Test `tests/freight_planner/test_tours.py`

- [ ] **Step 1 — failing test**

```python
def test_day_starts_empty_when_flag_off():
    ev = evaluate_tour(_tractor(), [_job("d1", 54.0, -1.0, pallets=5.0, kg=4000.0)])
    assert ev.feasible
    assert ev.day_starts == ()          # flag OFF -> no per-day starts emitted
```

- [ ] **Step 2 — run, expect FAIL** (`AttributeError: day_starts`).
- [ ] **Step 3 — implement:** add the dataclass above `TourEvaluation` and the field on it:

```python
@dataclass(frozen=True)
class DayStart:
    day_index: int
    start_lat: float
    start_lon: float
    start_node: str
    carried_pallets: float
    carried_kg: float
```

Add to `TourEvaluation`: `day_starts: tuple[DayStart, ...] = field(default_factory=tuple)`.
`_infeasible_tour` unchanged (positional args still valid).

- [ ] **Step 4 — run, expect PASS.** Checkpoint.

---

### Task 5: Mid-leg split in `evaluate_tour` (core)

**Files:** Modify `freight_planner/tours.py`; Test `tests/freight_planner/test_tours.py`

Add a `_DayCursor` dataclass and an `_advance_single_point` helper that splits a
single-point leg's drive across days, appending `DayStart`s and returning the final
day's `(residual_dm, break_min)`:

```python
from dataclasses import dataclass  # already imported

@dataclass
class _DayCursor:
    day_index: int
    day_drive: float
    day_elapsed: float
    drive_since_break: float

def _advance_single_point(cur, dm, sm, hgv, elapsed_cap_min, prev_lat, prev_lon,
                          dst_lat, dst_lon, running_p, running_kg, tag, day_starts):
    """Split drive `dm` to a single-point stop (service `sm`) across days, ending
    each full day part-way along prev->dst. Mutates cur, appends DayStarts.
    Returns (residual_dm_on_final_day, break_for_residual)."""
    dm_rem = dm
    while True:
        b_full = statutory_breaks(cur.drive_since_break, dm_rem)[0] if hgv else 0.0
        fits = (cur.day_drive + dm_rem <= _DAY_DRIVE_CAP_MIN + _EPS
                and cur.day_elapsed + dm_rem + b_full + sm <= elapsed_cap_min + _EPS)
        if fits:
            break
        x = _drive_fits(cur.drive_since_break,
                        _DAY_DRIVE_CAP_MIN - cur.day_drive,
                        elapsed_cap_min - cur.day_elapsed, hgv)
        if x > _EPS:
            bx = statutory_breaks(cur.drive_since_break, x)[0] if hgv else 0.0
            cur.day_drive += x
            cur.day_elapsed += x + bx
            dm_rem -= x
        frac = (dm - dm_rem) / dm if dm > _EPS else 1.0
        o_lat, o_lon = _interp_latlon(prev_lat, prev_lon, dst_lat, dst_lon, frac)
        cur.day_index += 1
        cur.day_drive = cur.day_elapsed = cur.drive_since_break = 0.0
        day_starts.append(DayStart(cur.day_index, o_lat, o_lon,
                                   f"OVERNIGHT:{tag}:{cur.day_index}",
                                   float(running_p), float(running_kg)))
    b = statutory_breaks(cur.drive_since_break, dm_rem)[0] if hgv else 0.0
    return dm_rem, b
```

In `evaluate_tour`:
1. Read the flag once: `from freight_planner.config import MULTIDAY_MIDLEG_OVERNIGHT as _MIDLEG`.
2. Init `day_starts: list[DayStart] = []`; if `_MIDLEG`, seed day 0:
   `day_starts.append(DayStart(0, vehicle.start_lat, vehicle.start_lon, "DEPOT", running_p, running_kg))`
   (a `tag = str(getattr(vehicle, "vehicle_id", "T"))` for node names).
3. Replace the split block (lines ~221-228) with:

```python
if _MIDLEG and job.leg_kind not in _TWO_POINT_KINDS:
    cur = _DayCursor(day_index, day_drive, day_elapsed, drive_since_break)
    dm, bmin = _advance_single_point(cur, dm, sm, hgv, elapsed_cap_min,
                                     prev_lat, prev_lon, job.lat, job.lon,
                                     running_p, running_kg, tag, day_starts)
    day_index, day_drive, day_elapsed, drive_since_break = (
        cur.day_index, cur.day_drive, cur.day_elapsed, cur.drive_since_break)
    new_since = statutory_breaks(drive_since_break, dm)[1] if hgv else 0.0
else:
    bmin, new_since = (statutory_breaks(drive_since_break, dm) if hgv else (0.0, 0.0))
    if day_elapsed > 0 and (day_drive + dm > _DAY_DRIVE_CAP_MIN
                            or day_elapsed + dm + bmin + sm > elapsed_cap_min):
        day_index += 1
        day_drive = 0.0; day_elapsed = 0.0; drive_since_break = 0.0
        bmin, new_since = (statutory_breaks(0.0, dm) if hgv else (0.0, 0.0))
```

The due/floor checks, `arrive_min = day_elapsed + dm + bmin`, load/capacity, append, and
`day_drive += dm; day_elapsed = depart_min` all stay unchanged (with `dm` = the residual).

4. Return-leg block (lines ~269-275): when `_MIDLEG`, run the same split to home
   (`sm=0`, dst=home) and take `day_index` from the cursor; else keep the current block.
5. Pass `day_starts=tuple(day_starts)` into the successful `TourEvaluation(...)` return.

- [ ] **Step 1 — failing tests** (add to `test_tours.py`; a fixture flips the flag):

```python
import pytest
from freight_planner import config as _cfg

@pytest.fixture
def midleg_on():
    old = _cfg.MULTIDAY_MIDLEG_OVERNIGHT
    _cfg.MULTIDAY_MIDLEG_OVERNIGHT = True
    try:
        yield
    finally:
        _cfg.MULTIDAY_MIDLEG_OVERNIGHT = old

def _far_tour_jobs():
    # a Scotland cluster forcing >1 day from CB22
    return [_job("s1", 56.46, -2.97, pallets=4, kg=3000),
            _job("s2", 57.14, -2.10, pallets=4, kg=3000),
            _job("s3", 57.48, -4.22, pallets=4, kg=3000)]

def test_flag_off_byte_identical(midleg_on):
    veh, jobs = _tractor(), _far_tour_jobs()
    _cfg.MULTIDAY_MIDLEG_OVERNIGHT = False
    off = evaluate_tour(veh, jobs)
    _cfg.MULTIDAY_MIDLEG_OVERNIGHT = True
    on = evaluate_tour(veh, jobs)
    assert on.total_km == off.total_km
    assert on.total_drive_minutes == off.total_drive_minutes
    assert on.days <= off.days                      # never more (realistic geography)
    assert tuple(s.day_index for s in off.stops) == tuple(  # OFF unchanged from prior
        s.day_index for s in evaluate_tour_off(veh, jobs))  # helper re-reads OFF

def test_on_km_identical_and_days_not_more(midleg_on):
    veh, jobs = _tractor(), _far_tour_jobs()
    _cfg.MULTIDAY_MIDLEG_OVERNIGHT = False
    off = evaluate_tour(veh, jobs)
    _cfg.MULTIDAY_MIDLEG_OVERNIGHT = True
    on = evaluate_tour(veh, jobs)
    assert abs(on.total_km - off.total_km) < 1e-6
    assert on.days <= off.days
    assert on.day_starts and on.day_starts[0].start_node == "DEPOT"

def test_on_records_overnight_between_stops(midleg_on):
    on = evaluate_tour(_tractor(), _far_tour_jobs())
    overnights = [d for d in on.day_starts if d.day_index > 0]
    assert overnights                                # at least one mid-leg night
    for d in overnights:
        assert d.start_node.startswith("OVERNIGHT:")
        assert d.carried_pallets >= 0.0

def test_on_capacity_still_infeasible(midleg_on):
    over = [_job("x", 56.0, -3.0, pallets=30.0, kg=3000)]   # > 26 pal
    assert evaluate_tour(_tractor(), over).feasible is False

def test_on_late_still_trips(midleg_on):
    jobs = _far_tour_jobs()
    due = {jobs[-1].job_id: 0}                        # must finish day 0 -> impossible
    assert evaluate_tour(_tractor(), jobs, due_offsets=due).feasible is False
```

(`evaluate_tour_off` = a tiny module-level helper that forces the flag OFF around a call,
to snapshot the pre-change day_index tuple.)

- [ ] **Step 2 — run, expect FAIL** (`day_starts`/behaviour not implemented).
- [ ] **Step 3 — implement** the branch + helpers above.
- [ ] **Step 4 — run the new tests, expect PASS.**
- [ ] **Step 5 — corpus invariant** (add): build ~6 varied far tours, assert for each
  `km_on == km_off` (±1e-6) and `days_on <= days_off`. Checkpoint.

---

### Task 6: Full regression

- [ ] Run `python -m pytest tests/freight_planner/ -q`. Expect prior count + new tests, all green.
- [ ] Spot-confirm no non-tour test changed behaviour (flag OFF is the default). Checkpoint.

---

## Self-review notes

- **Spec coverage:** flag (T1), both helpers (T2/T3), data model (T4), split + return +
  invariants + hard-constraint tests (T5), regression (T6). Two-point deferral is the
  `not in _TWO_POINT_KINDS` guard.
- **Days-≤ caveat:** holds because every realistic single leg is < one day's drive
  (max UK ≈ 525 min < 600 cap); the invariant test uses realistic tours.
- **Identity:** OFF path is the exact current block; `day_starts` defaults to `()`.
