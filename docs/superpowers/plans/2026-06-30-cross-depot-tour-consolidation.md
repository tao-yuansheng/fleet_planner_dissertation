# Cross-depot tour consolidation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let multi-day far tours pool depot-staged freight across depots into one regional sweep (multi-depot loading), instead of building a separate tour per source depot.

**Architecture:** In `run_multiday_seed_plan`, behind a default-off `consolidate_tours` flag, pool all far jobs and cluster once with the existing cohesion `build_tours` (emergent regions, centroid anchor). A new `resolve_cluster` turns each cluster into `(anchor_depot, ordered_jobs, evaluation)`: single-depot → as today; depot-loadable multi-depot → one tour with front **load-stops** at the other depots; multi-depot containing a far DIRECT move → fall back per source depot. The vehicle-assignment loop is refactored to consume that uniform list, so the flag-off path is unchanged.

**Tech Stack:** Python 3.11, pytest, pandas. Tour layer (`tours.py`, `tour_plan.py`) uses haversine — tests need no OSRM.

> **Session constraint:** do NOT `git commit` (standing instruction). Replace every "Commit" step with the **Checkpoint** shown: run the full freight_planner suite green. Test runner:
> `cd /e/BEAT/ZECURE-Phase2-main/BackEnd/logistics && PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 /e/BEAT/ZECURE-Phase2-main/.venv-1/Scripts/python.exe -m pytest <args>`

---

## File structure

- `freight_planner/tours.py` — add `DEPOT_LOAD` leg-kind constant + `load_stop_job()` factory; `resolve_cluster()` (the per-cluster single/multi/DIRECT-fallback logic). These are pure tour-geometry helpers, so they live with `build_tours`/`evaluate_tour`.
- `freight_planner/tour_plan.py` — refactor the bucket→assign loop to build a uniform `resolved: list[(depot, jobs, evaluation)]`, switched by `consolidate_tours`; emit a `depot_load` route-stop for load-stops at commit.
- `freight_planner/run_alns.py` — `--consolidate-tours` flag → `run_multiday_seed_plan(..., consolidate_tours=...)`.
- `tests/freight_planner/test_tours.py` — `DEPOT_LOAD` walk + `resolve_cluster` cases.
- `tests/freight_planner/test_tour_plan.py` — flag-on consolidation + flag-off identity (existing tests stay green).

---

## Task 1: `DEPOT_LOAD` waypoint + `load_stop_job` factory

**Files:**
- Modify: `freight_planner/tours.py` (add constant + factory near the other leg-kind constants, ~line 49)
- Test: `tests/freight_planner/test_tours.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_tours.py
from freight_planner.tours import DEPOT_LOAD, evaluate_tour, load_stop_job
from freight_planner.routing_adapter import RouteJob, RouteVehicle


def _veh(lat=52.0, lon=0.0):
    return RouteVehicle(vehicle_id="V", start_node="D", start_lat=lat, start_lon=lon,
                        start_time="2026-01-12 06:00:00", capacity_pallets=26.0,
                        capacity_kg=28000.0, vehicle_type="tractor",
                        home_depot="D", home_lat=lat, home_lon=lon)


def test_depot_load_stop_adds_km_but_not_load():
    veh = _veh()
    deliver = RouteJob(job_id="d1", leg_kind="CUSTOMER_DELIVERY", node="C",
                       lat=54.0, lon=-1.0, pallets=5.0, kg=4000.0,
                       earliest_start="", latest_finish="")
    load = load_stop_job("BEDFORD", 52.12, -0.43)
    assert load.leg_kind == DEPOT_LOAD and load.pallets == 0.0

    base = evaluate_tour(veh, [deliver])
    withload = evaluate_tour(veh, [load, deliver])
    assert withload.feasible
    # the load-stop adds its hop km, never changes the carried/peak load
    assert withload.total_km > base.total_km
    assert max(s.load_pallets_after for s in withload.stops) == 5.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `... -m pytest tests/freight_planner/test_tours.py::test_depot_load_stop_adds_km_but_not_load -v`
Expected: FAIL — `ImportError: cannot import name 'DEPOT_LOAD'` / `load_stop_job`.

- [ ] **Step 3: Write minimal implementation**

```python
# freight_planner/tours.py — near CUSTOMER_PICKUP / DIRECT_CUSTOMER_MOVE constants
DEPOT_LOAD = "DEPOT_LOAD"


