# Shuttle Carve-Out + Zero-Cost Merge Sweep (K1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.
>
> **STANDING RULES (this repo):** NO `git commit` — ever. Do not add commit steps; do not run
> `git add`. Pipeline outputs go to `freight_planner/out`. Tests: run from
> `e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics` with `python -m pytest`.

**Goal:** Serve mega-shipper address-days (≥ 26 pallets one direction) with dedicated
nearly-full shuttle round trips pinned against ALNS scatter, and collapse residual km-neutral
same-address split visits in a post-ALNS sweep.

**Spec:** `docs/superpowers/specs/2026-07-03-shuttle-carveout-design.md` — read it first.

**Architecture:** Component 1 = new pure module `freight_planner/shuttle.py` (detect + pack)
applied pre-loop inside `route_seed.run_route_seed_plan`, with pinning enforced in
`alns.py`. Component 2 = new module `freight_planner/merge_sweep.py` applied inside
`alns.improve_existing_solution` between optimization and record emission. All knobs in
`freight_planner/config.py` (the planner knob home — NOT `cambridge/config.py`).

**Tech stack:** pandas, dataclasses, pytest; evaluators `routing_adapter.try_insert_job` /
`evaluate_day` (OSRM router with haversine fallback — tests monkeypatch routers as existing
tests do; see `tests/freight_planner/test_route_seed.py` for the fixture style).

**Key code facts (verified 2026-07-03):**
- `route_seed.run_route_seed_plan` (route_seed.py:181): builds `vrows` (vehicle_id → row),
  `coords = _job_coords(compatibility)`, `options = _ok_options(compatibility)`
  (leg_id → list[(vehicle_id, same_depot_bool)]), `runnable` DataFrame (hard_blocker == ""),
  `ordered = sorted(runnable.itertuples(index=False), key=_priority_key)`, per-(vid,day)
  `routes[(vid, day)] = (trips, day_eval)`, `_rv(vid, day)` cached RouteVehicle,
  ledger transitions in the main loop (route_seed.py:329-336).
- `RouteSeedResult` (route_seed.py:44): selected / rejected / ledger / routes / route_trips /
  route_jobs.
- Candidate row fields (jobs.py): job_id, leg_id, order_id, freight_id, leg_kind,
  service_date, service_pc, origin_pc, pallets, source_depot, target_depot, hard_blocker,
  dependency_type.
- Vehicles frame fields: vehicle_id, vehicle_type, home_depot, capacity_pallets, capacity_kg,
  current_lat, current_lon, catchment_km, median_trips_per_day, multi_trip_share.
- Leg-kind constants (route_seed.py:30-33): CUSTOMER_PICKUP, CUSTOMER_DELIVERY,
  DIRECT_CUSTOMER_MOVE, HUB_DROP.
- `tour_plan.py` calls `run_route_seed_plan` TWICE: prepass (line 270) and the real daily
  seed (line 361, `ledger=..., excluded_vehicle_days=reserved`); result exposed as
  `seed.daily` (line 616). Both calls carve (consistent world); `run_alns` reads
  `seed.daily.shuttle_job_ids`.
- ALNS: `improve_route_seed` (alns.py:1136) → `improve_existing_solution` (alns.py:1036) →
  core `improve_solution`. Destroy pool `job_ids = sorted(job_loc)` (alns.py:700); destroy
  ops at alns.py:735-741; targeted ruination specs at alns.py:764-779; EVICTION at
  alns.py:599-618 (`best = (delta, key, candidate_trips, evicted.job_id)`); `_CONSERVE` /
  `_conserve_check` diagnostics; emission via `_records_from_solution` (alns.py:1093).
- ALNS job meta objects (`_build_job_meta`): `.day`, `.eligible_vehicles`,
  `.candidate` (dict of the candidate row). VehicleMeta has `.home_depot`; ALNS has its own
  `_route_vehicle(vehicle_meta[vid], day)` (used at alns.py:1110).

---

### Task 1: Config knobs + shuttle detection/packing module (TDD)

**Files:**
- Modify: `freight_planner/config.py` (append knobs)
- Create: `freight_planner/shuttle.py`
- Create: `tests/freight_planner/test_shuttle.py`

- [ ] **Step 1.1: Append knobs to `freight_planner/config.py`** (leaf module — keep zero imports):

```python
# --- Mega-shipper shuttle carve-out (K1, spec 2026-07-03) -------------------
# An address-day qualifies for dedicated shuttle trips when its same-direction
# volume reaches one artic load; a packed bin only ships as a shuttle trip when
# nearly full (exact-full is unattainable with 1-5-pallet orders).
SHUTTLE_ENABLED: bool = True
SHUTTLE_MIN_PALLETS: float = 26.0
SHUTTLE_MIN_FILL: float = 0.9

# --- Zero-cost same-address merge sweep (K1 component 2) --------------------
# Post-ALNS pass collapsing same-day same-address split visits when the merge is
# feasible and net-km >= 0. Operational realism, not a km saver (replay-proven
# km-neutral) — never applies a net-negative merge.
MERGE_SWEEP_ENABLED: bool = True
```

- [ ] **Step 1.2: Write failing tests for detection + packing** in
  `tests/freight_planner/test_shuttle.py`. Use plain DataFrames — `shuttle.py` is pure.

