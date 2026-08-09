"""Intraday tour attachment — free-ride a late-booked far order onto an in-flight
tour's mutable tail (spec 2026-07-12-multiday-tour-insertion).

Pure matching layer: ``run_rolling`` supplies the in-flight tours and the unassigned
candidates, and applies the returned attachments (splice the tail, mark served,
notify the driver). The head/tail split reuses ``epoch_state.committed_stop_count``,
so the committed prefix always includes the stop the driver is currently rolling
toward — an attachment can never trigger a mid-leg detour.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from freight_planner.config import TOUR_DAY_START_HOUR
from freight_planner.epoch_state import committed_stop_count
from freight_planner.plan_schema import SelectedPlanRecord
from freight_planner.routing_adapter import RouteJob, RouteVehicle
from freight_planner.tours import (
    TOUR_OVERNIGHT,
    TourEvaluation,
    best_tour_attachment,
    evaluate_tour,
    tour_emission_events,
    tour_tail_from,
)

_FREIGHT_STATE_AFTER = {
    "CUSTOMER_PICKUP": "AT_DEPOT", "DIRECT_CUSTOMER_MOVE": "DELIVERED",
    "HUB_DROP": "WITH_NETWORK", "CUSTOMER_DELIVERY": "DELIVERED",
}


@dataclass
class InflightTour:
    """One in-flight tour's live state at an epoch. ``jobs`` and ``stop_times`` are
    index-aligned; ``stop_times`` are absolute (arrive_iso, depart_iso) per stop."""
    tour_id: str
    vehicle: RouteVehicle
    jobs: list
    evaluation: TourEvaluation
    stop_times: list
    depot_depart_iso: str
    start_date: date


@dataclass
class Candidate:
    """An unassigned, booked-today far order offered as one insertable leg.

    ``target_depot``: where a depot-bound leg's freight must LAND (blank for
    customer-terminal legs). A tour returns to its vehicle's home, so a
    CUSTOMER_PICKUP may only ride a tour homed at its target depot — the
    WT254009 rule (2026-07-16: a Bedford-bound 23-pal pickup was commissioned
    onto an idle Stoke tractor, which hauled the freight ~195 km to the wrong
    depot)."""
    order_id: str
    job: RouteJob
    due_iso: str
    ready_iso: str
    standalone_km: float = float("inf")
    target_depot: str = ""
    # Collocated depot-delivery (2026-07-17): the freight physically SITS at this
    # depot, so only a vehicle homed there may carry the leg (the mirror constraint
    # of target_depot, which is about where pickup freight must LAND).
    depot_bound: str = ""


def _depot_bound_mismatch(cand: Candidate, home_depot: str) -> bool:
    """True when ``cand``'s freight would ride the WRONG vehicle for its depot.
    Two mirror cases: a CUSTOMER_PICKUP's freight must LAND at its target_depot
    (the tour returns to the vehicle's home — WT254009); a collocated depot-loaded
    delivery's freight must be BOARDED at its depot_bound depot (it is physically
    there). DIRECT delivers at the customer and HUB_DROP terminates at the hub,
    so absent either field the vehicle returns empty and any home qualifies."""
    if str(getattr(cand, "depot_bound", "") or "") and str(home_depot) != str(cand.depot_bound):
        return True
    return (str(cand.job.leg_kind) == "CUSTOMER_PICKUP"
            and bool(cand.target_depot)
            and str(home_depot) != str(cand.target_depot))


@dataclass
class Attachment:
    """A committed free-ride: ``new_jobs`` = the tour's full job list (frozen head +
    modified tail); ``new_eval`` is the tail's evaluation from the resume state.
    ``resume_date`` is the tail's day-0 date — pass it straight to ``emit_tail_records``
    so emitted times match the feasibility this attachment was accepted under."""
    order_id: str
    tour_id: str
    committed_count: int
    new_jobs: list
    new_eval: TourEvaluation
    added_km: float
    resume_date: date


def _days_between(a_iso: str, b_iso: str) -> int:
    try:
        return (date.fromisoformat(str(b_iso)[:10]) - date.fromisoformat(str(a_iso)[:10])).days
    except ValueError:
        return 0


def _resume_date(tour: InflightTour, committed_count: int) -> date:
    """The date the tail's day 0 corresponds to: the last head stop's day (day
    granularity), or the tour's start date when nothing is committed yet."""
    if committed_count > 0:
        return tour.start_date + timedelta(
            days=int(tour.evaluation.stops[committed_count - 1].day_index))
    return tour.start_date


