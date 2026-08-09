# Vehicle Catchment (B15) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-vehicle service-area radii learned from history, enforced as a soft proportional penalty in the optimizer's ranking cost, so rigids stop being sent on long hauls (B15) without any hard gate.

**Architecture:** `catchment.py` calibrates radii (per-vehicle P95 with per-type fallback and a floor) from the qargo frame + geocode cache and exposes the job-distance helper; `vehicle_cost.out_of_area_penalty_km` is the pure arithmetic; the penalty term is added at every generalized-cost site (ALNS insertion ranking, `changed_costs`, cost ledger init, `route_cost`/`solution_cost`) and at the seed's `best_insertion` — reported km stays physical everywhere.

**Tech Stack:** Python 3.12, pandas/numpy, pytest.

**Spec:** `docs/superpowers/specs/2026-07-02-vehicle-catchment-design.md` (has the calibration table and the flat-vs-proportional counter-example).

**Standing rules:** NO `git commit` (skip all commit steps). Pipeline outputs → `freight_planner/out`. Viz regen = trip_app only. Tests from `BackEnd/logistics` with `PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1` and venv python `E:\BEAT\ZECURE-Phase2-main\.venv-1\Scripts\python.exe`.

> **Amendment (executed between Tasks 1 and 2):** Task 1's knobs plus all
> planner-owned tour/break/wait knobs were MOVED to the new
> **`freight_planner/config.py`** (Task 1b, stakeholder decision). All
> `cambridge.config` imports of `CATCHMENT_*` / `OUT_OF_AREA_KM_FACTOR` in the
> tasks below must read **`freight_planner.config`** instead. Shared infra
> (`DEPOT_ANCHORS`, `VEHICLE_DEPOT_MAP`, `ALL_RIGIDS`/`ALL_TRACTORS`) still
> comes from `cambridge.config`.

---

### Task 1: Config knobs + `out_of_area_penalty_km` arithmetic

**Files:**
- Modify: `cambridge/config.py` (append to the tour-knob block, after `MAX_STOP_WAIT_MIN`)
- Modify: `freight_planner/vehicle_cost.py`
- Test: `tests/freight_planner/test_catchment.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

from freight_planner.vehicle_cost import out_of_area_penalty_km


def test_no_penalty_within_or_at_the_radius():
    assert out_of_area_penalty_km(30.0, 100.0) == 0.0
    assert out_of_area_penalty_km(100.0, 100.0) == 0.0


def test_penalty_is_factor_times_overshoot():
    # OUT_OF_AREA_KM_FACTOR default 1.0: each km beyond counts once extra
    assert out_of_area_penalty_km(150.0, 100.0) == 50.0


def test_unknown_catchment_disables_the_penalty():
    assert out_of_area_penalty_km(500.0, 0.0) == 0.0
    assert out_of_area_penalty_km(500.0, -1.0) == 0.0
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/freight_planner/test_catchment.py -q` → ImportError.

- [ ] **Step 3: Implement**

`cambridge/config.py` (after `MAX_STOP_WAIT_MIN`):

```python
CATCHMENT_PERCENTILE: float = 95.0     # per-vehicle radius = P95 of its history
CATCHMENT_MIN_SAMPLES: int = 20        # fewer samples -> fall back to the type radius
CATCHMENT_RADIUS_FLOOR_KM: float = 30.0
OUT_OF_AREA_KM_FACTOR: float = 1.0     # each km beyond catchment counts double in ranking
```

`freight_planner/vehicle_cost.py` — add `from cambridge.config import OUT_OF_AREA_KM_FACTOR` and:

```python
def out_of_area_penalty_km(dist_km: float, catchment_km: float) -> float:
    """Phantom km added to the RANKING cost for a job beyond its vehicle's
    catchment: each km beyond the radius counts ``OUT_OF_AREA_KM_FACTOR`` times
    extra. Zero within the radius or when the catchment is unknown (<= 0), so
    vehicles without a calibrated radius are never penalized. Never appears in
    reported/physical km — ranking only, like the per-type fuel rates."""
    if catchment_km <= 0.0:
        return 0.0
    return OUT_OF_AREA_KM_FACTOR * max(0.0, float(dist_km) - float(catchment_km))
```

