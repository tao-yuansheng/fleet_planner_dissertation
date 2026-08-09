# Tour OSRM Durations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Time the tour gate (`is_tour_only`) and tour executor (`evaluate_tour`) with OSRM per-road-type durations — the same model the daily router uses — behind a `TOUR_OSRM_DURATIONS` flag, replacing the flat 50 km/h gate and 80 km/h executor.

**Architecture:** Two pure-function call-site swaps in `tours.py`, each gated so that (a) with the flag OFF it is byte-identical to today, and (b) with the flag ON it uses `road_costs.road_minutes` (OSRM) when a duration-capable router is active, else each keeps its *own* current fallback speed (gate 50, executor 80). A new `route_costs.osrm_durations_active()` predicate lets the executor choose. Coverage is protected by the existing stranded-backhaul repair; validated on a week.

**Tech Stack:** Python 3.12, pytest, pandas, OSRM router (`freight_planner.route_costs` active-router shim).

**Repo note:** this working tree is **not** a git repo, so this plan uses **no-commit checkpoints** (run the test subset + pause for review) in place of `git commit` steps — matching the prior tour-consolidation plan.

**Test/run commands** run from `ZECURE-Phase2-main/BackEnd/logistics/` with the project venv:
`PY="E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe"`

---

### Task 1: `TOUR_OSRM_DURATIONS` flag + `osrm_durations_active()` predicate

**Files:**
- Modify: `freight_planner/config.py:24` (add flag after `TOUR_ATTACH_ENABLED`)
- Modify: `freight_planner/route_costs.py:171` (add predicate after `road_minutes`)
- Test: `tests/freight_planner/test_tours.py`

- [ ] **Step 1: Write the failing test** — append to `tests/freight_planner/test_tours.py`:

```python
from freight_planner import config, route_costs


def test_tour_osrm_durations_flag_defaults_on():
    assert config.TOUR_OSRM_DURATIONS is True


def test_osrm_durations_active_reflects_router_and_flag():
    route_costs.reset_router()
    config.USE_OSRM_DURATIONS = False
    assert route_costs.osrm_durations_active() is False

    class _StubOSRM:
        def distance_km(self, a, b, c, d): return 40.0
        def duration_h(self, a, b, c, d, depart_time=None): return 0.5

    config.USE_OSRM_DURATIONS = True
    route_costs.set_router(_StubOSRM())
    assert route_costs.osrm_durations_active() is True
    route_costs.reset_router()
    config.USE_OSRM_DURATIONS = False
```

- [ ] **Step 2: Run it, expect failure**

Run: `$PY -m pytest tests/freight_planner/test_tours.py::test_osrm_durations_active_reflects_router_and_flag -v`
Expected: FAIL — `AttributeError: module 'freight_planner.route_costs' has no attribute 'osrm_durations_active'`.

- [ ] **Step 3: Add the config flag** — in `freight_planner/config.py`, immediately after the `TOUR_ATTACH_ENABLED` line (24):

```python
TOUR_OSRM_DURATIONS: bool = True               # tours time legs with OSRM per-road-type durations (like the daily
                                               # router) instead of flat 50 km/h (gate) / 80 km/h (executor), so the
                                               # tour boundary + scheduling track real road speed (2026-07-15).
                                               # --no-tour-osrm-durations ablates to the flat model (byte-identical).
```

- [ ] **Step 4: Add the predicate** — in `freight_planner/route_costs.py`, immediately after `road_minutes` (ends line 170):

```python
def osrm_durations_active() -> bool:
    """True when OSRM per-road-type durations are enabled AND a duration-capable
    router is installed — the exact predicate road_minutes uses to choose OSRM
    over the constant-speed fallback."""
    from freight_planner import config
    return (bool(config.USE_OSRM_DURATIONS) and _active_router is not None
            and hasattr(_active_router, "duration_h"))
```

- [ ] **Step 5: Run tests, expect pass**

Run: `$PY -m pytest tests/freight_planner/test_tours.py -k "osrm_durations_active or flag_defaults_on" -v`
Expected: PASS (2 passed).

---

### Task 2: `is_tour_only` uses OSRM durations (gated)