def attach_intraday(candidates: list, tours: list, frontier: datetime,
                    due_by_job: dict) -> list:
    """Match each candidate to the cheapest in-flight tour whose tail can free-ride it
    without adding a tour day. Greedy, deterministic in the given candidate order; a
    tour absorbs at most one order per call (its shape is re-derived next epoch).
    Returns the committed ``Attachment``s; does not mutate the tours."""
    out: list = []
    used: set = set()
    for cand in candidates:
        best = None  # (added_km, tour, committed_count, new_jobs, new_eval)
        for tour in tours:
            if tour.tour_id in used:
                continue
            if _depot_bound_mismatch(cand, tour.vehicle.home_depot):
                continue          # pickup freight must land at ITS depot, not the tour's
            cc = committed_stop_count(tour.depot_depart_iso, tour.stop_times, frontier)
            head, tail, rveh, rcur = tour_tail_from(tour.vehicle, tour.jobs, tour.evaluation, cc)
            if not tail:
                continue
            rdate = _resume_date(tour, cc)
            base_iso = rdate.isoformat()
            due_offsets = {j.job_id: max(0, _days_between(base_iso, due_by_job[j.job_id]))
                           for j in tail if j.job_id in due_by_job}
            due_offsets[cand.job.job_id] = max(0, _days_between(base_iso, cand.due_iso))
            floor_offsets = {cand.job.job_id: max(0, _days_between(base_iso, cand.ready_iso))}
            got = best_tour_attachment(rveh, tail, cand.job, resume=rcur,
                                       due_offsets=due_offsets, floor_offsets=floor_offsets,
                                       standalone_km=cand.standalone_km, max_extra_days=0)
            if got is None:
                continue
            new_tail, new_eval, added_km = got
            if best is None or added_km < best[0]:
                best = (added_km, tour, cc, list(head) + list(new_tail), new_eval, rdate)
        if best is not None:
            added_km, tour, cc, new_jobs, new_eval, rdate = best
            used.add(tour.tour_id)
            out.append(Attachment(cand.order_id, tour.tour_id, cc, new_jobs, new_eval,
                                  added_km, rdate))
    return out


def build_inflight_tour(ta, records: list, vehicle: RouteVehicle,
                        day_start_hour: int = TOUR_DAY_START_HOUR):
    """Assemble one ``InflightTour`` from a frozen ``TourAssignment``, its emitted
    ``records`` (any order — sorted here by sequence), and a ``RouteVehicle`` carrying
    the assigned vehicle's capacity and the tour's anchor depot as home. Returns
    ``None`` when jobs and records don't align 1:1 (conservative: skip a tour we can't
    cleanly index rather than risk disturbing its committed head)."""
    wanted = [str(job.job_id) for job in ta.jobs]
    wanted_set = set(wanted)
    by_job = {str(r.job_id): r for r in records if str(r.job_id) in wanted_set}
    recs = [by_job[job_id] for job_id in wanted if job_id in by_job]
    if not recs or len(recs) != len(ta.jobs):
        return None
    sd = date.fromisoformat(str(ta.start_date)[:10])
    depot_depart = datetime(sd.year, sd.month, sd.day, day_start_hour).isoformat(sep=" ")
    return InflightTour(
        tour_id=f"TOUR:{ta.vehicle_id}:{ta.start_date}", vehicle=vehicle,
        jobs=list(ta.jobs), evaluation=ta.evaluation,
        stop_times=[(r.planned_arrive, r.planned_depart) for r in recs],
        depot_depart_iso=depot_depart, start_date=sd)


def apply_attachments(attachments: list, merged_tour_records: list,
                      candidate_meta_by_order: dict, plan_id: str = "PLAN") -> list:
    """Return a new ``merged_tour_records`` with each attachment's tour re-emitted
    (committed head kept bit-identical, tail rebuilt via ``emit_tail_records``).
    Records for untouched tours pass through unchanged and in order."""
    by_tour = {att.tour_id: att for att in attachments}
    grouped: dict = {}
    order: list = []
    for r in merged_tour_records:
        rid = str(r.route_id)
        if rid not in grouped:
            grouped[rid] = []
            order.append(rid)
        grouped[rid].append(r)
    out: list = []
    for rid in order:
        recs = grouped[rid]
        att = by_tour.get(rid)
        if att is None:
            out.extend(recs)
            continue
        recs_sorted = sorted(recs, key=lambda r: int(r.sequence))
        meta = candidate_meta_by_order.get(att.order_id, {})
        out.extend(emit_tail_records(recs_sorted, att.committed_count, att.new_jobs,
                                     att.new_eval, att.resume_date, meta, plan_id=plan_id))
    return out


