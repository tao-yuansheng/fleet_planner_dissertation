"""Milestone 8a: multiday tour core.

The daily seed builds one route per (vehicle, day) that starts and ends at the
home depot inside a shift. Far work (Scotland-style) cannot round-trip in a day,
so it falls out as NO_FEASIBLE_ROUTE. This module serves that work as a
*multi-day tour*: a geographically cohesive batch of far stops visited over
consecutive days while the vehicle sleeps out.

Geometry-derived, per the design decision:
  * a job is tour-only when a single-day there-and-back-plus-service route from
    its depot cannot fit the daily driving cap (local road model — the same model
    the daily seed is bound by, so this captures exactly what it cannot do);
  * tour legs cost real road distance (``road_km``: OSRM when installed) at the
    long-haul motorway speed (``MULTIDAY_AVG_SPEED_KMH``), plus fixed
    vehicle-type visit dwell; the local 50 km/h x 1.3 model would badly overestimate
    motorway trunking (Scotland ~13h one-way vs the real ~6h);
  * a tour day is capped by driving (``MAX_DRIVING_H_PER_DAY``) AND elapsed duty
    time (``TOUR_DAY_ELAPSED_CAP_MIN``), with a hard ceiling of
    ``MAX_TOUR_DAYS_HARD`` days;
  * a due date is a deadline: stops may be served early (never dwell — wasted
    time and resource), but a stop reached after its due day is infeasible.

The batcher keeps tours cohesive (a cohesion radius stops Scotland and Cornwall
being merged just because pallets and dates fit), and vehicle choice follows Q4:
prefer an artic, but take a rigid when the tour is light on pallets.

This module is the deterministic core; freight-ledger gating and pipeline
orchestration are wired separately.
"""
from __future__ import annotations

import os

from dataclasses import dataclass, field, replace
from datetime import date, timedelta

# Diagnostic channel (FP_TOUR_DEBUG=1): batching/clustering decisions are not
# persisted anywhere, which made the 2026-07-22 slack-run tour split
# undiagnosable from artifacts. Zero cost when the env var is unset.
_TOUR_DEBUG = bool(os.environ.get("FP_TOUR_DEBUG"))


def _tdbg(msg: str) -> None:
    if _TOUR_DEBUG:
        print(f"[tour-debug] {msg}", flush=True)

from freight_planner.shared.config import (
    CUSTOMER_DAY_START,
    DEPOT_ANCHORS,
    MAX_DRIVING_H_PER_DAY,
    MAX_TOUR_DAYS_HARD,
    MULTIDAY_AVG_SPEED_KMH,
)
from freight_planner import config as _config
from freight_planner.config import (
    LIGHT_TOUR_PALLETS,
    TOUR_COHESION_KM,
    TOUR_DAY_ELAPSED_CAP_MIN,
    TOUR_ORIGIN_AT_DEPOT_RADIUS_KM,
)
from freight_planner.route_costs import (
    drive_minutes,
    haversine_km,
    osrm_durations_active,
    road_km,
    road_minutes,
    service_minutes,
    statutory_breaks,
)
from freight_planner.routing_adapter import (
    CUSTOMER_DELIVERY,
    CUSTOMER_PICKUP,
    DIRECT_CUSTOMER_MOVE,
    HUB_DROP,
    RouteJob,
    RouteVehicle,
)

_DAY_DRIVE_CAP_MIN = MAX_DRIVING_H_PER_DAY * 60.0
_TWO_POINT_KINDS = (DIRECT_CUSTOMER_MOVE, HUB_DROP)
_EPS = 1e-6

# Customer-facing stop kinds a real customer dock gates (excludes HUB_DROP, a hub
# with its own schedule, and DEPOT_LOAD, which visits our own depot). CUSTOMER_DAY_START
# (08:00) is a floor the daily side enforces (soft-priced earliness / route-start shift,
# routing_adapter.py); tours had NO time-of-day concept at all, so a stop reached right
# after the TOUR_DAY_START_HOUR (05:00) wake could be emitted as, e.g., "delivered 05:00"
# — no dock is open then (audit #5, 2026-07-26). _CUSTOMER_WINDOW_OPEN_MIN is that floor
# in tour-day-relative minutes (every day's minute 0 = TOUR_DAY_START_HOUR, `_tour_clock`).
_CUSTOMER_CLAMP_KINDS = (CUSTOMER_DELIVERY, CUSTOMER_PICKUP, DIRECT_CUSTOMER_MOVE)
_CUSTOMER_WINDOW_OPEN_MIN = (CUSTOMER_DAY_START.hour * 60 + CUSTOMER_DAY_START.minute
                             - int(_config.TOUR_DAY_START_HOUR) * 60)

# LIGHT_TOUR_PALLETS, TOUR_COHESION_KM, TOUR_ORIGIN_AT_DEPOT_RADIUS_KM are imported
# from freight_planner.config (the single place all planner tour-tuning knobs live).

# A synthetic front-of-tour waypoint: the vehicle calls at a depot to load freight
# already staged there (cross-depot consolidation). Zero pallets/kg — the carried
# load is already counted in the deliveries — so it only contributes the depot-hop
# km. `evaluate_tour`'s default waypoint branch handles it; the tour commit skips it.
DEPOT_LOAD = "DEPOT_LOAD"


def load_stop_job(depot: str, lat: float, lon: float) -> RouteJob:
    return RouteJob(job_id=f"LOAD:{depot}", leg_kind=DEPOT_LOAD, node=depot,
                    lat=float(lat), lon=float(lon), pallets=0.0, kg=0.0)


# ----------------------------------------------------------- primitives -------

def longhaul_drive_minutes(straight_km: float) -> float:
    """Drive minutes for a long-haul leg at the motorway straight-line speed."""
    return (float(straight_km) / MULTIDAY_AVG_SPEED_KMH) * 60.0


def nearest_depot(lat: float, lon: float, anchors: dict = DEPOT_ANCHORS) -> tuple[str, float]:
    """Return (depot_name, straight-line km) of the closest depot anchor."""
    best_name, best_km = "", float("inf")
    for name, (d_lat, d_lon) in anchors.items():
        km = haversine_km(lat, lon, d_lat, d_lon)
        if km < best_km:
            best_name, best_km = name, km
    return best_name, best_km


def _gate_minutes(a_lat: float, a_lon: float, b_lat: float, b_lon: float,
                  vehicle_type: str) -> float:
    """One tour-gate segment's drive minutes. With TOUR_OSRM_DURATIONS on, use
    road_minutes (OSRM per-road-type duration; it itself falls back to the flat
    drive_minutes(road_km) when no OSRM router is active, so offline is unchanged).
    With the flag off, the current flat 50 km/h model exactly."""
    if _config.TOUR_OSRM_DURATIONS:
        return road_minutes(a_lat, a_lon, b_lat, b_lon, vehicle_type)
    return drive_minutes(road_km(a_lat, a_lon, b_lat, b_lon))


def is_tour_only(
    lat: float,
    lon: float,
    *,
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    pallets: float = 0.0,
    depot: str = "",
    vehicle_type: str = "tractor",
    drive_cap_min: float = _DAY_DRIVE_CAP_MIN,
    anchors: dict = DEPOT_ANCHORS,
) -> bool:
    """True when a same-day round trip from the depot cannot fit the driving cap.

    Uses the *local* road model (the daily seed's reachability), so a job flagged
    here is one the daily planner genuinely cannot serve there-and-back in a day.

    For a two-point leg (direct / hub-drop) pass ``origin_lat``/``origin_lon``:
    the vehicle drives ``depot -> origin -> dest -> depot``, so a near destination
    with a far origin is correctly tour-only (B14) rather than slipping into the
    daily pool and stranding on the shift bound.
    """
    anchor = anchors.get(depot)
    if anchor is None:
        name, _km = nearest_depot(lat, lon, anchors)
        anchor = anchors.get(name)
    if anchor is None:
        return False
    if origin_lat is not None and origin_lon is not None:
        carry_min = (_gate_minutes(anchor[0], anchor[1], origin_lat, origin_lon, vehicle_type)
                     + _gate_minutes(origin_lat, origin_lon, lat, lon, vehicle_type)
                     + _gate_minutes(lat, lon, anchor[0], anchor[1], vehicle_type))
        return carry_min + service_minutes(pallets, vehicle_type) > drive_cap_min
    one_way_min = _gate_minutes(anchor[0], anchor[1], lat, lon, vehicle_type)
    return 2.0 * one_way_min + service_minutes(pallets, vehicle_type) > drive_cap_min


# ------------------------------------------------------- tour evaluation ------

