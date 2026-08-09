"""Milestone 4 integration: greedy multi-stop constructive seed.

Wires the route evaluator into route construction so coverage becomes real. Each
vehicle builds an actual per-day route via best-position insertion; cumulative
load, time windows, and the shift bound now genuinely constrain what fits. The
freight ledger still gates delivery-before-pickup across days.

This supersedes the trivial `seed_planner.py` (one-job-per-vehicle, artificial
100%) and is the constructive starting point ALNS will improve in Milestone 5.

Per-day, single-day routes: a vehicle starts each day at its home depot. Multiday
spans / overnight positioning are Milestone 8.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time

import pandas as pd

from freight_planner.catchment import job_distance_km
from freight_planner import config as _fp_cfg
from freight_planner.config import SHUTTLE_ENABLED, SHUTTLE_MIN_FILL
from freight_planner.cross_depot import assignment_kind
from freight_planner.freight_ledger import (
    FREIGHT_AT_CUSTOMER_ORIGIN, FREIGHT_DELIVERED, FREIGHT_ON_VEHICLE, FreightLedger,
)
from freight_planner.option_mutex import OptionMutex
from freight_planner.plan_records import build_plan_records
from freight_planner.plan_schema import SelectedPlanRecord
from freight_planner.planner_state import RejectedJob
from freight_planner.routing_adapter import (
    RouteJob, RouteVehicle, apply_avail_override, evaluate_day, try_insert_job,
)
from freight_planner.shuttle import _EPS as _SHUTTLE_EPS, detect_shuttle_bins
from freight_planner.vehicle_cost import out_of_area_penalty_km

CUSTOMER_PICKUP = "CUSTOMER_PICKUP"
CUSTOMER_DELIVERY = "CUSTOMER_DELIVERY"
DIRECT_CUSTOMER_MOVE = "DIRECT_CUSTOMER_MOVE"
HUB_DROP = "HUB_DROP"

_DEP_RANK = {
    "PRODUCES_DEPOT_FREIGHT": 0,
    "NONE_DIRECT": 1,
    "PICKUP_TERMINAL": 2,
    "PRESTAGED_DELIVERY": 3,
    "REQUIRES_PRIOR_PICKUP": 4,
}


@dataclass
class RouteSeedResult:
    selected: list[SelectedPlanRecord]
    rejected: list[RejectedJob]
    ledger: FreightLedger
    routes: dict  # (vehicle_id, day) -> DayEvaluation
    route_trips: dict  # (vehicle_id, day) -> list[list[RouteJob]]
    route_jobs: dict  # (vehicle_id, day) -> list[RouteJob] flattened committed trips
    shuttle_job_ids: set = field(default_factory=set)
    shuttle_stats: dict = field(default_factory=dict)


def _g(row, key, default=""):
    value = getattr(row, key, default)
    return default if value is None else value


class _FloorOverride:
    """Proxies every attribute of ``row`` unchanged except ``earliest_start``
    (raised to ``floor`` if the row's own is earlier) and ``creation_floor``
    (forced truthy) -- lets ``make_route_job`` refuse an early arrival for a
    row that has no ``creation_floor`` column of its own."""
    __slots__ = ("_row", "_floor")

    def __init__(self, row, floor: str):
        self._row = row
        self._floor = floor

    def __getattr__(self, name):
        if name == "creation_floor":
            return self._floor
        if name == "earliest_start":
            existing = str(getattr(self._row, "earliest_start", "") or "")
            return max(existing, self._floor) if existing else self._floor
        return getattr(self._row, name)


def _coord_or_none(value):
    if value is None or value == "":
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _slip_rank(value) -> float:
    """E6 aging weight; absent/NaN -> 0.0 so legacy frames sort unchanged."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f else f


def _priority_key(job) -> tuple:
    return (
        # E6 aging: slipped orders outrank fresh work (0.0 when absent -> unchanged)
        -_slip_rank(_g(job, "slip_priority", 0.0)),
        str(_g(job, "service_date", "")),
        _DEP_RANK.get(str(_g(job, "dependency_type", "")), 5),
        str(_g(job, "latest_finish", "") or "~"),
        str(_g(job, "job_id", "")),
    )


def _job_coords(compatibility: pd.DataFrame) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    if compatibility is None or compatibility.empty:
        return out
    ok = compatibility[compatibility["compatibility_status"].astype(str).eq("OK")]
    for row in ok.itertuples(index=False):
        leg = str(_g(row, "leg_id", ""))
        if leg in out:
            continue
        lat, lon = getattr(row, "service_lat", None), getattr(row, "service_lon", None)
        if pd.notna(lat) and pd.notna(lon):
            out[leg] = (float(lat), float(lon))
    return out


def _ok_options(compatibility: pd.DataFrame) -> dict[str, list[tuple[str, bool]]]:
    out: dict[str, list[tuple[str, bool]]] = {}
    if compatibility is None or compatibility.empty:
        return out
    ok = compatibility[compatibility["compatibility_status"].astype(str).eq("OK")]
    for row in ok.itertuples(index=False):
        leg = str(_g(row, "leg_id", ""))
        out.setdefault(leg, []).append((str(_g(row, "vehicle_id", "")), bool(_g(row, "same_depot", False))))
    return out


def _time_of(ts, fallback: time) -> time:
    if not ts:
        return fallback
    try:
        return datetime.fromisoformat(str(ts)).time()
    except ValueError:
        return fallback


def _route_vehicle(vrow, day: str) -> RouteVehicle:
    start_t = _time_of(_g(vrow, "available_from", ""), time(int(_fp_cfg.FLEET_DAY_START_HOUR), 0))
    d = date.fromisoformat(day) if day else date(2026, 1, 1)
    lat = float(_g(vrow, "current_lat", 0.0) or 0.0)
    lon = float(_g(vrow, "current_lon", 0.0) or 0.0)
    depot = str(_g(vrow, "home_depot", ""))
    # A blank shift_end means NO wall (user rule 2026-07-16: duty/driving caps
    # bind, 19:00 is soft) — never invent one. A set value keeps its wall.
    se = str(_g(vrow, "shift_end", "") or "")
    return RouteVehicle(
        vehicle_id=str(_g(vrow, "vehicle_id", "")),
        start_node=depot,
        start_lat=lat, start_lon=lon,
        start_time=datetime.combine(d, start_t).isoformat(sep=" "),
        capacity_pallets=float(_g(vrow, "capacity_pallets", 0.0) or 0.0),
        capacity_kg=float(_g(vrow, "capacity_kg", 0.0) or 0.0),
        vehicle_type=str(_g(vrow, "vehicle_type", "")),
        home_depot=depot, home_lat=lat, home_lon=lon,
        return_to_depot=True,
        shift_end=datetime.combine(d, _time_of(se, time(18, 0))).isoformat(sep=" ") if se else "",
    )


def _reorder(jobs: list[RouteJob], evaluation) -> list[RouteJob]:
    by_id = {j.job_id: j for j in jobs}
    return [by_id[s.job_id] for s in evaluation.stops]


def make_route_job(job, coords: dict[str, tuple[float, float]]) -> RouteJob | None:
    """Build a RouteJob from a candidate row, or None if the leg has no coords."""
    leg_id = str(_g(job, "leg_id", ""))
    if leg_id not in coords:
        return None
    lat, lon = coords[leg_id]
    o_lat = _g(job, "origin_lat", None)
    o_lon = _g(job, "origin_lon", None)
    return RouteJob(
        job_id=str(_g(job, "job_id", "")),
        leg_kind=str(_g(job, "leg_kind", "")),
        node=str(_g(job, "service_pc", "")),
        lat=lat, lon=lon,
        pallets=float(_g(job, "pallets", 0.0) or 0.0),
        kg=float(_g(job, "weight_kg", 0.0) or 0.0),
        earliest_start=str(_g(job, "earliest_start", "")),
        latest_finish=str(_g(job, "latest_finish", "")),
        origin_lat=_coord_or_none(o_lat),
        origin_lon=_coord_or_none(o_lon),
        order_id=str(_g(job, "freight_id", _g(job, "order_id", "")) or _g(job, "order_id", "")),
        # non-anticipation: a creation-floored collection (dynamic pipeline stamps
        # `creation_floor`) may not be ARRIVED at before its booking. Absent column
        # (static planner) -> "" -> False, so full-knowledge baselines are unchanged.
        no_early_arrival=bool(str(_g(job, "creation_floor", "") or "").strip()),
        # collocated depot-delivery (2026-07-17): readiness floor + home-depot bound
        # ride the job so every re-evaluation re-derives the same hold/gate
        depart_floor=str(_g(job, "depart_floor", "") or ""),
        depot_bound=str(_g(job, "depot_bound", "") or ""),
        # Soft delivery window (2026-07-18): the tight customer window rides the job
        # for the earliness/tardiness penalty — delivery legs only, so a late pickup
        # keeps its hard window.
        window_open=(str(_g(job, "raw_window_start", "") or "")
                     if str(_g(job, "leg_kind", "")) == "CUSTOMER_DELIVERY" else ""),
        deadline=(str(_g(job, "raw_window_end", "") or "")
                  if str(_g(job, "leg_kind", "")) == "CUSTOMER_DELIVERY" else ""),
    )


def rebuild_daily_routes_after_drop(
    records: list,
    affected_route_ids: set,
    candidate_by_leg: dict,
    coords: dict,
    vehicle_row_by_id: dict,
    avail_overrides: dict | None = None,
    job_floors: dict[str, str] | None = None,
) -> tuple[list, set]:
    """Re-time a daily (non-tour) route after a commit-boundary drop removed one
    of its legs.

    ``avail_overrides`` (keyed ``(vehicle_id, day)``, same shape as everywhere
    else -- ``alns._rv_ov``, ``run_rolling.stop_timings``) must carry the SAME
    dispatch-floor/build-context override this vehicle-day's stops were
    ORIGINALLY accepted under. Without it, the bare vehicle profile is used and
    the shortened route re-times from the vehicle's raw availability, not from
    whenever it was actually dispatched.

    That alone is NOT enough (found 2026-07-28, WT262812/802/818 on
    ROUTE:Y90RNW/2026-02-02): a vehicle-day's own avail_override only reflects
    when IT first launched, but an individual stop inserted much later can be
    protected by a LATER per-job dispatch floor (the E6 watermark's own
    ``floor_ok`` check at insertion time) that no vehicle-level override
    captures. ``job_floors`` (leg_id -> ISO floor, e.g. from ``placement``'s
    per-job epoch/floor trace) closes that gap: a leg with an entry gets
    wrapped so ``make_route_job`` sees a ``creation_floor``, and the EXISTING
    no_early_arrival machinery (built for booking-time non-anticipation) makes
    the evaluator WAIT for that floor instead of arriving early -- the same
    behaviour it already gives a booking-time-floored stop -- rather than
    silently backdating it.

    ``drop_orphan_deliveries`` / ``drop_superseded_option_legs`` /
    ``drop_freightless_tours`` filter the final selected records by leg_id, but a
    dropped leg's drive minutes and any statutory break riding on it do not
    disappear with it — they belong to whichever leg now follows. Left as a bare
    filter, the surviving legs keep the STALE arrival/departure/break/drive
    values an evaluator computed against a route that no longer exists (audit
    follow-up 2026-07-27: R888GNW/2026-02-02 drove 297 min in one day with zero
    break recorded, and downstream stops carried an hours-long phantom gap).
    Tours already get this treatment via ``rebuild_pruned_tour_records``; daily
    routes did not.

    A no-op for any route_id not in ``affected_route_ids`` — the overwhelming
    majority of routes in a run are untouched by any drop, so paying
    ``evaluate_day`` again for them is wasted work and needless risk of drift.

    Returns ``(records, failed_route_ids)``. Shortening a route can shift every
    downstream arrival earlier, which can in turn make a still-no_early_arrival
    -gated stop (a live micro-insertion's booking floor) arrive before it was
    ever real — the evaluator correctly refuses that as infeasible. Crashing the
    whole run over one such route is worse than keeping its pre-drop values
    (already-shipped behaviour, no worse than before this fix), so a route that
    can't be cleanly re-timed falls back unchanged and is reported in
    ``failed_route_ids`` for the caller to log.
    """
    if not affected_route_ids:
        return list(records), set()
    by_route: dict[str, list] = {}
    order: list[str] = []
    for r in records:
        rid = str(getattr(r, "route_id", ""))
        if rid not in by_route:
            by_route[rid] = []
            order.append(rid)
        by_route[rid].append(r)

    out: list = []
    failed: set = set()
    for rid in order:
        group = by_route[rid]
        if rid.startswith("TOUR:") or rid not in affected_route_ids:
            out.extend(group)
            continue
        vid = str(group[0].vehicle_id)
        day = str(group[0].service_date)
        vrow = vehicle_row_by_id.get(vid)
        if vrow is None:
            out.extend(group)
            continue
        by_trip: dict[int, list] = {}
        for r in group:
            by_trip.setdefault(int(r.trip_index), []).append(r)
        trips: list[list[RouteJob]] = []
        rec_by_job: dict[str, object] = {}
        rebuildable = True
        for ti in sorted(by_trip):
            trip_jobs: list[RouteJob] = []
            for r in sorted(by_trip[ti], key=lambda r: int(r.sequence)):
                cand = candidate_by_leg.get(str(r.leg_id))
                _floor = (job_floors or {}).get(str(r.leg_id))
                if cand is not None and _floor:
                    cand = _FloorOverride(cand, _floor)
                rjob = make_route_job(cand, coords) if cand is not None else None
                if rjob is None:
                    rebuildable = False
                    break
                trip_jobs.append(rjob)
                rec_by_job[rjob.job_id] = r
            if not rebuildable:
                break
            trips.append(trip_jobs)
        if not rebuildable or not trips:
            out.extend(group)
            continue
        vehicle = apply_avail_override(_route_vehicle(vrow, day),
                                       (avail_overrides or {}).get((vid, day)), day)
        day_eval = evaluate_day(vehicle, trips)
        if not day_eval.feasible:
            failed.add(rid)
            out.extend(group)
            continue
        for trip_eval in day_eval.trip_evaluations:
            for position, stop in enumerate(trip_eval.stops, start=1):
                r = rec_by_job.get(stop.job_id)
                if r is None:
                    continue
                out.append(replace(
                    r, sequence=position,
                    planned_arrive=stop.arrive, planned_depart=stop.depart,
                    planned_km=stop.leg_km, planned_drive_minutes=stop.drive_minutes,
                    break_minutes_before=stop.break_minutes_before,
                    load_pallets_after=stop.load_pallets_after,
                    load_kg_after=stop.load_kg_after,
                ))
    return out, failed


def same_order_handoff_conflict(seq: list[RouteJob], candidate: RouteJob) -> bool:
    """True when one continuous route would skip a required depot handoff.

    A crossdock pickup creates freight at the depot; a paired depot delivery must
    depart from the depot after that handoff. Until routes can include an
    explicit depot-intermediate stop, keep those two legs on separate routes.
    """
    oid = str(getattr(candidate, "order_id", "") or "")
    if not oid or candidate.leg_kind not in (CUSTOMER_PICKUP, CUSTOMER_DELIVERY):
        return False
    counterpart = CUSTOMER_DELIVERY if candidate.leg_kind == CUSTOMER_PICKUP else CUSTOMER_PICKUP
    return any(str(getattr(job, "order_id", "") or "") == oid and job.leg_kind == counterpart
               for job in seq)


def run_route_seed_plan(
    candidates: pd.DataFrame,
    vehicles: pd.DataFrame,
    compatibility: pd.DataFrame,
    freight_states: pd.DataFrame,
    plan_id: str = "ROUTESEED",
    ledger: FreightLedger | None = None,
    excluded_vehicle_days: set[tuple[str, str]] | None = None,
    avail_overrides: dict[tuple[str, str], str] | None = None,
    option_mutex: OptionMutex | None = None,
) -> RouteSeedResult:
    # A shared ledger lets the multiday orchestrator run the daily seed first so
    # collections are recorded before tour deliveries are committed against it.
    if ledger is None:
        ledger = FreightLedger.from_initial_states(freight_states)
    excluded = excluded_vehicle_days or set()
    overrides = avail_overrides or {}
    rejected: list[RejectedJob] = []
    # (vehicle, day) -> (trips, day_evaluation). Each trip is one depot loop.
    routes: dict[tuple[str, str], tuple[list[list[RouteJob]], object]] = {}

    if candidates is None or candidates.empty:
        return RouteSeedResult([], rejected, ledger, {}, {}, {}, set(), {})

    vrows = {str(_g(r, "vehicle_id", "")): r for r in vehicles.itertuples(index=False)}
    coords = _job_coords(compatibility)
    options = _ok_options(compatibility)
    runnable = candidates[candidates.get("hard_blocker", pd.Series(dtype=str)).fillna("").eq("")]
    ordered = sorted(runnable.itertuples(index=False), key=_priority_key)

    rv_cache: dict[tuple[str, str], RouteVehicle] = {}

    def _rv(vid, day):
        cached = rv_cache.get((vid, day))
        if cached is None:
            # Override values: "HH:MM" (T1 trunk next-day delay) or a
            # routing_adapter.DutyOverride (E6 rolling: post-freeze start plus
            # duty carry). Shared application in apply_avail_override.
            cached = apply_avail_override(
                _route_vehicle(vrows[vid], day), overrides.get((vid, day)), day)
            rv_cache[(vid, day)] = cached
        return cached

    def _flatten(trips: list[list[RouteJob]]) -> list[RouteJob]:
        return [job for trip in trips for job in trip]

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
            # freight-readiness gate: deliveries must already sit at the depot,
            # pickups must not be already-DELIVERED on the shared ledger (the
            # daily seed runs after tour commits; pickup_to_depot would raise).
            ready = []
            for row in rows:
                fid = str(_g(row, "freight_id", _g(row, "order_id", "")) or "")
                if sbin.leg_kind == CUSTOMER_DELIVERY and not ledger.exists_at_depot(
                        fid, sbin.anchor_depot):
                    continue
                if sbin.leg_kind == CUSTOMER_PICKUP and ledger.state_of(fid) == FREIGHT_DELIVERED:
                    continue
                ready.append(row)
            load = sum(float(_g(r, "pallets", 0.0) or 0.0) for r in ready)
            if load + _SHUTTLE_EPS < SHUTTLE_MIN_FILL * sbin.bin_capacity:
                continue  # re-gate after readiness drop (epsilon matches shuttle.py)
            rjobs = [make_route_job(r, coords) for r in ready]
            if any(rj is None for rj in rjobs):
                continue
            for vid in sbin.eligible_vehicles:
                if vid not in vrows or (vid, day) in excluded:
                    continue
                veh = _rv(vid, day)
                old_trips, _old_eval = routes.get((vid, day), ([], None))
                candidate_trips = [list(t) for t in old_trips] + [list(rjobs)]
                # _trip_cap is deliberately NOT consulted here: a dedicated shuttle
                # legitimately runs multiple round trips a day (the observed CB9/ST4
                # reality); evaluate_day's duty/driving caps are the honest limit.
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
                break
            # not committed -> bin dissolves: jobs simply stay in `ordered`
        if shuttle_job_ids:
            ordered = [j for j in ordered
                       if str(_g(j, "job_id", "")) not in shuttle_job_ids]
        shuttle_stats = {"trips": n_trips, "jobs": n_jobs, "pallets": pallets,
                        "address_days": len(per_address),
                        "top": sorted(per_address.items(),
                                      key=lambda kv: -kv[1])[:5]}

    last_insert_failure = "NO_FEASIBLE_ROUTE"

    def best_insertion(rjob, vehicle_ids, day):
        nonlocal last_insert_failure
        best = None
        for vid in vehicle_ids:
            if vid not in vrows or (vid, day) in excluded:
                continue  # vehicle reserved for a multiday tour on this day
            veh = _rv(vid, day)
            # Out-of-area phantom km (B15). The seed ranks on raw km, not GBP
            # like the ALNS: it compares same-day candidates whose physical km
            # barely differ, so inflating only the out-of-area vehicle's delta
            # is all the steering needed — no fuel-rate conversion required.
            pen_km = out_of_area_penalty_km(
                job_distance_km(veh.home_lat, veh.home_lon, rjob),
                float(_g(vrows[vid], "catchment_km", 0.0) or 0.0))
            old_trips, old_eval = routes.get((vid, day), ([], None))
            base_km = old_eval.total_km if old_eval is not None else 0.0

            # First try every existing depot loop. Same-freight pickup/delivery
            # cannot be on the same trip, but can be on a later trip.
            for idx, trip in enumerate(old_trips):
                if same_order_handoff_conflict(trip, rjob):
                    continue
                trip_ev = try_insert_job(veh, trip, rjob, "best")
                if not trip_ev.feasible:
                    last_insert_failure = trip_ev.failure_reason or "NO_FEASIBLE_ROUTE"
                    continue
                candidate_trips = [list(t) for t in old_trips]
                candidate_trips[idx] = _reorder(trip + [rjob], trip_ev)
                day_ev = evaluate_day(veh, candidate_trips)
                if not day_ev.feasible:
                    last_insert_failure = day_ev.failure_reason or "NO_FEASIBLE_ROUTE"
                    continue
                delta = day_ev.total_km - base_km + pen_km
                if best is None or delta < best[3]:
                    best = (vid, candidate_trips, day_ev, delta)

            # Then try opening a new trip from the depot if the vehicle-day has
            # enough time/drive budget left. No trip-count cap (user rule
            # 2026-07-16): duty/driving feasibility is the honest limit.
            candidate_trips = [list(t) for t in old_trips] + [[rjob]]
            day_ev = evaluate_day(veh, candidate_trips)
            if not day_ev.feasible:
                last_insert_failure = day_ev.failure_reason or "NO_FEASIBLE_ROUTE"
            if day_ev.feasible:
                delta = day_ev.total_km - base_km + pen_km
                # Opening an extra loop on an already-used vehicle is a
                # fallback, not a reason to strand work that could use an
                # idle vehicle-day. Keep coverage/resource headroom ahead of
                # marginal-km compression in the greedy seed.
                score = delta + (10000.0 if old_trips else 0.0)
                if best is None or score < best[3]:
                    best = (vid, candidate_trips, day_ev, score)
        return best

    # DIRECT/XDOCK mutual exclusion (2026-07-23): once one group's leg for an
    # option set commits, the rival group is superseded. Required because the
    # freight ledger alone does not stop the DIRECT leg delivering a unit an XDOCK
    # pickup has just staged AT_DEPOT (which raises FreightUnavailableError).
    mutex = option_mutex if option_mutex is not None else OptionMutex()

    for job in ordered:
        leg_id = str(_g(job, "leg_id", ""))
        job_id = str(_g(job, "job_id", ""))
        order_id = str(_g(job, "order_id", ""))
        freight_id = str(_g(job, "freight_id", order_id) or order_id)
        leg_kind = str(_g(job, "leg_kind", ""))
        day = str(_g(job, "service_date", ""))

        _ocand = {"option_set": str(_g(job, "option_set", "") or ""),
                  "option_group": str(_g(job, "option_group", "") or "")}
        if not mutex.insertable(_ocand):
            rejected.append(RejectedJob(job_id, "OPTION_SUPERSEDED"))
            continue
        source_depot = str(_g(job, "source_depot", ""))
        target_depot = str(_g(job, "target_depot", ""))

        # 1) freight readiness (vehicle-independent)
        if leg_kind == CUSTOMER_DELIVERY:
            if not ledger.exists_at_depot(freight_id, source_depot):
                rejected.append(RejectedJob(job_id, "DELIVERY_BEFORE_PICKUP"))
                continue
        elif leg_kind == DIRECT_CUSTOMER_MOVE:
            unit = ledger.get(freight_id)
            # A direct move consumes freight AT its origin. If the freight is not
            # there — already delivered, or staged AT_DEPOT because its XDOCK pickup
            # ran (this pass or a prior epoch) — the DIRECT option is superseded.
            # Reject cleanly rather than let ledger.deliver_direct raise (with both
            # option groups now flowing, this leg can legitimately be un-runnable).
            if unit is None or unit.state not in (FREIGHT_AT_CUSTOMER_ORIGIN, FREIGHT_ON_VEHICLE):
                reason = "OPTION_SUPERSEDED" if unit is not None else "DELIVERY_BEFORE_PICKUP"
                rejected.append(RejectedJob(job_id, reason))
                continue
        elif leg_kind in (CUSTOMER_PICKUP, HUB_DROP):
            if ledger.state_of(freight_id) == FREIGHT_DELIVERED:
                rejected.append(RejectedJob(job_id, "FREIGHT_ALREADY_DELIVERED"))
                continue

        rjob = make_route_job(job, coords)
        if rjob is None:
            rejected.append(RejectedJob(job_id, "BAD_GEOCODE"))
            continue

        opts = options.get(leg_id, [])
        if not opts:
            rejected.append(RejectedJob(job_id, "NO_OK_VEHICLE_PAIR"))
            continue
        same = [vid for vid, s in opts if s]
        cross = [vid for vid, s in opts if not s]

        last_insert_failure = "NO_FEASIBLE_ROUTE"
        chosen = best_insertion(rjob, same, day)
        if chosen is None:
            chosen = best_insertion(rjob, cross, day)
        if chosen is None:
            rejected.append(RejectedJob(job_id, last_insert_failure or "NO_FEASIBLE_ROUTE"))
            continue

        vid, new_trips, ev, _delta = chosen
        if leg_kind == CUSTOMER_PICKUP:
            ledger.pickup_to_depot(freight_id, target_depot)
        elif leg_kind == CUSTOMER_DELIVERY:
            ledger.deliver_from_depot(freight_id, source_depot)
        elif leg_kind == HUB_DROP:
            ledger.handoff_to_hub(freight_id)
        else:
            ledger.deliver_direct(freight_id)

        routes[(vid, day)] = (new_trips, ev)
        mutex.assign(_ocand)   # this group now owns the option set

    # Build selected-plan rows once, from the FINAL route order, not incrementally
    # during insertion (best-position insertion reorders earlier stops).
    route_trips = {k: v[0] for k, v in routes.items()}
    route_jobs = {k: _flatten(v[0]) for k, v in routes.items()}
    candidate_by_job = {str(r.get("job_id")): r for r in candidates.to_dict("records")}

    def _home(vehicle_id):
        return str(_g(vrows[vehicle_id], "home_depot", "")) if vehicle_id in vrows else ""

    def _reason(candidate, home):
        src = str(candidate.get("source_depot", "") if isinstance(candidate, dict)
                  else getattr(candidate, "source_depot", ""))
        return "SAME_DEPOT_SEED" if assignment_kind(home, src) == "SAME" else "CROSS_DEPOT_SEED"

    records = build_plan_records(route_trips, candidate_by_job, _rv, _home, _reason, plan_id)

    return RouteSeedResult(
        records, rejected, ledger,
        {k: v[1] for k, v in routes.items()},
        route_trips,
        route_jobs,
        shuttle_job_ids,
        shuttle_stats,
    )


