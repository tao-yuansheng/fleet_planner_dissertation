# Daily Depot-Collocated DIRECT Consolidation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** A same-day FULL_FLEET DIRECT whose origin sits within 2 km of its source depot is emitted as a depot-loaded `CUSTOMER_DELIVERY` (single leg) carrying a trip-wide departure floor and a hard depot-bound gate, so same-origin orders co-load into one multi-drop run instead of atomic out-and-back arcs.

**Spec:** `docs/superpowers/specs/2026-07-17-daily-depot-direct-consolidation-design.md`

**Architecture:** One emission-site change in `legs.py` (all downstream frames inherit), two evaluator mechanisms in `routing_adapter.py` (trip-wide `depart_floor`, new `DEPOT_BOUND` infeasibility), column plumbing legs→candidates→RouteJob, a belt-and-braces tour-candidate gate, and CLI knobs mirroring the shipped tour-side flag.

**Environment:** repo root for all commands: `e:\BEAT\ZECURE-Phase2-main\BackEnd\logistics`. NOT a git repository — no commits; verification is pytest (`python -m pytest tests/freight_planner -q`, 914 green baseline). TDD: run each new test RED before implementing.

---

### Task 1: Config knobs

**Files:** Modify: `freight_planner/config.py` (next to the tour knobs, after `TOUR_DEPOT_DIRECT_AS_DELIVERY` block)

- [x] **Step 1.1** Add:

```python
DAILY_DEPOT_DIRECT_AS_DELIVERY: bool = True  # a same-day DIRECT whose origin is collocated with its
                                             # source depot is emitted as a depot-loaded delivery so
                                             # same-origin orders co-load (ST4 8JB, 2026-07-17).
                                             # --no-daily-depot-direct-as-delivery ablates.
DAILY_ORIGIN_AT_DEPOT_RADIUS_KM: float = 2.0  # collocation radius for the DAILY rule — deliberately
                                              # tighter than the tour side's 8 km: on a daily trip an
                                              # unpriced approach is real km, not tour noise.
COLLOCATED_STAGING_MIN: float = 30.0  # collection window open -> freight loadable at the dock
                                      # (drives the reclassified leg's departure floor)
```

Verified by the flag/emission tests in Tasks 3 and 7.

### Task 2: `routing_adapter.py` — `depot_bound` gate + trip-wide `depart_floor`

**Files:** Modify: `freight_planner/routing_adapter.py`; Test: `tests/freight_planner/test_routing_adapter.py`