- [ ] **Step 4: Run to verify pass** — 3 passed; also `python -m pytest tests/freight_planner/test_breaks.py -q` (config import untouched sanity).

### Task 2: `catchment.py` — calibration + job-distance helper

**Files:**
- Create: `freight_planner/catchment.py`
- Test: `tests/freight_planner/test_catchment.py`

- [ ] **Step 1: Write the failing tests** (append). Before writing them, check the geocode cache entry shape: read `freight_planner/geocode.py::coords` / `coords_from_cache_entry` and build the test cache in whatever form `coords` accepts (adjust `_CACHE` below if the real accessor differs — report if you had to).

```python
import pandas as pd

from freight_planner.catchment import build_vehicle_catchment, job_distance_km
from freight_planner.routing_adapter import RouteJob

# ~0.9 km per 0.01 deg lat at this latitude; use big separations so percentiles are unambiguous
_DEPOT = (52.0, 0.0)          # test vehicles are mapped to a fake depot anchor
_NEAR = "NR1 1AA"             # ~20 km north
_FAR = "FR1 1AA"              # ~200 km north
_CACHE = {"NR1 1AA": {"latitude": 52.18, "longitude": 0.0},
          "FR1 1AA": {"latitude": 53.8, "longitude": 0.0}}


def _orders(reg: str, n_near: int, n_far: int, col: str = "resource_rigid") -> pd.DataFrame:
    rows = []
    for i in range(n_near + n_far):
        pc = _NEAR if i < n_near else _FAR
        rows.append({
            "status": "INVOICE_POSTED", "order_import_integration_type": "",
            "origin_postal_code": "XX1 1XX", "destination_postal_code": pc,
            "resource_rigid": "", "resource_tractor": "", "resource_van": "",
            col: reg,
        })
    return pd.DataFrame(rows)


def test_vehicle_with_enough_history_gets_its_own_p95(monkeypatch):
    import freight_planner.catchment as fc
    monkeypatch.setattr(fc, "_home_anchor", lambda reg: _DEPOT)
    # 19 near + 21 far: P95 of the mix sits at the far distance (~200 km)
    df = _orders("AB12CDE", 19, 21)
    radii = build_vehicle_catchment(df, _CACHE, type_of={"AB12CDE": "rigid"})
    assert 150.0 < radii["AB12CDE"] < 260.0


def test_thin_history_falls_back_to_the_type_radius(monkeypatch):
    import freight_planner.catchment as fc
    monkeypatch.setattr(fc, "_home_anchor", lambda reg: _DEPOT)
    rich = _orders("RICH1", 30, 0)                     # rigid, all near (~20 km) -> type P95 ~20
    thin = _orders("THIN1", 3, 0)                      # only 3 samples -> fallback
    radii = build_vehicle_catchment(pd.concat([rich, thin], ignore_index=True), _CACHE,
                                    type_of={"RICH1": "rigid", "THIN1": "rigid"})
    # both end up floored at 30 (type P95 ~20 < floor): proves fallback AND floor
    assert radii["THIN1"] == 30.0
    assert radii["RICH1"] == 30.0


def test_cancelled_orders_and_unmapped_regs_are_ignored(monkeypatch):
    import freight_planner.catchment as fc
    monkeypatch.setattr(fc, "_home_anchor", lambda reg: _DEPOT if reg == "OK1" else None)
    df = _orders("OK1", 25, 0)
    cancelled = _orders("OK1", 0, 25)
    cancelled["status"] = "CANCELLED"
    ghost = _orders("NODEPOT", 25, 0)
    radii = build_vehicle_catchment(pd.concat([df, cancelled, ghost], ignore_index=True),
                                    _CACHE, type_of={"OK1": "rigid", "NODEPOT": "rigid"})
    assert radii["OK1"] == 30.0        # near-only P95 ~20, floored to 30; far CANCELLED ignored
    assert "NODEPOT" not in radii


def test_job_distance_uses_the_farther_endpoint_for_two_point_moves():
    near_dest = RouteJob(job_id="J", leg_kind="DIRECT_CUSTOMER_MOVE", node="X",
                         lat=52.1, lon=0.0, pallets=1.0, kg=10.0,
                         origin_lat=53.8, origin_lon=0.0)   # far collection
    d = job_distance_km(52.0, 0.0, near_dest)
    assert d > 150.0                                        # the far origin dominates
    plain = RouteJob(job_id="K", leg_kind="CUSTOMER_DELIVERY", node="Y",
                     lat=52.1, lon=0.0, pallets=1.0, kg=10.0)
    assert job_distance_km(52.0, 0.0, plain) < 20.0
```