**Files:**
- Modify: `freight_planner/tours.py:48-54` (import), `:98` (add `_gate_minutes`), `:128-134` (swap)
- Test: `tests/freight_planner/test_tours.py`

- [ ] **Step 1: Write the failing test** — append to `tests/freight_planner/test_tours.py`:

```python
class _FastOSRM:
    """A far point served fast: 0.5 h each way regardless of coords."""
    def distance_km(self, a, b, c, d): return 40.0
    def duration_h(self, a, b, c, d, depart_time=None): return 0.5


def test_is_tour_only_boundary_widens_under_osrm(monkeypatch):
    # ~230 haversine km (~300 road km) north of CB22 (52.086, 0.172)
    lat, lon = 54.16, 0.17
    monkeypatch.setattr("freight_planner.shared.routing.TRUCK_DURATION_FACTOR", 1.0, raising=False)
    monkeypatch.setitem(config.FREIGHT_DURATION_FACTOR, "tractor", 1.0)

    # flag OFF -> flat 50 km/h -> the ~300 road-km round trip busts the day -> tour
    config.TOUR_OSRM_DURATIONS = False
    route_costs.reset_router()
    assert is_tour_only(lat, lon, depot="CB22") is True

    # flag ON + fast OSRM (0.5 h each way) -> round trip fits the day -> NOT a tour
    config.TOUR_OSRM_DURATIONS = True
    config.USE_OSRM_DURATIONS = True
    route_costs.set_router(_FastOSRM())
    assert is_tour_only(lat, lon, depot="CB22") is False

    route_costs.reset_router()
    config.USE_OSRM_DURATIONS = False
    config.TOUR_OSRM_DURATIONS = True


def test_is_tour_only_flag_on_without_osrm_is_unchanged():
    lat, lon = 54.16, 0.17
    route_costs.reset_router()
    config.USE_OSRM_DURATIONS = False
    config.TOUR_OSRM_DURATIONS = False
    off = is_tour_only(lat, lon, depot="CB22")
    config.TOUR_OSRM_DURATIONS = True                 # ON but no OSRM router -> 50 km/h fallback
    assert is_tour_only(lat, lon, depot="CB22") == off
```

- [ ] **Step 2: Run it, expect failure**

Run: `$PY -m pytest tests/freight_planner/test_tours.py::test_is_tour_only_boundary_widens_under_osrm -v`
Expected: FAIL on the second assertion — the gate still uses flat 50 km/h, so the far point is still a tour.

- [ ] **Step 3: Add the import** — in `freight_planner/tours.py`, change the `route_costs` import block (48-54) to add `osrm_durations_active` and `road_minutes`:

```python
from freight_planner.route_costs import (
    drive_minutes,
    haversine_km,
    osrm_durations_active,
    road_km,
    road_minutes,
    service_minutes,
    statutory_breaks,
)
```

- [ ] **Step 4: Add `_gate_minutes`** — in `freight_planner/tours.py`, immediately before `def is_tour_only` (line 100):

```python
def _gate_minutes(a_lat: float, a_lon: float, b_lat: float, b_lon: float,
                  vehicle_type: str) -> float:
    """One tour-gate segment's drive minutes. With TOUR_OSRM_DURATIONS on, use
    road_minutes (OSRM per-road-type duration; it *itself* falls back to the flat
    drive_minutes(road_km) when no OSRM router is active, so offline is unchanged).
    With the flag off, the current flat 50 km/h model exactly."""
    if _config.TOUR_OSRM_DURATIONS:
        return road_minutes(a_lat, a_lon, b_lat, b_lon, vehicle_type)
    return drive_minutes(road_km(a_lat, a_lon, b_lat, b_lon))
```

- [ ] **Step 5: Swap the gate body** — in `freight_planner/tours.py`, replace lines 128-134 (the `if origin_lat ... return ...` block through the one-way return) with:

```python
    if origin_lat is not None and origin_lon is not None:
        carry_min = (_gate_minutes(anchor[0], anchor[1], origin_lat, origin_lon, vehicle_type)
                     + _gate_minutes(origin_lat, origin_lon, lat, lon, vehicle_type)
                     + _gate_minutes(lat, lon, anchor[0], anchor[1], vehicle_type))
        return carry_min + service_minutes(pallets, vehicle_type) > drive_cap_min
    one_way_min = _gate_minutes(anchor[0], anchor[1], lat, lon, vehicle_type)
    return 2.0 * one_way_min + service_minutes(pallets, vehicle_type) > drive_cap_min
```