```python
import pandas as pd
import pytest

from freight_planner.shuttle import ShuttleBin, detect_shuttle_bins, eligible_shuttle_vehicles


def _cand(job_id, pc, pallets, day="2026-01-12", kind="CUSTOMER_PICKUP",
          src="", tgt="CB22", leg=None):
    return {
        "job_id": job_id, "leg_id": leg or job_id.replace("JOB:", ""),
        "order_id": job_id, "freight_id": job_id, "leg_kind": kind,
        "service_date": day, "service_pc": pc, "origin_pc": "",
        "pallets": pallets, "source_depot": src, "target_depot": tgt,
        "hard_blocker": "",
    }


def _vehicles():
    return pd.DataFrame([
        {"vehicle_id": "TRACTOR1", "vehicle_type": "tractor", "home_depot": "CB22",
         "capacity_pallets": 26.0, "capacity_kg": 28000.0},
        {"vehicle_id": "RIGID1", "vehicle_type": "rigid", "home_depot": "CB22",
         "capacity_pallets": 14.0, "capacity_kg": 12000.0},
        {"vehicle_id": "TRACTOR_BED", "vehicle_type": "tractor", "home_depot": "BEDFORD",
         "capacity_pallets": 26.0, "capacity_kg": 28000.0},
    ])


def _options_for(cands, vids=("TRACTOR1", "RIGID1")):
    return {c["leg_id"]: [(v, True) for v in vids] for c in cands}


def test_group_below_threshold_does_not_qualify():
    cands = [_cand(f"JOB:a{i}", "CB9 8QP", 5.0) for i in range(5)]  # 25 pal < 26
    bins = detect_shuttle_bins(pd.DataFrame(cands), _options_for(cands), _vehicles(),
                               min_pallets=26.0, min_fill=0.9)
    assert bins == []


def test_group_at_threshold_qualifies_and_packs_full_bins():
    # 52 pallets -> two full 26-pal artic bins, no residual
    cands = [_cand(f"JOB:b{i}", "CB9 8QP", 4.0) for i in range(13)]  # 52 pal
    bins = detect_shuttle_bins(pd.DataFrame(cands), _options_for(cands), _vehicles(),
                               min_pallets=26.0, min_fill=0.9)
    assert len(bins) == 2
    assert all(b.pallets >= 0.9 * 26.0 for b in bins)
    assert all(b.bin_capacity == 26.0 for b in bins)
    packed = {j for b in bins for j in b.job_ids}
    assert len(packed) == 13  # everything shipped, nothing dropped


def test_partial_bin_stays_residual():
    # 30 pallets on a 26 bin -> one full bin, 1 residual job (4 pal < 0.9*26)
    cands = [_cand(f"JOB:c{i}", "CB9 8QP", 5.0) for i in range(6)]  # 30 pal
    bins = detect_shuttle_bins(pd.DataFrame(cands), _options_for(cands), _vehicles(),
                               min_pallets=26.0, min_fill=0.9)
    assert len(bins) == 1
    assert 0.9 * 26.0 <= bins[0].pallets <= 26.0
    assert len(bins[0].job_ids) == 5  # 25 pal in the bin; 1 job residual


def test_min_fill_boundary():
    # single bin at 23 pal on 26 cap = 0.885 < 0.9 -> not shipped
    cands = [_cand("JOB:d0", "CB9 8QP", 23.0), _cand("JOB:d1", "CB9 8QP", 4.0)]
    # 27 pal qualifies the group; FFD: bin=[23], then 4 fits -> 27 > 26? no: 23+4=27 > 26
    # so bins are [23] and [4]; [23] is 0.885 of 26 -> below min fill -> nothing ships
    bins = detect_shuttle_bins(pd.DataFrame(cands), _options_for(cands), _vehicles(),
                               min_pallets=26.0, min_fill=0.9)
    assert bins == []
    bins = detect_shuttle_bins(pd.DataFrame(cands), _options_for(cands), _vehicles(),
                               min_pallets=26.0, min_fill=0.88)
    assert len(bins) == 1 and bins[0].pallets == 23.0


def test_direction_and_day_are_separate_groups():
    pick = [_cand(f"JOB:e{i}", "CB9 8QP", 7.0, kind="CUSTOMER_PICKUP") for i in range(4)]      # 28
    deli = [_cand(f"JOB:f{i}", "CB9 8QP", 7.0, kind="CUSTOMER_DELIVERY", src="CB22", tgt="")
            for i in range(4)]                                                                  # 28
    other_day = [_cand(f"JOB:g{i}", "CB9 8QP", 7.0, day="2026-01-13") for i in range(2)]        # 14
    cands = pick + deli + other_day
    bins = detect_shuttle_bins(pd.DataFrame(cands), _options_for(cands), _vehicles(),
                               min_pallets=26.0, min_fill=0.9)
    kinds = {(b.service_date, b.leg_kind) for b in bins}
    assert ("2026-01-12", "CUSTOMER_PICKUP") in kinds
    assert ("2026-01-12", "CUSTOMER_DELIVERY") in kinds
    assert not any(b.service_date == "2026-01-13" for b in bins)  # 14 pal never qualifies


def test_direct_and_hub_drop_never_carved():
    cands = [_cand(f"JOB:h{i}", "CB9 8QP", 9.0, kind="DIRECT_CUSTOMER_MOVE") for i in range(4)]
    bins = detect_shuttle_bins(pd.DataFrame(cands), _options_for(cands), _vehicles(),
                               min_pallets=26.0, min_fill=0.9)
    assert bins == []


def test_eligible_vehicles_tractor_first_anchor_only():
    cands = [_cand(f"JOB:k{i}", "CB9 8QP", 9.0) for i in range(3)]
    opts = {c["leg_id"]: [("RIGID1", True), ("TRACTOR1", True), ("TRACTOR_BED", False)]
            for c in cands}
    vids = eligible_shuttle_vehicles(cands, opts, _vehicles(), anchor_depot="CB22")
    assert vids == ["TRACTOR1", "RIGID1"]  # tractor first; BEDFORD tractor filtered by anchor


def test_vehicle_must_be_ok_for_every_job_in_group():
    cands = [_cand(f"JOB:m{i}", "CB9 8QP", 9.0) for i in range(3)]
    opts = _options_for(cands)
    opts[cands[0]["leg_id"]] = [("RIGID1", True)]  # TRACTOR1 not OK for one job
    vids = eligible_shuttle_vehicles(cands, opts, _vehicles(), anchor_depot="CB22")
    assert vids == ["RIGID1"]
```

