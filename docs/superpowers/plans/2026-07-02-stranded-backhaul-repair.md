# Stranded-Order Backhaul Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After the daily seed, flip only stranded XDOCK orders (pickup NO_FEASIBLE_ROUTE/NO_FEASIBLE_TOUR + delivery unassigned) to synthetic DIRECT jobs and serve them by insertion into existing tours (backhaul) or as new batched DIRECT tours.

**Architecture:** A repair step inside `run_multiday_seed_plan` (`freight_planner/tour_plan.py`) between the daily seed and the tour ledger commit. Evaluator support (`floor_offsets`/EARLY + `try_insert_tour_job`) lives in `freight_planner/tours.py`. Superseded legs are re-reasoned `REPAIRED_DIRECT` (ALNS ignores them automatically — not in `_REPAIRABLE_REASONS`); synthetic candidate rows thread to reports via `MultidaySeedResult.repaired_candidates`.

**Tech Stack:** Python 3, pytest, pandas. Spec: `docs/superpowers/specs/2026-07-02-stranded-backhaul-repair-design.md`.

**Session constraints:** NO `git commit`. Test prefix (Git Bash):
`cd /e/BEAT/ZECURE-Phase2-main/BackEnd/logistics && PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1 /e/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe -m pytest ...`

**Verified structural facts:**
- Daily strands: `RouteSeedResult.rejected` (`RejectedJob(job_id, reason)`); tour strands: `tour_rejected` list; both flow into `MultidaySeedResult.rejected` → manifest copies reason strings verbatim.
- ALNS re-inserts only reasons in `alns._REPAIRABLE_REASONS`; anything else passes through untouched (`_repairable_unassigned_meta`).
- Tour commit already handles `DIRECT_CUSTOMER_MOVE` (`_commit_leg` → `deliver_direct`; freight must exist and not be delivered — stranded orders are `AT_CUSTOMER_ORIGIN` ✓).
- `TourAssignment(vehicle_id, start_date, days, jobs, evaluation)` is a mutable dataclass, constructed in ONE place in the assignment loop.
- `build_route_stops` takes stop coords only from the compatibility frame — synthetic RD legs need a candidate-dict fallback.
- ST4-pickup strands are *fleet-contention* NFR (pickup at the Stoke yard, compat-eligible vehicles all tour-reserved); NE42 is a genuine far pickup. Both present as pickup-rejected + delivery-DBP.

---

### Task 1: Config knob

**Files:** Modify `cambridge/config.py` (tour-knob block, after `TOUR_DAY_START_HOUR`)

- [ ] **Step 1:** Add:

```python
STRANDED_REPAIR_ENABLED: bool = True           # post-seed DIRECT backhaul repair of stranded XDOCK orders
```

- [ ] **Step 2:** Verify: `python -c "from cambridge.config import STRANDED_REPAIR_ENABLED; print('ok')"` → `ok`

---

### Task 2: `floor_offsets` / EARLY in `evaluate_tour`

**Files:** Modify `freight_planner/tours.py` (evaluate_tour); Test `tests/freight_planner/test_tours.py`

- [ ] **Step 1: Failing test** (append to test_tours.py):

```python
def test_floor_offsets_stop_reached_before_its_ready_day_is_early():
    # the GLA stop is reachable on day 0, but its freight only exists from day 1
    # (a DIRECT collection ready tomorrow): serving it on day 0 is infeasible EARLY.
    veh = _tractor()
    jobs = [_job("GLA", *GLASGOW, pallets=4.0)]
    ev = evaluate_tour(veh, jobs, due_offsets={"GLA": 2}, floor_offsets={"GLA": 1})
    assert ev.feasible is False
    assert ev.reason == "EARLY"
    # floor 0 (already available) stays feasible
    ok = evaluate_tour(veh, jobs, due_offsets={"GLA": 2}, floor_offsets={"GLA": 0})
    assert ok.feasible
```

- [ ] **Step 2:** Run `pytest tests/freight_planner/test_tours.py -k floor_offsets -v` → FAIL (unexpected kwarg).

- [ ] **Step 3: Implement.** `evaluate_tour` signature gains `floor_offsets: dict | None = None` (after `elapsed_cap_min`). Directly below the LATE check insert:

```python
        # readiness floor: a stop reached before its freight exists (a DIRECT
        # collection not yet ready at the origin) is infeasible EARLY
        if floor_offsets and job.job_id in floor_offsets and day_index < int(floor_offsets[job.job_id]):
            return _infeasible_tour("EARLY")
```