- [ ] **Step 2: Run to verify failure** — ModuleNotFoundError.

- [ ] **Step 3: Implement** `freight_planner/catchment.py`:

```python
"""B15: per-vehicle service-area radii learned from history.

Each vehicle's catchment = P95 of the haversine distances from its home-depot
anchor to the customer postcodes of orders it actually served (qargo
resource_* columns). Thin histories fall back to the fleet-wide per-type P95;
everything is floored. The radius feeds a SOFT ranking penalty
(vehicle_cost.out_of_area_penalty_km) — no hard gate, coverage cannot drop.

Deployment caveat: calibrating from the planning window's own month is a
fleet-behavior prior, not per-order hindsight; a live deployment would feed
trailing months instead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from cambridge.config import (
    ALL_RIGIDS,
    ALL_TRACTORS,
    CATCHMENT_MIN_SAMPLES,
    CATCHMENT_PERCENTILE,
    CATCHMENT_RADIUS_FLOOR_KM,
    DEPOT_ANCHORS,
    VEHICLE_DEPOT_MAP,
)
from cambridge.scope import classify_order
from freight_planner import geocode
from freight_planner.route_costs import haversine_km

_RESOURCE_COLS = ("resource_rigid", "resource_tractor", "resource_van")


def _home_anchor(reg: str) -> tuple[float, float] | None:
    depot = VEHICLE_DEPOT_MAP.get(reg)
    return DEPOT_ANCHORS.get(depot) if depot else None


def _fleet_type(reg: str) -> str:
    if reg in ALL_TRACTORS:
        return "tractor"
    if reg in ALL_RIGIDS:
        return "rigid"
    return "van"


def _regs(value) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return []
    return [p.strip().replace(" ", "").upper() for p in text.split(",") if p.strip()]


def _service_pcs(row, flow) -> list[str]:
    o = str(row.get("origin_postal_code") or "").strip().upper()
    d = str(row.get("destination_postal_code") or "").strip().upper()
    if flow in ("PL_EXPORT", "LOCAL_COLLECT"):
        return [o] if o else []
    if flow in ("PL_IMPORT", "LOCAL_DELIVER"):
        return [d] if d else []
    return [p for p in (o, d) if p]


def job_distance_km(home_lat: float, home_lon: float, job) -> float:
    """Straight-line km from a vehicle's home to a job's farthest endpoint.

    Two-point moves (DIRECT/HUB_DROP with origin coords) use the MAX of the
    collection and delivery distances — a near delivery with a far collection
    is still out-of-area work. Straight-line on purpose: the radii were
    calibrated on the same metric."""
    d = haversine_km(home_lat, home_lon, float(job.lat), float(job.lon))
    o_lat = getattr(job, "origin_lat", None)
    o_lon = getattr(job, "origin_lon", None)
    if o_lat is not None and o_lon is not None:
        d = max(d, haversine_km(home_lat, home_lon, float(o_lat), float(o_lon)))
    return d


def build_vehicle_catchment(
    qargo_df: pd.DataFrame,
    postcode_cache: dict,
    type_of: dict[str, str] | None = None,
) -> dict[str, float]:
    """vehicle reg -> catchment radius km (P95 own history, type fallback, floored).

    Regs with no depot mapping are skipped entirely (no radius -> no penalty).
    ``type_of`` overrides the fleet-set type lookup (testing seam)."""
    samples: dict[str, list[float]] = {}
    for _, row in qargo_df.iterrows():
        if str(row.get("status") or "").upper() == "CANCELLED":
            continue
        flow = classify_order(row)
        pcs = _service_pcs(row, flow)
        if not pcs:
            continue
        for col in _RESOURCE_COLS:
            for reg in _regs(row.get(col)):
                anchor = _home_anchor(reg)
                if anchor is None:
                    continue
                for pc in pcs:
                    ll = geocode.coords(pc, postcode_cache)
                    if not ll:
                        continue
                    samples.setdefault(reg, []).append(
                        haversine_km(anchor[0], anchor[1], ll[0], ll[1]))

    kind = type_of.get if type_of else lambda reg, default=None: _fleet_type(reg)
    by_type: dict[str, list[float]] = {}
    for reg, arr in samples.items():
        by_type.setdefault(kind(reg) or "van", []).extend(arr)
    type_radius = {
        t: float(np.percentile(np.array(arr), CATCHMENT_PERCENTILE))
        for t, arr in by_type.items() if arr
    }

    radii: dict[str, float] = {}
    for reg, arr in samples.items():
        if len(arr) >= CATCHMENT_MIN_SAMPLES:
            r = float(np.percentile(np.array(arr), CATCHMENT_PERCENTILE))
        else:
            r = type_radius.get(kind(reg) or "van", 0.0)
        radii[reg] = max(CATCHMENT_RADIUS_FLOOR_KM, r)
    return radii
```