def load_stop_job(depot: str, lat: float, lon: float) -> RouteJob:
    """A synthetic front-of-tour waypoint: the vehicle calls at `depot` to load its
    staged freight. Zero pallets/kg — the carried load is already counted in the
    deliveries — so it only contributes the depot-hop km. `evaluate_tour`'s default
    waypoint branch handles it; the tour commit skips it (no order)."""
    return RouteJob(job_id=f"LOAD:{depot}", leg_kind=DEPOT_LOAD, node=depot,
                    lat=float(lat), lon=float(lon), pallets=0.0, kg=0.0,
                    earliest_start="", latest_finish="")
```

`evaluate_tour` needs no change: `DEPOT_LOAD` is not a delivery/pickup/hub/two-point kind, so it falls into the existing `else: on_p, on_kg = running_p, running_kg` (tours.py:189) — km counted, load unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `... -m pytest tests/freight_planner/test_tours.py::test_depot_load_stop_adds_km_but_not_load -v`
Expected: PASS.

- [ ] **Step 5: Checkpoint** — `... -m pytest tests/freight_planner/test_tours.py -q` → all green.

---

## Task 2: `resolve_cluster` (single / multi-depot / DIRECT-fallback)

**Files:**
- Modify: `freight_planner/tours.py` (after `build_tours`)
- Test: `tests/freight_planner/test_tours.py`

`resolve_cluster` re-evaluates a cluster against its *real* anchor depot (build_tours' centroid eval was only for clustering) and returns one or more `(anchor_depot, ordered_jobs, evaluation)` triples.

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_tours.py
from freight_planner.tours import resolve_cluster, build_tours

# Two depots in the SE; one far region (Scotland) with one delivery owned by each depot.
_ANCHORS = {"CB22": (52.086, 0.172), "BEDFORD": (52.122, -0.431)}


def _del(jid, lat, lon, pal):
    return RouteJob(job_id=jid, leg_kind="CUSTOMER_DELIVERY", node=jid, lat=lat, lon=lon,
                    pallets=pal, kg=pal * 800.0, earliest_start="", latest_finish="")


def _proto_for(depot):
    lat, lon = _ANCHORS[depot]
    return RouteVehicle(vehicle_id=f"P:{depot}", start_node=depot, start_lat=lat, start_lon=lon,
                        start_time="2026-01-12 06:00:00", capacity_pallets=26.0, capacity_kg=24000.0,
                        vehicle_type="tractor", home_depot=depot, home_lat=lat, home_lon=lon)


def test_multi_depot_delivery_cluster_consolidates_with_load_stops():
    ayr = _del("ayr", 55.46, -4.50, 3.0)        # CB22-owned
    airdrie = _del("air", 55.87, -3.97, 5.0)    # BEDFORD-owned
    src = {"ayr": "CB22", "air": "BEDFORD"}.get
    out = resolve_cluster([ayr, airdrie], src, due_by_job=None, proto_for=_proto_for, anchors=_ANCHORS)

    assert len(out) == 1                                   # one consolidated tour
    depot, ordered, ev = out[0]
    assert depot == "BEDFORD"                              # holds more pallets (5 > 3)
    assert any(j.leg_kind == DEPOT_LOAD for j in ordered)  # a load-stop at CB22
    assert ev.feasible
    assert {j.job_id for j in ordered if j.leg_kind != DEPOT_LOAD} == {"ayr", "air"}


def test_multi_depot_cluster_with_direct_falls_back_per_depot():
    ayr = _del("ayr", 55.46, -4.50, 3.0)
    direct = RouteJob(job_id="dir", leg_kind="DIRECT_CUSTOMER_MOVE", node="dir",
                      lat=55.87, lon=-3.97, pallets=4.0, kg=3000.0, earliest_start="", latest_finish="",
                      origin_lat=52.0, origin_lon=0.2)
    src = {"ayr": "CB22", "dir": "BEDFORD"}.get
    out = resolve_cluster([ayr, direct], src, due_by_job=None, proto_for=_proto_for, anchors=_ANCHORS)

    # not consolidated: one tour per source depot, no load-stops
    assert {d for d, _, _ in out} == {"CB22", "BEDFORD"}
    assert not any(j.leg_kind == DEPOT_LOAD for _, jobs, _ in out for j in jobs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `... -m pytest tests/freight_planner/test_tours.py -k resolve -v`
Expected: FAIL — `cannot import name 'resolve_cluster'`.

- [ ] **Step 3: Write minimal implementation**

```python
# freight_planner/tours.py — after build_tours
def _primary_depot(jobs, depots, source_depot_of):
    def _pal(d):
        return sum(float(j.pallets) for j in jobs if source_depot_of(j.job_id) == d)
    def _kg(d):
        return sum(float(j.kg) for j in jobs if source_depot_of(j.job_id) == d)
    return sorted(depots, key=lambda d: (-_pal(d), -_kg(d), d))[0]