Docstring: add one line — "``floor_offsets`` (job_id -> earliest day offset) is the mirror: a stop reached before its floor is infeasible (EARLY)."

- [ ] **Step 4:** `pytest tests/freight_planner/test_tours.py -q` → ALL PASS.

---

### Task 3: `try_insert_tour_job`

**Files:** Modify `freight_planner/tours.py` (below `evaluate_tour`); Test `tests/freight_planner/test_tours.py`

- [ ] **Step 1: Failing test:**

```python
def test_try_insert_tour_job_finds_the_empty_backhaul_slot_for_a_full_load():
    # tour: two 13-pallet drops (26 aboard at departure). A 26-pallet DIRECT can
    # only ride AFTER both drops (transient load on the emptied trailer) — the
    # X8RNW/NE42 backhaul. The helper must find that position.
    from freight_planner.tours import try_insert_tour_job
    veh = _tractor()
    d1 = _job("D1", *GLASGOW, pallets=13.0, kg=6000.0)
    d2 = _job("D2", *CARLISLE, pallets=13.0, kg=6000.0)
    base = [d1, d2]
    rd = RouteJob(job_id="RD:X", leg_kind="DIRECT_CUSTOMER_MOVE", node="SG1",
                  lat=51.90, lon=-0.20, pallets=26.0, kg=5655.0,
                  origin_lat=54.96, origin_lon=-1.85, order_id="X")   # NE42 -> SG1
    got = try_insert_tour_job(veh, base, rd, due_offsets={"D1": 0, "D2": 0, "RD:X": 2})
    assert got is not None
    new_jobs, new_eval = got
    assert new_eval.feasible
    assert [j.job_id for j in new_jobs].index("RD:X") == 2   # after both drops
    assert new_eval.peak_pallets <= 26.0 + 1e-6
```

- [ ] **Step 2:** Run `-k backhaul_slot` → FAIL (ImportError).

- [ ] **Step 3: Implement** in tours.py:

```python
def try_insert_tour_job(vehicle: RouteVehicle, jobs: list[RouteJob], candidate: RouteJob,
                        due_offsets: dict | None = None,
                        floor_offsets: dict | None = None):
    """Insert ``candidate`` into an existing tour at the best feasible position.

    Tries every position, evaluates with the full tour physics (capacity peak,
    two-cap day split, LATE, EARLY), and returns ``(new_jobs, evaluation)`` for
    the feasible insertion with the lowest total km — or ``None`` if no position
    fits. Used by the stranded-order backhaul repair: a full-load DIRECT can only
    ride where the trailer is empty, which this search finds naturally."""
    best = None
    for pos in range(len(jobs) + 1):
        seq = list(jobs)
        seq.insert(pos, candidate)
        ev = evaluate_tour(vehicle, seq, due_offsets, floor_offsets=floor_offsets)
        if ev.feasible and (best is None or ev.total_km < best[1].total_km):
            best = (seq, ev)
    return best
```

- [ ] **Step 4:** `pytest tests/freight_planner/test_tours.py -q` → ALL PASS.

---

### Task 4: `TourAssignment.depot`

**Files:** Modify `freight_planner/tour_plan.py`

- [ ] **Step 1:** Append a defaulted field (after `evaluation`):

```python
@dataclass
class TourAssignment:
    vehicle_id: str
    start_date: str
    days: int
    jobs: list           # list[RouteJob], in visit order
    evaluation: TourEvaluation
    depot: str = ""      # anchor depot the evaluation was built against
```

In the assignment loop where `TourAssignment(...)` is constructed, pass `depot=depot`. (Covered by Task 6 integration tests.)

- [ ] **Step 2:** `pytest tests/freight_planner/test_tour_plan.py -q` → ALL PASS.

---

### Task 5: route_stops coord fallback for synthetic legs

**Files:** Modify `freight_planner/manifest.py` (`build_route_stops` per-stop loop); Test `tests/freight_planner/test_manifest_kpi.py`

- [ ] **Step 1: Failing test:**