- [ ] **Step 6: Run tests, expect pass**

Run: `$PY -m pytest tests/freight_planner/test_tours.py -k "is_tour_only" -v`
Expected: PASS (new tests + all pre-existing `is_tour_only` tests still green — they run with the flag ON but no OSRM router, so they hit the 50 km/h fallback = unchanged).

---

### Task 3: `evaluate_tour` uses OSRM durations (gated)

**Files:**
- Modify: `freight_planner/tours.py:194` (add `_seg_minutes` + `_leg_minutes` after `_leg_km`), `:324` and `:403` (swap)
- Test: `tests/freight_planner/test_tours.py`

- [ ] **Step 1: Write the failing test** — append to `tests/freight_planner/test_tours.py`:

```python
class _SlowOSRM:
    def distance_km(self, a, b, c, d): return 300.0
    def duration_h(self, a, b, c, d, depart_time=None): return 3.0   # 3 h one-way


class _FourHourOSRM:
    def distance_km(self, a, b, c, d): return 480.0
    def duration_h(self, a, b, c, d, depart_time=None): return 4.0   # 4 h one-way


def test_evaluate_tour_times_first_leg_by_osrm(monkeypatch):
    monkeypatch.setattr("freight_planner.shared.routing.TRUCK_DURATION_FACTOR", 1.0, raising=False)
    monkeypatch.setitem(config.FREIGHT_DURATION_FACTOR, "tractor", 1.0)
    veh = _tractor()
    far = _job("far", 54.0, -1.0, pallets=5.0, kg=4000.0)

    config.TOUR_OSRM_DURATIONS = True
    config.USE_OSRM_DURATIONS = True
    route_costs.set_router(_SlowOSRM())
    ev = evaluate_tour(veh, [far])
    # single delivery: first stop arrives at the OSRM one-way duration (180 min); under 4.5 h -> no break
    assert abs(ev.stops[0].arrive_minute - 180.0) < 1e-6

    route_costs.reset_router()
    config.USE_OSRM_DURATIONS = False
    config.TOUR_OSRM_DURATIONS = True


def test_evaluate_tour_flag_off_matches_longhaul():
    veh = _tractor()
    far = _job("far", 54.0, -1.0, pallets=5.0, kg=4000.0)
    route_costs.reset_router()
    config.USE_OSRM_DURATIONS = False
    config.TOUR_OSRM_DURATIONS = False
    off = evaluate_tour(veh, [far])
    config.TOUR_OSRM_DURATIONS = True                          # ON but no OSRM -> 80 km/h fallback
    on = evaluate_tour(veh, [far])
    assert off.total_drive == on.total_drive
    assert [s.arrive_minute for s in off.stops] == [s.arrive_minute for s in on.stops]


def test_faster_osrm_collapses_tour_days(monkeypatch):
    monkeypatch.setattr("freight_planner.shared.routing.TRUCK_DURATION_FACTOR", 1.0, raising=False)
    monkeypatch.setitem(config.FREIGHT_DURATION_FACTOR, "tractor", 1.0)
    veh = _tractor()
    far = _job("far", 56.0, -3.0, pallets=5.0, kg=4000.0)      # ~624 road km from CB22

    config.TOUR_OSRM_DURATIONS = False                        # flat 80 -> 7.8 h each way -> 2 days
    route_costs.reset_router()
    assert evaluate_tour(veh, [far]).days == 2

    config.TOUR_OSRM_DURATIONS = True                         # 4 h each way -> 8 h -> 1 day
    config.USE_OSRM_DURATIONS = True
    route_costs.set_router(_FourHourOSRM())
    assert evaluate_tour(veh, [far]).days == 1

    route_costs.reset_router()
    config.USE_OSRM_DURATIONS = False
    config.TOUR_OSRM_DURATIONS = True
```

- [ ] **Step 2: Run it, expect failure**