def resolve_cluster(jobs, source_depot_of, due_by_job, proto_for, anchors=DEPOT_ANCHORS,
                    cohesion_km: float = 200.0):
    """Turn one emergent cluster into one or more (anchor_depot, ordered_jobs, evaluation),
    re-evaluated against the real anchor depot:
      * single holding depot          -> one tour anchored there (today's behaviour);
      * multi depot, all depot-loadable -> one tour, front load-stops at the other depots;
      * multi depot incl. a DIRECT move -> fall back to one tour per source depot.
    Infeasible consolidations fall back per depot."""
    depots = {source_depot_of(j.job_id) for j in jobs if source_depot_of(j.job_id) in anchors}
    if not depots:
        lat = sum(j.lat for j in jobs) / len(jobs)
        lon = sum(j.lon for j in jobs) / len(jobs)
        depots = {nearest_depot(lat, lon, anchors)[0]}

    def _build_at(depot, depot_jobs, extra=()):
        proto = proto_for(depot)
        ordered = _order_nearest_neighbour(list(depot_jobs) + list(extra), proto, due_by_job)
        ev = evaluate_tour(proto, ordered, _due_offsets(ordered, due_by_job))
        return depot, ordered, ev

    def _per_depot():
        out = []
        for dep in sorted(depots):
            dep_jobs = [j for j in jobs if source_depot_of(j.job_id) == dep]
            for t in build_tours(dep_jobs, proto_for(dep), cohesion_km, due_by_job):
                out.append((dep, t.jobs, t.evaluation))
        return out

    if len(depots) == 1:
        d, ordered, ev = _build_at(next(iter(depots)), jobs)
        return [(d, ordered, ev)] if ev.feasible else _per_depot()

    if any(j.leg_kind == DIRECT_CUSTOMER_MOVE for j in jobs):
        return _per_depot()

    primary = _primary_depot(jobs, depots, source_depot_of)
    load_stops = [load_stop_job(d, anchors[d][0], anchors[d][1]) for d in sorted(depots - {primary})]
    d, ordered, ev = _build_at(primary, jobs, load_stops)
    return [(d, ordered, ev)] if ev.feasible else _per_depot()