```python
def test_route_stops_falls_back_to_candidate_coords_for_synthetic_legs():
    # a repaired DIRECT leg has no compatibility row; its coords come from the
    # candidate dict (service_lat/service_lon) so the viz can still draw it.
    selected = _route_stop_selected([
        ["TOUR:V1:2026-01-15", "V1", "CB22", "2026-01-15", 1, "O1:RD", "O1",
         "DIRECT_CUSTOMER_MOVE", "CUSTOMER", "CUSTOMER",
         "2026-01-15 12:00:00", "2026-01-15 13:00:00", 500.0, 0.0, 0.0],
    ])
    cand = pd.DataFrame([{
        "job_id": "RD:O1", "leg_id": "O1:RD", "order_id": "O1",
        "leg_kind": "DIRECT_CUSTOMER_MOVE", "service_pc": "SG1 2FW",
        "source_depot": "STOKE", "target_depot": "STOKE", "hard_blocker": "",
        "service_lat": 51.90, "service_lon": -0.20,
        "origin_lat": 54.96, "origin_lon": -1.85, "origin_pc": "NE42 6HE",
    }])
    compat = pd.DataFrame([{"leg_id": "ZZZ", "compatibility_status": "OK",
                            "service_lat": 0.0, "service_lon": 0.0}])
    vehicles = pd.DataFrame([{"vehicle_id": "V1", "home_depot": "CB22",
                              "current_lat": 52.07, "current_lon": 0.17}])
    stops = build_route_stops(selected, cand, compat, vehicles)
    row = stops[stops.order_id == "O1"].iloc[0]
    assert (row["lat"], row["lon"]) == (51.90, -0.20)
    assert row["collect_pc"] == "NE42 6HE"
```

- [ ] **Step 2:** Run → FAIL (lat is NaN).

- [ ] **Step 3: Implement** in `build_route_stops`, after `lat, lon = leg_coord.get(leg, (np.nan, np.nan))`:

```python
            if (pd.isna(lat) or pd.isna(lon)) and c.get("service_lat") is not None:
                lat, lon = _opt(c.get("service_lat")), _opt(c.get("service_lon"))
```

- [ ] **Step 4:** `pytest tests/freight_planner/test_manifest_kpi.py -q` → ALL PASS.

---

### Task 6: The repair step

**Files:** Modify `freight_planner/tour_plan.py` (repair block + result threading), `freight_planner/run_alns.py` (report frame concat); Test `tests/freight_planner/test_tour_plan.py`

- [ ] **Step 1: Failing integration tests** (append; reuse module fixtures):