- [x] **Step 2.1 RED** — add to `test_routing_adapter.py` (reuse the file's existing `RouteVehicle`/`RouteJob` fixture style; vehicle factory has `home_depot`):

```python
def test_depot_bound_job_infeasible_on_foreign_homed_vehicle():
    veh = _vehicle(home_depot="CB22")          # file's existing vehicle factory
    job = _job(leg_kind="CUSTOMER_DELIVERY", depot_bound="STOKE")
    ev = evaluate_route(veh, [job])
    assert not ev.feasible and ev.failure_reason == "DEPOT_BOUND"

def test_depot_bound_job_feasible_on_home_vehicle():
    veh = _vehicle(home_depot="STOKE")
    job = _job(leg_kind="CUSTOMER_DELIVERY", depot_bound="STOKE")
    assert evaluate_route(veh, [job]).feasible

def test_depart_floor_on_mid_trip_job_holds_departure():
    veh = _vehicle(start_time="2026-01-12 06:00:00")
    lead = _job(leg_kind="CUSTOMER_DELIVERY")                     # no floor
    tail = _job(leg_kind="CUSTOMER_DELIVERY", depart_floor="2026-01-12 11:00:00")
    ev = evaluate_route(veh, [lead, tail])
    assert ev.feasible and ev.route_start >= "2026-01-12 11:00:00"
```

Run: `python -m pytest tests/freight_planner/test_routing_adapter.py -q` → expect the 3 new tests FAIL (unknown field / floor ignored).

- [x] **Step 2.2 GREEN** — in `RouteJob`, after `depart_floor: str = ""` add:

```python
    # Collocated depot-delivery (2026-07-17): freight physically sits at THIS depot,
    # so only a vehicle homed there may carry the job. "" = unconstrained.
    depot_bound: str = ""
```

In `evaluate_route`, immediately after `start_iso = _iso(start_dt)` add the gate:

```python
    for _j in ordered_jobs:
        _db = str(getattr(_j, "depot_bound", "") or "")
        if _db and str(vehicle.home_depot) != _db:
            # the freight sits at _db; a foreign-homed vehicle would board it at the
            # wrong depot (the daily model has no mid-route depot-load stop)
            return _infeasible("DEPOT_BOUND", start_iso)
```

Replace the lead-job floor block (the `if first_stop:` / `df = _parse(getattr(job, "depart_floor", ...))` lines inside the loop) with a pre-loop trip-wide floor, placed right after `first_stop = True`:

```python
    # Departure floor binds TRIP-WIDE: depot-loaded freight boards at departure
    # wherever its job rides in the sequence (B2 departure-based flooring — the
    # vehicle waits at the DEPOT). max() over members; lead-job-only was a
    # special case and lost the hold for mid-trip prestaged jobs.
    floors = [f for f in (_parse(getattr(j, "depart_floor", "") or "") for j in ordered_jobs)
              if f is not None]
    dfloor = max(floors) if floors else None
    if dfloor is not None and dfloor > clock:
        route_start_shift += (dfloor - clock).total_seconds() / 60.0
        clock = dfloor
```

(The `first_stop` flag stays — it still drives first-stop wait absorption.)

- [x] **Step 2.3** Run the full file: `python -m pytest tests/freight_planner/test_routing_adapter.py -q` → all PASS including the two shipped lead-floor tests (lead case ⊂ new rule).

### Task 3: `legs.py` — reclassified emission

**Files:** Modify: `freight_planner/legs.py`; Test: `tests/freight_planner/test_options_legs.py` (same-day option-trio fixtures live here)

- [x] **Step 3.1 RED** — using the file's existing same-day FULL_FLEET fixture (qargo row + DemandRecord + postcode cache), add a variant whose origin geocodes within 2 km of `DEPOT_ANCHORS["STOKE"]` (origin pc "ST4 8JB" with cache coords `(52.9668, -2.1672)`):

```python
def test_collocated_same_day_direct_emits_single_depot_delivery():
    legs = _build(...origin_pc="ST4 8JB", collocated_coords...)
    mine = [l for l in legs if l.order_id == OID]
    assert len(mine) == 1
    (leg,) = mine
    assert leg.leg_id.endswith(":DIR")            # identity preserved
    assert leg.leg_kind == "CUSTOMER_DELIVERY"
    assert leg.origin_node == "DEPOT" and leg.ready_state == "AT_DEPOT"
    assert leg.depot_bound == "STOKE"
    assert leg.origin_lat is None and leg.origin_pc == "ST4 8JB"
    assert leg.option_set == ""                   # no XC/XD alternative emitted
    assert leg.depart_floor == leg.freight_ready_time != ""
    # floor = collection effective open + COLLOCATED_STAGING_MIN
    assert leg.depart_floor == _plus_minutes(PICKUP_EFFECTIVE_START, 30)

def test_far_origin_same_day_keeps_option_trio():
    legs = _build(...origin 60 km away...)
    assert sorted(l.leg_id.rsplit(":", 1)[1] for l in legs) == ["DIR", "XC", "XD"]

def test_flag_off_restores_option_trio(monkeypatch):
    monkeypatch.setattr(_fp_config, "DAILY_DEPOT_DIRECT_AS_DELIVERY", False)
    legs = _build(...collocated...)
    assert len([l for l in legs if l.order_id == OID]) == 3
```

Run → FAIL (3 legs emitted / missing fields).

- [x] **Step 3.2 GREEN** — `legs.py` changes:

1. Imports: `from freight_planner.shared.config import DEPOT_ANCHORS`, `from freight_planner.route_costs import haversine_km`, `from freight_planner import config as _fp_config`.
2. `MovementLegRecord`: after `origin_pc` add
   ```python
   depart_floor: str = ""  # collocated depot-delivery: trip may not DEPART before this
   depot_bound: str = ""   # collocated depot-delivery: serving vehicle must be homed here
   ```
3. `_leg(...)`: accept and pass through `depart_floor: str = ""` / `depot_bound: str = ""`.
4. Helpers near `staged_delivery_start`:
   ```python
   def _collocated_with_depot(o_lat, o_lon, depot: str) -> bool:
       """True when a DIRECT's collection origin sits on the source depot's estate:
       collecting it is a dock move the delivering vehicle makes at departure, so the
       leg is functionally a depot-loaded delivery (daily analogue of the tour rule)."""
       if not _fp_config.DAILY_DEPOT_DIRECT_AS_DELIVERY or o_lat is None or o_lon is None:
           return False
       anchor = DEPOT_ANCHORS.get(str(depot or ""))
       if anchor is None:
           return False
       return haversine_km(float(o_lat), float(o_lon), anchor[0], anchor[1]) \
           <= float(_fp_config.DAILY_ORIGIN_AT_DEPOT_RADIUS_KM)

   def _collocated_ready(pes: str) -> str:
       """Departure floor for a collocated depot-delivery: collection open + staging.
       Window-open anchored (non-anticipative), NOT deadline+90 — the deadline
       pessimism is what killed same-day XDOCK for these orders."""
       try:
           t = datetime.fromisoformat(str(pes))
       except (TypeError, ValueError):
           return ""
       return (t + timedelta(minutes=float(_fp_config.COLLOCATED_STAGING_MIN))).isoformat(sep=" ")
   ```
5. Same-day branch: inside `for tag, fid, part_p, part_kg in parts:` guard the trio:
   ```python
   if _collocated_with_depot(o_lat, o_lon, origin_depot):
       ready = _collocated_ready(pes)
       out.append(_leg(
           record=record, suffix=_suffix("DIR", tag), flow=flow, leg_kind=CUSTOMER_DELIVERY,
           service_date=service_date,
           origin_node=DEPOT, destination_node=CUSTOMER,
           service_pc=dest_pc, source_depot=origin_depot, target_depot=dest_depot,
           ready_state="AT_DEPOT", result_state="DELIVERED",
           raw_window_start=drs, raw_window_end=dre,
           effective_window_start=des, effective_window_end=dee,
           window_hardness=dh, freight_ready_time=ready,
           cache=postcode_cache, origin_pc=origin_pc,
           freight_id=fid, pallets=part_p, weight_kg=part_kg,
           depart_floor=ready, depot_bound=origin_depot,
       ))
       n_collocated += 1
       continue
   ```
   (keep the existing DIR/XC/XD emission as the else-path; count logged once per build: `collocated depot-deliveries: {n} (radius {r} km)` — print/log like neighbouring build messages.)

- [x] **Step 3.3** `python -m pytest tests/freight_planner/test_options_legs.py -q` → PASS.

### Task 4: Plumbing — candidates + `make_route_job`

**Files:** Modify: `freight_planner/jobs.py`, `freight_planner/route_seed.py`; Test: `tests/freight_planner/test_route_seed.py`

- [x] **Step 4.1 RED**:

```python
def test_make_route_job_carries_floor_and_bound():
    row = _cand_row(leg_kind="CUSTOMER_DELIVERY", depart_floor="2026-01-12 06:30:00",
                    depot_bound="STOKE")   # namedtuple/dict fixture per file style
    rj = make_route_job(row, {row.leg_id: (52.0, -1.0)})
    assert rj.depart_floor == "2026-01-12 06:30:00" and rj.depot_bound == "STOKE"
```

Plus one candidates-frame assertion in the legs test (Task 3 file): `candidate_jobs_frame(...)` on a reclassified leg carries both columns.

- [x] **Step 4.2 GREEN**:
  - `jobs.py` `CandidateJobRecord`: after `day_flex_min` add `depart_floor: str = ""` and `depot_bound: str = ""`; in `build_candidate_jobs` mapping add
    `depart_floor=str(row.get("depart_floor") or "")`, `depot_bound=str(row.get("depot_bound") or "")`.
  - `route_seed.py` `make_route_job`: add
    `depart_floor=str(_g(job, "depart_floor", "") or "")`, `depot_bound=str(_g(job, "depot_bound", "") or "")`.
- [x] **Step 4.3** Run both test files → PASS.

### Task 5: Freight-state pin (no production change expected)

**Files:** Test only — the file already covering `build_initial_freight_states` (`test_planner_state.py` / `test_phase0_spine.py`; find with `grep -l build_initial_freight_states tests/freight_planner`).

- [x] **Step 5.1** Pin test (expected to PASS immediately — characterization of the delivery-only branch):

```python
def test_collocated_reclassified_order_starts_at_depot():
    # demand row FULL_END_TO_END + legs df with the ONE reclassified delivery leg
    recs = build_initial_freight_states(demand_df, legs_df, planning_start=d)
    (st,) = [r for r in recs if r.order_id == OID]
    assert st.initial_state == "AT_DEPOT_OR_HUB_PENDING"
    assert st.initial_depot == "STOKE" and st.ready_time == READY_ISO
```

If it fails, STOP and re-read `state.py` — the design depends on this branch.

### Task 6: Tour belt-and-braces + micro floor-stomp guard

**Files:** Modify: `freight_planner/tour_attach.py`, `freight_planner/run_rolling.py`; Tests: `tests/freight_planner/test_tour_commission.py`, `tests/freight_planner/test_micro_pass.py`

- [x] **Step 6.1 RED** (commission gate — mirror the WT254009 tests in the file):

```python
def test_depot_bound_delivery_never_commissions_foreign_vehicle():
    cand = _candidate(job=_delivery_job(depot_bound="STOKE"), target_depot="")
    picked = commission_intraday([cand], idle_vehicles=[_bedford_veh()], ...)
    assert picked == []   # honest fall-through, not a wrong-depot tour
```

And the micro guard (direct call on `new_arrival_meta` with a candidate row whose `depart_floor` column post-dates the micro floor):

```python
def test_new_arrival_keeps_later_column_floor():
    meta = new_arrival_meta({OID}, inputs, day, floor_iso="2026-01-12 10:30:00")
    assert meta[JOB_ID].rjob.depart_floor == "2026-01-12 11:00:00"  # column value, later
```

- [x] **Step 6.2 GREEN**:
  - `tour_attach.Candidate`: add `depot_bound: str = ""`.
  - `_depot_bound_mismatch`: extend the return to
    ```python
    if str(cand.depot_bound or "") and str(home_depot) != str(cand.depot_bound):
        return True   # collocated depot-delivery: freight sits at ITS depot
    return (str(cand.job.leg_kind) == "CUSTOMER_PICKUP"
            and bool(cand.target_depot)
            and str(home_depot) != str(cand.target_depot))
    ```
  - `run_rolling._tour_attach_candidates`: stamp `depot_bound=str(getattr(row, "depot_bound", "") or "")` in the `Candidate(...)` call.
  - `run_rolling.new_arrival_meta`: replace the unconditional `rj = replace(rj, depart_floor=floor_iso)` with
    ```python
    df0 = str(getattr(rj, "depart_floor", "") or "")
    rj = replace(rj, depart_floor=max(df0, floor_iso) if df0 else floor_iso)
    ```
- [x] **Step 6.3** Run both files → PASS.

### Task 7: CLI flags (both entrypoints)

**Files:** Modify: `freight_planner/run_alns.py`, `freight_planner/run_rolling.py`; Test: the file holding the `--tour-depot-direct-as-delivery` mapping tests (`grep -rl "tour_depot_direct_as_delivery" tests/freight_planner`).

- [x] **Step 7.1 RED**: flag-mapping tests (mirror the tour flag's): `--no-daily-depot-direct-as-delivery` → config False; `--daily-depot-direct-radius-km 5` → 5.0; `--collocated-staging-min 0` → 0.0.
- [x] **Step 7.2 GREEN** — in BOTH argparse blocks (next to the tour flag):

```python
    parser.add_argument("--daily-depot-direct-as-delivery",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="emit a same-day DIRECT collected AT its source depot as a depot-loaded "
                             "delivery so same-origin orders co-load (default: config, ON since "
                             "2026-07-17). --no-daily-depot-direct-as-delivery = legacy atomic arcs")
    parser.add_argument("--daily-depot-direct-radius-km", type=float, default=None,
                        help="collocation radius (km) for the daily depot-direct rule (default: config, 2.0)")
    parser.add_argument("--collocated-staging-min", type=float, default=None,
                        help="minutes from collection-window open to the freight being loadable at the "
                             "dock — the reclassified leg's departure floor (default: config, 30)")
```

And in BOTH `_apply_vehicle_day_cost_flags` functions:

```python
    if getattr(args, "daily_depot_direct_as_delivery", None) is not None:
        _fp_cfg.DAILY_DEPOT_DIRECT_AS_DELIVERY = bool(args.daily_depot_direct_as_delivery)
    if getattr(args, "daily_depot_direct_radius_km", None) is not None:
        _fp_cfg.DAILY_ORIGIN_AT_DEPOT_RADIUS_KM = float(args.daily_depot_direct_radius_km)
    if getattr(args, "collocated_staging_min", None) is not None:
        _fp_cfg.COLLOCATED_STAGING_MIN = float(args.collocated_staging_min)
```

- [x] **Step 7.3** Flag tests PASS. Also confirm `tests/freight_planner/conftest.py`'s autouse config-reset fixture covers the three new attributes (add them if it enumerates names).

### Task 8: Full suite, docs, probe run

- [x] **Step 8.1** `python -m pytest tests/freight_planner -q` → everything green (914 baseline + new). Fix any fallout (e.g. schema/dictionary tests that pin candidate columns).
- [x] **Step 8.2** Docs: RULES.md — corollary under the depot rules: *a collocated-origin same-day DIRECT is a depot-loaded delivery; its trip departs no earlier than collection-open + staging; only home-depot vehicles may carry it (DEPOT_BOUND)*. PIPELINE.md — leg-emission note + the trip-wide floor semantics. DESIGN_LOG.md — dated entry (what/why/receipts). Note the micro-pass nuance (intraday-booked collocated orders wait for the next anchor/warm re-opt — existing "deliveries are anchor-planned" rule).
- [x] **Step 8.3** Probe: rerun the 2-day window with the depot-gate command (argv in `run_depotgate/.../` runlog), out dir `run_collocated`. Acceptance per spec §7: seven ST4 8JB orders as co-loaded STOKE deliveries, no ST4 8JB ping-pong, km/veh-days down, ledger ≥ 453/0/0, plus `--no-daily-depot-direct-as-delivery` ablation reproducing the old shape if time allows.
- [x] **Step 8.4** Memory file + MEMORY.md line.

## Self-review

- Spec §3.1→Task 3, §3.2→Tasks 2/4/6, §3.3→Tasks 2/6, §3.4→Tasks 1/7, §5 tests→Tasks 2-7, §7→Task 8. No gaps.
- No placeholders: every code step carries the code; test fixtures reference existing factories by file.
- Type consistency: `depart_floor`/`depot_bound` are `str` end-to-end (leg record → candidate record → RouteJob → gate).

---

## Execution record (2026-07-17, inline)

All tasks executed TDD (RED verified before each GREEN). Suite: **922 passed** (was 914).

**Deviations from the plan as written:**

1. **B2 suffix guard added to `alns.py` (not in the plan).** The trip-wide floor made the
   shipped test `test_micro_pass_reuses_returned_vehicle_for_late_floored_job` fail: a
   floored job could now join a LAUNCHED trip's open suffix and silently re-time the
   committed prefix (`floor_ok` checks the re-timed deviation point AGAINST the floor,
   not that it stayed put — the old lead-only read was accidentally load-bearing).
   Fix: `_retimes_committed_departure` helper beside `floor_ok`, checked at all three
   insertion doors (`_ranked_inserts_for_job`, `_best_insert_for_job` normal + eject
   branches) using per-key base trip starts; the floored job falls through to a fresh
   trip — the exact Jan-13 reuse shape the test pins.
2. **`test_legs_staging.py::test_stoke_origin_fullfleet_is_unchanged` updated** (renamed
   `..._anchors_at_stoke`): its fixture used the depot's own postcode as the origin, the
   exact shape the new rule reclassifies. The pinned intent (FULL_FLEET anchors at the
   ORIGIN depot) survives, asserted on the reclassified delivery leg + its depot_bound.
3. Task 5's pin test passed immediately as predicted (characterization of the existing
   delivery-only state branch — no production change).