def _tour_clock(day_iso: str, minute: float) -> str:
    if minute is None or float(minute) < 0:
        return ""
    y, m, d = (int(x) for x in str(day_iso)[:10].split("-"))
    return (datetime(y, m, d, TOUR_DAY_START_HOUR)
            + timedelta(minutes=float(minute))).isoformat(sep=" ")


def emit_tail_records(orig_records: list, committed_count: int, new_jobs: list,
                      new_eval: TourEvaluation, resume_date: date,
                      candidate_meta: dict, plan_id: str = "PLAN") -> list:
    """Rebuild a tour's records after an attachment: the committed head records
    (first ``committed_count``) are kept bit-identical, and the tail is re-emitted
    from ``new_eval`` — existing tail stops reuse their original record's static
    fields (re-timed), and the newly attached stop is built from ``candidate_meta``.
    Absolute times are ``(resume_date + stop.day_index)`` at ``TOUR_DAY_START_HOUR``."""
    ordered = sorted(orig_records, key=lambda r: int(r.sequence))
    committed_ids = {str(job.job_id) for job in new_jobs[:committed_count]}
    committed_sequences = [int(r.sequence) for r in ordered if str(r.job_id) in committed_ids]
    cutoff = max(committed_sequences, default=0)
    head = [r for r in ordered if int(r.sequence) <= cutoff]
    by_job = {str(r.job_id): r for r in orig_records}
    for job in new_jobs:
        jid = str(job.job_id)
        if jid not in by_job:
            match = next((r for r in orig_records
                          if str(r.job_id).startswith(f"{jid}:")), None)
            if match is not None:
                by_job[jid] = match
    template = orig_records[-1] if orig_records else None
    out = list(head)
    tail_jobs = {str(job.job_id): job for job in new_jobs[committed_count:]}
    for event in tour_emission_events(new_eval, resume_date.isoformat()):
        stop = event["stop"]
        kind = str(event["kind"])
        job = tail_jobs.get(str(event["job_id"]))
        if kind == "job" and job is None:
            raise ValueError(f"tour tail evaluator emitted unknown job {event['job_id']}")
        seq = len(out) + 1
        day_iso = str(event["service_date"])
        arrive = (_tour_clock(day_iso, stop.arrive_minute) if stop is not None
                  else _tour_clock(day_iso, 0.0))
        depart = (_tour_clock(day_iso, stop.depart_minute) if stop is not None else arrive)
        dyn = dict(sequence=seq, service_date=day_iso,
                   planned_arrive=arrive, planned_depart=depart,
                   planned_km=float(stop.leg_km) if stop is not None else 0.0,
                   planned_drive_minutes=float(stop.leg_minutes) if stop is not None else 0.0,
                   load_pallets_after=float(event["pallets"]),
                   load_kg_after=float(event["kg"]),
                   break_minutes_before=float(stop.break_minutes_before) if stop is not None else 0.0)
        orig = by_job.get(str(job.job_id)) if job is not None else None
        if job is not None and orig is not None:
            out.append(replace(orig, **dyn))
        elif job is not None:
            m = candidate_meta
            out.append(SelectedPlanRecord(
                plan_id=plan_id,
                route_id=str(template.route_id) if template else "",
                trip_id=str(template.trip_id) if template else "",
                vehicle_id=str(template.vehicle_id) if template else "",
                vehicle_home_depot=str(template.vehicle_home_depot) if template else "",
                service_date=day_iso, sequence=seq,
                trip_index=int(template.trip_index) if template else 0,
                job_id=str(job.job_id), leg_id=str(m.get("leg_id", "")),
                order_id=str(m.get("order_id", "")), leg_kind=str(job.leg_kind),
                origin_node=str(m.get("preferred_start_node", "")),
                destination_node=str(m.get("preferred_end_node", "")),
                planned_arrive=dyn["planned_arrive"], planned_depart=dyn["planned_depart"],
                planned_km=dyn["planned_km"], planned_drive_minutes=dyn["planned_drive_minutes"],
                load_pallets_after=dyn["load_pallets_after"], load_kg_after=dyn["load_kg_after"],
                freight_state_before="",
                freight_state_after=_FREIGHT_STATE_AFTER.get(str(job.leg_kind), "DELIVERED"),
                assignment_reason="TOUR_ATTACH", break_minutes_before=dyn["break_minutes_before"]))
        else:
            leg_kind = "DEPOT_RETURN" if kind == "depot_return" else TOUR_OVERNIGHT
            node = str(event["node"])
            direct_job_id = (
                str(event["job_id"]).split("__DIRECT_COLLECT__:", 1)[1]
                if kind == "direct_overnight"
                and "__DIRECT_COLLECT__:" in str(event["job_id"])
                else ""
            )
            direct_orig = by_job.get(direct_job_id)
            direct_leg_id = (
                str(direct_orig.leg_id) if direct_orig is not None
                else str(candidate_meta.get("leg_id", "")) if direct_job_id else ""
            )
            direct_order_id = (
                str(direct_orig.order_id) if direct_orig is not None
                else str(candidate_meta.get("order_id", "")) if direct_job_id else ""
            )
            out.append(SelectedPlanRecord(
                plan_id=plan_id,
                route_id=str(template.route_id) if template else "",
                trip_id=str(template.trip_id) if template else "",
                vehicle_id=str(template.vehicle_id) if template else "",
                vehicle_home_depot=str(template.vehicle_home_depot) if template else "",
                service_date=day_iso, sequence=seq,
                trip_index=int(template.trip_index) if template else 0,
                job_id=str(event["job_id"]), leg_id=direct_leg_id,
                order_id=direct_order_id, leg_kind=leg_kind,
                origin_node=node,
                destination_node="DEPOT" if kind == "depot_return" else node,
                planned_arrive=arrive, planned_depart=depart,
                planned_km=dyn["planned_km"],
                planned_drive_minutes=dyn["planned_drive_minutes"],
                load_pallets_after=dyn["load_pallets_after"],
                load_kg_after=dyn["load_kg_after"], freight_state_before="",
                freight_state_after="RETURNED" if kind == "depot_return" else "ABOARD",
                assignment_reason="TOUR_RETURN" if kind == "depot_return" else "TOUR_OVERNIGHT",
                break_minutes_before=dyn["break_minutes_before"]))
    return out