```python
def _stranded_pair(oid, pick_pc, pick_loc, dele_pc, dele_loc, *, pal=4.0,
                   pick_day="2026-01-05", dele_day="2026-01-06", vehicles=("ST1",)):
    """An XDOCK pickup+delivery pair compat-limited to `vehicles`."""
    cands = [
        _cand(leg_id=f"{oid}:C", order_id=oid, leg_kind="CUSTOMER_PICKUP",
              service_pc=pick_pc, service_date=pick_day, pallets=pal,
              dependency_type="", option_group="XDOCK", option_set=oid,
              preferred_start_node="CUSTOMER", preferred_end_node="DEPOT",
              source_depot="STOKE", target_depot="STOKE"),
        _cand(leg_id=f"{oid}:D", order_id=oid, leg_kind="CUSTOMER_DELIVERY",
              service_pc=dele_pc, service_date=dele_day, pallets=pal,
              dependency_type="XDOCK_DELIVERY", predecessor_leg_id=f"{oid}:C",
              option_group="XDOCK", option_set=oid,
              source_depot="STOKE", target_depot="STOKE"),
    ]
    compat = ([_compat(f"{oid}:C", v, pick_loc) for v in vehicles]
              + [_compat(f"{oid}:D", v, dele_loc) for v in vehicles])
    freight = {"freight_id": oid, "initial_state": "AT_CUSTOMER_ORIGIN", "initial_depot": ""}
    return cands, compat, freight


STOKE_YARD = (52.9674, -2.1666)
LONDON = (51.50, -0.12)


def test_repair_batches_stranded_stoke_orders_onto_a_new_direct_tour():
    # ST1 is consumed by a far tour; two ST4->London XDOCK orders strand
    # (pickup NO_FEASIBLE_ROUTE: their only compat vehicle is reserved).
    # The repair must serve them as ONE new DIRECT tour on the free CB1.
    vehicles = _vehicles([{"vehicle_id": "ST1", "home_depot": "STOKE", "vtype": "tractor"},
                          {"vehicle_id": "CB1", "home_depot": "CB22", "vtype": "tractor"}])
    far = [_cand(leg_id="FAR:D", order_id="FAR", service_pc="G1", pallets=4.0)]
    far_compat = [_compat("FAR:D", "ST1", GLASGOW)]   # only ST1 -> tour reserves ST1
    c1, k1, f1 = _stranded_pair("S1", "ST4", STOKE_YARD, "KT9", LONDON)
    c2, k2, f2 = _stranded_pair("S2", "ST4", STOKE_YARD, "SW19", (51.42, -0.21))
    candidates = pd.DataFrame(far + c1 + c2)
    compat = pd.DataFrame(far_compat + k1 + k2)
    freight = pd.DataFrame([_prestaged("FAR", "STOKE"), f1, f2])

    res = run_multiday_seed_plan(candidates, vehicles, compat, freight, date(2026, 1, 5))

    rd_tours = [ta for ta in res.tours
                if any(str(j.job_id).startswith("RD:") for j in ta.jobs)]
    assert rd_tours, "stranded orders should be repaired onto a tour"
    rd_jobs = {j.job_id for ta in rd_tours for j in ta.jobs if str(j.job_id).startswith("RD:")}
    assert rd_jobs == {"RD:S1", "RD:S2"}
    assert len(rd_tours) == 1                                   # batched, not two runs
    assert rd_tours[0].vehicle_id == "CB1"                      # any-depot vehicle
    reasons = {rj.job_id: rj.reason for rj in res.rejected}
    assert reasons.get("JOB:S1:C") == "REPAIRED_DIRECT"
    assert reasons.get("JOB:S1:D") == "REPAIRED_DIRECT"
    assert {"RD:S1", "RD:S2"} <= {r.job_id for r in res.selected}
    assert len(plan_ledger_violations(res.selected, candidates)) == 0


def test_repair_attaches_a_backhaul_to_an_existing_tour():
    # one far prestaged delivery forms a Glasgow tour on ST1; a stranded order
    # collects at CARLISLE (on the way home) and delivers near base. With no
    # second vehicle, only Mode 1 (attach) can serve it.
    vehicles = _vehicles([{"vehicle_id": "ST1", "home_depot": "CB22", "vtype": "tractor"}])
    far = [_cand(leg_id="FAR:D", order_id="FAR", service_pc="G1", pallets=4.0)]
    far_compat = [_compat("FAR:D", "ST1", GLASGOW)]
    c1, k1, f1 = _stranded_pair("B1", "CAR", CARLISLE, "SG1", (51.90, -0.20),
                                pick_day="2026-01-05", dele_day="2026-01-07")
    candidates = pd.DataFrame(far + c1)
    compat = pd.DataFrame(far_compat + k1)
    freight = pd.DataFrame([_prestaged("FAR", "CB22"), f1])

    res = run_multiday_seed_plan(candidates, vehicles, compat, freight, date(2026, 1, 5))

    host = [ta for ta in res.tours if any(j.job_id == "JOB:FAR:D" for j in ta.jobs)]
    assert host and any(j.job_id == "RD:B1" for j in host[0].jobs)   # rode the SAME tour
    assert "RD:B1" in {r.job_id for r in res.selected}


def test_repair_leaves_orders_honestly_unassigned_when_no_vehicle_fits():
    # single vehicle fully consumed by the far tour AND the stranded delivery is
    # due before the tour could reach it -> repair fails, reasons unchanged.
    vehicles = _vehicles([{"vehicle_id": "ST1", "home_depot": "STOKE", "vtype": "tractor"}])
    far = [_cand(leg_id="FAR:D", order_id="FAR", service_pc="G1", pallets=4.0)]
    far_compat = [_compat("FAR:D", "ST1", GLASGOW)]
    c1, k1, f1 = _stranded_pair("N1", "ST4", STOKE_YARD, "KT9", LONDON,
                                pick_day="2026-01-05", dele_day="2026-01-05")
    candidates = pd.DataFrame(far + c1)
    compat = pd.DataFrame(far_compat + k1)
    freight = pd.DataFrame([_prestaged("FAR", "STOKE"), f1])

    res = run_multiday_seed_plan(candidates, vehicles, compat, freight, date(2026, 1, 5))

    assert "RD:N1" not in {r.job_id for r in res.selected}
    reasons = {rj.job_id: rj.reason for rj in res.rejected}
    assert reasons.get("JOB:N1:C") in ("NO_FEASIBLE_ROUTE", "NO_OK_VEHICLE_PAIR")
```