@dataclass(frozen=True)
class TourStop:
    job_id: str
    node: str
    day_index: int
    leg_km: float
    load_pallets_after: float
    load_kg_after: float
    # minutes since the tour-day's start (TOUR_DAY_START_HOUR anchors the clock at emit)
    arrive_minute: float = -1.0
    # the arriving leg's evaluated drive minutes (OSRM-aware when active, flat
    # long-haul otherwise) — record emission must use THIS, not re-derive at 80
    leg_minutes: float = 0.0
    depart_minute: float = -1.0
    break_minutes_before: float = 0.0
    # duty accumulators AFTER this stop, on its own day — so an in-flight tour can be
    # resumed mid-day from here (see tour_tail_from / evaluate_tour resume).
    day_drive_after: float = 0.0
    drive_since_break_after: float = 0.0
    # A late fresh dispatch separates wall-clock time from time actually worked.
    # depart_minute remains clock-relative for emission; this field carries duty.
    duty_elapsed_after: float = 0.0


@dataclass(frozen=True)
class DayStart:
    """Where a tour-day begins: depot on day 0, else the interpolated overnight
    point the vehicle woke at. carried_* is the freight aboard at wake-up. This is
    what a solver needs to treat a tour-day as a modifiable single-day route."""
    day_index: int
    start_lat: float
    start_lon: float
    start_node: str
    carried_pallets: float
    carried_kg: float


@dataclass(frozen=True)
class TourEvaluation:
    feasible: bool
    reason: str
    total_km: float
    total_drive_minutes: float
    days: int
    stops: tuple[TourStop, ...] = field(default_factory=tuple)
    # Peak simultaneous load over the whole tour (deliveries ride from the depot;
    # pickups/DIRECT moves load transiently). This is the true capacity constraint —
    # vehicle selection must screen on THIS, not the sum of every job's freight.
    peak_pallets: float = 0.0
    peak_kg: float = 0.0
    # Per-day start locations (empty unless MULTIDAY_MIDLEG_OVERNIGHT): day 0 is the
    # depot, each later entry the overnight boundary carried into the next day.
    day_starts: tuple[DayStart, ...] = field(default_factory=tuple)
    # Physical movements that are not customer-job completions.  In particular,
    # an overnight DIRECT has a collection-side drive/handling block on the day
    # before its delivery job.  Keeping it separate preserves the one-stop-per-job
    # indexing used by attachment logic while allowing truthful per-day emission.
    auxiliary_stops: tuple[TourStop, ...] = field(default_factory=tuple)


TOUR_OVERNIGHT = "TOUR_OVERNIGHT"     # synthetic selected-row leg_kind for a mid-leg sleep point


def overnight_row_specs(evaluation: "TourEvaluation", start_iso: str) -> list[dict]:
    """Emission specs for TOUR_OVERNIGHT selected rows — one per MID-LEG sleep
    (DayStart node ``OVERNIGHT:*``; the day-0 DEPOT entry and stop-boundary
    sleeps need no row: the viz reconstructs those from the stops themselves).

    Job rows are gap-numbered ``sequence = 2*i`` so the overnight's
    ``2*(stops before its day)+1`` slots between the last stop of the night
    before and the first stop of its own day. ``node`` self-carries the
    coordinates (``OVERNIGHT@lat,lon``) because SelectedPlanRecord has no
    lat/lon fields; ``service_date`` is the day the tour RESUMES from there."""
    out: list[dict] = []
    for ds in getattr(evaluation, "day_starts", ()) or ():
        if not str(ds.start_node).startswith("OVERNIGHT:"):
            continue
        n_prev = sum(1 for st in evaluation.stops if int(st.day_index) < int(ds.day_index))
        day_iso = (date.fromisoformat(str(start_iso)[:10])
                   + timedelta(days=int(ds.day_index))).isoformat()
        out.append({
            "sequence": 2 * n_prev + 1,
            "day_index": int(ds.day_index),
            "service_date": day_iso,
            "node": f"OVERNIGHT@{float(ds.start_lat):.5f},{float(ds.start_lon):.5f}",
            "pallets": float(ds.carried_pallets),
            "kg": float(ds.carried_kg),
        })
    return out


def tour_emission_events(evaluation: 'TourEvaluation', start_iso: str) -> list[dict]:
    '''Chronological evaluator events shared by every tour emission path.'''
    base = date.fromisoformat(str(start_iso)[:10])
    starts = {int(ds.day_index): ds for ds in (evaluation.day_starts or ())}
    events: list[dict] = []
    for day_index, ds in starts.items():
        if day_index <= 0 or not str(ds.start_node).startswith('OVERNIGHT:'):
            continue
        events.append({
            'kind': 'day_start', 'day_index': day_index, 'minute': 0.0,
            'service_date': (base + timedelta(days=day_index)).isoformat(),
            'job_id': f'OVERNIGHT_START:{day_index}',
            'node': f'OVERNIGHT@{float(ds.start_lat):.5f},{float(ds.start_lon):.5f}',
            'pallets': float(ds.carried_pallets), 'kg': float(ds.carried_kg),
            'stop': None,
        })
    for stop in evaluation.auxiliary_stops or ():
        aux_kind = ('outbound_overnight'
                    if str(stop.job_id).startswith('__OUTBOUND_LEG__')
                    else 'direct_overnight')
        aux_node = str(stop.node)
        if aux_kind == 'outbound_overnight':
            wake = starts.get(int(stop.day_index) + 1)
            if wake is None:
                raise ValueError(f'outbound segment {stop.job_id} has no next-day DayStart')
            aux_node = f'OVERNIGHT@{float(wake.start_lat):.5f},{float(wake.start_lon):.5f}'
        events.append({
            'kind': aux_kind,
            'day_index': int(stop.day_index),
            'minute': float(stop.arrive_minute),
            'service_date': (base + timedelta(days=int(stop.day_index))).isoformat(),
            'job_id': str(stop.job_id),
            'node': aux_node,
            'pallets': float(stop.load_pallets_after),
            'kg': float(stop.load_kg_after),
            'stop': stop,
        })
    for stop in evaluation.stops:
        jid = str(stop.job_id)
        kind, node = 'job', str(stop.node)
        if jid.startswith('__RETURN_LEG__'):
            kind = 'return_overnight'
            wake = starts.get(int(stop.day_index) + 1)
            if wake is None:
                raise ValueError(f'return segment {jid} has no next-day DayStart')
            node = f'OVERNIGHT@{float(wake.start_lat):.5f},{float(wake.start_lon):.5f}'
        elif jid == '__RETURN__':
            kind, node = 'depot_return', 'DEPOT'
        events.append({
            'kind': kind, 'day_index': int(stop.day_index),
            'minute': float(stop.arrive_minute),
            'service_date': (base + timedelta(days=int(stop.day_index))).isoformat(),
            'job_id': jid, 'node': node,
            'pallets': float(stop.load_pallets_after),
            'kg': float(stop.load_kg_after), 'stop': stop,
        })
    priority = {
        'day_start': 0,
        'direct_overnight': 1,
        'outbound_overnight': 1,
        'job': 2,
        'return_overnight': 3,
        'depot_return': 4,
    }
    events.sort(key=lambda e: (int(e['day_index']), float(e['minute']),
                               priority.get(str(e['kind']), 9)))
    for sequence, event in enumerate(events, start=1):
        event['sequence'] = sequence
    return events


def _leg_km(prev_lat: float, prev_lon: float, job: RouteJob) -> float:
    if (job.leg_kind in _TWO_POINT_KINDS
            and job.origin_lat is not None and job.origin_lon is not None):
        return (road_km(prev_lat, prev_lon, job.origin_lat, job.origin_lon)
                + road_km(job.origin_lat, job.origin_lon, job.lat, job.lon))
    return road_km(prev_lat, prev_lon, job.lat, job.lon)