def rebuild_pruned_tour_records(ta, records: list, vehicle: RouteVehicle):
    """Re-evaluate and re-emit a tour after final freight-leg pruning.

    Removing an early/co-located freight stop invalidates every downstream leg's
    geometry and leaves sequence holes. Keep original service days as latest-day
    bounds, then let the evaluator rebuild the shorter path and return segments.
    """
    if not records:
        return None, []
    record_ids = {str(r.job_id) for r in records}
    kept_jobs = [job for job in ta.jobs
                 if (str(job.job_id) in record_ids or str(job.leg_kind) == "DEPOT_LOAD")]
    if not kept_jobs:
        return None, []
    original_ids = [str(job.job_id) for job in ta.jobs]
    if [str(job.job_id) for job in kept_jobs] == original_ids:
        return ta, sorted(records, key=lambda r: int(r.sequence))

    base = date.fromisoformat(str(ta.start_date)[:10])
    record_by_job = {str(r.job_id): r for r in records}
    offsets: dict[str, int] = {}
    for job in kept_jobs:
        jid = str(job.job_id)
        rec = record_by_job.get(jid)
        if rec is None:
            rec = next((r for r in records if str(r.job_id).startswith(f"{jid}:")), None)
        if rec is not None:
            offsets[jid] = max(0, (date.fromisoformat(str(rec.service_date)[:10]) - base).days)
    evaluation = evaluate_tour(vehicle, kept_jobs, due_offsets=offsets)
    if not evaluation.feasible:
        raise ValueError(f"pruned tour {ta.vehicle_id}/{ta.start_date} cannot be re-evaluated: "
                         f"{evaluation.reason}")
    plan_id = str(records[0].plan_id)
    rebuilt = emit_tail_records(records, 0, kept_jobs, evaluation, base, {}, plan_id=plan_id)
    return replace(ta, jobs=kept_jobs, evaluation=evaluation, days=evaluation.days), rebuilt