- [ ] **Step 1.3: Run tests, verify they fail** with `ModuleNotFoundError`:
  `python -m pytest tests/freight_planner/test_shuttle.py -x -q`

- [ ] **Step 1.4: Implement `freight_planner/shuttle.py`**:

```python
"""Mega-shipper shuttle carve-out (K1): detection + packing.

Pure functions over the candidate frame — no ledger, no routing. The seed
applies the bins (route_seed.run_route_seed_plan) with the real evaluators.
Spec: docs/superpowers/specs/2026-07-03-shuttle-carveout-design.md
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from freight_planner.config import SHUTTLE_MIN_FILL, SHUTTLE_MIN_PALLETS

_CARVABLE_KINDS = ("CUSTOMER_PICKUP", "CUSTOMER_DELIVERY")


@dataclass(frozen=True)
class ShuttleBin:
    service_date: str
    service_pc: str
    leg_kind: str
    anchor_depot: str
    job_ids: tuple[str, ...]
    pallets: float
    bin_capacity: float
    eligible_vehicles: tuple[str, ...]  # anchor-depot vehicles, tractors first


def _anchor(row: dict) -> str:
    if str(row.get("leg_kind", "")) == "CUSTOMER_PICKUP":
        return str(row.get("target_depot", "") or "")
    return str(row.get("source_depot", "") or "")


def eligible_shuttle_vehicles(job_rows, options, vehicles: pd.DataFrame,
                              anchor_depot: str) -> list[str]:
    """Vehicles OK for EVERY job, homed at the anchor depot, tractors first."""
    ok: set[str] | None = None
    for row in job_rows:
        vids = {v for v, _same in options.get(str(row.get("leg_id", "")), [])}
        ok = vids if ok is None else (ok & vids)
    if not ok:
        return []
    vrows = vehicles[vehicles["vehicle_id"].astype(str).isin(ok)
                     & vehicles["home_depot"].astype(str).eq(anchor_depot)]
    vrows = vrows.assign(
        _rank=(vrows["vehicle_type"].astype(str) != "tractor").astype(int))
    vrows = vrows.sort_values(["_rank", "capacity_pallets"], ascending=[True, False])
    return [str(v) for v in vrows["vehicle_id"]]


def detect_shuttle_bins(runnable: pd.DataFrame, options, vehicles: pd.DataFrame,
                        min_pallets: float = SHUTTLE_MIN_PALLETS,
                        min_fill: float = SHUTTLE_MIN_FILL) -> list[ShuttleBin]:
    """Qualify address-days and pack them into nearly-full shuttle bins (FFD)."""
    if runnable is None or runnable.empty:
        return []
    df = runnable[runnable["leg_kind"].astype(str).isin(_CARVABLE_KINDS)].copy()
    if df.empty:
        return []
    df["_anchor"] = [_anchor(r) for r in df.to_dict("records")]
    df = df[df["_anchor"] != ""]
    bins: list[ShuttleBin] = []
    for (day, pc, kind, anchor), grp in df.groupby(
            ["service_date", "service_pc", "leg_kind", "_anchor"], sort=True):
        rows = grp.to_dict("records")
        total = sum(float(r.get("pallets") or 0.0) for r in rows)
        if total < float(min_pallets) or str(pc).strip() == "":
            continue
        vids = eligible_shuttle_vehicles(rows, options, vehicles, str(anchor))
        if not vids:
            continue
        cap_by_vid = dict(zip(vehicles["vehicle_id"].astype(str),
                              vehicles["capacity_pallets"].astype(float)))
        bin_cap = max(cap_by_vid.get(v, 0.0) for v in vids)
        if bin_cap <= 0.0:
            continue
        # first-fit-decreasing
        packed: list[list[dict]] = []
        loads: list[float] = []
        for r in sorted(rows, key=lambda r: -float(r.get("pallets") or 0.0)):
            p = float(r.get("pallets") or 0.0)
            for i, load in enumerate(loads):
                if load + p <= bin_cap:
                    packed[i].append(r)
                    loads[i] += p
                    break
            else:
                packed.append([r])
                loads.append(p)
        for jobs, load in zip(packed, loads):
            if load >= float(min_fill) * bin_cap:
                bins.append(ShuttleBin(
                    service_date=str(day), service_pc=str(pc), leg_kind=str(kind),
                    anchor_depot=str(anchor),
                    job_ids=tuple(str(r.get("job_id", "")) for r in jobs),
                    pallets=float(load), bin_capacity=float(bin_cap),
                    eligible_vehicles=tuple(vids)))
    return bins
```