def _seg_minutes(a_lat: float, a_lon: float, b_lat: float, b_lon: float,
                 vehicle_type: str) -> float:
    """One tour-executor segment's drive minutes. OSRM per-road-type duration when
    TOUR_OSRM_DURATIONS is on AND a duration-capable router is active; otherwise the
    long-haul flat speed (MULTIDAY_AVG_SPEED_KMH applied to road_km) -- the executor's
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


def _infeasible_tour(reason: str) -> TourEvaluation:
    return TourEvaluation(False, reason, 0.0, 0.0, 0, ())


def _drive_fits(drive_since_break: float, drive_room: float, duty_room: float,
                hgv: bool) -> float:
    """Max drive-minutes today under BOTH the drive-cap room and the duty-cap room.
    Duty must also absorb the statutory break owed while driving that much (so a
    long partial leg near a 4.5 h boundary yields less than the naive duty room)."""
    hi = min(float(drive_room), float(duty_room))
    if hi <= 0.0:
        return 0.0
    if not hgv:
        return hi
    if hi + statutory_breaks(drive_since_break, hi)[0] <= duty_room + _EPS:
        return hi                             # drive cap binds; duty has room for the break
    lo = 0.0
    for _ in range(40):                       # bisect the monotone x + breaks(since, x)
        mid = 0.5 * (lo + hi)
        if mid + statutory_breaks(drive_since_break, mid)[0] <= duty_room + _EPS:
            lo = mid
        else:
            hi = mid
    return lo


def _interp_latlon(a_lat: float, a_lon: float, b_lat: float, b_lon: float,
                   f: float) -> tuple[float, float]:
    """Linear interpolation at fraction f in [0, 1] along a->b (drive-time fraction
    equals km fraction, since longhaul_drive_minutes is linear in km)."""
    g = min(1.0, max(0.0, float(f)))
    return (a_lat + (b_lat - a_lat) * g, a_lon + (b_lon - a_lon) * g)


@dataclass
class _DayCursor:
    """Mutable day-state carried while splitting a leg across overnight rests."""
    day_index: int
    day_drive: float
    day_elapsed: float
    drive_since_break: float
    # Minutes after TOUR_DAY_START_HOUR on the wall clock. None keeps the legacy
    # clock == duty behavior for ordinary and already-running tours.
    clock_minute: float | None = None


def _advance_single_point(cur: _DayCursor, dm: float, sm: float, hgv: bool,
                          elapsed_cap_min: float, prev_lat: float, prev_lon: float,
                          dst_lat: float, dst_lon: float, running_p: float,
                          running_kg: float, tag: str,
                          day_starts: list, *, split_stops: list | None = None,
                          leg_km: float = 0.0, job_id: str = "") -> tuple[float, float]:
    """Handle the drive ``dm`` toward a single-point stop (service ``sm`` at arrival)
    when it will not fit the current day. AT MOST ONE overnight per leg — exactly the
    +1 day the stop-boundary split would add — but the vehicle first banks the
    drive-cap residual and sleeps part-way along prev->dst, so the next day starts
    closer (which can let a LATER stop fit a day earlier). Because a leg never adds
    more than OFF's one day and banking only frees room downstream, days_ON never
    exceed days_OFF. Mutates ``cur``; appends a ``DayStart`` on the overnight.
    Returns ``(arr_dm, break)`` — the drive that lands the arrival on the new day."""
    if cur.clock_minute is None:
        cur.clock_minute = float(cur.day_elapsed)
    b_full = statutory_breaks(cur.drive_since_break, dm)[0] if hgv else 0.0
    fits = (cur.day_drive + dm <= _DAY_DRIVE_CAP_MIN + _EPS
            and cur.day_elapsed + dm + b_full + sm <= elapsed_cap_min + _EPS)
    if fits:
        return dm, b_full                  # no overnight — arrival lands today
    # bank what fits the drive/duty caps today, then sleep part-way along the leg
    x = max(0.0, _drive_fits(cur.drive_since_break, _DAY_DRIVE_CAP_MIN - cur.day_drive,
                             elapsed_cap_min - cur.day_elapsed, hgv))
    dm_rem = dm - x
    frac = x / dm if dm > _EPS else 1.0
    o_lat, o_lon = _interp_latlon(prev_lat, prev_lon, dst_lat, dst_lon, frac)
    if split_stops is not None and x > _EPS:
        b_x = statutory_breaks(cur.drive_since_break, x)[0] if hgv else 0.0
        split_stops.append(TourStop(
            job_id=f"__OUTBOUND_LEG__:{job_id}:{cur.day_index}",
            node=f"OVERNIGHT:{tag}:{cur.day_index + 1}",
            day_index=cur.day_index,
            leg_km=float(leg_km) * frac,
            load_pallets_after=float(running_p),
            load_kg_after=float(running_kg),
            arrive_minute=float(cur.clock_minute) + x + b_x,
            leg_minutes=x,
            depart_minute=float(cur.clock_minute) + x + b_x,
            break_minutes_before=b_x,
            day_drive_after=cur.day_drive + x,
            drive_since_break_after=(statutory_breaks(cur.drive_since_break, x)[1]
                                     if hgv else 0.0),
            duty_elapsed_after=cur.day_elapsed + x + b_x,
        ))
    cur.day_index += 1
    cur.day_drive = 0.0
    cur.day_elapsed = 0.0
    cur.drive_since_break = 0.0            # the overnight daily rest clears the accumulator
    cur.clock_minute = 0.0
    day_starts.append(DayStart(cur.day_index, o_lat, o_lon,
                               f"OVERNIGHT:{tag}:{cur.day_index}",
                               float(running_p), float(running_kg)))
    b = statutory_breaks(0.0, dm_rem)[0] if hgv else 0.0
    return dm_rem, b                        # remainder rides the new day (whole, as OFF does)


def evaluate_tour(vehicle: RouteVehicle, ordered_jobs: list[RouteJob],
                  due_offsets: dict | None = None,
                  elapsed_cap_min: float = TOUR_DAY_ELAPSED_CAP_MIN,
                  floor_offsets: dict | None = None,
                  resume: "_DayCursor | None" = None) -> TourEvaluation:
    """Walk a multi-day tour: depot -> stops -> depot, splitting into days by the
    driving cap AND the elapsed duty cap (drive + fixed visit dwell), over road
    distance at the long-haul speed. Catches capacity, the day caps, and lateness.

    ``due_offsets`` (job_id -> days from tour start) is a *deadline*: a stop
    reached after its due day is infeasible (LATE). Early service is fine — the
    vehicle never dwells (dwell is wasted time and resource). ``floor_offsets``
    (job_id -> earliest day offset) is the mirror: a stop reached before its
    floor is infeasible (EARLY) — a DIRECT collection whose freight does not
    exist at the origin yet."""
    if not ordered_jobs:
        return _infeasible_tour("EMPTY")

    cap_p, cap_kg = float(vehicle.capacity_pallets), float(vehicle.capacity_kg)
    running_p = sum(float(j.pallets) for j in ordered_jobs if j.leg_kind == CUSTOMER_DELIVERY)
    running_kg = sum(float(j.kg) for j in ordered_jobs if j.leg_kind == CUSTOMER_DELIVERY)
    if running_p > cap_p + _EPS or running_kg > cap_kg + _EPS:
        return _infeasible_tour("CAPACITY")
    peak_p, peak_kg = running_p, running_kg   # load leaving the depot (all deliveries aboard)

    day_index = 0
    day_drive = 0.0
    day_elapsed = 0.0
    day_clock = 0.0
    drive_since_break = 0.0
    if resume is not None:
        # evaluate a tail from the vehicle's real mid-day state (start position is
        # already vehicle.start_*): seed day 0's consumed duty so "no new day" stays
        # truthful — an insert that would spill today's remaining hours counts +1 day.
        day_drive = float(resume.day_drive)
        day_elapsed = float(resume.day_elapsed)
        day_clock = (float(resume.clock_minute)
                     if resume.clock_minute is not None else day_elapsed)
        drive_since_break = float(resume.drive_since_break)
    hgv = str(vehicle.vehicle_type).lower() != "van"
    total_km = total_drive = 0.0
    prev_lat, prev_lon = vehicle.start_lat, vehicle.start_lon
    stops: list[TourStop] = []
    auxiliary_stops: list[TourStop] = []
    _midleg = bool(_config.MULTIDAY_MIDLEG_OVERNIGHT)
    _merge_addr = bool(_config.SAME_ADDRESS_DWELL_MERGE)
    tag = str(getattr(vehicle, "vehicle_id", "") or "T")
    day_starts: list[DayStart] = []
    if _midleg:
        day_starts.append(DayStart(0, float(vehicle.start_lat), float(vehicle.start_lon),
                                   "DEPOT", float(running_p), float(running_kg)))

    for job in ordered_jobs:
        leg_km = _leg_km(prev_lat, prev_lon, job)
        dm = _leg_minutes(prev_lat, prev_lon, job, vehicle.vehicle_type)
        stop_leg_km = leg_km
        stop_leg_minutes = dm
        sm = service_minutes(job.pallets, vehicle.vehicle_type)
        if (job.leg_kind in _TWO_POINT_KINDS
                and job.origin_lat is not None and job.origin_lon is not None):
            sm *= 2.0  # handling at the collection point AND the destination/hub
        elif _merge_addr and job.lat == prev_lat and job.lon == prev_lon:
            # Same-address consolidation: a stop at the dock the vehicle already
            # occupies adds no new dwell. The fixed visit allowance is charged
            # once per contiguous run.
            sm -= service_minutes(0.0, vehicle.vehicle_type)

        def _record_direct_collection(dm_a: float, sm_a: float, bmin_a: float) -> None:
            """Surface the collection-side work before advancing to the delivery day."""
            approach_km = road_km(
                prev_lat, prev_lon, float(job.origin_lat), float(job.origin_lon)
            )
            coll_arrive = day_clock + dm_a + bmin_a
            coll_depart = coll_arrive + sm_a
            coll_since = (
                statutory_breaks(drive_since_break, dm_a)[1] if hgv else 0.0
            )
            auxiliary_stops.append(TourStop(
                job_id=f"__DIRECT_COLLECT__:{job.job_id}",
                node=(
                    f"OVERNIGHT@{float(job.origin_lat):.5f},"
                    f"{float(job.origin_lon):.5f}"
                ),
                day_index=day_index,
                leg_km=approach_km,
                load_pallets_after=running_p + float(job.pallets),
                load_kg_after=running_kg + float(job.kg),
                arrive_minute=coll_arrive,
                leg_minutes=dm_a,
                depart_minute=coll_depart,
                break_minutes_before=bmin_a,
                day_drive_after=day_drive + dm_a,
                drive_since_break_after=coll_since,
                duty_elapsed_after=day_elapsed + dm_a + bmin_a + sm_a,
            ))
            day_starts.append(DayStart(
                day_index + 1,
                float(job.origin_lat),
                float(job.origin_lon),
                f"OVERNIGHT:{tag}:{day_index + 1}",
                running_p + float(job.pallets),
                running_kg + float(job.kg),
            ))
        # split decision — flag-gated. arr_dm is the drive that lands the arrival on
        # the final day: the whole leg when nothing splits, the residual when the
        # vehicle sleeps part-way along it. total_drive always books the full leg dm.
        # The `day_elapsed > 0` gate matches the stop-boundary split: mid-leg only
        # relocates a day boundary OFF would already have created, so days_ON never
        # exceed days_OFF (the first leg out of the depot is left whole, as OFF does).
        if _midleg and job.leg_kind not in _TWO_POINT_KINDS:
            cur = _DayCursor(day_index, day_drive, day_elapsed, drive_since_break,
                             clock_minute=day_clock)
            arr_dm, bmin = _advance_single_point(
                cur, dm, sm, hgv, elapsed_cap_min, prev_lat, prev_lon,
                job.lat, job.lon, running_p, running_kg, tag, day_starts,
                split_stops=auxiliary_stops, leg_km=leg_km, job_id=job.job_id)
            if arr_dm < dm - _EPS:
                residual_fraction = arr_dm / dm if dm > _EPS else 1.0
                stop_leg_minutes = arr_dm
                stop_leg_km = leg_km * residual_fraction
            day_index, day_drive, day_elapsed, drive_since_break = (
                cur.day_index, cur.day_drive, cur.day_elapsed, cur.drive_since_break)
            day_clock = float(cur.clock_minute or 0.0)
            new_since = statutory_breaks(drive_since_break, arr_dm)[1] if hgv else 0.0
            if (day_drive + arr_dm > _DAY_DRIVE_CAP_MIN + _EPS
                    or day_elapsed + arr_dm + bmin + sm > elapsed_cap_min + _EPS):
                return _infeasible_tour("DAY_CAP")
        else:
            # break cost feeds the split decision (the duty cap includes break time);
            # recomputed fresh if a split resets the accumulator
            arr_dm = dm
            bmin, new_since = (statutory_breaks(drive_since_break, dm) if hgv else (0.0, 0.0))
            full_fits = (day_drive + dm <= _DAY_DRIVE_CAP_MIN + _EPS
                         and day_elapsed + dm + bmin + sm <= elapsed_cap_min + _EPS)
            if not full_fits:
                split_done = False
                if (_config.TOUR_DIRECT_OVERNIGHT_SPLIT
                        and job.leg_kind in _TWO_POINT_KINDS
                        and job.origin_lat is not None and job.origin_lon is not None):
                    # Overnight DIRECT (user rule 2026-07-16): COLLECT today, sleep
                    # at the collection point, deliver tomorrow — the real
                    # overnight-direct move. Only when the collection segment +
                    # its handling still fit today; else the whole leg slides as
                    # before. The (single) stop lands on the DELIVERY day.
                    dm_a = _seg_minutes(prev_lat, prev_lon, job.origin_lat, job.origin_lon,
                                        vehicle.vehicle_type)
                    sm_a = service_minutes(job.pallets, vehicle.vehicle_type)
                    bmin_a = statutory_breaks(drive_since_break, dm_a)[0] if hgv else 0.0
                    coll_ok = (day_drive + dm_a <= _DAY_DRIVE_CAP_MIN + _EPS
                               and day_elapsed + dm_a + bmin_a + sm_a <= elapsed_cap_min + _EPS)
                    if (coll_ok and not (floor_offsets and job.job_id in floor_offsets
                                         and day_index < int(floor_offsets[job.job_id]))):
                        _record_direct_collection(dm_a, sm_a, bmin_a)
                        stop_leg_km = leg_km - auxiliary_stops[-1].leg_km
                        stop_leg_minutes = dm - dm_a
                        day_index += 1
                        day_drive = 0.0
                        day_elapsed = 0.0
                        day_clock = 0.0
                        drive_since_break = 0.0
                        arr_dm = dm - dm_a          # tomorrow drives origin->dest only
                        sm = sm - sm_a              # collection handling done today
                        bmin, new_since = (statutory_breaks(0.0, arr_dm) if hgv else (0.0, 0.0))
                        split_done = True
                if not split_done:
                    day_index += 1
                    day_drive = 0.0
                    day_elapsed = 0.0
                    day_clock = 0.0
                    drive_since_break = 0.0  # the overnight daily rest clears the accumulator
                    bmin, new_since = (statutory_breaks(0.0, dm) if hgv else (0.0, 0.0))
                    fresh_full_fits = (
                        dm <= _DAY_DRIVE_CAP_MIN + _EPS
                        and dm + bmin + sm <= elapsed_cap_min + _EPS
                    )
                    if (not fresh_full_fits
                            and _config.TOUR_DIRECT_OVERNIGHT_SPLIT
                            and job.leg_kind in _TWO_POINT_KINDS
                            and job.origin_lat is not None
                            and job.origin_lon is not None):
                        dm_a = _seg_minutes(
                            prev_lat, prev_lon, job.origin_lat, job.origin_lon,
                            vehicle.vehicle_type,
                        )
                        sm_a = service_minutes(job.pallets, vehicle.vehicle_type)
                        bmin_a = statutory_breaks(0.0, dm_a)[0] if hgv else 0.0
                        coll_ok = (
                            dm_a <= _DAY_DRIVE_CAP_MIN + _EPS
                            and dm_a + bmin_a + sm_a <= elapsed_cap_min + _EPS
                            and not (
                                floor_offsets
                                and job.job_id in floor_offsets
                                and day_index < int(floor_offsets[job.job_id])
                            )
                        )
                        rem_dm = dm - dm_a
                        rem_sm = sm - sm_a
                        rem_break, rem_since = (
                            statutory_breaks(0.0, rem_dm)
                            if hgv else (0.0, 0.0)
                        )
                        delivery_ok = (
                            rem_dm <= _DAY_DRIVE_CAP_MIN + _EPS
                            and rem_dm + rem_break + rem_sm
                            <= elapsed_cap_min + _EPS
                        )
                        if coll_ok and delivery_ok:
                            _record_direct_collection(dm_a, sm_a, bmin_a)
                            stop_leg_km = leg_km - auxiliary_stops[-1].leg_km
                            stop_leg_minutes = rem_dm
                            day_index += 1
                            arr_dm = rem_dm
                            sm = rem_sm
                            bmin, new_since = rem_break, rem_since
            if (day_drive + arr_dm > _DAY_DRIVE_CAP_MIN + _EPS
                    or day_elapsed + arr_dm + bmin + sm > elapsed_cap_min + _EPS):
                return _infeasible_tour("DAY_CAP")
        # deadline: a stop reached after its due day is infeasible (early is OK —
        # stakeholder: dwell is wasted time and resource; due = deadline)
        if due_offsets and job.job_id in due_offsets and day_index > int(due_offsets[job.job_id]):
            return _infeasible_tour("LATE")
        # readiness floor: a stop reached before its freight exists (a DIRECT
        # collection not yet ready at the origin) is infeasible EARLY
        if floor_offsets and job.job_id in floor_offsets and day_index < int(floor_offsets[job.job_id]):
            return _infeasible_tour("EARLY")

        raw_arrive_min = day_clock + arr_dm + bmin
        wait_min = 0.0
        # Customer-facing arrival floor (audit #5): no dock is open before 08:00, so an
        # early arrival becomes a real wait, cascading (via day_elapsed below) to any
        # later stop that day exactly as the daily side's route-start-shift / curb-wait
        # does. Driving/break accounting above is unaffected — only the wait is added.
        if (job.leg_kind in _CUSTOMER_CLAMP_KINDS
                and raw_arrive_min < _CUSTOMER_WINDOW_OPEN_MIN):
            wait_min = _CUSTOMER_WINDOW_OPEN_MIN - raw_arrive_min
        arrive_min = raw_arrive_min + wait_min
        depart_min = arrive_min + sm
        duty_after = day_elapsed + arr_dm + bmin + wait_min + sm
        if (day_drive + arr_dm > _DAY_DRIVE_CAP_MIN + _EPS
                or duty_after > elapsed_cap_min + _EPS):
            return _infeasible_tour("DAY_CAP")
        if hgv:
            drive_since_break = new_since

        if job.leg_kind == CUSTOMER_DELIVERY:
            running_p -= float(job.pallets)
            running_kg -= float(job.kg)
            on_p, on_kg = running_p, running_kg
        elif job.leg_kind in (CUSTOMER_PICKUP, HUB_DROP) or job.leg_kind in _TWO_POINT_KINDS:
            on_p = running_p + float(job.pallets)
            on_kg = running_kg + float(job.kg)
            if job.leg_kind == CUSTOMER_PICKUP:
                running_p, running_kg = on_p, on_kg
        else:
            on_p, on_kg = running_p, running_kg

        peak_p = max(peak_p, on_p)
        peak_kg = max(peak_kg, on_kg)
        if on_p > cap_p + _EPS or on_kg > cap_kg + _EPS:
            return _infeasible_tour("CAPACITY")

        total_km += leg_km
        total_drive += dm                  # the whole leg, regardless of day attribution
        day_drive += arr_dm                 # only the final-day residual (== dm when no split)
        day_elapsed = duty_after
        day_clock = depart_min
        stops.append(TourStop(job.job_id, job.node, day_index, stop_leg_km,
                              running_p, running_kg,
                              arrive_minute=arrive_min, leg_minutes=stop_leg_minutes,
                              depart_minute=depart_min,
                              break_minutes_before=bmin,
                              day_drive_after=day_drive,
                              drive_since_break_after=drive_since_break,
                              duty_elapsed_after=day_elapsed))
        prev_lat, prev_lon = job.lat, job.lon

    # Return leg to the home depot: record as first-class TourStop(s) so its km / drive /
    # time are attributed PER DAY (audit #2/#4) — the emission previously reconstructed the
    # whole return as a single residual row (whole-tour drive on one day, no timestamp).
    back_km = road_km(prev_lat, prev_lon, vehicle.home_lat, vehicle.home_lon)
    back_dm = _seg_minutes(prev_lat, prev_lon, vehicle.home_lat, vehicle.home_lon,
                           vehicle.vehicle_type)
    if back_km > _EPS or back_dm > _EPS:
        b_full = statutory_breaks(drive_since_break, back_dm)[0] if hgv else 0.0
        return_over_cap = (
            day_drive + back_dm > _DAY_DRIVE_CAP_MIN + _EPS
            or day_elapsed + back_dm + b_full > elapsed_cap_min + _EPS
        )
        must_split = _midleg and day_elapsed > 0 and return_over_cap
        if must_split:
            # sleep part-way home: bank what fits today (a return-overnight stop), the
            # remainder drives home tomorrow (one overnight, as the mid-leg split does).
            x = max(0.0, _drive_fits(drive_since_break, _DAY_DRIVE_CAP_MIN - day_drive,
                                     elapsed_cap_min - day_elapsed, hgv))
            # break owed DURING this partial drive (audit #11 caught this hardcoded to 0.0:
            # _drive_fits already reserves duty-room for it when sizing x, but the stop record
            # must surface it too, or a >4.5h day-1 stretch of a split return silently shows no
            # break at all). Included in the arrival, exactly like a normal customer leg's bmin.
            b_x = statutory_breaks(drive_since_break, x)[0] if hgv else 0.0
            frac = x / back_dm if back_dm > _EPS else 1.0
            o_lat, o_lon = _interp_latlon(prev_lat, prev_lon,
                                          vehicle.home_lat, vehicle.home_lon, frac)
            stops.append(TourStop(f"__RETURN_LEG__:{day_index}",
                                  f"OVERNIGHT:{tag}:{day_index + 1}", day_index,
                                  back_km * frac, running_p, running_kg,
                                  arrive_minute=day_clock + x + b_x, leg_minutes=x,
                                  depart_minute=day_clock + x + b_x, break_minutes_before=b_x,
                                  day_drive_after=day_drive + x, drive_since_break_after=0.0,
                                  duty_elapsed_after=day_elapsed + x + b_x))
            day_index += 1
            day_starts.append(DayStart(day_index, o_lat, o_lon,
                                       f"OVERNIGHT:{tag}:{day_index}",
                                       float(running_p), float(running_kg)))
            rem_dm = back_dm - x
            b_rem = statutory_breaks(0.0, rem_dm)[0] if hgv else 0.0
            if (rem_dm > _DAY_DRIVE_CAP_MIN + _EPS
                    or rem_dm + b_rem > elapsed_cap_min + _EPS):
                return _infeasible_tour("DAY_CAP")
            drive_since_break = statutory_breaks(0.0, rem_dm)[1] if hgv else 0.0
            day_drive, day_elapsed = rem_dm, rem_dm + b_rem
            day_clock = day_elapsed
            stops.append(TourStop("__RETURN__", "DEPOT", day_index,
                                  back_km * (1.0 - frac), running_p, running_kg,
                                  arrive_minute=rem_dm + b_rem, leg_minutes=rem_dm,
                                  depart_minute=rem_dm + b_rem, break_minutes_before=b_rem,
                                  day_drive_after=day_drive,
                                  drive_since_break_after=drive_since_break,
                                  duty_elapsed_after=day_elapsed))
        elif return_over_cap:
            # Mid-leg parking disabled: take the overnight rest at the last stop,
            # then run the complete return on a fresh day.
            day_index += 1
            b_fresh = statutory_breaks(0.0, back_dm)[0] if hgv else 0.0
            if (back_dm > _DAY_DRIVE_CAP_MIN + _EPS
                    or back_dm + b_fresh > elapsed_cap_min + _EPS):
                return _infeasible_tour("DAY_CAP")
            drive_since_break = (
                statutory_breaks(0.0, back_dm)[1] if hgv else 0.0
            )
            day_drive = back_dm
            day_elapsed = back_dm + b_fresh
            day_clock = day_elapsed
            stops.append(TourStop(
                "__RETURN__", "DEPOT", day_index, back_km,
                running_p, running_kg,
                arrive_minute=day_clock,
                leg_minutes=back_dm,
                depart_minute=day_clock,
                break_minutes_before=b_fresh,
                day_drive_after=day_drive,
                drive_since_break_after=drive_since_break,
                duty_elapsed_after=day_elapsed,
            ))
        else:
            if hgv:
                drive_since_break = statutory_breaks(drive_since_break, back_dm)[1]
            day_drive += back_dm
            day_elapsed += back_dm + b_full
            day_clock += back_dm + b_full
            stops.append(TourStop("__RETURN__", "DEPOT", day_index, back_km,
                                  running_p, running_kg, arrive_minute=day_clock,
                                  leg_minutes=back_dm, depart_minute=day_clock,
                                  break_minutes_before=b_full, day_drive_after=day_drive,
                                  drive_since_break_after=drive_since_break,
                                  duty_elapsed_after=day_elapsed))
    total_km += back_km
    total_drive += back_dm

    days = day_index + 1
    if days > MAX_TOUR_DAYS_HARD:
        return _infeasible_tour("TOUR_TOO_LONG")

    return TourEvaluation(True, "", total_km, total_drive, days, tuple(stops),
                          peak_pallets=peak_p, peak_kg=peak_kg,
                          day_starts=tuple(day_starts),
                          auxiliary_stops=tuple(auxiliary_stops))


def try_insert_tour_job(vehicle: RouteVehicle, jobs: list[RouteJob], candidate: RouteJob,
                        due_offsets: dict | None = None,
                        floor_offsets: dict | None = None,
                        resume: "_DayCursor | None" = None):
    """Insert ``candidate`` into an existing tour at the best feasible position.

    Tries every position, evaluates with the full tour physics (capacity peak,
    two-cap day split, LATE, EARLY), and returns ``(new_jobs, evaluation)`` for
    the feasible insertion with the lowest total km — or ``None`` if no position
    fits. Used by the stranded-order backhaul repair: a full-load DIRECT can only
    ride where the trailer is empty, which this search finds naturally.

    ``resume`` evaluates the sequence as an in-flight tour's tail from the
    vehicle's mid-day state (see ``evaluate_tour``)."""
    best = None
    for pos in range(len(jobs) + 1):
        seq = list(jobs)
        seq.insert(pos, candidate)
        ev = evaluate_tour(vehicle, seq, due_offsets, floor_offsets=floor_offsets,
                           resume=resume)
        if ev.feasible and (best is None or ev.total_km < best[1].total_km):
            best = (seq, ev)
    return best