@dataclass
class Commission:
    """A fresh one-vehicle tour dispatched TODAY for a far order nothing else
    can serve — the sibling of an ``Attachment`` (1b, 2026-07-16)."""
    order_id: str
    vehicle_id: str
    start_date: str
    jobs: list
    evaluation: TourEvaluation
    records: list


def _commission_records(vehicle, today_iso, jobs, evaluation, meta, order_id,
                        plan_id: str = "PLAN") -> list:
    rid = f"TOUR:{vehicle.vehicle_id}:{str(today_iso)[:10]}"
    base = date.fromisoformat(str(today_iso)[:10])
    out: list = []
    job_by_id = {str(job.job_id): job for job in jobs}
    for event in tour_emission_events(evaluation, base.isoformat()):
        stop = event["stop"]
        kind = str(event["kind"])
        job = job_by_id.get(str(event["job_id"]))
        direct_aux = kind == "direct_overnight"
        direct_job_id = (
            str(event["job_id"]).split("__DIRECT_COLLECT__:", 1)[1]
            if direct_aux and "__DIRECT_COLLECT__:" in str(event["job_id"])
            else ""
        )
        direct_job = job_by_id.get(direct_job_id)
        if kind == "job" and job is None:
            raise ValueError(f"tour evaluator emitted unknown job {event['job_id']}")
        leg_kind = (str(job.leg_kind) if job is not None else
                    ("DEPOT_RETURN" if kind == "depot_return" else TOUR_OVERNIGHT))
        node = str(event["node"])
        if kind == "job":
            origin_node = str(meta.get("preferred_start_node", ""))
            destination_node = str(meta.get("preferred_end_node", ""))
        elif kind == "depot_return":
            origin_node, destination_node = str(stop.node), "DEPOT"
        else:
            origin_node = destination_node = node
        day_iso = str(event["service_date"])
        arrive = (_tour_clock(day_iso, stop.arrive_minute) if stop is not None
                  else _tour_clock(day_iso, 0.0))
        depart = (_tour_clock(day_iso, stop.depart_minute) if stop is not None else arrive)
        out.append(SelectedPlanRecord(
            plan_id=plan_id, route_id=rid, trip_id=f"{rid}#T1",
            vehicle_id=str(vehicle.vehicle_id),
            vehicle_home_depot=str(vehicle.home_depot),
            service_date=day_iso, sequence=int(event["sequence"]), trip_index=0,
            job_id=str(event["job_id"]),
            leg_id=(str(meta.get("leg_id", ""))
                    if job is not None or direct_job is not None else ""),
            order_id=(str(meta.get("order_id", "") or order_id)
                      if job is not None or direct_job is not None else ""),
            leg_kind=leg_kind, origin_node=origin_node, destination_node=destination_node,
            planned_arrive=arrive, planned_depart=depart,
            planned_km=float(stop.leg_km) if stop is not None else 0.0,
            planned_drive_minutes=float(stop.leg_minutes) if stop is not None else 0.0,
            load_pallets_after=float(event["pallets"]),
            load_kg_after=float(event["kg"]),
            freight_state_before="",
            freight_state_after=(_FREIGHT_STATE_AFTER.get(leg_kind, "DELIVERED")
                                 if job is not None else
                                 ("RETURNED" if kind == "depot_return" else "ABOARD")),
            assignment_reason=("TOUR_COMMISSION" if job is not None else
                               ("TOUR_RETURN" if kind == "depot_return" else "TOUR_OVERNIGHT")),
            break_minutes_before=(float(stop.break_minutes_before)
                                  if stop is not None else 0.0)))
    return out


_TWO_POINT_LEG_KINDS = ("DIRECT_CUSTOMER_MOVE", "HUB_DROP")