```

(Imports already present in tours.py: `DEPOT_ANCHORS`, `nearest_depot`, `_order_nearest_neighbour`, `_due_offsets`, `evaluate_tour`, `build_tours`, `DIRECT_CUSTOMER_MOVE`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `... -m pytest tests/freight_planner/test_tours.py -k resolve -v`
Expected: PASS (both).

- [ ] **Step 5: Checkpoint** — `... -m pytest tests/freight_planner/test_tours.py -q` → green.

---

## Task 3: Pooled tour building behind `consolidate_tours` flag

**Files:**
- Modify: `freight_planner/tour_plan.py` — `run_multiday_seed_plan` signature + refactor bucket→assign into a uniform `resolved` list.
- Test: `tests/freight_planner/test_tour_plan.py`

Refactor: today's per-depot loop and the new pooled path both produce
`resolved: list[(depot, jobs, evaluation)]`; the existing vehicle-assignment loop consumes it unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/freight_planner/test_tour_plan.py — follow existing fixtures in this file
def test_consolidate_merges_two_depots_one_region(monkeypatch):
    # Build candidates: a CB22-owned and a BEDFORD-owned far DELIVERY in the same region,
    # both due within the span. With consolidate_tours=True they land on ONE tour.
    cands, vehicles, compat, freight, start = _scotland_two_depot_fixture()  # see helper below
    res_on = run_multiday_seed_plan(cands, vehicles, compat, freight, start, consolidate_tours=True)
    tours_on = res_on.tours
    assert len(tours_on) == 1
    veh_ids = {ta.vehicle_id for ta in tours_on}
    assert len(veh_ids) == 1                                  # one vehicle, not two
    # both deliveries served on that single tour
    served = {rj.job_id for ta in tours_on for rj in ta.jobs if rj.leg_kind != "DEPOT_LOAD"}
    assert served == {"JOB:ayr:D", "JOB:air:D"}

    res_off = run_multiday_seed_plan(cands, vehicles, compat, freight, start, consolidate_tours=False)
    assert len(res_off.tours) == 2                            # default: two separate tours
```

Add a `_scotland_two_depot_fixture()` helper in the test file building two FULL_FLEET
prestaged far deliveries (CB22 + BEDFORD source depots) ~50 km apart in Scotland, due same span,
plus two tractors (one per depot). (Model on the existing tour_plan fixtures.)

- [ ] **Step 2: Run test to verify it fails**

Run: `... -m pytest tests/freight_planner/test_tour_plan.py::test_consolidate_merges_two_depots_one_region -v`
Expected: FAIL — `run_multiday_seed_plan() got an unexpected keyword argument 'consolidate_tours'`.

- [ ] **Step 3: Write minimal implementation**

```python
# freight_planner/tour_plan.py
# (a) signature
def run_multiday_seed_plan(candidates, vehicles, compatibility, freight_states, start,
                           plan_id="MULTIDAY", cohesion_km=200.0, consolidate_tours=False):

# (b) a centroid proto for clustering (near _proto_vehicle)
def _centroid_proto(day):
    lats = [a[0] for a in DEPOT_ANCHORS.values()]
    lons = [a[1] for a in DEPOT_ANCHORS.values()]
    return RouteVehicle(vehicle_id="PROTO:POOL", start_node="POOL",
                        start_lat=sum(lats) / len(lats), start_lon=sum(lons) / len(lons),
                        start_time=f"{day} 06:00:00", capacity_pallets=_PROTO_CAPACITY_P,
                        capacity_kg=_PROTO_CAPACITY_KG, vehicle_type="tractor",
                        home_depot="POOL", home_lat=sum(lats) / len(lats), home_lon=sum(lons) / len(lons))

# (c) replace the `for depot, rjobs in buckets.items(): for tour in build_tours(...)` assignment
#     loop body so it first computes `resolved`, then assigns vehicles over `resolved`.
src_of = {jid: str(meta.get("source_depot", "")) for jid, meta in job_meta.items()}.get

def _source_depot_of(job_id):
    return src_of(job_id, "")

if consolidate_tours:
    pooled = [rj for rjobs in buckets.values() for rj in rjobs]
    clusters = build_tours(pooled, _centroid_proto(start.isoformat()), cohesion_km=cohesion_km,
                           due_by_job=due_by_job)
    resolved = []
    for tour in clusters:
        resolved += resolve_cluster(tour.jobs, _source_depot_of, due_by_job,
                                    lambda d: _proto_vehicle(d, start.isoformat()),
                                    cohesion_km=cohesion_km)
else:
    resolved = []
    for depot, rjobs in buckets.items():
        for tour in build_tours(rjobs, _proto_vehicle(depot, start.isoformat()),
                                cohesion_km=cohesion_km, due_by_job=due_by_job):
            resolved.append((depot, tour.jobs, tour.evaluation))

for depot, jobs, evaluation in resolved:
    if not evaluation.feasible:
        tour_rejected.extend(RejectedJob(j.job_id, "NO_FEASIBLE_TOUR")
                             for j in jobs if j.leg_kind != DEPOT_LOAD)
        continue
    day = min((due_by_job.get(j.job_id, start.isoformat()) for j in jobs
               if j.leg_kind != DEPOT_LOAD), default=start.isoformat())
    span = _span(day, evaluation.days)
    free = [route_vehicles[vid] for vid in route_vehicles
            if all((vid, s) not in reserved for s in span)]
    busyness = {v.vehicle_id: sum(busy_by_vd.get((v.vehicle_id, s), 0) for s in span) for v in free}
    total_p = sum(float(j.pallets) for j in jobs)
    total_kg = sum(float(j.kg) for j in jobs)
    chosen = select_tour_vehicle(total_p, free, busyness=busyness, prefer_depot=depot, tour_kg=total_kg)
    if chosen is None:
        tour_rejected.extend(RejectedJob(j.job_id, "NO_FEASIBLE_TOUR")
                             for j in jobs if j.leg_kind != DEPOT_LOAD)
        continue
    for s in span:
        reserved.add((chosen.vehicle_id, s))
    tour_assignments.append(TourAssignment(chosen.vehicle_id, day, evaluation.days, jobs, evaluation))
```