def best_tour_attachment(vehicle: RouteVehicle, tail_jobs: list[RouteJob],
                         candidate: RouteJob, *, resume: "_DayCursor | None" = None,
                         due_offsets: dict | None = None,
                         floor_offsets: dict | None = None,
                         standalone_km: float = float("inf"),
                         max_extra_days: int = 0):
    """Free-ride ``candidate`` onto an in-flight tour's ``tail_jobs``, or ``None``.

    Guards (all must hold): the tail with the candidate is feasible; it adds no more
    than ``max_extra_days`` days beyond the tail's own day count (0 = never grow the
    tour); and the added km does not exceed ``standalone_km`` (a dedicated run must
    not be cheaper). Returns ``(new_jobs, evaluation, added_km)`` for the lowest-km
    feasible insertion that clears the guards."""
    base = evaluate_tour(vehicle, tail_jobs, due_offsets, floor_offsets=floor_offsets,
                         resume=resume)
    if not base.feasible:
        return None
    got = try_insert_tour_job(vehicle, tail_jobs, candidate, due_offsets=due_offsets,
                              floor_offsets=floor_offsets, resume=resume)
    if got is None:
        return None
    new_jobs, ev = got
    if ev.days > base.days + max_extra_days:
        return None
    added_km = ev.total_km - base.total_km
    if added_km > standalone_km:
        return None
    return new_jobs, ev, added_km