(Exact rejection reason in the last test may vary — assert it is NOT `REPAIRED_DIRECT` and the order is not selected; adjust the tuple to observed reasons at RED stage.)

- [ ] **Step 2:** Run the three tests → FAIL (no repair step; no RD jobs).

- [ ] **Step 3: Implement in `tour_plan.py`.**

3a. Imports: add `STRANDED_REPAIR_ENABLED` to the cambridge.config import; add `try_insert_tour_job` to the freight_planner.tours import; `from dataclasses import replace as _dc_replace` (module already imports dataclass utilities — extend as needed).

3b. Extract the assignment-loop body into a closure so Mode 2 reuses it. Replace the current `for depot, jobs, evaluation in resolved:` block with:

```python
    def _assign_one(depot, jobs, evaluation, *, extra_busy=frozenset()) -> bool:
        real_jobs = [j for j in jobs if j.leg_kind != DEPOT_LOAD]
        if not evaluation.feasible:
            tour_rejected.extend(RejectedJob(j.job_id, "NO_FEASIBLE_TOUR") for j in real_jobs)
            return False
        # the sweep starts when the earliest stop in the batch is due — but never
        # before every member's freight has reached the depot (safety net for a
        # singleton whose feeding pickup lands after its own due date)
        day = min((due_by_job.get(j.job_id, start.isoformat()) for j in real_jobs),
                  default=start.isoformat())
        max_ready = max((ready_by_job.get(j.job_id, "") for j in real_jobs), default="")
        if max_ready and max_ready > day:
            day = max_ready
        span = _span(day, evaluation.days)
        free = [route_vehicles[vid] for vid in route_vehicles
                if all((vid, s) not in reserved and (vid, s) not in extra_busy for s in span)]
        busyness = {v.vehicle_id: sum(busy_by_vd.get((v.vehicle_id, s), 0) for s in span)
                    for v in free}
        chosen = select_tour_vehicle(evaluation.peak_pallets, free,
                                     busyness=busyness, prefer_depot=depot,
                                     tour_kg=evaluation.peak_kg)
        if chosen is None:
            tour_rejected.extend(RejectedJob(j.job_id, "NO_FEASIBLE_TOUR") for j in real_jobs)
            return False
        for s in span:
            reserved.add((chosen.vehicle_id, s))
        tour_assignments.append(TourAssignment(chosen.vehicle_id, day, evaluation.days,
                                               jobs, evaluation, depot=depot))
        return True

    for depot, jobs, evaluation in resolved:
        _assign_one(depot, jobs, evaluation)
```

3c. After `daily_result = run_route_seed_plan(...)` (step 3) and BEFORE the `builder = SelectedPlanBuilder(...)` commit, insert the repair block:

```python
    # ---- stranded-order backhaul repair (post-seed, pre-commit) ----------------
    # The residual tail is XDOCK orders whose far/contended pickup stranded and
    # whose delivery cascaded. Reality (X8RNW telematics) serves these as DIRECT
    # carries on tours' empty legs. Flip ONLY that pattern to a synthetic DIRECT
    # and (Mode 1) insert into an existing tour, else (Mode 2) batch leftovers
    # into new DIRECT tours via the normal machinery. Strictly bounded: both legs
    # must have stranded.
    repaired_order_ids: set[str] = set()
    repaired_candidates: list[dict] = []
    if STRANDED_REPAIR_ENABLED:
        _STRAND_PICK = {"NO_FEASIBLE_ROUTE", "NO_FEASIBLE_TOUR", "NO_OK_VEHICLE_PAIR"}
        cand_by_job = {str(c.get("job_id")): c for c in candidates.to_dict("records")}
        all_rej = list(daily_result.rejected) + list(tour_rejected)
        rej_reason = {str(rj.job_id): str(rj.reason) for rj in all_rej}
        committed_job_ids = {j.job_id for ta in tour_assignments for j in ta.jobs}
        daily_busy = set(daily_result.routes.keys())

        strands: dict[str, dict[str, dict]] = {}
        for jid, reason in rej_reason.items():
            cand = cand_by_job.get(jid)
            if cand is None or str(cand.get("option_group", "")) != "XDOCK":
                continue
            kind = str(cand.get("leg_kind", ""))
            if kind == "CUSTOMER_PICKUP" and reason in _STRAND_PICK:
                strands.setdefault(str(cand.get("order_id", "")), {})["P"] = cand
            elif kind == "CUSTOMER_DELIVERY":
                strands.setdefault(str(cand.get("order_id", "")), {})["D"] = cand

        rd_jobs: list = []
        rd_meta: dict[str, tuple[dict, dict]] = {}
        for oid, legs2 in sorted(strands.items()):
            pick, dele = legs2.get("P"), legs2.get("D")
            if pick is None or dele is None:
                continue
            pj, dj = str(pick.get("job_id")), str(dele.get("job_id"))
            if pj in committed_job_ids or dj in committed_job_ids:
                continue                        # never repair a partially served order
            o = coords.get(str(pick.get("leg_id", ""))); d = coords.get(str(dele.get("leg_id", "")))
            if not o or not d:
                continue
            rd = RouteJob(job_id=f"RD:{oid}", leg_kind=DIRECT_CUSTOMER_MOVE,
                          node=str(dele.get("service_pc", "")), lat=d[0], lon=d[1],
                          pallets=float(dele.get("pallets", 0.0) or 0.0),
                          kg=float(dele.get("weight_kg", 0.0) or 0.0),
                          origin_lat=o[0], origin_lon=o[1], order_id=oid)
            due_by_job[rd.job_id] = str(dele.get("service_date", "")) or start.isoformat()
            ready_by_job[rd.job_id] = str(pick.get("service_date", "")) or start.isoformat()
            rd_jobs.append(rd)
            rd_meta[rd.job_id] = (pick, dele)

        def _days_from(base_iso: str, day_iso: str) -> int:
            return (date.fromisoformat(day_iso) - date.fromisoformat(base_iso)).days

        leftovers: list = []
        for rd in rd_jobs:
            due, ready = due_by_job[rd.job_id], ready_by_job[rd.job_id]
            best = None  # (added_km, ta, new_jobs, new_eval, extra_days)
            for ta in tour_assignments:
                if due < ta.start_date or not ta.depot:
                    continue
                offs = {j.job_id: max(0, _days_from(ta.start_date, due_by_job[j.job_id]))
                        for j in ta.jobs if j.leg_kind != DEPOT_LOAD and j.job_id in due_by_job}
                offs[rd.job_id] = _days_from(ta.start_date, due)
                floors = {rd.job_id: max(0, _days_from(ta.start_date, ready))}
                vrow = vrows.get(ta.vehicle_id)
                proto = _proto_vehicle(ta.depot, ta.start_date)
                veh = _dc_replace(
                    proto,
                    capacity_pallets=float(_g(vrow, "capacity_pallets", proto.capacity_pallets)),
                    capacity_kg=float(_g(vrow, "capacity_kg", proto.capacity_kg)))
                got = try_insert_tour_job(veh, list(ta.jobs), rd,
                                          due_offsets=offs, floor_offsets=floors)
                if got is None:
                    continue
                new_jobs, new_eval = got
                extra = [s for s in _span(ta.start_date, new_eval.days)
                         if s not in _span(ta.start_date, ta.days)]
                if any((ta.vehicle_id, s) in reserved or (ta.vehicle_id, s) in daily_busy
                       for s in extra):
                    continue
                added = new_eval.total_km - ta.evaluation.total_km
                if best is None or added < best[0]:
                    best = (added, ta, new_jobs, new_eval, extra)
            if best is not None:
                _, ta, new_jobs, new_eval, extra = best
                ta.jobs = new_jobs
                ta.evaluation = new_eval
                ta.days = new_eval.days
                for s in extra:
                    reserved.add((ta.vehicle_id, s))
                repaired_order_ids.add(rd.order_id)
            else:
                leftovers.append(rd)

        if leftovers:
            pre = len(tour_assignments)
            for tour in build_tours(leftovers, _centroid_proto(start.isoformat()),
                                    cohesion_km=cohesion_km, due_by_job=due_by_job,
                                    ready_by_job=ready_by_job):
                for depot2, jobs2, ev2 in _resolve(tour.jobs):
                    _assign_one(depot2, jobs2, ev2, extra_busy=daily_busy)
            for ta in tour_assignments[pre:]:
                repaired_order_ids.update(j.order_id for j in ta.jobs
                                          if str(j.job_id).startswith("RD:"))

        # bookkeeping for the repaired: commit metadata + reasons + report rows
        for rd in rd_jobs:
            if rd.order_id not in repaired_order_ids:
                due_by_job.pop(rd.job_id, None); ready_by_job.pop(rd.job_id, None)
                continue
            pick, dele = rd_meta[rd.job_id]
            job_meta[rd.job_id] = {
                "job_id": rd.job_id, "leg_id": f"{rd.order_id}:RD", "order_id": rd.order_id,
                "freight_id": str(pick.get("freight_id", "") or rd.order_id),
                "leg_kind": DIRECT_CUSTOMER_MOVE,
                "service_date": due_by_job[rd.job_id],
                "service_pc": str(dele.get("service_pc", "")),
                "source_depot": str(pick.get("source_depot", "")),
                "target_depot": str(dele.get("target_depot", "")),
                "preferred_start_node": "CUSTOMER", "preferred_end_node": "CUSTOMER",
                "service_lat": rd.lat, "service_lon": rd.lon,
                "origin_lat": rd.origin_lat, "origin_lon": rd.origin_lon,
                "origin_pc": str(pick.get("service_pc", "")),
            }
            repaired_candidates.append(dict(job_meta[rd.job_id], hard_blocker=""))
        if repaired_order_ids:
            def _relabel(rejs):
                return [RejectedJob(rj.job_id, "REPAIRED_DIRECT")
                        if str(cand_by_job.get(str(rj.job_id), {}).get("order_id", ""))
                        in repaired_order_ids else rj
                        for rj in rejs]
            daily_result.rejected[:] = _relabel(daily_result.rejected)
            tour_rejected[:] = _relabel(tour_rejected)
```