Import `resolve_cluster`, `DEPOT_LOAD` from `freight_planner.tours`. Keep `Tour.total_pallets/total_kg`
usage replaced by the explicit `total_p/total_kg` sums (jobs now may include zero-load load-stops).

- [ ] **Step 4: Run tests to verify they pass**

Run: `... -m pytest tests/freight_planner/test_tour_plan.py -v`
Expected: PASS — new test green AND all existing tour_plan tests green (flag-off path unchanged).

- [ ] **Step 5: Checkpoint** — `... -m pytest tests/freight_planner/test_tours.py tests/freight_planner/test_tour_plan.py -q` → green.

---

## Task 4: Commit emits a `depot_load` route-stop (km reconciliation)

**Files:**
- Modify: `freight_planner/tour_plan.py` — the tour commit loop (`for sequence, rjob in enumerate(ta.jobs, ...)`).
- Test: `tests/freight_planner/test_tour_plan.py`

Load-stops have no `job_meta` entry, so the current commit `continue`s past them — correct for the
ledger (no order) but it drops their hop km from the stop rows. Emit a depot_load stop so route_stops
km reconciles with the tour evaluation.

- [ ] **Step 1: Write the failing test**

```python
def test_consolidated_tour_emits_depot_load_stop():
    cands, vehicles, compat, freight, start = _scotland_two_depot_fixture()
    res = run_multiday_seed_plan(cands, vehicles, compat, freight, start, consolidate_tours=True)
    kinds = [r.freight_state_after for r in res.tour_records]  # sanity: records exist
    # a depot_load stop is present among the committed tour records
    load_rows = [r for r in res.tour_records if getattr(r, "assignment_reason", "") == "DEPOT_LOAD"]
    assert load_rows, "expected a depot_load stop row for the cross-depot load"
```

(If `SelectedPlanRecord` has no field that distinguishes a depot-load stop, assert via the stop
`node`/`order_id` your builder sets — match the field actually emitted; adjust the assertion to the
real schema after Step 3.)

- [ ] **Step 2: Run test to verify it fails**

Run: `... -m pytest tests/freight_planner/test_tour_plan.py::test_consolidated_tour_emits_depot_load_stop -v`
Expected: FAIL — no depot_load row emitted.

- [ ] **Step 3: Write minimal implementation**