def tour_tail_from(vehicle: RouteVehicle, ordered_jobs: list[RouteJob],
                   tour_eval: TourEvaluation, committed_count: int):
    """Split an in-flight tour at ``committed_count`` (from
    ``epoch_state.committed_stop_count``) into
    ``(head_jobs, tail_jobs, resume_vehicle, resume_cursor)``.

    The head is the bit-frozen committed prefix — the stops the driver has begun or
    is already rolling toward — and is never touched. The tail is what a new order may
    free-ride into, evaluated from the vehicle's real mid-day resume state: repositioned
    to the last head stop, seeded with that day's consumed duty, and with capacity
    reduced by any freight still aboard from the head. ``committed_count == 0`` ⇒ the
    whole tour, resumed fresh from the depot (``resume_cursor is None``)."""
    n = max(0, min(int(committed_count), len(ordered_jobs)))
    head, tail = list(ordered_jobs[:n]), list(ordered_jobs[n:])
    if n == 0:
        return head, tail, vehicle, None
    last_job = ordered_jobs[n - 1]
    last_stop = next(
        (stop for stop in reversed(tour_eval.stops)
         if str(stop.job_id) == str(last_job.job_id)),
        None,
    )
    if last_stop is None:
        raise ValueError(
            f"committed tour job has no evaluated stop: {last_job.job_id}"
        )
    duty_elapsed = float(
        last_stop.duty_elapsed_after
        if last_stop.duty_elapsed_after > 0.0 else last_stop.depart_minute
    )
    cursor = _DayCursor(
        0,
        float(last_stop.day_drive_after),
        duty_elapsed,
        float(last_stop.drive_since_break_after),
        clock_minute=float(last_stop.depart_minute),
    )
    aboard_p, aboard_kg = float(last_stop.load_pallets_after), float(last_stop.load_kg_after)
    tail_deliv_p = sum(float(j.pallets) for j in tail if j.leg_kind == CUSTOMER_DELIVERY)
    tail_deliv_kg = sum(float(j.kg) for j in tail if j.leg_kind == CUSTOMER_DELIVERY)
    rveh = replace(vehicle, start_lat=float(last_job.lat), start_lon=float(last_job.lon),
                   start_node=str(last_job.node),
                   capacity_pallets=float(vehicle.capacity_pallets) - max(0.0, aboard_p - tail_deliv_p),
                   capacity_kg=float(vehicle.capacity_kg) - max(0.0, aboard_kg - tail_deliv_kg))
    return head, tail, rveh, cursor