NOTE for the engineer: `tour_rejected` accumulates BAD_GEOCODE/NO_FEASIBLE_TOUR before this point; `RejectedJob` may be frozen — the relabel builds new instances, fine either way. `coords`, `vrows`, `_g`, `job_meta`, `due_by_job`, `ready_by_job`, `_resolve`, `busy_by_vd` are all in scope at this location.

3d. Thread the results: `MultidaySeedResult` gains two defaulted fields:

```python
    repaired_order_ids: set = None
    repaired_candidates: list = None
```

and the constructor call at the end of `run_multiday_seed_plan` passes them.

3e. `run_alns.py`: where reports/manifest are built with `candidate_df`, create once:

```python
    report_candidate_df = candidate_df
    if getattr(seed, "repaired_candidates", None):
        report_candidate_df = pd.concat(
            [candidate_df, pd.DataFrame(seed.repaired_candidates)], ignore_index=True)
```

and use `report_candidate_df` in the report-building calls (the ALNS `improve_route_seed` call keeps the ORIGINAL `candidate_df` — repaired legs must not enter the optimizer).

- [ ] **Step 4:** Run the three tests → PASS (iterate on fixture geometry if a case fails for setup reasons — the assertions are the contract).

- [ ] **Step 5:** `pytest tests/freight_planner/ tests/cambridge/ -q` → ALL PASS (~645).

---

### Task 7: Full-run validation + logs

- [ ] **Step 1:** Both weeks (background, OSRM up):

```bash
python -m freight_planner.run_alns --start 2026-01-12 --end 2026-01-17 --time-budget 90 --out-dir freight_planner/out
python -m freight_planner.run_alns --start 2026-01-19 --end 2026-01-24 --time-budget 90 --out-dir freight_planner/out
```

- [ ] **Step 2:** Acceptance vs baseline (wk1 99.4% / 103,188 km; wk2 99.5% / 107,122 km):
  - NFR+DBP tail ≤ 2/wk (SN5 case may remain); `REPAIRED_DIRECT` rows appear in the unassigned table.
  - Coverage wk1 → ~99.7%, wk2 → ~99.8%; total km rise ≤ ~2k/wk; tour km must NOT balloon.
  - 0 temporal / 0 ledger / 0 phantom (repaired DIRECTs must not confuse the validators).
  - **Flagship:** order `27623f3d` (NE42→SG1, 26 pal) selected as `RD:27623f3d…` on a northbound tour, positioned after that tour's drops.

- [ ] **Step 3:** Regenerate trip_app for both weeks (viz_app only). Inspect one repaired tour's stops render (coords fallback working).

- [ ] **Step 4:** QUEST_LOG session entry + memory update. NO git commit.
