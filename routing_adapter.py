"""Milestone 4: route sequencing adapter.

Replaces one-job-per-vehicle assignment with a real multi-stop route evaluator.
Given a vehicle and an ordered list of jobs, it walks the sequence computing
distance, drive/service/wait time, the load timeline, and the node timeline, and
flags physical infeasibility (capacity, time window).

The interface (`evaluate_route`, `try_insert_job`) is deliberately decoupled from
the candidate-job DataFrame schema so the existing VRPTW/ALNS solver can drive it
later (Milestone 5). Distance/time/service come from `route_costs` (Q1 reuse of
the old calibration).

Load model (M4 altitude):
  * deliveries are loaded at the depot before departure (initial load) and
    dropped at their stop;
  * pickups add load at their stop and are carried onward;
  * direct moves are carried over their inbound segment (momentary load).
Multi-stop on the same route is now physical; trunk legs and multiday spans are
later milestones.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from functools import lru_cache

import pandas as pd

from freight_planner.shared.config import MAX_DRIVING_H_PER_DAY
from freight_planner import config as _fp_cfg
from freight_planner.config import MAX_STOP_WAIT_MIN
from freight_planner.route_costs import drive_minutes, road_km, road_minutes, service_minutes, statutory_breaks

CUSTOMER_PICKUP = "CUSTOMER_PICKUP"
CUSTOMER_DELIVERY = "CUSTOMER_DELIVERY"
DIRECT_CUSTOMER_MOVE = "DIRECT_CUSTOMER_MOVE"
HUB_DROP = "HUB_DROP"

# Legs collected at a customer origin then driven to a destination in one move.
_TWO_POINT_KINDS = (DIRECT_CUSTOMER_MOVE, HUB_DROP)

_EPS = 1e-6
# non-anticipation: tolerance (minutes) for an early drive-up before a
# no_early_arrival collection's booking — 1 s absorbs whole-second route-timing
# rounding against the creation-floored earliest_start (matches the audit).
_EARLY_ARRIVAL_TOL_MIN = 1.0 / 60.0


@dataclass(frozen=True)
class RouteVehicle:
    vehicle_id: str
    start_node: str
    start_lat: float
    start_lon: float
    start_time: str
    capacity_pallets: float
    capacity_kg: float
    vehicle_type: str
    home_depot: str
    home_lat: float
    home_lon: float
    return_to_depot: bool = True
    shift_end: str = ""
    # E6 rolling: duty carried in from trips frozen earlier the same day. Defaults
    # reproduce the fresh-day behaviour exactly.
    drive_since_break0: float = 0.0        # minutes driven since the last statutory break
    max_drive_minutes_cap: float | None = None  # remaining daily driving budget


@dataclass(frozen=True)
class DutyOverride:
    """Rich (vehicle_id, day) availability override for E6 rolling epochs.

    The override dicts threaded through the seed and ALNS keep their shape;
    only the VALUE widens: a plain "HH:MM" string stays the T1 trunk next-day
    convention, a DutyOverride carries the state a vehicle returns with after
    its frozen trips — full start datetime plus the duty already consumed.
    """
    start_iso: str                          # "YYYY-MM-DD HH:MM:SS"
    drive_since_break0: float = 0.0
    drive_minutes_left: float | None = None


def apply_avail_override(veh: RouteVehicle, override, day: str) -> RouteVehicle:
    """Single application point for both override value forms (alns._rv_ov and
    route_seed's closure both delegate here so semantics cannot drift)."""
    if not override:
        return veh
    if isinstance(override, DutyOverride):
        return replace(
            veh,
            start_time=str(override.start_iso),
            drive_since_break0=float(override.drive_since_break0 or 0.0),
            max_drive_minutes_cap=override.drive_minutes_left,
        )
    d = date.fromisoformat(day) if day else date(2026, 1, 1)
    t = datetime.strptime(str(override), "%H:%M").time()
    return replace(veh, start_time=datetime.combine(d, t).isoformat(sep=" "))


@dataclass(frozen=True)
class RouteJob:
    job_id: str
    leg_kind: str
    node: str
    lat: float
    lon: float
    pallets: float
    kg: float
    earliest_start: str = ""
    latest_finish: str = ""
    origin_lat: float | None = None  # for DIRECT moves: the collection point
    origin_lon: float | None = None
    order_id: str = ""
    # non-anticipation (user rule 2026-07-11): when the earliest_start is a
    # creation floor (dynamic pipeline), the vehicle may not even ARRIVE before it
    # — a curb wait at a site whose freight is not yet booked is the leak. Set for
    # dynamic collection legs only; the static planner leaves it False (unchanged).
    no_early_arrival: bool = False
    # E6 dispatch floor, job-carried (2026-07-16): when this job LEADS a trip, the
    # trip may not START DRIVING before this time (departure-based flooring, B2) —
    # the vehicle waits at the DEPOT, not the curb. Riding on the job means every
    # later re-evaluation (snapshots, emission) re-derives the same held departure.
    # Stamped by new_arrival_meta on micro arrivals; static planner leaves it "".
    depart_floor: str = ""
    # Collocated depot-delivery (2026-07-17): the freight physically sits at THIS
    # depot, so only a vehicle homed there may carry the job — the daily model has
    # no mid-route depot-load stop, so a foreign vehicle would board the freight
    # at the wrong depot. "" = unconstrained (every pre-existing job).
    depot_bound: str = ""
    # Soft delivery window (2026-07-18): the customer's TIGHT window, penalty-only
    # (convex tardiness past `deadline`, small earliness before `window_open`), NOT a
    # hard cutoff. Set for CUSTOMER_DELIVERY with a stated window; "" otherwise
    # (missing-window deliveries, pickups, every non-delivery leg).
    window_open: str = ""
    deadline: str = ""


@dataclass(frozen=True)
class StopTiming:
    job_id: str
    node: str
    leg_kind: str
    arrive: str
    wait_minutes: float
    service_minutes: float
    depart: str
    leg_km: float
    load_pallets_after: float
    load_kg_after: float
    break_minutes_before: float = 0.0
    drive_minutes: float = 0.0   # leg drive time the evaluator actually used (OSRM or constant)
    minutes_late: float = 0.0    # delivery arrival past its tight deadline (soft windows, 2026-07-18)
    minutes_early: float = 0.0   # delivery arrival before its window opens


@dataclass(frozen=True)
class RouteEvaluation:
    feasible: bool
    failure_reason: str
    total_km: float
    total_drive_minutes: float
    total_service_minutes: float
    total_wait_minutes: float
    # route_start is the ACTUAL departure (a first-stop earliest_start shifts it
    # later, just-in-time); DayEvaluation.day_start stays the nominal shift start.
    route_start: str
    route_end: str
    stops: tuple[StopTiming, ...] = field(default_factory=tuple)
    end_drive_since_break: float = 0.0
    lateness_cost: float = 0.0   # GBP soft earliness/tardiness penalty over this route's deliveries

    @property
    def load_timeline(self) -> list[tuple[str, float, float]]:
        return [(s.node, s.load_pallets_after, s.load_kg_after) for s in self.stops]

    @property
    def node_timeline(self) -> list[tuple[str, str, str]]:
        return [(s.node, s.arrive, s.depart) for s in self.stops]


@dataclass(frozen=True)
class DayEvaluation:
    feasible: bool
    failure_reason: str
    total_km: float
    total_drive_minutes: float
    total_service_minutes: float
    total_wait_minutes: float
    day_start: str
    day_end: str
    trip_evaluations: tuple[RouteEvaluation, ...] = field(default_factory=tuple)
    lateness_cost: float = 0.0   # GBP soft earliness/tardiness penalty summed over the day's trips


def _day_infeasible(reason: str, start: str, trips: list[RouteEvaluation] | None = None) -> DayEvaluation:
    return DayEvaluation(
        feasible=False, failure_reason=reason, total_km=0.0,
        total_drive_minutes=0.0, total_service_minutes=0.0, total_wait_minutes=0.0,
        day_start=start, day_end=start, trip_evaluations=tuple(trips or ()),
    )


@lru_cache(maxsize=1 << 18)
def _parse_str(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)  # fast path: our ISO timestamps
    except ValueError:
        parsed = pd.to_datetime(ts, errors="coerce")
        return None if pd.isna(parsed) else parsed.to_pydatetime()


def _parse(ts) -> datetime | None:
    # The search re-evaluates routes millions of times over the *same* ISO strings
    # (job windows, vehicle shift bounds); cache the string->datetime parse so it is
    # paid once per distinct timestamp, not once per evaluation. datetime is immutable
    # so sharing the cached instance is safe.
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts
    return _parse_str(str(ts))


def _iso(dt: datetime) -> str:
    return dt.isoformat(sep=" ")


def _infeasible(reason: str, start: str) -> RouteEvaluation:
    return RouteEvaluation(
        feasible=False, failure_reason=reason,
        total_km=0.0, total_drive_minutes=0.0, total_service_minutes=0.0,
        total_wait_minutes=0.0, route_start=start, route_end=start, stops=(),
    )


def _delivery_lateness(job, service_start) -> tuple[float, float, float]:
    """(cost_gbp, minutes_late, minutes_early) for a CUSTOMER_DELIVERY vs its TIGHT
    window (soft windows, 2026-07-18): convex tardiness past ``deadline`` + small
    linear earliness before ``window_open``. (0,0,0) for non-deliveries or when the
    leg has no deadline. Points/single-deadlines open at 06:00, so earliness only
    bites RANGE windows."""
    if job.leg_kind != CUSTOMER_DELIVERY or not job.deadline:
        return 0.0, 0.0, 0.0
    dl = _parse(job.deadline)
    if dl is None:
        return 0.0, 0.0, 0.0
    late = max(0.0, (service_start - dl).total_seconds() / 60.0)
    early = 0.0
    wo = _parse(job.window_open)
    if wo is not None:
        early = max(0.0, (wo - service_start).total_seconds() / 60.0)
    cost = (float(_fp_cfg.TARDINESS_COEF) * (late ** float(_fp_cfg.TARDINESS_POWER))
            + float(_fp_cfg.EARLINESS_COEF) * early)
    return cost, late, early


def evaluate_route(
    vehicle: RouteVehicle,
    ordered_jobs: list[RouteJob],
    freight_state=None,  # accepted for interface parity; availability gated upstream
    detail: bool = True,
    drive_since_break: float = 0.0,
) -> RouteEvaluation:
    """``detail=False`` is the search fast path: it computes feasibility, total_km and
    stop *ordering* (job_id) but skips formatting the per-stop arrive/depart ISO strings,
    which the ALNS never reads. The final plan emit uses detail=True for full timings."""
    start_dt = _parse(vehicle.start_time) or datetime(2026, 1, 1, 6, 0)
    start_iso = _iso(start_dt)

    for _j in ordered_jobs:
        _db = str(getattr(_j, "depot_bound", "") or "")
        if _db and str(vehicle.home_depot) != _db:
            # collocated depot-delivery: its freight sits at _db, and this route
            # never visits _db to load — hard infeasible, like CAPACITY/SHIFT
            return _infeasible("DEPOT_BOUND", start_iso)

    cap_p = float(vehicle.capacity_pallets)
    cap_kg = float(vehicle.capacity_kg)

    # Deliveries ride from the depot: their freight is on board at departure.
    initial_p = sum(float(j.pallets) for j in ordered_jobs if j.leg_kind == CUSTOMER_DELIVERY)
    initial_kg = sum(float(j.kg) for j in ordered_jobs if j.leg_kind == CUSTOMER_DELIVERY)
    if initial_p > cap_p + _EPS or initial_kg > cap_kg + _EPS:
        return _infeasible("CAPACITY", start_iso)

    running_p, running_kg = initial_p, initial_kg
    clock = start_dt
    prev_lat, prev_lon = vehicle.start_lat, vehicle.start_lon
    total_km = total_drive = total_service = total_wait = 0.0
    total_lateness_cost = 0.0
    stops: list[StopTiming] = []
    hgv = str(vehicle.vehicle_type).lower() != "van"
    merge_addr = _fp_cfg.SAME_ADDRESS_DWELL_MERGE
    route_start_shift = 0.0
    first_stop = True

    # The dispatch floor rides ON jobs and binds TRIP-WIDE: depot-loaded freight
    # boards at DEPARTURE wherever its job rides in the sequence, so the trip may
    # not start driving before the LATEST member floor — hold the vehicle AT THE
    # DEPOT (B2 departure-based flooring), so a returned vehicle can take a
    # floored afternoon trip instead of forcing a fresh activation. (Lead-job-only
    # was the old read; it silently lost the hold for mid-trip floored jobs.)
    floors = [f for f in (_parse(getattr(j, "depart_floor", "") or "") for j in ordered_jobs)
              if f is not None]
    dfloor = max(floors) if floors else None
    if dfloor is not None and dfloor > clock:
        route_start_shift += (dfloor - clock).total_seconds() / 60.0
        clock = dfloor

    for job in ordered_jobs:
        if (job.leg_kind in _TWO_POINT_KINDS
                and job.origin_lat is not None and job.origin_lon is not None):
            # true origin->dest move: collect at the customer origin, then deliver
            # (DIRECT) or hand to the network at the hub (HUB_DROP)
            leg_km = (road_km(prev_lat, prev_lon, job.origin_lat, job.origin_lon)
                      + road_km(job.origin_lat, job.origin_lon, job.lat, job.lon))
            dm = (road_minutes(prev_lat, prev_lon, job.origin_lat, job.origin_lon, vehicle.vehicle_type)
                  + road_minutes(job.origin_lat, job.origin_lon, job.lat, job.lon, vehicle.vehicle_type))
        else:
            leg_km = road_km(prev_lat, prev_lon, job.lat, job.lon)
            dm = road_minutes(prev_lat, prev_lon, job.lat, job.lon, vehicle.vehicle_type)
        break_min = 0.0
        if hgv:
            break_min, drive_since_break = statutory_breaks(drive_since_break, dm)
        arrive = clock + timedelta(minutes=dm + break_min)

        es = _parse(job.earliest_start)
        wait = max(0.0, (es - arrive).total_seconds() / 60.0) if es else 0.0
        if wait > 0.0 and first_stop:
            # leave the depot later instead of idling at the first customer (only
            # the first stop can absorb slack this way — by a later stop the
            # vehicle is already committed, so its wait is real curbside idle).
            # += : composes with a depart_floor hold applied above.
            route_start_shift += wait
            arrive = arrive + timedelta(minutes=wait)
            wait = 0.0
        elif getattr(job, "no_early_arrival", False) and wait > _EARLY_ARRIVAL_TOL_MIN:
            # non-anticipation (user rule 2026-07-11): a creation-floored collection
            # may not be ARRIVED at before it was booked. A non-first stop cannot
            # absorb the wait via a route-start shift, so an early drive-up is a
            # leak, not an honest curb wait -> infeasible (the optimiser sequences
            # it later so arrive >= booking, or onto another route).
            return _infeasible("EARLY_ARRIVAL", start_iso)
        elif wait > MAX_STOP_WAIT_MIN:
            return _infeasible("EXCESS_WAIT", start_iso)
        first_stop = False
        service_start = arrive + timedelta(minutes=wait)

        lf = _parse(job.latest_finish)
        if lf is not None and service_start > lf + timedelta(seconds=1):
            # For deliveries under soft windows this is the widened OPERATING/duty
            # bound (past end-of-day = a genuine same-day impossibility -> slips);
            # for pickups it is the hard collection window. The customer DELIVERY
            # deadline is handled softly just below, not here.
            return _infeasible("TIME_WINDOW", start_iso)

        # Soft delivery window (2026-07-18): the customer deadline is a penalty, not
        # a cutoff, so the solver delivers slightly late rather than slipping a day.
        # --hard-time-windows (SOFT off) restores the hard cutoff on the deadline.
        mins_late = mins_early = 0.0
        if job.leg_kind == CUSTOMER_DELIVERY and job.deadline:
            if not _fp_cfg.SOFT_DELIVERY_WINDOWS:
                _dl = _parse(job.deadline)
                if _dl is not None and service_start > _dl + timedelta(seconds=1):
                    return _infeasible("TIME_WINDOW", start_iso)
            else:
                _lc, mins_late, mins_early = _delivery_lateness(job, service_start)
                total_lateness_cost += _lc

        sm = service_minutes(job.pallets, vehicle.vehicle_type)
        if (job.leg_kind in _TWO_POINT_KINDS
                and job.origin_lat is not None and job.origin_lon is not None):
            # Two-point work has handling at the collection point and at the
            # destination/hub. We still emit one logical stop row, but feasibility
            # must carry both service blocks.
            sm *= 2.0
        elif merge_addr and stops and job.lat == prev_lat and job.lon == prev_lon:
            # Same-address consolidation: the vehicle is already parked at this
            # customer dock from the preceding stop, so this order adds no new
            # dwell. The depot anchor is not a customer visit: a first stop that
            # happens to be collocated with the depot still pays its fixed dwell.
            sm -= service_minutes(0.0, vehicle.vehicle_type)
        depart = service_start + timedelta(minutes=sm)

        if job.leg_kind == CUSTOMER_DELIVERY:
            running_p -= float(job.pallets)
            running_kg -= float(job.kg)
            on_board_p, on_board_kg = running_p, running_kg
        elif job.leg_kind == CUSTOMER_PICKUP:
            running_p += float(job.pallets)
            running_kg += float(job.kg)
            on_board_p, on_board_kg = running_p, running_kg
        else:  # DIRECT_CUSTOMER_MOVE: carried over the inbound segment, then dropped
            on_board_p = running_p + float(job.pallets)
            on_board_kg = running_kg + float(job.kg)

        if on_board_p > cap_p + _EPS or on_board_kg > cap_kg + _EPS:
            return _infeasible("CAPACITY", start_iso)

        total_km += leg_km
        total_drive += dm
        total_service += sm
        total_wait += wait
        stops.append(StopTiming(
            job_id=job.job_id, node=job.node, leg_kind=job.leg_kind,
            arrive=_iso(arrive) if detail else "", wait_minutes=wait, service_minutes=sm,
            depart=_iso(depart) if detail else "", leg_km=leg_km,
            load_pallets_after=running_p, load_kg_after=running_kg,
            break_minutes_before=break_min, drive_minutes=dm,
            minutes_late=mins_late, minutes_early=mins_early,
        ))
        clock = depart
        prev_lat, prev_lon = job.lat, job.lon

    if vehicle.return_to_depot and ordered_jobs:
        back_km = road_km(prev_lat, prev_lon, vehicle.home_lat, vehicle.home_lon)
        back_dm = road_minutes(prev_lat, prev_lon, vehicle.home_lat, vehicle.home_lon, vehicle.vehicle_type)
        back_break = 0.0
        if hgv:
            back_break, drive_since_break = statutory_breaks(drive_since_break, back_dm)
        total_km += back_km
        total_drive += back_dm
        clock = clock + timedelta(minutes=back_dm + back_break)

    shift_end_dt = _parse(vehicle.shift_end)
    if shift_end_dt is not None and clock > shift_end_dt + timedelta(seconds=1):
        return _infeasible("SHIFT", start_iso)

    return RouteEvaluation(
        feasible=True, failure_reason="",
        total_km=total_km, total_drive_minutes=total_drive,
        total_service_minutes=total_service, total_wait_minutes=total_wait,
        route_start=_iso(start_dt + timedelta(minutes=route_start_shift)), route_end=_iso(clock),
        stops=tuple(stops), end_drive_since_break=drive_since_break,
        lateness_cost=total_lateness_cost,
    )


def try_insert_job(
    vehicle: RouteVehicle,
    ordered_jobs: list[RouteJob],
    candidate_job: RouteJob,
    position_or_policy: int | str = "best",
    freight_state=None,
    detail: bool = True,
    min_position: int = 0,
) -> RouteEvaluation:
    """Insert ``candidate_job`` into the route and re-evaluate.

    An int inserts at that index. "best" tries every position and returns the
    feasible evaluation with the lowest total km (or the last infeasible
    evaluation if no position is feasible). ``detail=False`` is the search fast path.
    ``min_position`` (E6 watermark, spec 4.7a) forbids insertion before that
    index: the committed prefix of an in-flight trip is fact — inserting ahead
    of it would retime stops the driver has already served. Default 0 = today's
    behaviour, bit-identical.
    """
    if isinstance(position_or_policy, int):
        seq = list(ordered_jobs)
        seq.insert(max(int(position_or_policy), int(min_position)), candidate_job)
        return evaluate_route(vehicle, seq, freight_state, detail=detail)

    best: RouteEvaluation | None = None
    last_infeasible: RouteEvaluation | None = None
    for pos in range(int(min_position), len(ordered_jobs) + 1):
        seq = list(ordered_jobs)
        seq.insert(pos, candidate_job)
        ev = evaluate_route(vehicle, seq, freight_state, detail=detail)
        if ev.feasible:
            if best is None or ev.total_km < best.total_km:
                best = ev
        else:
            last_infeasible = ev
    return best if best is not None else (last_infeasible or _infeasible("NO_POSITION", vehicle.start_time))


def evaluate_day(
    vehicle: RouteVehicle,
    trips: list[list[RouteJob]],
    reload_minutes: float = 30.0,
    max_drive_minutes: float = MAX_DRIVING_H_PER_DAY * 60.0,
    detail: bool = True,
    trip_earliest: dict[int, str] | None = None,
) -> DayEvaluation:
    """Evaluate a vehicle-day made of multiple depot-loop trips.

    Capacity resets at the depot at the start of every trip. Driver elapsed time
    and drive minutes do not reset, and neither does the statutory-break
    accumulator — the 30-min reload is shorter than a qualifying 45-min break.
    A reload/turnaround dwell is inserted between non-empty trips.

    ``trip_earliest`` maps a trip's index in ``trips`` (pre-filter) to the
    earliest it may depart the depot — the dispatch-floor seam for the dynamic
    dispatcher: a trip planned mid-day cannot leave before now + delta_R1, so
    the plan holds the vehicle idle at the depot instead of pretending it left
    hours ago (structural review, Fix 2b).
    """
    start_dt = _parse(vehicle.start_time) or datetime(2026, 1, 1, 6, 0)
    start_iso = _iso(start_dt)
    clock = start_dt
    evaluations: list[RouteEvaluation] = []
    total_km = total_drive = total_service = total_wait = 0.0

    non_empty = [(i, list(t)) for i, t in enumerate(trips) if t]
    if not non_empty:
        return DayEvaluation(True, "", 0.0, 0.0, 0.0, 0.0, start_iso, start_iso, ())

    # E6 rolling: trips frozen earlier the same day consume duty this call never
    # sees — the vehicle carries in its break accumulator and a reduced budget.
    carry = float(getattr(vehicle, "drive_since_break0", 0.0) or 0.0)
    cap = getattr(vehicle, "max_drive_minutes_cap", None)
    effective_cap = float(max_drive_minutes) if cap is None else min(float(max_drive_minutes), float(cap))
    # 13h duty cap per CHAIN (spec 2026-07-16): a depot gap >= SPLIT_SHIFT_GAP_H
    # ends a chain (driver rests or swaps — split shift), so a held evening trip
    # on a morning vehicle is legal while any single working stretch stays <= 13h.
    # The DRIVING cap above stays whole-day (EU daily driving doesn't reset).
    duty_cap_h = float(_fp_cfg.MAX_DUTY_H_PER_DAY)
    gap_h = float(_fp_cfg.SPLIT_SHIFT_GAP_H)
    chain_start = chain_end = None
    for idx, (orig_idx, trip) in enumerate(non_empty):
        if idx > 0:
            clock = clock + timedelta(minutes=float(reload_minutes))
        if trip_earliest:
            te = _parse(trip_earliest.get(orig_idx) or "")
            if te is not None and te > clock:
                clock = te
        trip_vehicle = replace(vehicle, start_time=_iso(clock))
        ev = evaluate_route(trip_vehicle, trip, detail=detail, drive_since_break=carry)
        evaluations.append(ev)
        if not ev.feasible:
            return _day_infeasible(ev.failure_reason, start_iso, evaluations)
        carry = ev.end_drive_since_break
        total_km += ev.total_km
        total_drive += ev.total_drive_minutes
        total_service += ev.total_service_minutes
        total_wait += ev.total_wait_minutes
        if total_drive > effective_cap + _EPS:
            return _day_infeasible("DRIVING_CAP", start_iso, evaluations)
        ts, te_ = _parse(ev.route_start), _parse(ev.route_end)
        if ts is not None and te_ is not None:
            if chain_start is None:
                chain_start, chain_end = ts, te_
            elif (ts - chain_end).total_seconds() / 3600.0 >= gap_h:
                if (chain_end - chain_start).total_seconds() / 3600.0 > duty_cap_h + 1e-9:
                    return _day_infeasible("DUTY_CAP", start_iso, evaluations)
                chain_start, chain_end = ts, te_
            else:
                chain_end = max(chain_end, te_)
        clock = _parse(ev.route_end) or clock
    if (chain_start is not None
            and (chain_end - chain_start).total_seconds() / 3600.0 > duty_cap_h + 1e-9):
        return _day_infeasible("DUTY_CAP", start_iso, evaluations)

    shift_end_dt = _parse(vehicle.shift_end)
    if shift_end_dt is not None and clock > shift_end_dt + timedelta(seconds=1):
        return _day_infeasible("SHIFT", start_iso, evaluations)

    return DayEvaluation(
        feasible=True, failure_reason="", total_km=total_km,
        total_drive_minutes=total_drive, total_service_minutes=total_service,
        total_wait_minutes=total_wait, day_start=start_iso, day_end=_iso(clock),
        trip_evaluations=tuple(evaluations),
        lateness_cost=sum(e.lateness_cost for e in evaluations),
    )