# ---------------------------------------------------------- tour batcher ------

@dataclass
class Tour:
    jobs: list[RouteJob]
    evaluation: TourEvaluation
    # Days the tour departs BEFORE its batch's earliest due (fix A, 2026-07-22):
    # 0 = the classic depart-on-earliest-due; >0 = the evaluation assumed an
    # earlier start because departing on the due day arrived LATE somewhere.
    lead_days: int = 0

    @property
    def total_pallets(self) -> float:
        return sum(float(j.pallets) for j in self.jobs)

    @property
    def total_kg(self) -> float:
        return sum(float(j.kg) for j in self.jobs)


def _due(job: RouteJob, due_by_job: dict | None) -> str:
    return str(due_by_job.get(job.job_id, "")) if due_by_job else ""


# A tour of D days can at most usefully depart D-1 days before its earliest due.
MAX_TOUR_LEAD_DAYS = MAX_TOUR_DAYS_HARD - 1


def eval_with_lead(vehicle: RouteVehicle, ordered: list[RouteJob],
                   due_by_job: dict | None,
                   ready_by_job: dict | None = None) -> tuple["TourEvaluation", int]:
    """Evaluate a tour; on LATE, retry with the departure pulled EARLIER (fix A,
    2026-07-22, the slack-run Scotland split): a batch departs on its earliest
    member due, so two same-due stops where the far one lands day+1 evaluate
    LATE — but the freight is usually already staged, and departing a day early
    serves everyone on time (what a human planner does). Lead is bounded by
    freight readiness and ``MAX_TOUR_LEAD_DAYS``. Returns ``(evaluation, lead)``;
    lead 0 = the classic behavior, non-LATE failures never retry."""
    offs = _due_offsets(ordered, due_by_job)
    ev = evaluate_tour(vehicle, ordered, offs)
    if ev.feasible or not offs or ev.reason != "LATE":
        return ev, 0
    dues = [d for d in (_due(j, due_by_job) for j in ordered) if d]
    if not dues:
        return ev, 0
    min_due = min(dues)[:10]
    readys = [r for r in (str((ready_by_job or {}).get(j.job_id, "") or "")
                          for j in ordered) if r]
    max_ready = max(readys) if readys else ""
    try:
        min_due_d = date.fromisoformat(min_due)
    except ValueError:
        return ev, 0
    for lead in range(1, MAX_TOUR_LEAD_DAYS + 1):
        depart = (min_due_d - timedelta(days=lead)).isoformat()
        if max_ready and max_ready > depart:
            break                          # freight not at the depot that early
        ev2 = evaluate_tour(vehicle, ordered, {k: v + lead for k, v in offs.items()})
        if ev2.feasible:
            _tdbg(f"  lead retry: feasible at lead={lead} (departs {depart})")
            return ev2, lead
        if ev2.reason != "LATE":
            break
    return ev, 0


def _date_spread_days(jobs: list[RouteJob], due_by_job: dict | None) -> int:
    """Calendar span (days) between the earliest and latest due date in a set."""
    dues = []
    for j in jobs:
        s = _due(j, due_by_job)
        if s:
            try:
                dues.append(date.fromisoformat(s))
            except ValueError:
                pass
    return (max(dues) - min(dues)).days if len(dues) >= 2 else 0


def _due_offsets(jobs: list[RouteJob], due_by_job: dict | None) -> dict:
    """Map job_id -> days from the batch's earliest due date (for dwelling)."""
    if not due_by_job:
        return {}
    dues = {}
    for j in jobs:
        s = _due(j, due_by_job)
        if s:
            try:
                dues[j.job_id] = date.fromisoformat(s)
            except ValueError:
                pass
    if not dues:
        return {}
    base = min(dues.values())
    return {jid: (d - base).days for jid, d in dues.items()}


def _order_nearest_neighbour(jobs: list[RouteJob], vehicle: RouteVehicle,
                             due_by_job: dict | None = None) -> list[RouteJob]:
    """Visit order: by due date first, then by destination DEADLINE
    (``latest_finish`` timestamp), then greedy nearest-neighbour as a final
    tiebreak. Earliest-deadline-first within a day is what keeps every stop on
    time on a multi-day sweep (user rule 2026-07-11: deliver sequentially by the
    destination timestamp) — a same-day stop with a tight deadline must not be
    left for last just because a slacker stop is geographically nearer. Stops
    with no deadline (``""``) sort after dated ones within the same due day."""
    remaining = list(jobs)
    ordered: list[RouteJob] = []
    cur_lat, cur_lon = vehicle.start_lat, vehicle.start_lon
    while remaining:
        nxt = min(remaining, key=lambda j: (_due(j, due_by_job),
                                            str(getattr(j, "latest_finish", "") or "~"),
                                            haversine_km(cur_lat, cur_lon, j.lat, j.lon)))
        ordered.append(nxt)
        remaining.remove(nxt)
        cur_lat, cur_lon = nxt.lat, nxt.lon
    return ordered