Run: `$PY -m pytest tests/freight_planner/test_tours.py::test_evaluate_tour_times_first_leg_by_osrm -v`
Expected: FAIL — `evaluate_tour` still times legs at 80 km/h, so `arrive_minute` is `road_km/80*60`, not 180.

- [ ] **Step 3: Add `_seg_minutes` and `_leg_minutes`** — in `freight_planner/tours.py`, immediately after `_leg_km` (ends line 193):

```python
def _seg_minutes(a_lat: float, a_lon: float, b_lat: float, b_lon: float,
                 vehicle_type: str) -> float:
    """One tour-executor segment's drive minutes. OSRM per-road-type duration when
    TOUR_OSRM_DURATIONS is on AND a duration-capable router is active; otherwise the
    long-haul flat speed (MULTIDAY_AVG_SPEED_KMH applied to road_km) — the executor's
    current behavior, kept byte-identical offline."""
    if _config.TOUR_OSRM_DURATIONS and osrm_durations_active():
        return road_minutes(a_lat, a_lon, b_lat, b_lon, vehicle_type)
    return longhaul_drive_minutes(road_km(a_lat, a_lon, b_lat, b_lon))


def _leg_minutes(prev_lat: float, prev_lon: float, job: RouteJob,
                 vehicle_type: str) -> float:
    """Drive minutes for a tour leg, mirroring _leg_km: a two-point direct/hub-drop
    sums prev->origin and origin->dest (linear, so the fallback equals
    longhaul_drive_minutes over the summed road_km, byte-identical to today)."""
    if (job.leg_kind in _TWO_POINT_KINDS
            and job.origin_lat is not None and job.origin_lon is not None):
        return (_seg_minutes(prev_lat, prev_lon, job.origin_lat, job.origin_lon, vehicle_type)
                + _seg_minutes(job.origin_lat, job.origin_lon, job.lat, job.lon, vehicle_type))
    return _seg_minutes(prev_lat, prev_lon, job.lat, job.lon, vehicle_type)
```

- [ ] **Step 4: Swap the per-leg time** — in `freight_planner/tours.py:324`, replace:

```python
        dm = longhaul_drive_minutes(leg_km)
```
with:
```python
        dm = _leg_minutes(prev_lat, prev_lon, job, vehicle.vehicle_type)
```

- [ ] **Step 5: Swap the return-leg time** — in `freight_planner/tours.py:403`, replace:

```python
    back_dm = longhaul_drive_minutes(back_km)
```
with:
```python
    back_dm = _seg_minutes(prev_lat, prev_lon, vehicle.home_lat, vehicle.home_lon,
                           vehicle.vehicle_type)
```

(Leave `leg_km = _leg_km(...)` at 323 and `back_km = road_km(...)` at 402 untouched — those still feed km accounting.)

- [ ] **Step 6: Run tests, expect pass**

Run: `$PY -m pytest tests/freight_planner/test_tours.py -v`
Expected: PASS — all new executor tests plus every pre-existing `test_tours.py` test (they run flag-ON without an OSRM router = 80 km/h fallback = unchanged).

- [ ] **Step 7: CHECKPOINT (no-commit)** — run the full tour + route-cost suite and confirm green before proceeding:

Run: `$PY -m pytest tests/freight_planner/test_tours.py tests/freight_planner/test_tour_plan.py tests/freight_planner/test_route_costs.py tests/freight_planner/test_route_costs_road_minutes.py -q`
Expected: all pass. Pause for review.

---

### Task 4: CLI ablation flag `--tour-osrm-durations` on `run_rolling` + `run_alns`

**Files:**
- Modify: `freight_planner/run_rolling.py:1473` (argparse), `:1492` (setter)
- Modify: `freight_planner/run_alns.py:766` (setter), `:827` (argparse)
- Test: `tests/freight_planner/test_vehicle_day_cost.py`

- [ ] **Step 1: Write the failing test** — append to `tests/freight_planner/test_vehicle_day_cost.py`:

```python
def test_run_rolling_cli_no_tour_osrm_sets_config_false(monkeypatch):
    import argparse
    from freight_planner import run_rolling, config as fp_cfg
    monkeypatch.setattr(fp_cfg, "TOUR_OSRM_DURATIONS", True)
    run_rolling._apply_vehicle_day_cost_flags(argparse.Namespace(tour_osrm_durations=False))
    assert fp_cfg.TOUR_OSRM_DURATIONS is False


def test_run_rolling_cli_absent_tour_osrm_keeps_default(monkeypatch):
    import argparse
    from freight_planner import run_rolling, config as fp_cfg
    monkeypatch.setattr(fp_cfg, "TOUR_OSRM_DURATIONS", True)
    run_rolling._apply_vehicle_day_cost_flags(argparse.Namespace(tour_osrm_durations=None))
    assert fp_cfg.TOUR_OSRM_DURATIONS is True


def test_run_alns_cli_sets_tour_osrm_config(monkeypatch):
    import argparse
    from freight_planner import run_alns, config as fp_cfg
    monkeypatch.setattr(fp_cfg, "TOUR_OSRM_DURATIONS", True)
    run_alns._apply_vehicle_day_cost_flags(argparse.Namespace(tour_osrm_durations=False))
    assert fp_cfg.TOUR_OSRM_DURATIONS is False
```

- [ ] **Step 2: Run it, expect failure**

Run: `$PY -m pytest tests/freight_planner/test_vehicle_day_cost.py -k tour_osrm -v`
Expected: FAIL — the setter ignores `tour_osrm_durations` (config stays True).

- [ ] **Step 3: Add the `run_rolling` setter** — in `freight_planner/run_rolling.py`, immediately after line 1492 (`_fp_cfg.TOUR_DEPOT_DIRECT_AS_DELIVERY = ...`):

```python
    if getattr(args, "tour_osrm_durations", None) is not None:
        _fp_cfg.TOUR_OSRM_DURATIONS = bool(args.tour_osrm_durations)
```

- [ ] **Step 4: Add the `run_rolling` argparse** — in `freight_planner/run_rolling.py`, immediately after the `--tour-depot-direct-as-delivery` argument (ends ~1473):

```python
    parser.add_argument("--tour-osrm-durations",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="time tours with OSRM per-road-type durations (default: config, ON); "
                             "--no-tour-osrm-durations reverts to the flat 50/80 km/h model")
```

- [ ] **Step 5: Add the `run_alns` setter** — in `freight_planner/run_alns.py`, immediately after line 766 (`_fp_cfg.TOUR_DEPOT_DIRECT_AS_DELIVERY = ...`):

```python
    if getattr(args, "tour_osrm_durations", None) is not None:
        _fp_cfg.TOUR_OSRM_DURATIONS = bool(args.tour_osrm_durations)
```

- [ ] **Step 6: Add the `run_alns` argparse** — in `freight_planner/run_alns.py`, immediately after the `--tour-depot-direct-as-delivery` argument (ends ~827):

```python
    parser.add_argument("--tour-osrm-durations",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="time tours with OSRM per-road-type durations (default: config, ON); "
                             "--no-tour-osrm-durations reverts to the flat 50/80 km/h model")
```

- [ ] **Step 7: Run tests, expect pass**

Run: `$PY -m pytest tests/freight_planner/test_vehicle_day_cost.py -k tour_osrm -v`
Expected: PASS (3 passed).

---

### Task 5: Integration coverage guard + docs

**Files:**
- Test: `tests/freight_planner/test_tour_plan.py`
- Modify: `freight_planner/PIPELINE.md` (§ tour classification), `freight_planner/README_DYNAMIC.md` (flags list)

- [ ] **Step 1: Write the coverage regression guard** — append to `tests/freight_planner/test_tour_plan.py`, reusing the `_hull_directs()` fixture, `_HJOBS` set, and `_committed_orders()` helper already defined in that file (lines 708-738):

```python
def test_tour_osrm_flag_on_keeps_far_orders_served():
    # Regression guard: the new TOUR_OSRM_DURATIONS code paths must not drop coverage.
    # With the flag ON but no OSRM router in the unit context, the gate uses the 50 km/h
    # fallback (unchanged), so the far Hull orders stay served. The widened-gate-under-OSRM
    # coverage safety (far orders routed daily, recovered by the stranded-backhaul repair)
    # is validated end-to-end on the week run in Task 6.
    from freight_planner import config
    config.TOUR_OSRM_DURATIONS = True
    cands, vehicles, compat, freight, start = _hull_directs()
    res = run_multiday_seed_plan(cands, vehicles, compat, freight, start)
    committed, dropped = _committed_orders(res)
    assert not (_HJOBS & set(dropped)), f"orders dropped: {dropped}"
    assert {"h0", "h1", "h2"} <= committed
```