NOTE on `kind`: when `type_of` is a dict, `type_of.get` accepts `(reg)` and returns None for misses — the `or "van"` covers it; the lambda fallback mirrors that signature. Keep it exactly this shape.

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/freight_planner/test_catchment.py -q` → 7 passed.

### Task 3: Penalty in every ALNS cost site (+ `VehicleMeta.catchment_km`)

**Files:**
- Modify: `freight_planner/alns.py`
- Test: `tests/freight_planner/test_alns.py`

- [ ] **Step 1: Write the failing tests** (append to test_alns.py; its helpers `_vmeta`, `_rjob`, `_served`, `CB22` etc. exist; `VehicleMeta` import exists)

```python
# --- B15: out-of-area penalty in the ranking cost ---------------------------

FAR_NORTH_2 = (54.0, -1.0)   # ~220 km from CB22 — beyond a 50 km rigid catchment


def _vmeta_catch(vid, vtype, catchment_km):
    return VehicleMeta(
        vehicle_id=vid, home_depot="CB22", lat=CB22[0], lon=CB22[1],
        available_from="2026-01-05 06:00:00", shift_end="2026-01-05 20:00:00",
        capacity_pallets=26.0, capacity_kg=24000.0, vehicle_type=vtype,
        catchment_km=catchment_km,
    )


def test_out_of_area_job_prefers_the_in_catchment_tractor():
    day = "2026-01-05"
    vmeta = {"RIG": _vmeta_catch("RIG", "rigid", 50.0),
             "TRAC": _vmeta_catch("TRAC", "tractor", 400.0)}
    meta = JobMeta(rjob=_rjob("FARJOB", FAR_NORTH_2), day=day,
                   eligible_vehicles=["RIG", "TRAC"], candidate={})

    ranked = _ranked_inserts_for_job(
        meta, {}, {("RIG", day): [], ("TRAC", day): []}, vmeta, {}, set(), top=2)

    # without the penalty the cheaper-per-km rigid wins; with it the tractor must
    assert ranked[0][1][0] == "TRAC"


def test_in_catchment_job_still_prefers_the_cheaper_rigid():
    day = "2026-01-05"
    vmeta = {"RIG": _vmeta_catch("RIG", "rigid", 50.0),
             "TRAC": _vmeta_catch("TRAC", "tractor", 400.0)}
    meta = JobMeta(rjob=_rjob("NEARJOB", NEAR_CB22), day=day,
                   eligible_vehicles=["TRAC", "RIG"], candidate={})

    ranked = _ranked_inserts_for_job(
        meta, {}, {("RIG", day): [], ("TRAC", day): []}, vmeta, {}, set(), top=2)

    assert ranked[0][1][0] == "RIG"