def _min_gap_km(job: RouteJob, tour_jobs: list[RouteJob]) -> float:
    return min(haversine_km(job.lat, job.lon, t.lat, t.lon) for t in tour_jobs)


def build_tours(
    jobs: list[RouteJob],
    vehicle: RouteVehicle,
    cohesion_km: float = TOUR_COHESION_KM,
    due_by_job: dict | None = None,
    max_span_days: int = MAX_TOUR_DAYS_HARD,
    ready_by_job: dict | None = None,
) -> list[Tour]:
    """Batch far jobs into cohesive multi-day tours.

    Seeds each tour with the farthest unassigned job, then greedily adds the
    nearest remaining job that (a) is within ``cohesion_km`` of the tour, (b)
    keeps the tour's *due-date* spread within ``max_span_days`` so far work
    sitting at the depot on nearby dates is batched onto one sweep without serving
    any stop wildly off its due date, (c) keeps the tour feasible (capacity +
    day cap), and (d) has its freight AT the depot by tour departure: the tour
    departs on the batch's earliest due date with every delivery aboard, so a
    job whose ``ready_by_job`` date (ISO, e.g. the day after its feeding pickup)
    is later than that departure cannot ride it — it seeds a later tour instead.
    The cohesion radius prevents merging distant regions.
    """
    remaining = list(jobs)
    tours: list[Tour] = []

    def _dist_from_depot(j: RouteJob) -> float:
        return haversine_km(vehicle.start_lat, vehicle.start_lon, j.lat, j.lon)

    def _freight_ready_by_departure(members: list[RouteJob]) -> bool:
        # departure = earliest due date in the batch; every member's freight must
        # be at the depot by then (ISO strings compare lexicographically).
        if not ready_by_job or not due_by_job:
            return True
        dues = [d for d in (_due(j, due_by_job) for j in members) if d]
        readys = [r for r in (str(ready_by_job.get(j.job_id, "") or "") for j in members) if r]
        if not dues or not readys:
            return True
        return max(readys) <= min(dues)

    while remaining:
        seed = max(remaining, key=_dist_from_depot)
        remaining.remove(seed)
        tour_jobs = [seed]
        _tdbg(f"batch seed={seed.job_id} from=({vehicle.start_lat:.3f},{vehicle.start_lon:.3f}) "
              f"pool={[j.job_id for j in remaining]}")
        # A candidate whose trial fails (capacity, day caps, LATE) must not end
        # the tour's growth — later candidates may still fit (the KA1-vs-KA6
        # fragmentation bug). Infeasibility is monotone as the tour grows (more
        # jobs = more load, more driving, later arrivals), so a failed candidate
        # stays blocked for THIS tour and seeds/joins another instead.
        blocked: set[str] = set()

        while remaining:
            within = [j for j in remaining
                      if j.job_id not in blocked
                      and _min_gap_km(j, tour_jobs) <= cohesion_km
                      and _date_spread_days(tour_jobs + [j], due_by_job) <= max_span_days
                      and _freight_ready_by_departure(tour_jobs + [j])]
            if not within:
                if _TOUR_DEBUG and remaining:
                    for j in remaining:
                        why = ("blocked" if j.job_id in blocked
                               else f"cohesion({_min_gap_km(j, tour_jobs):.0f}km)"
                               if _min_gap_km(j, tour_jobs) > cohesion_km
                               else f"due-spread({_date_spread_days(tour_jobs + [j], due_by_job)}d)"
                               if _date_spread_days(tour_jobs + [j], due_by_job) > max_span_days
                               else "ready-gate"
                               if not _freight_ready_by_departure(tour_jobs + [j])
                               else "??")
                        _tdbg(f"  growth stop: {j.job_id} excluded by {why}")
                break
            cand = min(within, key=lambda j: _min_gap_km(j, tour_jobs))
            trial = _order_nearest_neighbour(tour_jobs + [cand], vehicle, due_by_job)
            ev, _lead = eval_with_lead(vehicle, trial, due_by_job, ready_by_job)
            if not ev.feasible:
                _tdbg(f"  trial +{cand.job_id} INFEASIBLE reason={ev.reason} "
                      f"order={[j.job_id for j in trial]}")
                if _TOUR_DEBUG:
                    offs = _due_offsets(trial, due_by_job)
                    for j in trial:
                        _tdbg(f"    job {j.job_id} kind={j.leg_kind} @({j.lat:.4f},{j.lon:.4f}) "
                              f"origin=({j.origin_lat},{j.origin_lon}) pal={j.pallets} kg={j.kg} "
                              f"due={due_by_job.get(j.job_id) if due_by_job else None} off={offs.get(j.job_id)}")
                    _tdbg(f"    proto=({vehicle.start_lat:.4f},{vehicle.start_lon:.4f}) "
                          f"start_time={vehicle.start_time} cap={vehicle.capacity_pallets}/{vehicle.capacity_kg} "
                          f"slack={_config.TRAVEL_TIME_SLACK}")
                blocked.add(cand.job_id)
                continue
            _tdbg(f"  grew +{cand.job_id} days={ev.days}")
            # Fix B (2026-07-22): blocked assumes infeasibility is monotone as the
            # tour grows — true for capacity/day-caps, FALSE for due-anchoring. A
            # member with an EARLIER due re-anchors the departure day, so every
            # LATE-blocked candidate deserves a retry.
            prev_dues = [d for d in (_due(j, due_by_job) for j in tour_jobs) if d]
            cand_due = _due(cand, due_by_job)
            if blocked and cand_due and prev_dues and cand_due < min(prev_dues):
                _tdbg(f"  earlier due {cand_due} re-anchors batch -> unblocking {sorted(blocked)}")
                blocked.clear()
            tour_jobs = trial
            remaining.remove(cand)

        ordered = _order_nearest_neighbour(tour_jobs, vehicle, due_by_job)
        final_ev, final_lead = eval_with_lead(vehicle, ordered, due_by_job, ready_by_job)
        _tdbg(f"batch formed: {[j.job_id for j in ordered]} feasible={final_ev.feasible} "
              f"days={final_ev.days} km={final_ev.total_km:.0f} lead={final_lead}")
        tours.append(Tour(ordered, final_ev, final_lead))

    return tours


# ------------------------------------------- cross-depot consolidation --------

def _primary_depot(jobs, depots, depot_of) -> str:
    """The holding depot with the most of the cluster's freight (by pallets, then kg,
    then id) — the smaller share is the one collected in passing as a load-stop."""
    def _pal(d):
        return sum(float(j.pallets) for j in jobs if depot_of(j) == d)

    def _kg(d):
        return sum(float(j.kg) for j in jobs if depot_of(j) == d)

    return sorted(depots, key=lambda d: (-_pal(d), -_kg(d), d))[0]


def _origin_at_depot(job, anchors: dict = DEPOT_ANCHORS, radius_km: float = TOUR_ORIGIN_AT_DEPOT_RADIUS_KM) -> bool:
    """True when a DIRECT move's collection origin sits at a depot (e.g. the Stoke yard,
    where the order origin IS the satellite depot). Collecting it is then a depot visit
    via the move's own origin->dest leg, not cross-territory customer collection."""
    if job.origin_lat is None or job.origin_lon is None:
        return False
    return any(haversine_km(float(job.origin_lat), float(job.origin_lon), a[0], a[1]) <= radius_km
               for a in anchors.values())


