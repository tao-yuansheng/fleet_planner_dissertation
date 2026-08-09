"""Milestone 8a integration: multiday tour orchestration.

Ordering matters because a far delivery (served by a tour) can depend on a near
collection (served by the daily seed). So:

  1. classify far/tour-only jobs (geometry) and split them from daily jobs;
  2. build cohesive tours from the far jobs and *reserve* a vehicle for each
     tour's day span (so the daily seed can't use it);
  3. run the daily seed on the remaining jobs against a *shared* ledger, which
     records collections;
  4. commit the tours against that same ledger — by now any feeding collection
     is in place, so tour deliveries gate correctly.

A tour that can't get a vehicle releases its jobs (``NO_FEASIBLE_TOUR``) without
consuming one, so it never blocks normal dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from datetime import date, datetime, timedelta

import pandas as pd

from freight_planner.shared.config import DEPOT_ANCHORS
from freight_planner import config as _fp_config
from freight_planner.config import (
    TOUR_COHESION_KM,
    TOUR_DAY_START_HOUR,
    TOUR_TRACTOR_KM,
    TRUNK_DAY_DEPOTS,
    TRUNK_DEPOTS,
    TRUNK_ENABLED,
)
from freight_planner.cross_depot import assignment_kind
from freight_planner.freight_ledger import (
    FREIGHT_AT_CUSTOMER_ORIGIN, FREIGHT_AT_DEPOT, FREIGHT_DELIVERED,
    FREIGHT_ON_VEHICLE, FreightLedger,
)
from freight_planner.plan_records import _FREIGHT_STATE_AFTER
from freight_planner.plan_schema import SelectedPlanBuilder, SelectedPlanRecord
from freight_planner.planner_state import RejectedJob
from freight_planner.option_mutex import OptionMutex
from freight_planner.route_seed import (
    RouteSeedResult,
    _g,
    _job_coords,
    _route_vehicle,
    make_route_job,
    run_route_seed_plan,
)
from freight_planner.routing_adapter import RouteJob, RouteVehicle
from freight_planner.route_costs import road_km
from freight_planner.tours import (
    _DAY_DRIVE_CAP_MIN,
    _tdbg as _tour_dbg,
    _origin_at_depot,
    DEPOT_LOAD,
    TOUR_OVERNIGHT,
    tour_emission_events,
    TourEvaluation,
    build_tours,
    evaluate_tour,
    is_tour_only,
    load_stop_job,
    nearest_depot,
    resolve_cluster,
    select_tour_vehicle,
)
from freight_planner.trunk import draw_tractors, trunk_schedule
from freight_planner.vehicles import fleet_capacity_ceiling

CUSTOMER_PICKUP = "CUSTOMER_PICKUP"
CUSTOMER_DELIVERY = "CUSTOMER_DELIVERY"
DIRECT_CUSTOMER_MOVE = "DIRECT_CUSTOMER_MOVE"
HUB_DROP = "HUB_DROP"

# B37 7HB (Palletline national hub, Starley Way, Birmingham) approximate
# lat/lon, hardcoded so the trunk round-trip km computation needs no postcode
# cache (tour_plan has none in scope). road_km() itself is OSRM-cache-backed
# at run time with a haversine x ROAD_DISTANCE_FACTOR fallback, so this constant
# only needs to be a reasonable anchor point, not a live-geocoded lookup.
B37_LATLON = (52.4666, -1.7226)

# LE10 3BS (hazchem trunk hub) lat/lon, hardcoded for the same reason as
# B37_LATLON above. Value pinned from freight_planner/data/Output/postcode_cache.json
# entry "LE103BS" (== legs.HUB_POSTCODE[LE10_HUB]); test_tour_plan.py asserts
# this constant matches the cache entry at 1e-3 tolerance.
LE10_LATLON = (52.540784, -1.413558)

# Proto vehicle used to screen tour-batch feasibility. Its capacity is the real
# fleet ceiling (28 t / 26 pal) from the vehicle master -- NOT a stale copy -- so a
# 24-28 t load is not wrongly rejected. The specific real vehicle (which must fit the
# tour's PEAK load) is selected afterwards.
_PROTO_CAPACITY_P, _PROTO_CAPACITY_KG = fleet_capacity_ceiling()


@dataclass
class TourAssignment:
    vehicle_id: str
    start_date: str
    days: int
    jobs: list           # list[RouteJob], in visit order
    evaluation: TourEvaluation
    depot: str = ""      # anchor depot the evaluation was built against


@dataclass
class MultidaySeedResult:
    selected: list[SelectedPlanRecord]
    rejected: list[RejectedJob]
    ledger: FreightLedger
    routes: dict          # daily (vehicle_id, day) -> RouteEvaluation
    route_jobs: dict      # daily (vehicle_id, day) -> list[RouteJob]
    tours: list[TourAssignment]
    daily: RouteSeedResult | None = None     # the daily seed (for ALNS to improve)
    reserved: set[tuple[str, str]] = None    # (vehicle_id, day) held by tours
    tour_records: list[SelectedPlanRecord] = None  # tour rows (fixed; not ALNS-improved)
    trunk: object = None                     # TrunkPlan | None (T1 fixed nightly trunk service)


def _proto_vehicle(depot: str, day: str) -> RouteVehicle:
    lat, lon = DEPOT_ANCHORS.get(depot, DEPOT_ANCHORS["CB22"])
    return RouteVehicle(
        vehicle_id=f"PROTO:{depot}", start_node=depot, start_lat=lat, start_lon=lon,
        start_time=f"{day} 05:00:00", capacity_pallets=_PROTO_CAPACITY_P,
        capacity_kg=_PROTO_CAPACITY_KG, vehicle_type="tractor",
        home_depot=depot, home_lat=lat, home_lon=lon,
    )


def _anchor_or_nearest(src: str, lat: float, lon: float) -> str:
    """Trust a real staging depot; fall back to the nearest anchor only when the
    source depot is not a known anchor (should not happen once legs resolve every
    OVERFLOW to a real gateway)."""
    return src if src in DEPOT_ANCHORS else nearest_depot(lat, lon)[0]


def _as_depot_delivery(rjob: RouteJob, anchor_xy, enabled: bool) -> RouteJob:
    """A DIRECT move whose collection origin is AT its anchor depot is functionally a
    depot-loaded delivery: the collect happens where the tour already starts, so the atomic
    collect->deliver pairing serves no purpose and blocks same-destination consolidation
    (two directs evaluate as two round trips -> infeasible). Reclassify it to a
    CUSTOMER_DELIVERY so it batches like a delivery. Non-depot-origin directs (a real
    backtrack collection) and non-directs are returned unchanged."""
    if not enabled or rjob.leg_kind != DIRECT_CUSTOMER_MOVE or anchor_xy is None:
        return rjob
    if not _origin_at_depot(rjob, {"_anchor": anchor_xy}):   # check the ANCHOR depot specifically
        return rjob
    return _dc_replace(rjob, leg_kind=CUSTOMER_DELIVERY, origin_lat=None, origin_lon=None)


def _centroid_proto(day: str) -> RouteVehicle:
    """Proto anchored at the centroid of the depots — used only to seed/feasibility-test
    the pooled cross-depot clustering. Every depot is SE while far work is hundreds of km
    out, so any SE reference yields the same clusters; the centroid is a deterministic,
    hardcoding-free default."""
    lats = [a[0] for a in DEPOT_ANCHORS.values()]
    lons = [a[1] for a in DEPOT_ANCHORS.values()]
    clat, clon = sum(lats) / len(lats), sum(lons) / len(lons)
    return RouteVehicle(
        vehicle_id="PROTO:POOL", start_node="POOL", start_lat=clat, start_lon=clon,
        start_time=f"{day} 05:00:00", capacity_pallets=_PROTO_CAPACITY_P,
        capacity_kg=_PROTO_CAPACITY_KG, vehicle_type="tractor",
        home_depot="POOL", home_lat=clat, home_lon=clon,
    )


def cross_depot_tour_eval(vehicle, depot: str, jobs: list,
                          due_offsets: dict | None = None) -> tuple | None:
    """Re-price a cross-depot tour pick from the vehicle's REAL home.

    The cluster evaluation prices a tour from a proto anchored AT the depot, so
    handing it verbatim to a vehicle homed elsewhere teleports the truck: the
    home<->depot repositioning exists in no leg (WT255892's Stoke->KY11 tour was
    drawn CB22->KY11 with the ~230 km approach nowhere, 2026-07-16). Re-evaluate
    from the vehicle's own home with a DEPOT_LOAD call at the anchor depot — the
    freight is staged there — so the deadhead becomes real km/minutes and a
    visible stop. ``due_offsets`` (fix C, 2026-07-22) re-checks the members'
    deadlines against the LONGER cross-depot route — without it a tour that was
    on time from the anchor proto could ship a day late silently (the slack-run
    Fort William case). Returns (jobs_with_load, evaluation) or None if it no
    longer fits."""
    lat, lon = DEPOT_ANCHORS.get(depot, DEPOT_ANCHORS["CB22"])
    jobs2 = [load_stop_job(depot, lat, lon)] + list(jobs)
    ev2 = evaluate_tour(vehicle, jobs2, due_offsets)
    return (jobs2, ev2) if ev2.feasible else None


def _commit_leg(ledger: FreightLedger, leg_kind: str, order_id: str,
                source_depot: str, target_depot: str) -> tuple[bool, str]:
    """Gate freight readiness and apply the ledger transition for one tour leg."""
    if leg_kind == CUSTOMER_DELIVERY:
        if not ledger.exists_at_depot(order_id, source_depot):
            return False, "DELIVERY_BEFORE_PICKUP"
        ledger.deliver_from_depot(order_id, source_depot)
    elif leg_kind == CUSTOMER_PICKUP:
        if ledger.state_of(order_id) == FREIGHT_DELIVERED:
            return False, "FREIGHT_ALREADY_DELIVERED"
        ledger.pickup_to_depot(order_id, target_depot)
    elif leg_kind == HUB_DROP:
        if ledger.state_of(order_id) == FREIGHT_DELIVERED:
            return False, "FREIGHT_ALREADY_DELIVERED"
        ledger.handoff_to_hub(order_id)
    else:  # DIRECT_CUSTOMER_MOVE
        # A direct move needs freight AT its origin. If it is elsewhere (already
        # delivered, or staged AT_DEPOT because its XDOCK pickup ran this pass or a
        # prior epoch), the DIRECT option is superseded — skip cleanly rather than
        # let ledger.deliver_direct raise (both option groups now flow in).
        unit = ledger.get(order_id)
        if unit is None or unit.state not in (FREIGHT_AT_CUSTOMER_ORIGIN, FREIGHT_ON_VEHICLE):
            return False, ("OPTION_SUPERSEDED" if unit is not None else "DELIVERY_BEFORE_PICKUP")
        ledger.deliver_direct(order_id)
    return True, ""


def _span(start_date: str, days: int) -> list[str]:
    base = date.fromisoformat(start_date)
    return [(base + timedelta(days=d)).isoformat() for d in range(max(1, days))]


def _tour_clock(day_iso: str, minute: float) -> str:
    """Anchor a day-relative stop minute to the tour day's calendar clock."""
    if minute is None or minute < 0:
        return f"{day_iso} 12:00:00"
    total = TOUR_DAY_START_HOUR * 60 + int(round(float(minute)))
    return f"{day_iso} {total // 60:02d}:{total % 60:02d}:00"