def test_out_of_area_rigid_still_serves_when_it_is_the_only_option():
    # soft, not a gate: coverage cannot drop
    day = "2026-01-05"
    vmeta = {"RIG": _vmeta_catch("RIG", "rigid", 50.0)}
    j = _rjob("ONLY", FAR_NORTH_2)
    job_meta = {"ONLY": JobMeta(j, day, ["RIG"], {})}

    result = improve_solution({("RIG", day): [j]}, job_meta, vmeta,
                              iterations=40, rng_seed=1)

    assert _served(result.solution) == {"ONLY"}


def test_solution_cost_includes_the_out_of_area_penalty():
    day = "2026-01-05"
    inside = {"V": _vmeta_catch("V", "rigid", 400.0)}
    outside = {"V": _vmeta_catch("V", "rigid", 50.0)}
    sol = {("V", day): [_rjob("J", FAR_NORTH_2)]}

    assert solution_cost(sol, outside) > solution_cost(sol, inside)
```

- [ ] **Step 2: Run to verify failure** — TypeError (`VehicleMeta` has no `catchment_km`) then ranking assertions.

- [ ] **Step 3: Implement** in `freight_planner/alns.py`:

1. Imports: extend `from freight_planner.vehicle_cost import fuel_cost_per_km` with `out_of_area_penalty_km`; add `from freight_planner.catchment import job_distance_km`.
2. `VehicleMeta`: add final field `catchment_km: float = 0.0`; `_build_vehicle_meta` reads `catchment_km=float(getattr(row, "catchment_km", 0.0) or 0.0)`.
3. Module-level helper (near `route_km`):

```python
def _oa_penalty_km(vm: VehicleMeta, rjob: RouteJob) -> float:
    return out_of_area_penalty_km(job_distance_km(vm.lat, vm.lon, rjob), vm.catchment_km)


def _trips_penalty_km(trips, vm: VehicleMeta) -> float:
    return sum(_oa_penalty_km(vm, j) for j in _flatten(_as_trips(trips)))
```

4. `route_cost` (module-level) becomes `fuel_cost_per_km(vm.vehicle_type) * (route_km(trips, vm, day) + _trips_penalty_km(trips, vm))` — update its docstring to say the ranking cost includes phantom out-of-area km while reported distance stays physical.
5. In `improve_solution`:
   - init: `route_cost_by_key[(vid, day)] = fuel_cost_per_km(...) * (k_phys + _trips_penalty_km(trips, vehicle_meta[vid]))` (keep `phys_km_before += k_phys` physical).
   - `changed_costs`: `out[key] = fuel_cost_per_km(...) * (ev.total_km + _trips_penalty_km(tt, vehicle_meta[key[0]]))`.
6. `_best_insert_for_job` AND `_ranked_inserts_for_job`: compute once per vehicle `pen = _oa_penalty_km(vehicle_meta[vid], meta.rjob)` next to `rate = ...`, and every `delta`/candidate append becomes `rate * (day_ev.total_km - base_km + pen)`. (Both the existing-trip and new-trip branches, and the eject branch in `_best_insert_for_job`.)
7. `km_before`/`km_after`/route_totals: UNTOUCHED (physical km).

- [ ] **Step 4: Run** the 4 new tests, then `python -m pytest tests/freight_planner -q` — expect all green (existing fixtures have `catchment_km=0.0` → penalty disabled, nothing shifts). If any existing test moved, that's a bug in your change — investigate, don't adjust the test.

### Task 4: Penalty in the seed's `best_insertion`

**Files:**
- Modify: `freight_planner/route_seed.py` (~lines 227-272)
- Test: `tests/freight_planner/test_route_seed.py` (append; read its fixture style first — it builds candidates/vehicles/compat frames like test_alns's `_cand`/`_compat` helpers)

- [ ] **Step 1: Write the failing test**

```python
def test_seed_prefers_in_catchment_tractor_for_far_job():
    # rigid catchment 50 km, tractor 400: a ~220 km job must seed onto the tractor
    far = (54.0, -1.0)
    vehicles = pd.DataFrame([
        {"vehicle_id": "RIG", "home_depot": "CB22", "current_lat": 52.07, "current_lon": 0.17,
         "available_from": "2026-01-05 06:00:00", "shift_end": "2026-01-05 20:00:00",
         "capacity_kg": 24000.0, "capacity_pallets": 26.0, "vehicle_type": "rigid",
         "catchment_km": 50.0},
        {"vehicle_id": "TRAC", "home_depot": "CB22", "current_lat": 52.07, "current_lon": 0.17,
         "available_from": "2026-01-05 06:00:00", "shift_end": "2026-01-05 20:00:00",
         "capacity_kg": 24000.0, "capacity_pallets": 26.0, "vehicle_type": "tractor",
         "catchment_km": 400.0},
    ])
    candidates = pd.DataFrame([_cand("F:D", "F", far, "CB22")])
    compatibility = pd.DataFrame([
        _compat("F:D", "RIG", True, far), _compat("F:D", "TRAC", True, far),
    ])
    freight = pd.DataFrame([
        {"freight_id": "F", "initial_state": "AT_DEPOT_OR_HUB_PENDING", "initial_depot": "CB22"},
    ])

    seed = run_route_seed_plan(candidates, vehicles, compatibility, freight)

    assert {r.vehicle_id for r in seed.selected} == {"TRAC"}