- [ ] **Step 1.5: Run the tests, verify all pass**:
  `python -m pytest tests/freight_planner/test_shuttle.py -q` → all PASS.
  If the FFD walk order makes a specific assertion fail, fix the TEST arithmetic only if the
  implementation matches the spec (FFD, min-fill gate) — never weaken the spec rule.

---

### Task 2: Apply bins in the seed + `shuttle_job_ids` on the result (TDD)

**Files:**
- Modify: `freight_planner/route_seed.py`
- Test: `tests/freight_planner/test_route_seed.py` (append)

- [ ] **Step 2.1: Write the failing integration test.** Follow the existing fixture style in
  `tests/freight_planner/test_route_seed.py` (it already builds candidates/vehicles/
  compatibility frames and monkeypatches routers — REUSE its helpers; read the file first).
  New tests:

```python
def test_shuttle_carveout_builds_dedicated_trips(...existing fixture args...):
    # 6 pickup jobs x 5 pal at "CB9 8QP", one 26-pal tractor at CB22, plus one
    # ordinary 2-pal job elsewhere. Expect: one shuttle trip with 5 of the 6 CB9
    # jobs (25 pal >= 0.9*26), the 6th CB9 job seeded normally (residual), the
    # ordinary job seeded normally, result.shuttle_job_ids == the 5 carved ids,
    # ledger shows the 5 freights at CB22.

def test_shuttle_disabled_is_identical(monkeypatch, ...):
    # monkeypatch freight_planner.route_seed.SHUTTLE_ENABLED to False (import into
    # route_seed namespace, see Step 2.3) -> shuttle_job_ids == set() and the plan
    # equals the pre-task behaviour (same selected count).

def test_shuttle_bin_without_vehicle_dissolves_to_pool(...):
    # same fixture but the tractor's compatibility row excludes the CB9 legs ->
    # detect finds no eligible vehicle -> no shuttle trips, all jobs seeded
    # normally, coverage unchanged, shuttle_job_ids == set().

def test_shuttle_delivery_requires_freight_at_depot(...):
    # a qualifying DELIVERY group whose freight_ids are NOT at the source depot in
    # the ledger -> carve skips them (they stay in the pool and hit the normal
    # DELIVERY_BEFORE_PICKUP path); no phantom ledger transitions.
```

- [ ] **Step 2.2: Run, verify failing** (`AttributeError: shuttle_job_ids` / assertion):
  `python -m pytest tests/freight_planner/test_route_seed.py -q -k shuttle`

- [ ] **Step 2.3: Implement.** In `freight_planner/route_seed.py`:

(a) imports:

```python
from freight_planner.config import SHUTTLE_ENABLED, SHUTTLE_MIN_FILL
from freight_planner.shuttle import detect_shuttle_bins
```

(b) `RouteSeedResult` gains two fields (after `route_jobs`):

```python
    shuttle_job_ids: set = field(default_factory=set)
    shuttle_stats: dict = field(default_factory=dict)
```

(add `field` to the dataclasses import if missing; update BOTH `return RouteSeedResult(...)`
sites — the early-empty return at route_seed.py:200 and the final one — passing the new
fields; the early return passes empties).

(c) carve-out phase in `run_route_seed_plan`, inserted right after
`ordered = sorted(runnable.itertuples(index=False), key=_priority_key)` and after the `_rv` /
`_trip_cap` / `_flatten` defs (it uses `_rv`); processes bins in date order:

```python
    shuttle_job_ids: set[str] = set()
    shuttle_stats: dict = {}
    if SHUTTLE_ENABLED:
        cand_by_job = {str(_g(r, "job_id", "")): r for r in ordered}
        bins = detect_shuttle_bins(runnable, options, vehicles)
        n_trips = n_jobs = 0
        pallets = 0.0
        per_address: dict[tuple[str, str], int] = {}
        for sbin in sorted(bins, key=lambda b: (b.service_date, b.service_pc)):
            day = sbin.service_date
            rows = [cand_by_job[j] for j in sbin.job_ids if j in cand_by_job]
            # freight-readiness gate (deliveries must already sit at the depot)
            ready = []
            for row in rows:
                fid = str(_g(row, "freight_id", _g(row, "order_id", "")) or "")
                if sbin.leg_kind == CUSTOMER_DELIVERY and not ledger.exists_at_depot(
                        fid, sbin.anchor_depot):
                    continue
                ready.append(row)
            load = sum(float(_g(r, "pallets", 0.0) or 0.0) for r in ready)
            if load < SHUTTLE_MIN_FILL * sbin.bin_capacity:  # re-gate after readiness drop
                continue
            rjobs = [make_route_job(r, coords) for r in ready]
            if any(rj is None for rj in rjobs):
                continue
            committed = False
            for vid in sbin.eligible_vehicles:
                if vid not in vrows or (vid, day) in excluded:
                    continue
                veh = _rv(vid, day)
                old_trips, _old_eval = routes.get((vid, day), ([], None))
                candidate_trips = [list(t) for t in old_trips] + [list(rjobs)]
                day_ev = evaluate_day(veh, candidate_trips)
                if not day_ev.feasible:
                    continue
                routes[(vid, day)] = (candidate_trips, day_ev)
                for row in ready:
                    fid = str(_g(row, "freight_id", _g(row, "order_id", "")) or "")
                    if sbin.leg_kind == CUSTOMER_PICKUP:
                        ledger.pickup_to_depot(fid, str(_g(row, "target_depot", "")))
                    else:
                        ledger.deliver_from_depot(fid, str(_g(row, "source_depot", "")))
                    shuttle_job_ids.add(str(_g(row, "job_id", "")))
                n_trips += 1
                n_jobs += len(ready)
                pallets += load
                key = (day, sbin.service_pc)
                per_address[key] = per_address.get(key, 0) + 1
                committed = True
                break
            # not committed -> bin dissolves: jobs simply stay in `ordered`
        if shuttle_job_ids:
            ordered = [j for j in ordered
                       if str(_g(j, "job_id", "")) not in shuttle_job_ids]
        shuttle_stats = {"trips": n_trips, "jobs": n_jobs, "pallets": pallets,
                         "address_days": len(per_address),
                         "top": sorted(per_address.items(),
                                       key=lambda kv: -kv[1])[:5]}
```