def _max_shift_minutes(vehicles: pd.DataFrame, default: float = 720.0) -> float:
    """Longest available shift window across the fleet (elapsed, not the legal
    driving cap) — the budget the daily seed is actually bound by. A job is
    tour-only only if even this most generous shift cannot round-trip it."""
    best = 0.0
    for r in vehicles.itertuples(index=False):
        try:
            af = datetime.fromisoformat(str(_g(r, "available_from", "")))
            se = datetime.fromisoformat(str(_g(r, "shift_end", "")))
        except ValueError:
            continue
        best = max(best, (se - af).total_seconds() / 60.0)
    return best or default


def run_multiday_seed_plan(
    candidates: pd.DataFrame,
    vehicles: pd.DataFrame,
    compatibility: pd.DataFrame,
    freight_states: pd.DataFrame,
    start: date,
    plan_id: str = "MULTIDAY",
    cohesion_km: float = TOUR_COHESION_KM,
    consolidate_tours: bool = True,
    external_reserved: set[tuple[str, str]] | None = None,
    extra_avail_overrides: dict[tuple[str, str], object] | None = None,
    trunk_from: str | None = None,
    depot_direct_as_delivery: bool | None = None,
) -> MultidaySeedResult:
    if depot_direct_as_delivery is None:
        depot_direct_as_delivery = _fp_config.TOUR_DEPOT_DIRECT_AS_DELIVERY
    ledger = FreightLedger.from_initial_states(freight_states)
    if candidates is None or candidates.empty:
        # A late rolling epoch can filter (via `visible`) down to zero plannable
        # candidate legs. daily MUST be a valid empty RouteSeedResult, not the
        # dataclass default None: solve_window feeds seed.daily straight into
        # improve_route_seed, which dereferences .route_jobs and would crash.
        empty_daily = RouteSeedResult([], [], ledger, {}, {}, {}, set(), {})
        return MultidaySeedResult([], [], ledger, {}, {}, [], daily=empty_daily,
                                  reserved=set(), tour_records=[])

    coords = _job_coords(compatibility)
    vrows = {str(_g(r, "vehicle_id", "")): r for r in vehicles.itertuples(index=False)}
    runnable = candidates[candidates.get("hard_blocker", pd.Series(dtype=str)).fillna("").eq("")]

    # 1) classify tour-only (far) legs by geometry. The daily seed is bound by
    #    BOTH the legal driving cap and the elapsed shift, whichever is tighter,
    #    so a job is tour-only when even the most generous vehicle cannot
    #    round-trip it within min(longest shift, driving cap). Gating on the shift
    #    alone let far work whose round trip busts the 10h driving cap but fits a
    #    long shift fall through both phases and strand as NO_FEASIBLE_ROUTE.
    shift_min = _max_shift_minutes(vehicles)
    tour_gate_min = min(shift_min, _DAY_DRIVE_CAP_MIN)
    tour_leg_ids: set[str] = set()
    for row in runnable.itertuples(index=False):
        leg_id = str(_g(row, "leg_id", ""))
        c = coords.get(leg_id)
        if c is None:
            continue
        # Never tour a pickup that feeds a delivery: the daily seed commits before
        # tours, so its (possibly daily) delivery must not run before the pickup.
        # Keeping these in the daily phase guarantees pickup-before-delivery.
        if str(_g(row, "dependency_type", "")) == "PRODUCES_DEPOT_FREIGHT":
            continue
        # Two-point legs (direct / hub-drop) carry depot->origin->dest->depot, so
        # classify on the full carry, not just the destination distance (B14).
        o_lat = o_lon = None
        if str(_g(row, "leg_kind", "")) in (DIRECT_CUSTOMER_MOVE, HUB_DROP):
            ol, oo = _g(row, "origin_lat", None), _g(row, "origin_lon", None)
            if ol is not None and oo is not None and pd.notna(ol) and pd.notna(oo):
                o_lat, o_lon = float(ol), float(oo)
        if is_tour_only(c[0], c[1], origin_lat=o_lat, origin_lon=o_lon,
                        pallets=float(_g(row, "pallets", 0.0) or 0.0),
                        depot=str(_g(row, "source_depot", "")), drive_cap_min=tour_gate_min):
            tour_leg_ids.add(leg_id)

    is_tour = candidates["leg_id"].astype(str).isin(tour_leg_ids)
    tour_candidates = candidates[is_tour]
    daily_candidates = candidates[~is_tour]

    # 2) build RouteJobs, bucket far jobs by nearest depot (across dates) so far
    #    work sitting at a depot on nearby dates can share one multi-day sweep
    job_meta: dict[str, dict] = {}
    due_by_job: dict[str, str] = {}
    ready_by_job: dict[str, str] = {}
    buckets: dict[str, list] = {}
    tour_rejected: list[RejectedJob] = []
    # freight fed by an in-window predecessor (crossdock pickup / inbound trunk)
    # reaches the staging depot on the predecessor's day -> the tour carrying it
    # can depart the NEXT morning at the earliest. Prestaged freight has no floor.
    svc_by_leg = {str(c.get("leg_id")): str(c.get("service_date", "") or "")
                  for c in candidates.to_dict("records")}
    for row in tour_candidates.itertuples(index=False):
        job_id = str(_g(row, "job_id", ""))
        rjob = make_route_job(row, coords)
        if rjob is None:
            tour_rejected.append(RejectedJob(job_id, "BAD_GEOCODE"))
            continue
        c = coords[str(_g(row, "leg_id", ""))]
        # FULL_FLEET is collected and staged at its origin depot and the vehicle
        # returns there, so anchor the tour at the COLLECTION depot (source_depot),
        # not the depot nearest the delivery. Other flows / OVERFLOW fall back to
        # the depot nearest the work point.
        src = str(_g(row, "source_depot", ""))
        depot = _anchor_or_nearest(src, c[0], c[1])
        pre_kind = rjob.leg_kind
        rjob = _as_depot_delivery(rjob, DEPOT_ANCHORS.get(depot), depot_direct_as_delivery)
        if pre_kind == DIRECT_CUSTOMER_MOVE and rjob.leg_kind == CUSTOMER_DELIVERY:
            # a depot-loaded direct became a delivery: its freight loads at the depot, so place
            # it AT_DEPOT in the ledger — else the delivery commit (exists_at_depot) rejects it
            # (it was registered AT_CUSTOMER_ORIGIN) and the order silently drops.
            fid = str(_g(row, "freight_id", "") or _g(row, "order_id", ""))
            ledger.register(fid, FREIGHT_AT_DEPOT, src)
        buckets.setdefault(depot, []).append(rjob)
        job_meta[job_id] = row._asdict()
        due_by_job[job_id] = str(_g(row, "service_date", "")) or start.isoformat()
        pred = str(_g(row, "predecessor_leg_id", "") or "")
        pred_day = svc_by_leg.get(pred, "")
        if pred_day:
            ready_by_job[job_id] = (date.fromisoformat(pred_day) + timedelta(days=1)).isoformat()
        # non-anticipation (user rule 2026-07-11): a tour cannot depart before its
        # freight was booked. Floor the readiness day to the order's creation day
        # (the dynamic pipeline stamps `creation_floor` on collection legs); the
        # later of the pred-based ready and the booking day wins, and _assign_one
        # starts the sweep no earlier. Static frames carry no creation_floor, so
        # their full-knowledge tours are unchanged.
        cf_day = str(_g(row, "creation_floor", "") or "")[:10]
        if cf_day:
            prev = str(ready_by_job.get(job_id, "") or "")
            ready_by_job[job_id] = max(prev, cf_day) if prev else cf_day

    # daily pre-pass: route the daily jobs with no tours so we can see which
    # vehicle-days are idle, and steer tours onto those (minimal displacement).
    # Deliberately NOT given avail_overrides: this is a tour-discovery estimate
    # that runs before tour reservation, so the T1 trunk draw (which needs
    # `reserved` to skip tour-committed vehicle-days) hasn't happened yet.
    prepass = run_route_seed_plan(daily_candidates, vehicles, compatibility, freight_states)
    busy_by_vd = {vd: len(jobs) for vd, jobs in prepass.route_jobs.items()}

    # build tours and reserve vehicles across their day spans. E6 rolling seeds
    # vehicle-days already frozen by committed trips/tours: they are reserved
    # before tour selection, so neither tours nor the daily seed can touch them.
    route_vehicles = {vid: _route_vehicle(vrows[vid], start.isoformat()) for vid in vrows}
    reserved: set[tuple[str, str]] = set(external_reserved or set())
    tour_assignments: list[TourAssignment] = []

    # Resolve a uniform list of (anchor_depot, jobs, evaluation). The default path
    # builds tours per source-depot bucket (unchanged). With consolidate_tours, pool
    # all far jobs and cluster once (emergent regions), then resolve each cluster —
    # depot-loadable multi-depot clusters become one tour with front load-stops.
    src_of = {jid: str(meta.get("source_depot", "")) for jid, meta in job_meta.items()}

    def _resolve(cluster_jobs):
        return resolve_cluster(cluster_jobs, lambda jid: src_of.get(jid, ""),
                               due_by_job, lambda d: _proto_vehicle(d, start.isoformat()),
                               cohesion_km=cohesion_km, ready_by_job=ready_by_job)

    if consolidate_tours:
        pooled = [rj for rjobs in buckets.values() for rj in rjobs]
        resolved: list[tuple] = []
        for tour in build_tours(pooled, _centroid_proto(start.isoformat()),
                                cohesion_km=cohesion_km, due_by_job=due_by_job,
                                ready_by_job=ready_by_job):
            resolved += _resolve(tour.jobs)
    else:
        resolved = []
        for depot, rjobs in buckets.items():
            for tour in build_tours(rjobs, _proto_vehicle(depot, start.isoformat()),
                                    cohesion_km=cohesion_km, due_by_job=due_by_job,
                                    ready_by_job=ready_by_job):
                resolved.append((depot, tour.jobs, tour.evaluation, tour.lead_days))

    # Salvage pass (consolidation mode only): cluster resolution can strand
    # single-job long sweeps (per-depot fallback around a far-origin DIRECT,
    # blocked accretion, an infeasible consolidation). Re-pool those degenerate
    # tours once so readiness-compatible neighbours merge (KA1-vs-ML6: 35 km
    # apart, same due day, two ~1,000 km sweeps). Two-point moves stay put — a
    # far-origin DIRECT poisons the re-pool (its backtrack pickup can make the whole
    # consolidation infeasible, fragmenting the deliveries that WOULD merge without it).
    # DIRECT consolidation happens in the MAIN pass (resolve_cluster's km-guard), not here.
    single_idx = [i for i, (_, jobs, ev, _lead) in enumerate(resolved)
                  if consolidate_tours
                  and ev.feasible
                  and len([j for j in jobs if j.leg_kind != DEPOT_LOAD]) == 1
                  and all(j.leg_kind not in (DIRECT_CUSTOMER_MOVE, HUB_DROP)
                          for j in jobs if j.leg_kind != DEPOT_LOAD)]
    if len(single_idx) >= 2:
        pool = [j for i in single_idx for j in resolved[i][1] if j.leg_kind != DEPOT_LOAD]
        keep = [t for i, t in enumerate(resolved) if i not in set(single_idx)]
        salvaged: list[tuple] = []
        for tour in build_tours(pool, _centroid_proto(start.isoformat()),
                                cohesion_km=cohesion_km, due_by_job=due_by_job,
                                ready_by_job=ready_by_job):
            salvaged += _resolve(tour.jobs)
        resolved = keep + salvaged

    # Tours and daily routes are separate construction components, but an option
    # set has one physical mode. Accepted tours claim that mode here; the same
    # tracker is then handed to the daily seed so same-group feeder legs remain
    # eligible and rival groups cannot be selected independently.
    tour_option_mutex = OptionMutex()

    def _option_candidate(job) -> dict:
        meta = job_meta.get(str(job.job_id), {})
        return {
            "option_set": str(meta.get("option_set", "") or ""),
            "option_group": str(meta.get("option_group", "") or ""),
        }

    def _assign_one(depot, jobs, evaluation, lead=0, *, extra_busy=frozenset()) -> bool:
        real_jobs = [j for j in jobs if j.leg_kind != DEPOT_LOAD]
        option_candidates = [_option_candidate(j) for j in real_jobs]
        proposed: dict[str, str] = {}
        option_conflict = False
        for cand in option_candidates:
            option_set = cand["option_set"]
            option_group = cand["option_group"]
            if not option_set or not option_group:
                continue
            if proposed.get(option_set, option_group) != option_group:
                option_conflict = True
                break
            proposed[option_set] = option_group
            if not tour_option_mutex.insertable(cand):
                option_conflict = True
                break
        if option_conflict:
            tour_rejected.extend(
                RejectedJob(j.job_id, "OPTION_SUPERSEDED") for j in real_jobs)
            return False
        if not evaluation.feasible:
            tour_rejected.extend(RejectedJob(j.job_id, "NO_FEASIBLE_TOUR") for j in real_jobs)
            return False
        # the sweep starts when the earliest stop in the batch is due — pulled
        # EARLIER by the evaluation's lead days (fix A, 2026-07-22: a LATE batch
        # departs early enough that its last due is met) — but never before every
        # member's freight has reached the depot (safety net for a singleton
        # whose feeding pickup lands after its own due date)
        day = min((due_by_job.get(j.job_id, start.isoformat()) for j in real_jobs),
                  default=start.isoformat())
        if lead:
            try:
                day = (date.fromisoformat(str(day)[:10]) - timedelta(days=int(lead))).isoformat()
            except ValueError:
                pass
        max_ready = max((ready_by_job.get(j.job_id, "") for j in real_jobs), default="")
        if max_ready and max_ready > day:
            day = max_ready
        # Rolling anchors may not start a tour in the PAST (same never-re-draw-
        # the-past rule as the trunk): a member due yesterday would otherwise
        # backdate day-1, fabricating driving the vehicle never did and
        # double-booking it against its real committed day (caught 2026-01-14
        # on run i5000: a Jan-16 seed emitted a "Jan-15 13:06" tour delivery).
        # The clamped tour serves late and the ledger accounts it as SLIPPED —
        # honest lateness, never fictitious punctuality. Static path
        # (trunk_from=None) is unchanged.
        if trunk_from is not None and day < str(trunk_from):
            day = str(trunk_from)
        span = _span(day, evaluation.days)
        _tour_dbg(f"assign: {[j.job_id for j in real_jobs]} depot={depot} start={day} "
                  f"days={evaluation.days} span={span}")
        free = [route_vehicles[vid] for vid in route_vehicles
                if all((vid, s) not in reserved and (vid, s) not in extra_busy for s in span)]
        # busyness across the span from the pre-pass -> prefer idle vehicles
        busyness = {v.vehicle_id: sum(busy_by_vd.get((v.vehicle_id, s), 0) for s in span)
                    for v in free}
        chosen = select_tour_vehicle(evaluation.peak_pallets, free,
                                     busyness=busyness, prefer_depot=depot,
                                     tour_kg=evaluation.peak_kg,
                                     tour_km=float(evaluation.total_km))
        if chosen is None:
            _tour_dbg(f"assign: NO VEHICLE free for span {span} -> release {[j.job_id for j in real_jobs]}")
            tour_rejected.extend(RejectedJob(j.job_id, "NO_FEASIBLE_TOUR") for j in real_jobs)
            return False
        if str(chosen.home_depot) != str(depot):
            # Cross-depot pick: the evaluation priced the tour from the depot
            # proto — emitting that verbatim teleports the truck. Materialize the
            # repositioning (re-evaluate from the vehicle's real home + DEPOT_LOAD
            # call at the anchor); if the true tour no longer fits or its span now
            # collides, fall back to a same-depot vehicle. A tour carrying any
            # CUSTOMER_PICKUP may NEVER go cross-depot: the freight's target IS
            # the anchor depot, but the vehicle returns to its own home — the
            # pickup would land at the wrong depot (WT254009 rule, 2026-07-16).
            # deadlines re-checked against the LONGER cross-depot route relative
            # to the tour's ACTUAL start day (fix C): the anchor-proto evaluation
            # said on-time, the real vehicle's deadhead may not be.
            def _offs_from_start(day_iso: str) -> dict:
                out = {}
                for j in real_jobs:
                    d = str(due_by_job.get(j.job_id, "") or "")[:10]
                    if not d:
                        continue
                    try:
                        out[j.job_id] = max(0, (date.fromisoformat(d)
                                                - date.fromisoformat(str(day_iso)[:10])).days)
                    except ValueError:
                        pass
                return out
            has_pickup = any(j.leg_kind == CUSTOMER_PICKUP for j in real_jobs)
            cross_vehicle = chosen
            cross = (None if has_pickup
                     else cross_depot_tour_eval(chosen, depot, jobs,
                                                due_offsets=_offs_from_start(day)))
            # decision-audit #10 (2026-07-26): the rigid/tractor type gate (Q4)
            # was decided on the PROTO (depot-anchored) km before we knew this
            # pick was cross-depot -- a light tour that looked short from the
            # staging depot can be genuine long-haul once the vehicle's real
            # deadhead is counted in, which is tractor work regardless of
            # pallet count (9-pal 240 km proto -> rigid; vehicle 180 km away ->
            # ~600 km realized). Treat a rigid pick whose REALIZED km busts the
            # rigid-suitable range the same as an infeasible cross-depot pick,
            # so a same-depot or better-typed alternative gets a fair chance.
            type_busted_km = None
            if (cross is not None and str(chosen.vehicle_type) == "rigid"
                    and float(cross[1].total_km) > float(TOUR_TRACTOR_KM)):
                type_busted_km = float(cross[1].total_km)
                cross = None
            span2 = _span(day, cross[1].days) if cross else []
            if cross and all((chosen.vehicle_id, s) not in reserved
                             and (chosen.vehicle_id, s) not in extra_busy for s in span2):
                jobs, evaluation, span = cross[0], cross[1], span2
            else:
                same = [v for v in free if str(v.home_depot) == str(depot)]
                chosen = select_tour_vehicle(evaluation.peak_pallets, same,
                                             busyness=busyness, prefer_depot=depot,
                                             tour_kg=evaluation.peak_kg,
                                             tour_km=float(evaluation.total_km))
                if chosen is None and type_busted_km is not None:
                    # No same-depot vehicle either, but the original pick was
                    # rejected for being the wrong TYPE, not infeasibility --
                    # give a differently-typed cross-depot alternative a fair
                    # chance, sized on the realized km, before falling back to
                    # the rejected pick as a coverage-first last resort below.
                    alt_pool = [v for v in free if v.vehicle_id != cross_vehicle.vehicle_id]
                    alt = select_tour_vehicle(evaluation.peak_pallets, alt_pool,
                                              busyness=busyness, prefer_depot=depot,
                                              tour_kg=evaluation.peak_kg,
                                              tour_km=type_busted_km)
                    if alt is not None:
                        alt_cross = cross_depot_tour_eval(alt, depot, jobs,
                                                          due_offsets=_offs_from_start(day))
                        span_alt = _span(day, alt_cross[1].days) if alt_cross else []
                        if alt_cross and all((alt.vehicle_id, s) not in reserved
                                             and (alt.vehicle_id, s) not in extra_busy
                                             for s in span_alt):
                            chosen, jobs, evaluation, span = alt, alt_cross[0], alt_cross[1], span_alt
                if chosen is None and not has_pickup:
                    # Last resort, coverage-first: no same-depot vehicle either —
                    # accept a due-missing cross-depot tour rather than dropping
                    # the orders, but LOUDLY, never silently (fix C, 2026-07-22;
                    # pre-fix this shipped as the invisible default).
                    late = cross_depot_tour_eval(cross_vehicle, depot, jobs)
                    span3 = _span(day, late[1].days) if late else []
                    if late and all((cross_vehicle.vehicle_id, s) not in reserved
                                    and (cross_vehicle.vehicle_id, s) not in extra_busy
                                    for s in span3):
                        _tour_dbg(f"assign: cross-depot re-eval MISSES a due date for "
                                  f"{[j.job_id for j in real_jobs]} — serving late (last resort)")
                        chosen = cross_vehicle
                        jobs, evaluation, span = late[0], late[1], span3
                if chosen is None:
                    _tour_dbg(f"assign: cross-depot fallback found NO same-depot vehicle "
                              f"-> release {[j.job_id for j in real_jobs]}")
                    tour_rejected.extend(RejectedJob(j.job_id, "NO_FEASIBLE_TOUR")
                                         for j in real_jobs)
                    return False
        _tour_dbg(f"assign: OK vehicle={chosen.vehicle_id} home={chosen.home_depot} span={span}")
        for cand in option_candidates:
            tour_option_mutex.assign(cand)
        for s in span:
            reserved.add((chosen.vehicle_id, s))
        tour_assignments.append(TourAssignment(chosen.vehicle_id, day, evaluation.days,
                                               jobs, evaluation, depot=depot))
        return True

    for depot, jobs, evaluation, lead in resolved:
        _assign_one(depot, jobs, evaluation, lead=lead)

    # 2.5) T1 fixed nightly B37 hub trunk (spec 2026-07-04): runs AFTER tour
    # reservation (so the draw can skip tour-reserved vehicle-days) and BEFORE
    # the daily seed (so the drawn tractors' next-day delay is in effect when
    # daily jobs are placed). Drawn night-days are NOT added to `reserved` --
    # the tractor still works day N normally; only its day-N+1 morning start is
    # pushed back via avail_overrides, threaded into the daily seed below.
    trunk = None
    if TRUNK_ENABLED:
        # window_end derived from the frame's max service_date (the orchestrator
        # receives no explicit end). Inert today: trunk_schedule ignores
        # window_end (kept for signature symmetry) — revisit if it ever gains an
        # upper-bound filter.
        window_end = start.isoformat()
        if len(candidates):
            svc_dates = candidates["service_date"].astype(str).str[:10]
            svc_dates = svc_dates[svc_dates != ""]
            if len(svc_dates):
                window_end = max(window_end, svc_dates.max())
        roundtrip_km = {
            (depot, "B37_HUB"): 2.0 * road_km(*DEPOT_ANCHORS[depot], *B37_LATLON)
            for depot in (*TRUNK_DEPOTS, *TRUNK_DAY_DEPOTS) if depot in DEPOT_ANCHORS
        }
        # LE10 hazchem trunk is CB22-only (trunk.LE10_FORCED_DEPOT) -- only that
        # one (depot, hub) key is ever looked up, but guard the anchor lookup
        # the same way as the B37 comprehension above.
        if "CB22" in DEPOT_ANCHORS:
            roundtrip_km[("CB22", "LE10_HUB")] = 2.0 * road_km(*DEPOT_ANCHORS["CB22"], *LE10_LATLON)
        # Sizing runs on the FULL candidate frame, not daily_candidates: far
        # EXPORT legs can be tour-classified yet still need their depot->hub trunk
        # leg counted — trunk demand does not depend on last-mile mode. Imports
        # never charge the trunk (export-only, 2026-07-24: the invisible hub
        # delivers them to the depot). trunk_schedule filters flow/leg_kind
        # /TRUNK_DEPOTS internally.
        nights = trunk_schedule(candidates, start.isoformat(), window_end, roundtrip_km)
        if trunk_from is not None:
            # E6 dynamic (structural review Fix 1): an epoch solve never
            # re-draws nights before its decision day. Past nights are facts
            # owned by the day-close authority; a re-imagined draw mints
            # phantom morning rests for days already committed and driven.
            nights = [n for n in nights if str(n.night) >= str(trunk_from)]
        trunk = draw_tractors(nights, vehicles, reserved)

    # 3) daily seed on the shared ledger, with reserved vehicle-days excluded.
    # E6 epoch overrides merge over the trunk's (epoch state is more informed:
    # a vehicle both trunk-delayed and freeze-carried keeps the freeze value).
    merged_overrides = dict(trunk.avail_overrides) if trunk is not None else {}
    merged_overrides.update(extra_avail_overrides or {})
    daily_result = run_route_seed_plan(
        daily_candidates, vehicles, compatibility, freight_states,
        ledger=ledger, excluded_vehicle_days=reserved,
        avail_overrides=(merged_overrides or None),
        option_mutex=tour_option_mutex,
    )

    # 4) commit tours against the same (now collection-aware) ledger
    builder = SelectedPlanBuilder(plan_id=plan_id)
    for ta in tour_assignments:
        home = str(_g(vrows[ta.vehicle_id], "home_depot", "")) if ta.vehicle_id in vrows else ""
        events = tour_emission_events(ta.evaluation, ta.start_date)
        event_by_job = {str(e["job_id"]): e for e in events if e["kind"] == "job"}
        for _idx, rjob in enumerate(ta.jobs, start=1):
            event = event_by_job.get(str(rjob.job_id))
            if event is None:
                raise ValueError(f"tour evaluator omitted job {rjob.job_id}")
            stop = event["stop"]
            sequence = int(event["sequence"])
            if rjob.leg_kind == DEPOT_LOAD:
                # A cross-depot load-stop: the vehicle calls at another depot to load its
                # staged freight. Emit it as a visible stop (its hop km, at the depot) but
                # not a ledger leg (no order). Unique job_id so two tours can load at one depot.
                day_iso = (date.fromisoformat(ta.start_date)
                           + timedelta(days=stop.day_index)).isoformat()
                builder.assign(
                    route_id=f"TOUR:{ta.vehicle_id}:{ta.start_date}",
                    vehicle_id=ta.vehicle_id, vehicle_home_depot=home, sequence=sequence,
                    job={"job_id": f"{rjob.job_id}:{ta.vehicle_id}:{ta.start_date}",
                         "service_date": day_iso, "leg_id": "", "order_id": "",
                         "leg_kind": DEPOT_LOAD,
                         "preferred_start_node": rjob.node, "preferred_end_node": rjob.node},
                    assignment_reason="DEPOT_LOAD",
                    planned_arrive=_tour_clock(day_iso, stop.arrive_minute),
                    planned_depart=_tour_clock(day_iso, stop.depart_minute),
                    planned_km=stop.leg_km,
                    planned_drive_minutes=stop.leg_minutes,
                    load_pallets_after=stop.load_pallets_after,
                    load_kg_after=stop.load_kg_after,
                    freight_state_after="AT_DEPOT",
                    break_minutes_before=stop.break_minutes_before,
                )
                continue
            cand = job_meta.get(rjob.job_id)
            if cand is None:
                continue
            order_id = str(cand.get("order_id", ""))
            freight_id = str(cand.get("freight_id", "") or order_id)
            src, tgt = str(cand.get("source_depot", "")), str(cand.get("target_depot", ""))
            ok, reason = _commit_leg(ledger, rjob.leg_kind, freight_id, src, tgt)
            if not ok:
                tour_rejected.append(RejectedJob(rjob.job_id, reason))
                continue
            day_iso = (date.fromisoformat(ta.start_date)
                       + timedelta(days=stop.day_index)).isoformat()
            arrive = _tour_clock(day_iso, stop.arrive_minute)
            depart = _tour_clock(day_iso, stop.depart_minute)
            label = "SAME_DEPOT_TOUR" if assignment_kind(home, src) == "SAME" else "CROSS_DEPOT_TOUR"
            builder.assign(
                route_id=f"TOUR:{ta.vehicle_id}:{ta.start_date}",
                vehicle_id=ta.vehicle_id, vehicle_home_depot=home, sequence=sequence,
                # a reclassified depot-loaded direct EMITS as the delivery it now is (rjob.leg_kind),
                # so the plan/board/map read it as a depot delivery rather than a direct carry.
                job={**cand, "service_date": day_iso, "leg_kind": rjob.leg_kind},
                assignment_reason=label,
                planned_arrive=arrive, planned_depart=depart,
                planned_km=stop.leg_km,
                planned_drive_minutes=stop.leg_minutes,
                load_pallets_after=stop.load_pallets_after,
                load_kg_after=stop.load_kg_after,
                freight_state_after=_FREIGHT_STATE_AFTER.get(rjob.leg_kind, "DELIVERED"),
                break_minutes_before=stop.break_minutes_before,
            )
        for event in (e for e in events if e["kind"] != "job"):
            stop = event["stop"]
            kind = str(event["kind"])
            day_iso = str(event["service_date"])
            node = str(event["node"])
            job_id = f"{event['job_id']}:{ta.vehicle_id}:{ta.start_date}"
            leg_kind = "DEPOT_RETURN" if kind == "depot_return" else TOUR_OVERNIGHT
            direct_job_id = (
                str(event["job_id"]).split("__DIRECT_COLLECT__:", 1)[1]
                if kind == "direct_overnight"
                and "__DIRECT_COLLECT__:" in str(event["job_id"])
                else ""
            )
            direct_cand = job_meta.get(direct_job_id, {})
            arrive = (_tour_clock(day_iso, stop.arrive_minute) if stop is not None
                      else _tour_clock(day_iso, 0.0))
            depart = (_tour_clock(day_iso, stop.depart_minute) if stop is not None else arrive)
            builder.assign(
                route_id=f"TOUR:{ta.vehicle_id}:{ta.start_date}",
                vehicle_id=ta.vehicle_id, vehicle_home_depot=home,
                sequence=int(event["sequence"]),
                job={"job_id": job_id, "service_date": day_iso,
                     "leg_id": str(direct_cand.get("leg_id", "")),
                     "order_id": str(direct_cand.get("order_id", "")),
                     "leg_kind": leg_kind, "preferred_start_node": node,
                     "preferred_end_node": "DEPOT" if kind == "depot_return" else node},
                assignment_reason="TOUR_RETURN" if kind == "depot_return" else "TOUR_OVERNIGHT",
                planned_arrive=arrive, planned_depart=depart,
                planned_km=float(stop.leg_km) if stop is not None else 0.0,
                planned_drive_minutes=float(stop.leg_minutes) if stop is not None else 0.0,
                load_pallets_after=float(event["pallets"]), load_kg_after=float(event["kg"]),
                freight_state_after="RETURNED" if kind == "depot_return" else "ABOARD",
                break_minutes_before=float(stop.break_minutes_before) if stop is not None else 0.0,
            )

    selected = list(daily_result.selected) + builder.records
    rejected = list(daily_result.rejected) + tour_rejected
    return MultidaySeedResult(
        selected, rejected, ledger,
        daily_result.routes, daily_result.route_jobs, tour_assignments,
        daily=daily_result, reserved=reserved, tour_records=list(builder.records),
        trunk=trunk,
    )