```

(Reuse/define `_cand`/`_compat` in the file's existing style; if the file already has equivalent fixture helpers under other names, use those instead and note it.)

- [ ] **Step 2: Run to verify failure** — the rigid wins today (cheaper km identical geometry? both at same depot so km identical; tie-break may pick either — ensure RED is meaningful: if the test passes by luck of ordering, make the rigid alphabetically-first eligible AND assert the mechanism instead by giving the rigid a 5 km closer... simplest robust fixture: keep both at the same depot, same km — then WITHOUT the penalty the ranking is a tie broken by iteration order with RIG listed first in compatibility, so the buggy code picks RIG deterministically. Verify that's what happens; if not, report what the pre-change code picked and why the test is still a valid red.)

- [ ] **Step 3: Implement** in `route_seed.py::best_insertion`:

1. Imports: `from freight_planner.catchment import job_distance_km` and `from freight_planner.vehicle_cost import out_of_area_penalty_km`.
2. At the top of the per-vehicle loop body (after `veh = _rv(vid, day)`):

```python
            pen_km = out_of_area_penalty_km(
                job_distance_km(veh.home_lat, veh.home_lon, rjob),
                float(_g(vrows[vid], "catchment_km", 0.0) or 0.0))
```

3. Existing-trip branch: `delta = day_ev.total_km - base_km + pen_km`.
4. New-trip branch: `delta = day_ev.total_km - base_km + pen_km` (the `score = delta + (10000.0 ...)` line is unchanged).
(The seed ranks on km not GBP — the penalty km inflates only the out-of-area candidate, which is all the flip needs; noted in the spec.)

- [ ] **Step 4: Run** the new test, then `python -m pytest tests/freight_planner -q` — green (frames without `catchment_km` get 0.0 → disabled).

### Task 5: run_alns wiring — calibration + diagnostic log

**Files:**
- Modify: `freight_planner/run_alns.py`

No new pytest (wiring); verified by import check + the Task 6 runs.

- [ ] **Step 1: Calibration in build-inputs.** In `main()`'s `with runlog.stage("build inputs"):` block, right after `vehicle_df = vehicle_states_frame(start)` (~line 192): the qargo frame and postcode cache variables exist in that scope (read the surrounding code for their actual names — the qargo DataFrame is loaded earlier for demand/legs; `postcode_cache` is loaded near the top of main). Add:

```python
        from freight_planner.catchment import build_vehicle_catchment  # top-level import preferred
        catchment = build_vehicle_catchment(qargo_df, postcode_cache)
        vehicle_df["catchment_km"] = vehicle_df["vehicle_id"].astype(str).map(catchment).fillna(0.0)
        n_own = sum(1 for v in vehicle_df["vehicle_id"].astype(str) if v in catchment)
        runlog.log(f"catchment: radii for {len(catchment)} regs; {n_own}/{len(vehicle_df)} fleet vehicles mapped")