def _commission_worthy(cand) -> bool:
    """A fresh dedicated tour is only for orders too FAR to serve there-and-back in
    a day (the NE42 case, 985 km). An order the daily solve merely rejected (late
    booking, capacity at that epoch) but which IS daily-serviceable must NOT be
    dressed up as a long-haul tour — a ~20 km CB9 collection commissioned as
    TOUR:W88RNW is a 47 km round trip, not a tour. Mirrors the seed's geometry
    classifier (tour_plan.py:290 -> is_tour_only) so commissioning and the seed
    agree on what 'far' means; a False here means the order rolls back to normal
    routing / the next window instead of a wasteful one-off tour (2026-07-20)."""
    from freight_planner.tours import is_tour_only
    job = cand.job
    o_lat = o_lon = None
    if (str(job.leg_kind) in _TWO_POINT_LEG_KINDS
            and job.origin_lat is not None and job.origin_lon is not None):
        o_lat, o_lon = float(job.origin_lat), float(job.origin_lon)
    return is_tour_only(float(job.lat), float(job.lon), origin_lat=o_lat, origin_lon=o_lon,
                        pallets=float(getattr(job, "pallets", 0.0) or 0.0),
                        depot=str(getattr(cand, "target_depot", "")
                                  or getattr(cand, "depot_bound", "") or ""))


def commission_intraday(candidates: list, idle_vehicles: list, today_iso: str,
                        floor_minute: float, meta_by_order: dict | None = None,
                        plan_id: str = "PLAN") -> list:
    """Dispatch a FRESH one-vehicle tour TODAY for far orders neither the daily
    pass nor ``attach_intraday`` can serve — models the human move of phoning an
    idle driver mid-morning (the NE42 pair sat unserved while 14-17 capable
    artics idled). Day-1 duty starts at the dispatch floor (``floor_minute``
    past TOUR_DAY_START_HOUR): the evaluation RESUMES from an already-elapsed
    morning, so a 10:30 commissioning cannot pretend a 05:00 launch — and an
    atomic two-point DIRECT that no longer fits today honestly books its whole
    leg on day 2 (two-point legs cannot sleep mid-leg; a true same-day far
    DIRECT launch needs that physics first). The delivery promise is a hard
    deadline; one idle vehicle serves one order — consolidation stays the
    seed's job."""
    from freight_planner.tours import _DayCursor, evaluate_tour, select_tour_vehicle
    out: list = []
    used: set = set()
    for cand in candidates:
        # Geometry gate (2026-07-20): only genuinely-far orders earn a dedicated
        # tour. A near order the solve rejected rolls to normal routing, not a
        # 47 km round-trip "tour" (TOUR:W88RNW / R88GNW were CB9 collections).
        if not _commission_worthy(cand):
            continue
        # WT254009 rule: a depot-bound pickup may only be commissioned onto a
        # vehicle homed at its target depot (the tour returns home carrying the
        # freight). Customer-terminal legs keep the whole idle pool but PREFER
        # the target/nearest depot — without prefer_depot an all-idle pool sorts
        # on capacity alone and geography never enters (how a Stoke tractor won
        # Bedford-local work).
        pool = [v for v in idle_vehicles if v.vehicle_id not in used
                and not _depot_bound_mismatch(cand, v.home_depot)]
        picked = None
        while pool:
            v = select_tour_vehicle(float(cand.job.pallets), pool, tour_kg=float(cand.job.kg),
                                    prefer_depot=(cand.target_depot or cand.depot_bound or None),
                                    tour_km=float(getattr(cand, "standalone_km", 0.0) or 0.0))
            if v is None:
                break
            # This is an IDLE driver beginning a fresh shift at the dispatch
            # floor.  The floor is wall-clock time after the 05:00 tour anchor,
            # not duty already worked before dispatch.
            resume = _DayCursor(
                day_index=0,
                day_drive=0.0,
                day_elapsed=0.0,
                drive_since_break=0.0,
                clock_minute=max(0.0, float(floor_minute)),
            )
            due_off = {cand.job.job_id: max(0, _days_between(today_iso, cand.due_iso))}
            floor_off = {cand.job.job_id: max(0, _days_between(today_iso, cand.ready_iso))}
            ev = evaluate_tour(v, [cand.job], due_off, floor_offsets=floor_off, resume=resume)
            if ev.feasible:
                picked = (v, ev)
                break
            pool = [p for p in pool if p.vehicle_id != v.vehicle_id]
        if picked is None:
            continue
        v, ev = picked
        used.add(v.vehicle_id)
        meta = (meta_by_order or {}).get(cand.order_id, {})
        recs = _commission_records(v, today_iso, [cand.job], ev, meta, cand.order_id, plan_id)
        out.append(Commission(cand.order_id, v.vehicle_id, str(today_iso)[:10],
                              [cand.job], ev, recs))
    return out