def resolve_cluster(jobs, source_depot_of, due_by_job, proto_for,
                    anchors: dict = DEPOT_ANCHORS, cohesion_km: float = TOUR_COHESION_KM,
                    ready_by_job: dict | None = None):
    """Turn one emergent cluster into one or more (anchor_depot, ordered_jobs, evaluation),
    re-evaluated against the real anchor depot (build_tours' centroid eval was only for
    clustering):

      * single holding depot            -> one tour anchored there (today's behaviour);
      * multi depot, all depot-loadable -> one tour, front load-stops at the other depots;
      * multi depot incl. a non-depot DIRECT -> consolidate it as an en-route pickup when
        that is feasible and no more km than splitting; else fall back per source depot.

    An infeasible consolidation also falls back per depot, so coverage never drops.
    """
    def _depot_of(j):
        d = source_depot_of(j.job_id)
        return d if d in anchors else nearest_depot(j.lat, j.lon, anchors)[0]

    depots = {_depot_of(j) for j in jobs}

    def _build_at(depot, depot_jobs, extra=()):
        proto = proto_for(depot)
        ordered = _order_nearest_neighbour(list(depot_jobs) + list(extra), proto, due_by_job)
        ev, lead = eval_with_lead(proto, ordered, due_by_job, ready_by_job)
        return depot, ordered, ev, lead

    def _per_depot():
        groups: dict[str, list] = {}
        for j in jobs:
            groups.setdefault(_depot_of(j), []).append(j)
        out = []
        for dep in sorted(groups):
            for t in build_tours(groups[dep], proto_for(dep), cohesion_km, due_by_job,
                                 ready_by_job=ready_by_job):
                out.append((dep, t.jobs, t.evaluation, t.lead_days))
        return out

    # A non-depot-origin DIRECT is an en-route CUSTOMER collection on the sweep (the
    # X8RNW pattern). It is allowed, but only kept when it does not cost more km than
    # splitting per depot -- so an off-corridor origin (or an infeasible fold) falls back
    # automatically and coverage never drops. Depot-origin DIRECTs and pure-delivery
    # clusters keep today's behaviour (the guard is inert for them).
    has_nondepot_direct = any(
        j.leg_kind == DIRECT_CUSTOMER_MOVE and not _origin_at_depot(j, anchors) for j in jobs)

    def _keep_or_split(depot, ordered, ev, lead=0):
        if not ev.feasible:
            _tdbg(f"resolve: fold@{depot} {[j.job_id for j in ordered]} INFEASIBLE "
                  f"reason={ev.reason} -> per-depot split")
            return _per_depot()
        if has_nondepot_direct:
            split = _per_depot()
            if not all(e.feasible for _, _, e, _l in split):
                return [(depot, ordered, ev, lead)]  # split can't serve it either -> keep the feasible fold
            split_km = sum(e.total_km for _, _, e, _l in split)
            if ev.total_km > split_km + _EPS:
                _tdbg(f"resolve: fold@{depot} km={ev.total_km:.0f} > split km={split_km:.0f} -> split")
                return split
        _tdbg(f"resolve: kept fold@{depot} {[j.job_id for j in ordered]} days={ev.days} lead={lead}")
        return [(depot, ordered, ev, lead)]

    if len(depots) == 1:
        depot, ordered, ev, lead = _build_at(next(iter(depots)), jobs)
        return _keep_or_split(depot, ordered, ev, lead)

    # Load-stops are only for freight STAGED at a depot (deliveries). DIRECT moves and
    # regional pickups collect during the sweep via their own legs, so they ride along
    # without a load-stop (never double-visiting their depot). Anchor + load-stops are
    # taken from the DELIVERY depots only.
    delivery_depots = {_depot_of(j) for j in jobs if j.leg_kind == CUSTOMER_DELIVERY}
    if len(delivery_depots) <= 1:
        primary = (next(iter(delivery_depots)) if delivery_depots
                   else _primary_depot(jobs, depots, _depot_of))
        depot, ordered, ev, lead = _build_at(primary, jobs)
        return _keep_or_split(depot, ordered, ev, lead)

    primary = _primary_depot([j for j in jobs if j.leg_kind == CUSTOMER_DELIVERY],
                             delivery_depots, _depot_of)
    load_stops = [load_stop_job(d, anchors[d][0], anchors[d][1])
                  for d in sorted(delivery_depots - {primary})]
    depot, ordered, ev, lead = _build_at(primary, jobs, load_stops)
    return _keep_or_split(depot, ordered, ev, lead)


# ------------------------------------------------------ Q4 vehicle choice -----

# Deadhead granularity for tour vehicle choice: vehicles whose home is within one
# bucket of the staging depot are treated as equidistant (type/capacity then decide),
# so the geographic key separates depot-scale distances (~40-180 km apart) without
# churning on small differences. One bucket (~50 km) ~= one displaced local job, so
# busyness and deadhead combine on a common scale (see select_tour_vehicle).
_DEADHEAD_BUCKET_KM: float = 50.0

# Scarce satellite depots (tiny dedicated fleets) that must NOT be drained to serve a
# tour anchored ELSEWHERE — mirrors scope.SPOKE_DELIVERY_RADIUS_KM, the same "capacity-
# constrained spoke" set used for FULL_FLEET staging (Stoke: 5 tractors). A satellite
# vehicle is still freely used for a tour anchored at ITS OWN depot. (decision-audit #7)
from freight_planner.shared.scope import SPOKE_DELIVERY_RADIUS_KM as _SPOKE_RADII
_SCARCE_TOUR_DEPOTS: frozenset = frozenset(_SPOKE_RADII)


def _fits(vehicle: RouteVehicle, tour_pallets: float, tour_kg: float = 0.0) -> bool:
    return (float(vehicle.capacity_pallets) + _EPS >= float(tour_pallets)
            and float(vehicle.capacity_kg) + _EPS >= float(tour_kg))


def select_tour_vehicle(
    tour_pallets: float,
    vehicles: list[RouteVehicle],
    light_threshold: float = LIGHT_TOUR_PALLETS,
    busyness: dict[str, float] | None = None,
    prefer_depot: str | None = None,
    tour_kg: float = 0.0,
    tour_km: float = 0.0,
) -> RouteVehicle | None:
    """Pick a vehicle for a tour.

    Only vehicles that can carry the load — both pallets AND weight (kg) — are
    eligible; the tour is built against a generic proto vehicle, so the real
    vehicle's weight cap must be re-checked here or a heavy tour can be assigned
    to a vehicle that cannot carry it. Preference order:
      1. NOT draining a scarce satellite for a tour anchored elsewhere (Stoke's 5
         tractors are reserved for Stoke-anchored work);
      2. least combined (displaced local work + repositioning deadhead) — busyness
         and the home->staging distance on one scale, ~50 km ≈ one displaced job;
      3. Q4 type (artic, but a rigid when the tour is light on pallets);
      4. smallest sufficient capacity (least waste).

    This supersedes the earlier deadhead patch (decision-audit #7, 2026-07-26): that
    patch left busyness as a strictly-higher key, so an IDLE scarce-Stoke tractor still
    beat a lightly-busy CB22 tractor 41 km from the load and the deadhead was never
    consulted. Now (a) a scarce-satellite vehicle is deprioritised for non-satellite
    tours (coverage-safe: still returned if it is the only fit), and (b) busyness and
    deadhead combine, so a large empty repositioning outweighs a small busyness edge.
    A same-depot vehicle sits at deadhead ~0 and scarcity 0, so its natural preference
    is preserved; with no ``prefer_depot`` the geographic/scarcity terms are neutral and
    the pick is busyness-then-type-then-capacity as before. (Return-leg length — key #19
    — is still not modelled here; that needs a per-candidate tour re-eval, deferred.)
    """
    eligible = [v for v in vehicles if _fits(v, tour_pallets, tour_kg)]
    if not eligible:
        return None
    busyness = busyness or {}
    stage = DEPOT_ANCHORS.get(prefer_depot) if prefer_depot else None
    # Q4 light-tour rigid preference is DISTANCE-BOUNDED (2026-07-21): beyond
    # TOUR_TRACTOR_KM the artic wins even when light — long-range is tractor
    # work in the real fleet (per-pallet economics on filled decks), the rigid
    # preference survives only for light AND short tours.
    from freight_planner.config import TOUR_TRACTOR_KM
    prefer_rigid = (float(tour_pallets) <= float(light_threshold)
                    and float(tour_km) <= float(TOUR_TRACTOR_KM))
    preferred_type = "rigid" if prefer_rigid else "tractor"

    def _key(v: RouteVehicle) -> tuple:
        busy = float(busyness.get(v.vehicle_id, 0.0))
        same_depot = bool(prefer_depot and v.home_depot == prefer_depot)
        # protect the scarce satellite unless the tour is anchored at its own depot
        scarcity_rank = 1 if (prefer_depot and v.home_depot in _SCARCE_TOUR_DEPOTS
                              and not same_depot) else 0
        # deadhead in ~50 km buckets; a same-depot vehicle is 0 by definition (freight
        # is at its home), asserted by STRING so it holds even if home coords are unset.
        if same_depot:
            deadhead_bucket = 0.0
        elif stage is not None:
            deadhead_bucket = round(haversine_km(v.home_lat, v.home_lon, stage[0], stage[1])
                                    / _DEADHEAD_BUCKET_KM)
        else:
            deadhead_bucket = 0.0
        combined = busy + deadhead_bucket          # displaced local work + repositioning
        type_rank = 0 if str(v.vehicle_type).lower() == preferred_type else 1
        return (scarcity_rank, combined, type_rank, float(v.capacity_pallets))

    return sorted(eligible, key=_key)[0]