- [ ] **Step 2: Run it, expect pass** (guards that the new code paths don't drop coverage):

Run: `$PY -m pytest tests/freight_planner/test_tour_plan.py::test_tour_osrm_flag_on_keeps_far_orders_served -v`
Expected: PASS.

- [ ] **Step 3: Document in PIPELINE.md** — in the "Tour classification" subsection (around line 559), add:

```markdown
- **OSRM-timed boundary** (`config.TOUR_OSRM_DURATIONS`, default ON, spec
  `2026-07-15-tour-osrm-durations-design.md`): `is_tour_only` and `evaluate_tour` time legs
  with `road_minutes` (OSRM per-road-type durations) instead of the flat 50 km/h gate / 80
  km/h executor, moving the same-day round-trip boundary from ~250 to ~425 road-km. Offline /
  `--no-tour-osrm-durations` = the flat model (byte-identical). Coverage of far orders now
  routed daily is protected by the stranded-backhaul repair.
```

- [ ] **Step 4: Document in README_DYNAMIC.md** — add a row/line beside the other tour flags noting `--no-tour-osrm-durations` reverts tours to the flat 50/80 km/h model (default ON).

- [ ] **Step 5: CHECKPOINT (no-commit)** — full freight_planner suite:

Run: `$PY -m pytest tests/freight_planner -q`
Expected: all pass. Pause for review.

---

### Task 6: Week validation (manual run + compare)

**Files:** none (produces two run folders + a comparison).

- [ ] **Step 1: Run the pair** (ON vs ablation, separate out-dirs and postcode caches so they don't collide; both keep the daily router on OSRM):

```
cd ZECURE-Phase2-main/BackEnd/logistics
$PY -m freight_planner.run_rolling --start 2026-01-12 --end 2026-01-18 \
    --qargo freight_planner/data/enriched_orders/2026-01.parquet \
    --tour-osrm-durations   --out-dir freight_planner/run_osrm_on
$PY -m freight_planner.run_rolling --start 2026-01-12 --end 2026-01-18 \
    --qargo freight_planner/data/enriched_orders/2026-01.parquet \
    --no-tour-osrm-durations --postcode-cache data/Output/postcode_cache_offcopy.json \
    --out-dir freight_planner/run_osrm_off
```
(Use the exact `--qargo`/`--postcode-cache` paths the prior week runs used; confirm from `freight_planner/run_week_fix/.../run_manifest.json` if unsure.)

- [ ] **Step 2: Compare** the two `md/kpi_summary.md` plus the stdout `rolling ledger` line, on: assigned orders (**coverage — must be identical**), tour count, tour vehicle-days, total vehicle-days, planned km, service ledger (ON_TIME/SLIPPED/UNSERVED).

Expected direction: coverage identical; fewer tours; tour vehicle-days down or flat; service neutral (no new SLIPPED). Report the table.

- [ ] **Step 3: Gate on coverage.** If assigned-orders drops in the ON run, STOP — the widened gate is stranding far orders the stranded-backhaul repair didn't catch. Do not call the feature done; escalate to strengthen the repair (a targeted post-daily far-order → tour fallback) as a follow-up task before flipping default ON stays.

- [ ] **Step 4: CHECKPOINT** — present the validation table and the coverage verdict for review.

---

## Notes / out of scope (from the spec)

- **Viz anchor** (`TOUR_ANCHOR_KMH=80` in `viz_timeline_maplogic.cjs`) is unchanged — a small viz-consistency follow-up, no KPI depends on it.
- **`MULTIDAY_MIDLEG_OVERNIGHT`** (default OFF) interpolates split points assuming time ∝ km; approximate under OSRM. Deferred while the feature is off.
- **Piece 2** (corridors boarding non-tour legs; broadening the flag-off intraday tour-attach spine) is a separate spec after this ships.