Notes for the implementer: `evaluate_day` is already imported by route_seed (check the
imports; if only `try_insert_job` is imported, add `evaluate_day`). `_trip_cap` is
deliberately NOT consulted (spec: dedicated shuttles are the observed multi-trip reality;
`evaluate_day`'s duty/driving caps are the honest limit). The main loop after this phase is
UNCHANGED — it can still top-up shuttle trips via `best_insertion`.

(d) thread the fields into the final `RouteSeedResult(...)` construction:
`shuttle_job_ids=shuttle_job_ids, shuttle_stats=shuttle_stats`.

- [ ] **Step 2.4: Run the new tests + the whole route_seed file**:
  `python -m pytest tests/freight_planner/test_route_seed.py -q` → all PASS.

- [ ] **Step 2.5: Run the seed-adjacent suites**:
  `python -m pytest tests/freight_planner/test_route_seed.py tests/freight_planner/test_tour_plan.py tests/freight_planner/test_alns.py -q`
  → all PASS (tour_plan calls run_route_seed_plan twice; nothing else changes there —
  `seed.daily.shuttle_job_ids` just becomes available).

---

### Task 3: Pin shuttle jobs in ALNS (TDD)

**Files:**
- Modify: `freight_planner/alns.py`
- Modify: `freight_planner/run_alns.py`
- Test: `tests/freight_planner/test_alns.py` (append)

- [ ] **Step 3.1: Write the failing tests** (use the existing `_ChainRouter` /
  vehicle+candidate fixture style already in `tests/freight_planner/test_alns.py` — read the
  B15/B16 tests there first and copy their setup helpers):

```python
def test_pinned_jobs_never_destroyed(...):
    # 2-vehicle solution, 6 jobs, pin 2 of them on vehicle A; run
    # improve_existing_solution(iterations=300, pinned_job_ids={...}).
    # Assert: in the returned solution both pinned jobs are still on vehicle A
    # (same (vid, day) key), and appear exactly once (job conservation).

def test_pinned_jobs_never_evicted(...):
    # Craft a tight-capacity route where inserting an unassigned job would only
    # fit by evicting the pinned job (the eviction branch). Run with the pinned
    # set -> the pinned job keeps its place; the incoming job lands elsewhere or
    # stays unassigned.

def test_no_pinned_set_is_backward_compatible(...):
    # pinned_job_ids=None -> behaviour identical to before (smoke: result equal
    # to a run without the kwarg on the same rng_seed).
```

- [ ] **Step 3.2: Run, verify failing** (TypeError: unexpected kwarg):
  `python -m pytest tests/freight_planner/test_alns.py -q -k pinned`

- [ ] **Step 3.3: Implement pinning in `alns.py`.**

(a) `improve_solution` (the core; `job_ids = sorted(job_loc)` is at alns.py:700) gains
`pinned_job_ids=None` in its signature. At the top:

```python
    pinned = frozenset(str(j) for j in (pinned_job_ids or ()))
```

Replace EVERY rebuild of the destroy pool (grep `job_ids = ` inside `improve_solution`;
there is the init at line 700 and any post-move refresh) with the filtered form:

```python
    job_ids = sorted(j for j in job_loc if j not in pinned)
```

(b) post-filter the destroy ops' output (they walk `routes` directly), right after the
`removed = ...` block at alns.py:735-741:

```python
        if pinned:
            removed = [j for j in removed if j not in pinned]
```

(c) targeted ruination (alns.py:772-775): filter pinned out of each ruined trip list:

```python
                        ruined = [j.job_id for j in trip if j.job_id not in pinned]
```

(d) eviction guard (alns.py:599): inside `for stop_idx, evicted in enumerate(trip):` add
first line:

```python
                if evicted.job_id in pinned:
                    continue
```

(e) `_conserve_check` call sites: extend the FP_ALNS_CONSERVE diagnostics — after each
existing `_conserve_check(...)` in `improve_solution` add (only when `_CONSERVE`):

```python
        if _CONSERVE and pinned:
            assigned = {j.job_id for trips in routes.values() for t in _as_trips(trips) for j in t}
            missing_pins = pinned - assigned
            assert not missing_pins, f"pinned jobs left the solution: {sorted(missing_pins)[:10]}"
```

(implement as a tiny helper `_pinned_check(routes, pinned, where)` next to `_conserve_check`
to avoid triplicating the comprehension).

(f) thread the kwarg: `improve_existing_solution(..., pinned_job_ids=None)` passes through
to `improve_solution`; `improve_route_seed(..., pinned_job_ids=None)` passes through to
`improve_existing_solution`.

(g) `run_alns.py`: at the `improve_route_seed(` call (line 231) add:

```python
                pinned_job_ids=getattr(seed.daily, "shuttle_job_ids", set()),
```

and add the shuttle run-log line right after the `multiday seed` stage block (near the
existing `seed selected=...` log, run_alns.py:222):

```python
    sstats = getattr(seed.daily, "shuttle_stats", {}) or {}
    if sstats.get("trips"):
        top = ", ".join(f"{pc} {d}: {n} trips" for (d, pc), n in sstats.get("top", []))
        runlog.log(
            f"shuttle: {sstats['address_days']} address-days -> {sstats['trips']} trips / "
            f"{sstats['jobs']} jobs / {sstats['pallets']:,.0f} pallets ({top})")
```

- [ ] **Step 3.4: Run**: `python -m pytest tests/freight_planner/test_alns.py -q` → all PASS.

- [ ] **Step 3.5: Conservation smoke with the diagnostics on** (PowerShell):
  `$env:FP_ALNS_CONSERVE = "1"; python -m pytest tests/freight_planner/test_alns.py -q; Remove-Item Env:FP_ALNS_CONSERVE`
  → all PASS.

---

### Task 4: Zero-cost merge sweep module (TDD)

**Files:**
- Create: `freight_planner/merge_sweep.py`
- Create: `tests/freight_planner/test_merge_sweep.py`

- [ ] **Step 4.1: Write failing tests.** The sweep operates on the ALNS solution dict
`(vid, day) -> list[list[RouteJob]]`, ALNS job-meta (`.candidate` dict,
`.eligible_vehicles`), and VehicleMeta. Build minimal RouteJob/VehicleMeta fixtures the same
way `tests/freight_planner/test_alns.py` does (read it first; reuse/adapt its helpers).
Monkeypatch the router (haversine chain) as those tests do. Cases:

```python
def test_applies_net_positive_merge(...):
    # Vehicle A: depot->X->pc->depot (host, passes pc). Vehicle B: depot->pc->depot
    # (guest, ONLY that stop). Merging guest onto A empties B's trip: saving = B's
    # whole round trip, delta ~0 -> applied. Assert: guest job now in A's trips,
    # B's day removed/empty, result.applied == 1, result.km_delta < 0.

def test_skips_net_negative_merge(...):
    # Guest's route serves pc PLUS a far stop beyond it (removal saves ~0), host
    # needs a detour (delta > 0) -> net < 0 -> NOT applied; solution unchanged.

def test_skips_capacity_infeasible(...):
    # Host trip at peak capacity -> try_insert_job infeasible -> skipped, census
    # bucket "TRIP_CAPACITY" == 1.

def test_pinned_guest_never_moves(...):
    # guest job id in pinned -> skipped, census "PINNED".

def test_depot_mismatch_skipped(...):
    # guest source_depot BEDFORD, host vehicle home_depot CB22 -> census
    # "DEPOT_MISMATCH".
```

- [ ] **Step 4.2: Run, verify failing** (ModuleNotFoundError):
  `python -m pytest tests/freight_planner/test_merge_sweep.py -q`

- [ ] **Step 4.3: Implement `freight_planner/merge_sweep.py`**:

```python
"""Zero-cost same-address merge sweep (K1 component 2).

Post-ALNS, collapse same-day same-postcode split visits when the merge is
feasible and net-km >= 0. Operational realism (one truck per dock), NOT a km
saver — the replay proved these merges are km-neutral. Never degrades
feasibility, coverage, or km.
Spec: docs/superpowers/specs/2026-07-03-shuttle-carveout-design.md
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from freight_planner.route_seed import same_order_handoff_conflict
from freight_planner.routing_adapter import evaluate_day, try_insert_job


@dataclass
class MergeSweepResult:
    applied: int = 0
    candidates: int = 0
    rollbacks: int = 0
    km_delta: float = 0.0
    census: Counter = field(default_factory=Counter)


def _day_km(vehicle, trips) -> float:
    ev = evaluate_day(vehicle, trips)
    return ev.total_km if ev.feasible else float("inf")


def apply_zero_cost_merges(solution: dict, job_meta: dict, vehicle_meta: dict,
                           route_vehicle, excluded: set, pinned: frozenset,
                           ) -> MergeSweepResult:
    """One greedy pass. Mutates ``solution`` in place.

    ``route_vehicle`` is alns._route_vehicle (vehicle_meta row + day -> RouteVehicle);
    injected to avoid a circular import.
    """
    res = MergeSweepResult()
    # index customer stops: (day, pc) -> list[(key, trip_idx, job)]
    groups: dict = {}
    for key, trips in solution.items():
        vid, day = key
        for ti, trip in enumerate(trips):
            for job in trip:
                meta = job_meta.get(job.job_id)
                if meta is None:
                    continue
                pc = str(meta.candidate.get("service_pc", "") or "").upper().strip()
                if not pc:
                    continue
                groups.setdefault((day, pc), []).append((key, ti, job))

    for (day, pc), members in sorted(groups.items()):
        vids = {key[0] for key, _ti, _j in members}
        if len(vids) < 2:
            continue
        # host = the (key, trip) holding the most group stops
        by_trip = Counter((key, ti) for key, ti, _j in members)
        (host_key, host_ti), _n = by_trip.most_common(1)[0]
        host_vid, _hday = host_key
        hmeta_v = vehicle_meta.get(host_vid)
        if hmeta_v is None or host_key in excluded:
            continue
        for key, ti, job in list(members):
            if key == host_key:
                continue
            res.candidates += 1
            if job.job_id in pinned:
                res.census["PINNED"] += 1
                continue
            meta = job_meta.get(job.job_id)
            kind = str(meta.candidate.get("leg_kind", "") or "")
            depot_field = "target_depot" if kind == "CUSTOMER_PICKUP" else "source_depot"
            jd = str(meta.candidate.get(depot_field, "") or "")
            if kind in ("CUSTOMER_PICKUP", "CUSTOMER_DELIVERY") and jd \
                    and jd != str(hmeta_v.home_depot):
                res.census["DEPOT_MISMATCH"] += 1
                continue
            if host_vid not in set(meta.eligible_vehicles):
                res.census["OK_SET_EXCLUDED"] += 1
                continue
            host_trips = solution.get(host_key)
            if host_trips is None or host_ti >= len(host_trips):
                res.census["HOST_GONE"] += 1
                continue
            host_trip = host_trips[host_ti]
            if same_order_handoff_conflict(host_trip, job):
                res.census["HANDOFF_CONFLICT"] += 1
                continue
            hveh = route_vehicle(hmeta_v, day)
            trip_ev = try_insert_job(hveh, host_trip, job, "best")
            if not trip_ev.feasible:
                res.census[f"TRIP_{trip_ev.failure_reason or 'NO_POSITION'}"] += 1
                continue
            new_host_trip = [j for j in host_trip] + [job]
            order = {s.job_id: i for i, s in enumerate(trip_ev.stops) if s.job_id}
            new_host_trip.sort(key=lambda j: order.get(j.job_id, len(order)))
            new_host = [list(t) for t in host_trips]
            new_host[host_ti] = new_host_trip
            base_host = _day_km(hveh, host_trips)
            after_host = _day_km(hveh, new_host)
            gmeta_v = vehicle_meta.get(key[0])
            gveh = route_vehicle(gmeta_v, day)
            guest_trips = solution.get(key, [])
            new_guest = [[j for j in t if j.job_id != job.job_id] for t in guest_trips]
            new_guest = [t for t in new_guest if t]
            base_guest = _day_km(gveh, guest_trips)
            after_guest = _day_km(gveh, new_guest) if new_guest else 0.0
            if after_host == float("inf") or after_guest == float("inf"):
                res.rollbacks += 1
                res.census["ROLLBACK"] += 1
                continue
            net = (base_guest - after_guest) - (after_host - base_host)
            if net < 0.0:
                res.census["NET_NEGATIVE"] += 1
                continue
            solution[host_key] = new_host
            if new_guest:
                solution[key] = new_guest
            else:
                solution.pop(key, None)
            res.applied += 1
            res.km_delta += (after_host - base_host) - (base_guest - after_guest)
            res.census["APPLIED"] += 1
    return res
```

Implementer note on the reorder: `trip_ev.stops` timing rows carry `job_id` for customer
stops — verify against `routing_adapter.StopTiming`; if depot rows have empty job_id the
`if s.job_id` filter already handles them. `route_seed._reorder` does the same thing for
RouteJob lists — if its signature fits (`_reorder(jobs, evaluation)`), PREFER importing and
using it instead of the inline sort (DRY).

- [ ] **Step 4.4: Run**: `python -m pytest tests/freight_planner/test_merge_sweep.py -q`
  → all PASS.

---

### Task 5: Wire the sweep into ALNS emission (TDD)

**Files:**
- Modify: `freight_planner/alns.py` (`improve_existing_solution`, alns.py:1036-1133)
- Modify: `freight_planner/run_alns.py` (log line)
- Test: `tests/freight_planner/test_alns.py` (append)

- [ ] **Step 5.1: Failing test**: an `improve_existing_solution` end-to-end fixture where a
net-positive same-pc merge exists (reuse Task 4's two-vehicle geometry); assert the returned
`RouteSeedImprovement.solution` has the guest job on the host vehicle, `selected` records
agree (emission consistency), `km_after == km_before + sweep delta` within 1e-6, and job
conservation holds. Plus a `MERGE_SWEEP_ENABLED=False` monkeypatch test asserting identical
behaviour to pre-task.

- [ ] **Step 5.2: Run, verify failing.**

- [ ] **Step 5.3: Implement.** In `improve_existing_solution`, after
`improvement = improve_solution(...)` (alns.py:1066-1083) and after `final_job_meta` is
built (alns.py:1085-1088) — the sweep needs meta for every served job — insert:

```python
    sweep = None
    if MERGE_SWEEP_ENABLED:
        sweep = apply_zero_cost_merges(
            improvement.solution, final_job_meta, vehicle_meta,
            _route_vehicle, excluded_vehicle_days or set(),
            frozenset(str(j) for j in (pinned_job_ids or ())))
        if sweep.applied:
            improvement.km_after += sweep.km_delta
```

with imports at the top of alns.py:

```python
from freight_planner.config import MERGE_SWEEP_ENABLED
from freight_planner.merge_sweep import apply_zero_cost_merges
```

(`improvement.solution` is mutated in place, so the existing `_records_from_solution`,
`_route_totals_from_solution`, and the `_CONSERVE` emission check at alns.py:1094-1117 all
see the swept solution with no further changes. Attach the census for the caller:
add `merge_sweep: object = None` field to `RouteSeedImprovement` and pass `merge_sweep=sweep`
in the return.)

In `run_alns.py`, after the `best alns km` log (line 259-262):

```python
    ms = getattr(imp, "merge_sweep", None)
    if ms is not None:
        runlog.log(
            f"merge-sweep: applied {ms.applied} of {ms.candidates} candidates "
            f"(km delta {ms.km_delta:+,.1f}, rollbacks {ms.rollbacks}) "
            f"census={dict(ms.census)}")
```

- [ ] **Step 5.4: Run**: `python -m pytest tests/freight_planner/test_alns.py tests/freight_planner/test_merge_sweep.py -q` → all PASS.

- [ ] **Step 5.5: Full suite**: `python -m pytest tests/freight_planner -q` → green
  (±the 3 known environmental failures: test_postcode_resolver / test_routing /
  test_window_start when live OSRM answers cache-miss tests — report them if seen, do not
  chase).

---

### Task 6: Validation runs + measurement (NO tuning loops)

**Files:** none created in-repo; outputs under `freight_planner/out`, probe scripts in the
session scratchpad.

- [ ] **Step 6.1:** Confirm OSRM is up (`http://localhost:5000`). Run wk1 from
  `e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics`:
  `python -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-17`
  (defaults: forward_structural / planning_window / osrm — same as the 91,390 baseline).
- [ ] **Step 6.2:** Run wk2: `python -m freight_planner.run_alns --start 2026-01-19 --end 2026-01-24`
- [ ] **Step 6.3:** Collect, per week, from the run log + plan outputs:
  coverage (must hold 99.7% / 99.8%), total km (baselines 91,390 / 104,743), the `shuttle:`
  and `merge-sweep:` log lines, mean routes per district-day (rerun scratchpad
  `k1_overlap.py`), CB9 8QP vehicles/day (probe from route_stops.csv), redundant
  same-address visits (probe from route_stops.csv, baselines 440/492), pallet-fill
  distribution and vehicle-days (regression watch: emptier general routes).
- [ ] **Step 6.4:** Report the numbers AS THEY LAND — one run per week, no parameter
  tuning, no reruns. A km increase with improved structure metrics is a stakeholder
  conversation, not a silent revert. If coverage drops, STOP and report (do not iterate).
- [ ] **Step 6.5:** Regenerate the trip viz for wk1 via `viz_app.py` (trip_app ONLY — never
  folium viz_map) so shuttle trips are inspectable.

---

## Self-review checklist (done at plan-writing time)

- Spec coverage: detection ✓(T1) packing+min-fill ✓(T1) vehicle order/anchor ✓(T1)
  seed application+ledger+readiness+dissolve ✓(T2) `_trip_cap` bypass ✓(T2 note)
  pinning incl. eviction+conserve ✓(T3) run-log lines ✓(T3g/T5) sweep algorithm+rollback
  ✓(T4) sweep wiring before emission ✓(T5) config knobs ✓(T1) measurement ✓(T6).
- Types: `detect_shuttle_bins(runnable_df, options, vehicles_df, min_pallets, min_fill)`,
  `ShuttleBin.job_ids: tuple[str,...]`, `RouteSeedResult.shuttle_job_ids: set`,
  `improve_*(..., pinned_job_ids)`, `apply_zero_cost_merges(solution, job_meta,
  vehicle_meta, route_vehicle, excluded, pinned)` — used consistently across tasks.
- Known softness (implementer judgment allowed): exact fixture helpers in test files
  (Task 2/3/4 reuse the existing test-file styles rather than inventing new harnesses);
  `_reorder` reuse note in Task 4.