```

(put the import at the top of the file with the other freight_planner imports; use the real qargo frame variable name.)

- [ ] **Step 2: Post-ALNS diagnostic.** After the `best alns ...` log line (~line 251), add an out-of-area count over the final daily solution:

```python
    from freight_planner.catchment import job_distance_km          # already imported at top
    from freight_planner.vehicle_cost import out_of_area_penalty_km
    _vrow = {str(r.vehicle_id): r for r in vehicle_df.itertuples(index=False)}
    n_oa = n_jobs = 0
    for (vid, _day), trips in imp.solution.items():
        row = _vrow.get(str(vid))
        if row is None:
            continue
        cat = float(getattr(row, "catchment_km", 0.0) or 0.0)
        for trip in (trips if trips and not hasattr(trips[0], "job_id") else [trips]):
            for j in trip:
                n_jobs += 1
                if out_of_area_penalty_km(
                        job_distance_km(float(row.current_lat), float(row.current_lon), j), cat) > 0.0:
                    n_oa += 1
    if n_jobs:
        runlog.log(f"catchment: {n_oa}/{n_jobs} daily jobs beyond their vehicle's radius ({100.0 * n_oa / n_jobs:.1f}%)")
```

(the trips normalization mirrors `_as_trips`; if `imp.solution` values are always list-of-lists here, simplify accordingly after checking.)

- [ ] **Step 3: Verify** — `python -c "import freight_planner.run_alns"` clean; `python -m pytest tests/freight_planner -q` still green.

### Task 6: Validation runs + docs

- [ ] **Step 1:** Full suite: `python -m pytest tests -q` — expect green minus the 3 known environmental failures.
- [ ] **Step 2:** One run per week into the official out dir:

```
FP_ALNS_CONSERVE=1 python -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-17 --time-budget 90 --out-dir freight_planner/out
FP_ALNS_CONSERVE=1 python -m freight_planner.run_alns --start 2026-01-19 --end 2026-01-24 --time-budget 90 --out-dir freight_planner/out
```

- [ ] **Step 3:** Measure the B15 symptom before/after from each run's `plan/route_stops.csv`: count rows with `vehicle_type == "rigid"` and `leg_km > 120` (and the same per unique vehicle, to confirm the survivors are the long-lane regs). Compare coverage/km against baselines wk1 99.7% / 94,034 km, wk2 99.8% / 108,296 km. Report deltas; NO tuning loops.
- [ ] **Step 4:** Regenerate both trip apps (`viz_app.py`, same paths as before). Runsheets auto-emit.
- [ ] **Step 5:** QUEST_LOG session entry (B15 → DONE, measured numbers, catchment log lines) + memory file update.

## Self-review notes

- Spec coverage: calibration → Task 2; penalty arithmetic + knobs → Task 1; ALNS sites (insert rankings, changed_costs, init ledger, route_cost/solution_cost) → Task 3; seed → Task 4; wiring + diagnostic → Task 5; validation criteria → Task 6. Two-point max-endpoint rule → Task 2 helper + test. Coverage-guard (soft not gate) → Task 3 test 3.
- Type consistency: `out_of_area_penalty_km(dist_km, catchment_km)` and `job_distance_km(home_lat, home_lon, job)` used with identical signatures in Tasks 3-5; `VehicleMeta.catchment_km` default 0.0 matches the disabled-penalty contract everywhere.
- Executor judgment notes: Task 2's geocode cache entry shape and Task 4's tie-break RED check are explicitly flagged as verify-first items; Task 5 tells the executor to confirm variable names rather than trust the plan's guesses.