```python
# freight_planner/tour_plan.py — inside the commit loop, before the `cand is None: continue`
if rjob.leg_kind == DEPOT_LOAD:
    stop = stop_by_job.get(rjob.job_id)
    day_iso = (date.fromisoformat(ta.start_date)
               + timedelta(days=stop.day_index)).isoformat() if stop else ta.start_date
    builder.assign(
        route_id=f"TOUR:{ta.vehicle_id}:{ta.start_date}",
        vehicle_id=ta.vehicle_id, vehicle_home_depot=home, sequence=sequence,
        job={"order_id": "", "leg_id": "", "job_id": rjob.job_id, "service_pc": rjob.node,
             "source_depot": rjob.node, "target_depot": rjob.node, "leg_kind": DEPOT_LOAD,
             "pallets": 0.0, "weight_kg": 0.0, "service_lat": rjob.lat, "service_lon": rjob.lon},
        assignment_reason="DEPOT_LOAD",
        planned_arrive=f"{day_iso} 12:00:00", planned_depart=f"{day_iso} 12:00:00",
        planned_km=stop.leg_km if stop else 0.0,
        planned_drive_minutes=longhaul_drive_minutes(stop.leg_km) if stop else 0.0,
        load_pallets_after=stop.load_pallets_after if stop else 0.0,
        load_kg_after=stop.load_kg_after if stop else 0.0,
        freight_state_after="AT_DEPOT",
    )
    continue
```

Verify `builder.assign` accepts a job dict lacking a candidate row (it reads via `.get`). If
`build_plan_records`/`SelectedPlanBuilder` requires specific keys, supply them in the dict above
(match the schema seen in `plan_records.py`). Adjust the Step-1 assertion to the field actually set.

- [ ] **Step 4: Run test to verify it passes**

Run: `... -m pytest tests/freight_planner/test_tour_plan.py -v`
Expected: PASS, existing tests still green.

- [ ] **Step 5: Checkpoint** — full suite: `... -m pytest tests/freight_planner/ -q` → green.

---

## Task 5: Wire `--consolidate-tours` + measure

**Files:**
- Modify: `freight_planner/run_alns.py` — arg + pass-through.

- [ ] **Step 1: Add the flag**

```python
# in build the argparse block
parser.add_argument("--consolidate-tours", action="store_true",
                    help="pool far depot-staged jobs across depots into shared regional tours")
# at the multiday seed call
seed = run_multiday_seed_plan(candidate_df, vehicle_df, compatibility_df, freight_states_df, start,
                              consolidate_tours=args.consolidate_tours)
```

- [ ] **Step 2: Full suite green**

Run: `... -m pytest tests/freight_planner/ tests/planning_agent/ -q`
Expected: PASS.

- [ ] **Step 3: A/B measure (manual, scratch out-dirs)**

Run flag-off and flag-on on 2026-01-12→17, OSRM warmed, equal ALNS budget, to separate `--out-dir`s.
Compare on the **final plan**: total km, tour vehicle-days, coverage (assigned %), and confirm the
Scotland pair (`X888RNW:2026-01-13` + `W88RNW:2026-01-15`) becomes one tour. Require 0 temporal / 0
ledger violations. Record the result in `freight_planner/QUEST_LOG.md`.

---

## Self-review notes (planner)

- **Spec coverage:** §1 core mechanism → Task 3 (pool + `_centroid_proto` + `resolve_cluster`); §2 route/feasibility/vehicle → Tasks 1–2 (load-stops, re-eval at primary, `select_tour_vehicle` on combined load); §3 eligibility → `resolve_cluster` three branches (Task 2) + flag-off identity (Task 3); testing §→ Tasks 1–4; measurement §→ Task 5.
- **Regression safety nuance (refines spec):** flag-**off** reproduces today's plan exactly (old per-depot path, restructured only to emit the shared `resolved` list — guarded by existing tour_plan tests). Under flag-**on**, single-depot clusters are *equivalent* but not necessarily byte-identical, because pooled clustering uses the centroid seed rather than a per-depot seed. The regression guarantee is the default-off path; that's why the flag exists.
- **DIRECT fallback** keeps Stoke-style work exactly as today.
- **Open detail to confirm at Task 4:** the exact `SelectedPlanRecord`/`build_plan_records` fields for a non-order stop — adjust the dict + assertion to the real schema when implementing.
